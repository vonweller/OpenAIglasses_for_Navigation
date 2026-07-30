# app_main.py
# -*- coding: utf-8 -*-
import os, sys, time, json, asyncio, base64, audioop, socket, ipaddress
from typing import Any, Dict, Optional, Tuple, List, Callable, Set, Deque
from collections import deque
from dataclasses import dataclass
import re
# 在其它 import 之后加：
from .qwen_extractor import extract_english_label
from .navigation_master import NavigationMaster, OrchestratorResult 
# 新增：导入盲道导航器
from .workflow_blindpath import BlindPathNavigator
# 新增：导入过马路导航器
from .workflow_crossstreet import CrossStreetNavigator
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
import uvicorn
import cv2
import numpy as np
from ultralytics import YOLO
from .obstacle_detector_client import ObstacleDetectorClient

import torch  # 添加这行


import mediapipe as mp
from . import bridge_io
from .paths import APP_DIR, MODEL_DIR as PROJECT_MODEL_DIR, RUNTIME_CONFIG_PATH as PROJECT_RUNTIME_CONFIG_PATH
from .performance import (
    DEFAULT_PROFILE,
    PROFILES,
    normalize_profile,
    pipeline_metrics,
    profile_payload,
)
import threading

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---- .env ----
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DEFAULT_MODEL_DIR = str(PROJECT_MODEL_DIR)
DEFAULT_NAV_SEG_MODEL = os.path.join(DEFAULT_MODEL_DIR, "yolo-seg.pt")
DEFAULT_OBSTACLE_MODEL = os.path.join(DEFAULT_MODEL_DIR, "yoloe-11l-seg.pt")
DEFAULT_TRAFFIC_MODEL = os.path.join(DEFAULT_MODEL_DIR, "trafficlight.pt")
DEFAULT_HAND_TASK = os.path.join(DEFAULT_MODEL_DIR, "hand_landmarker.task")
DEFAULT_ITEM_MODEL = os.path.join(DEFAULT_MODEL_DIR, "yoloe-26s-seg.pt")
# 只预热最常用目标，避免启动后长时间占用同一个 YOLOE 模型并阻塞实时找物。
# 其他目标进入找物时按需预热，UI 会显示“正在准备识别特征”。
DEFAULT_YOLOE_PREWARM_CLASSES = "cell phone"
RUNTIME_CONFIG_PATH = str(PROJECT_RUNTIME_CONFIG_PATH)
MODEL_CONFIG_FIELDS = {
    "blind_path_model": ("BLIND_PATH_MODEL", DEFAULT_NAV_SEG_MODEL),
    "obstacle_model": ("OBSTACLE_MODEL", DEFAULT_OBSTACLE_MODEL),
    "trafficlight_model": ("TRAFFICLIGHT_MODEL", DEFAULT_TRAFFIC_MODEL),
    "hand_task_path": ("HAND_TASK_PATH", DEFAULT_HAND_TASK),
    "item_search_model": ("YOLOE_MODEL_PATH", DEFAULT_ITEM_MODEL),
}
PERFORMANCE_PROFILE = DEFAULT_PROFILE

def _load_runtime_config_env():
    global PERFORMANCE_PROFILE
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if isinstance(data, dict):
        PERFORMANCE_PROFILE = normalize_profile(data.get("performance_profile", DEFAULT_PROFILE))
        os.environ["AIGLASS_PERFORMANCE_PROFILE"] = PERFORMANCE_PROFILE
        os.environ["AIGLASS_YOLOE_IMGSZ"] = str(PROFILES[PERFORMANCE_PROFILE].yolo_imgsz)
    models = data.get("models", data) if isinstance(data, dict) else {}
    if not isinstance(models, dict):
        return
    for field, (env_key, _default) in MODEL_CONFIG_FIELDS.items():
        value = str(models.get(field) or "").strip()
        if value and not os.getenv(env_key):
            os.environ[env_key] = value

_load_runtime_config_env()
from . import yolomedia  # 确保和 app_main.py 同目录，文件名就是 yolomedia.py
# ---- Windows 事件循环策略 ----
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ---- DashScope ASR 基础 ----
from dashscope import audio as dash_audio  # 若未安装，会在原项目里抛错提示

API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

MODEL        = "paraformer-realtime-v2"
SAMPLE_RATE  = 16000
AUDIO_FMT    = "pcm"
CHUNK_MS     = 20
BYTES_CHUNK  = SAMPLE_RATE * CHUNK_MS // 1000 * 2
SILENCE_20MS = bytes(BYTES_CHUNK)

# ---- 引入我们的模块 ----
from .audio_stream import (
    register_stream_route,         # 挂 /stream.wav
    broadcast_pcm16_realtime,      # 实时向连接分发 16k PCM
    finish_pcm16_stream,           # 刷新流式音频末尾不足一帧的数据
    hard_reset_audio,              # 音频+AI 播放总闸
    soft_reset_audio,
    BYTES_PER_20MS_16K,
    is_playing_now,
    current_ai_task,
    get_stream_status,
)
from .omni_client import stream_chat, OmniStreamPiece
from .asr_core import (
    ASRCallback,
    set_current_recognition,
    stop_current_recognition,
)
from .audio_player import initialize_audio_system, play_voice_text

# ---- 同步录制器 ----
from . import sync_recorder
import signal
import atexit

# ---- IMU UDP ----
UDP_IP   = "0.0.0.0"
UDP_PORT = 12345

app = FastAPI()

# ====== 状态与容器 ======
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

ui_clients: Dict[int, WebSocket] = {}
ui_client_locks: Dict[int, asyncio.Lock] = {}
current_partial: str = ""
recent_finals: List[str] = []
RECENT_MAX = 50
last_frames: Deque[Tuple[float, bytes]] = deque(maxlen=10)

camera_viewers: Set[WebSocket] = set()
esp32_camera_ws: Optional[WebSocket] = None
imu_ws_clients: Set[WebSocket] = set()
esp32_audio_ws: Optional[WebSocket] = None
camera_stats: Dict[str, Any] = {}
_latest_output_lock = threading.Lock()
_latest_output_frame: Optional[bytes] = None
_latest_output_version = 0
_last_processed_output_at = 0.0
_output_event: Optional[asyncio.Event] = None
_viewer_broadcast_task: Optional[asyncio.Task] = None
_visual_worker_stop = threading.Event()
_visual_worker_thread: Optional[threading.Thread] = None
current_item_target_zh = ""
asr_diag: Dict[str, Any] = {
    "audio_ws_connected": False,
    "streaming": False,
    "started_at": None,
    "last_audio_at": None,
    "audio_chunks": 0,
    "last_command": "",
    "last_error": "",
    "last_partial_at": None,
    "last_final_at": None,
}

# 【新增】盲道导航相关全局变量
blind_path_navigator = None
navigation_active = False
yolo_seg_model = None
obstacle_detector = None

# 【新增】过马路导航相关全局变量
cross_street_navigator = None
cross_street_active = False
orchestrator = None  # 新增

# 【新增】omni对话状态标志
omni_conversation_active = False  # 标记omni对话是否正在进行
omni_previous_nav_state = None  # 保存omni激活前的导航状态，用于恢复


def _apply_performance_profile(profile_key: str) -> dict:
    """Apply a profile to server-side inference and connected camera controls."""
    global PERFORMANCE_PROFILE
    PERFORMANCE_PROFILE = normalize_profile(profile_key)
    profile = PROFILES[PERFORMANCE_PROFILE]
    interval = max(1, int(round(profile.camera_fps / max(1.0, profile.inference_hz))))
    os.environ["AIGLASS_PERFORMANCE_PROFILE"] = PERFORMANCE_PROFILE
    os.environ["AIGLASS_YOLOE_IMGSZ"] = str(profile.yolo_imgsz)
    os.environ["AIGLASS_YOLOE_SEGMENT_INTERVAL"] = str(interval)
    os.environ["AIGLASS_YOLOE_TRACK_INTERVAL"] = str(interval)
    try:
        yolomedia.YOLOE_IMGSZ = profile.yolo_imgsz
        yolomedia.YOLOE_SEGMENT_INTERVAL = interval
        yolomedia.YOLOE_TRACK_INTERVAL = interval
    except Exception:
        pass
    try:
        if blind_path_navigator is not None:
            blind_path_navigator.BLINDPATH_DETECTION_INTERVAL = interval
        if cross_street_navigator is not None:
            cross_street_navigator.CROSSWALK_DETECTION_INTERVAL = interval
    except Exception:
        pass
    return profile_payload(PERFORMANCE_PROFILE)


def _current_mode() -> str:
    if yolomedia_running:
        return "ITEM_SEARCH"
    if orchestrator is None:
        return "IDLE"
    try:
        return str(orchestrator.get_state() or "IDLE")
    except Exception:
        return "UNKNOWN"


def _offer_viewer_frame(jpeg_bytes: bytes, processed: bool = False) -> None:
    """Store only the newest frame for the single broadcaster coroutine."""
    global _latest_output_frame, _latest_output_version, _last_processed_output_at
    if not jpeg_bytes:
        return
    now_mono = time.monotonic()
    if not processed and _last_processed_output_at > 0:
        # 推理结果刚输出时短暂保留叠加画面，避免原始帧立即覆盖中文状态和框选。
        hold_sec = max(0.045, min(0.12, 0.65 / max(1.0, PROFILES[PERFORMANCE_PROFILE].inference_hz)))
        if now_mono - _last_processed_output_at < hold_sec:
            return
    with _latest_output_lock:
        if _output_event is not None and _output_event.is_set():
            pipeline_metrics.on_output_drop()
        _latest_output_frame = bytes(jpeg_bytes)
        _latest_output_version += 1
        if processed:
            _last_processed_output_at = now_mono
    if _output_event is not None:
        _output_event.set()


async def _viewer_broadcast_loop() -> None:
    """Broadcast the newest JPEG; slow clients never create a frame backlog."""
    seen_version = 0
    last_sent_at = 0.0
    while True:
        if _output_event is None:
            await asyncio.sleep(0.05)
            continue
        await _output_event.wait()
        _output_event.clear()
        min_interval = 1.0 / max(1.0, float(PROFILES[PERFORMANCE_PROFILE].camera_fps))
        delay = min_interval - (time.monotonic() - last_sent_at)
        if delay > 0:
            await asyncio.sleep(delay)
        with _latest_output_lock:
            version = _latest_output_version
            jpeg_bytes = _latest_output_frame
        if not jpeg_bytes or version == seen_version:
            continue
        seen_version = version

        async def _send_one(ws: WebSocket):
            try:
                await asyncio.wait_for(ws.send_bytes(jpeg_bytes), timeout=0.35)
                return None
            except Exception:
                return ws

        clients = list(camera_viewers)
        if clients:
            dead = await asyncio.gather(*(_send_one(ws) for ws in clients))
            for ws in dead:
                if ws is not None:
                    camera_viewers.discard(ws)
            pipeline_metrics.on_broadcast()
            last_sent_at = time.monotonic()


async def _send_ui_message(ws: WebSocket, message: str) -> None:
    lock = ui_client_locks.get(id(ws))
    if lock is None:
        await ws.send_text(message)
        return
    async with lock:
        await ws.send_text(message)


def _visual_worker() -> None:
    """Run navigation/traffic inference outside the camera WebSocket loop."""
    last_seq = 0
    last_idle_status = None
    last_traffic_inference_at = 0.0
    while not _visual_worker_stop.is_set():
        mode = _current_mode()
        if yolomedia_running or mode in ("IDLE", "CHAT", "ITEM_SEARCH", "UNKNOWN"):
            if last_idle_status != mode and not yolomedia_running:
                bridge_io.set_yolo_status(
                    running=False,
                    phase="idle",
                    target="",
                    backend="",
                    device="cuda" if torch.cuda.is_available() else "cpu",
                )
                last_idle_status = mode
            time.sleep(0.05)
            continue
        if orchestrator is None:
            time.sleep(0.05)
            continue
        packet = bridge_io.wait_next_raw_bgr(last_seq, timeout_sec=0.5)
        if packet is None:
            continue
        last_seq = packet.seq
        if mode == "TRAFFIC_LIGHT_DETECTION":
            interval = 1.0 / max(1.0, PROFILES[PERFORMANCE_PROFILE].inference_hz)
            now_mono = time.monotonic()
            if now_mono - last_traffic_inference_at < interval:
                continue
            last_traffic_inference_at = now_mono

        target_zh = "红绿灯" if mode == "TRAFFIC_LIGHT_DETECTION" else (
            "斑马线" if mode in ("CROSSING", "SEEKING_CROSSWALK", "WAIT_TRAFFIC_LIGHT", "SEEKING_NEXT_BLINDPATH") else "盲道"
        )
        bridge_io.set_yolo_status(
            running=True,
            phase="infer",
            target=target_zh,
            backend="YOLO",
            device="cuda" if torch.cuda.is_available() else "cpu",
            source_seq=packet.seq,
            frame_age_ms=max(0.0, (time.time() - packet.captured_at) * 1000.0),
            last_error="",
        )
        started = time.perf_counter()
        guidance_text = ""
        try:
            if mode == "TRAFFIC_LIGHT_DETECTION":
                from . import trafficlight_detection
                result = trafficlight_detection.process_single_frame(packet.bgr)
                out_img = result.get("vis_image") if isinstance(result, dict) else packet.bgr
            else:
                result = orchestrator.process_frame(packet.bgr)
                out_img = result.annotated_image if result.annotated_image is not None else packet.bgr
                guidance_text = str(result.guidance_text or "")
        except Exception as exc:
            bridge_io.set_yolo_status(running=False, phase="failed", target=target_zh, last_error=str(exc))
            continue

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        pipeline_metrics.on_processed(
            captured_at=packet.captured_at,
            inference_ms=elapsed_ms,
        )
        bridge_io.set_yolo_status(
            running=True,
            phase="result",
            target=target_zh,
            backend="YOLO",
            device="cuda" if torch.cuda.is_available() else "cpu",
            inference_ms=elapsed_ms,
            source_seq=packet.seq,
            frame_age_ms=max(0.0, (time.time() - packet.captured_at) * 1000.0),
            last_error="",
        )
        bridge_io.send_vis_bgr(out_img if out_img is not None else packet.bgr, quality=78)
        if guidance_text:
            try:
                play_voice_text(guidance_text)
                bridge_io.send_ui_final(f"[导航] {guidance_text}")
            except Exception:
                pass


def _persist_runtime_config(models: Optional[dict] = None) -> None:
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    if models is not None:
        existing["models"] = dict(models)
    existing["performance_profile"] = PERFORMANCE_PROFILE
    try:
        with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[CONFIG] failed to persist runtime config: {exc}", flush=True)


async def _send_camera_profile(ws: WebSocket) -> None:
    profile = PROFILES[PERFORMANCE_PROFILE]
    for command in (
        f"SET:FRAMESIZE={profile.framesize}",
        f"SET:QUALITY={profile.jpeg_quality}",
        f"SET:FPS={profile.camera_fps}",
    ):
        try:
            await ws.send_text(command)
        except Exception:
            break


# 【新增】模型加载函数
def load_navigation_models():
    """加载盲道导航所需的模型"""
    global yolo_seg_model, obstacle_detector

    try:
        seg_model_path = os.getenv("BLIND_PATH_MODEL", DEFAULT_NAV_SEG_MODEL)
        #print(f"[NAVIGATION] 尝试加载模型: {seg_model_path}")

        if os.path.exists(seg_model_path):
            print(f"[NAVIGATION] 模型文件存在，开始加载...")
            yolo_seg_model = YOLO(seg_model_path)

            # 强制放到 GPU
            if torch.cuda.is_available():
                yolo_seg_model.to("cuda")
                print(f"[NAVIGATION] 盲道分割模型加载成功并放到GPU: {yolo_seg_model.device}")
            else:
                print("[NAVIGATION] CUDA不可用，模型仍在CPU")

            # 测试模型是否能正常运行
            try:
                test_img = np.zeros((640, 640, 3), dtype=np.uint8)
                results = yolo_seg_model.predict(
                    test_img,
                    device="cuda" if torch.cuda.is_available() else "cpu",
                    verbose=False
                )
                print(f"[NAVIGATION] 模型测试成功，支持的类别数: {len(yolo_seg_model.names) if hasattr(yolo_seg_model, 'names') else '未知'}")
                if hasattr(yolo_seg_model, 'names'):
                    print(f"[NAVIGATION] 模型类别: {yolo_seg_model.names}")
            except Exception as e:
                print(f"[NAVIGATION] 模型测试失败: {e}")
        else:
            print(f"[NAVIGATION] 错误：找不到模型文件: {seg_model_path}")
            print(f"[NAVIGATION] 当前工作目录: {os.getcwd()}")
            print(f"[NAVIGATION] 请检查文件路径是否正确")
            
        # 障碍物 YOLOE 预加载会触发较重的特征下载，默认改为启动后按需加载
        obstacle_model_path = os.getenv("OBSTACLE_MODEL", DEFAULT_OBSTACLE_MODEL)
        obstacle_detector = None
        print(f"[NAVIGATION] 障碍物检测器已改为按需加载: {obstacle_model_path}")
        
    except Exception as e:
        print(f"[NAVIGATION] 模型加载失败: {e}")
        import traceback
        traceback.print_exc()

# 在程序启动时加载模型
print("[NAVIGATION] 开始加载导航模型...")
load_navigation_models()
print(f"[NAVIGATION] 模型加载完成 - yolo_seg_model: {yolo_seg_model is not None}")

# 【新增】启动同步录制
ENABLE_SYNC_RECORDING = os.getenv("AIGLASS_RECORDING", "0").strip().lower() in ("1", "true", "yes", "on")
if ENABLE_SYNC_RECORDING:
    print("[RECORDER] 启动同步录制系统...")
    sync_recorder.start_recording()
    print("[RECORDER] 录制系统已启动，将自动保存视频和音频")
else:
    print("[RECORDER] 同步录制已关闭（AIGLASS_RECORDING=0），降低调试时CPU/磁盘负载")

# 【新增】注册退出处理器，确保Ctrl+C时保存录制文件
def cleanup_on_exit():
    """程序退出时的清理工作"""
    if not ENABLE_SYNC_RECORDING:
        return
    print("\n[SYSTEM] 正在关闭录制器...")
    try:
        sync_recorder.stop_recording()
        print("[SYSTEM] 录制文件已保存")
    except Exception as e:
        print(f"[SYSTEM] 关闭录制器时出错: {e}")

def signal_handler(sig, frame):
    """处理Ctrl+C信号"""
    print("\n[SYSTEM] 收到中断信号，正在安全退出...")
    cleanup_on_exit()
    import sys
    sys.exit(0)

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)  # 终止信号
atexit.register(cleanup_on_exit)  # 正常退出时也调用

if ENABLE_SYNC_RECORDING:
    print("[RECORDER] 已注册退出处理器 - Ctrl+C时会自动保存录制文件")



# 【新增】预加载红绿灯检测模型（避免进入WAIT_TRAFFIC_LIGHT状态时卡顿）
try:
    from . import trafficlight_detection
    print("[TRAFFIC_LIGHT] 开始预加载红绿灯检测模型...")
    if trafficlight_detection.init_model():
        print("[TRAFFIC_LIGHT] 红绿灯检测模型预加载成功")
        # 执行一次测试推理，完全预热模型
        try:
            test_img = np.zeros((640, 640, 3), dtype=np.uint8)
            _ = trafficlight_detection.process_single_frame(test_img)
            print("[TRAFFIC_LIGHT] 模型预热完成")
        except Exception as e:
            print(f"[TRAFFIC_LIGHT] 模型预热失败: {e}")
    else:
        print("[TRAFFIC_LIGHT] 红绿灯检测模型预加载失败")
except Exception as e:
    print(f"[TRAFFIC_LIGHT] 红绿灯模型预加载出错: {e}")

# ============== 关键：系统级"硬重置"总闸 =================
interrupt_lock = asyncio.Lock()

# ============== YOLO媒体线程管理 =================
yolomedia_thread: Optional[threading.Thread] = None
yolomedia_stop_event = threading.Event()
yolomedia_running = False
yolomedia_sending_frames = False  # 新增：标记YOLO是否已经开始发送处理后的帧

# 物品名称到YOLO类别的映射
ITEM_TO_CLASS_MAP = {
    "红牛": "Red_Bull",
    "AD钙奶": "AD_milk",
    "ad钙奶": "AD_milk",
    "钙奶": "AD_milk",
}

async def ui_broadcast_raw(msg: str):
    dead = []
    for k, ws in list(ui_clients.items()):
        try:
            await _send_ui_message(ws, msg)
        except Exception:
            dead.append(k)
    for k in dead:
        ui_clients.pop(k, None)
        ui_client_locks.pop(k, None)


async def ui_broadcast_partial(text: str):
    global current_partial
    current_partial = text
    if text:
        _set_asr_diag(last_partial_at=time.time())
    await ui_broadcast_raw("PARTIAL:" + text)

async def ui_broadcast_final(text: str):
    global current_partial, recent_finals
    current_partial = ""
    _set_asr_diag(last_final_at=time.time())
    recent_finals.append(text)
    if len(recent_finals) > RECENT_MAX:
        recent_finals = recent_finals[-RECENT_MAX:]
    await ui_broadcast_raw("FINAL:" + text)
    print(f"[ASR/AI FINAL] {text}", flush=True)

async def full_system_reset(reason: str = ""):
    """
    回到刚启动后的状态：
    1) 停播 + 取消AI任务 + 切断所有/stream.wav（hard_reset_audio）
    2) 停止 ASR 实时识别流（关键）
    3) 清 UI 状态
    4) 清最近相机帧（避免把旧帧又拼进下一轮）
    5) 告知 ESP32：RESET（可选）
    """
    # 1) 音频&AI
    await hard_reset_audio(reason or "full_system_reset")

    # 2) ASR
    await stop_current_recognition()

    # 3) UI
    global current_partial, recent_finals
    current_partial = ""
    recent_finals = []

    # 4) 相机帧
    try:
        last_frames.clear()
    except Exception:
        pass

    # 5) 通知 ESP32
    try:
        if esp32_audio_ws and (esp32_audio_ws.client_state == WebSocketState.CONNECTED):
            await esp32_audio_ws.send_text("RESET")
    except Exception:
        pass

    print("[SYSTEM] full reset done.", flush=True)

# ========= 启动/停止 YOLO 媒体处理 =========
def start_yolomedia_with_target(target_name: str, display_name: Optional[str] = None):
    """启动yolomedia线程，搜索指定物品"""
    global yolomedia_thread, yolomedia_stop_event, yolomedia_running, yolomedia_sending_frames, current_item_target_zh
    
    # 如果已经在运行，先停止
    if yolomedia_running:
        stop_yolomedia()
    
    # 查找对应的YOLO类别
    yolo_class = ITEM_TO_CLASS_MAP.get(target_name, target_name)
    current_item_target_zh = str(display_name or target_name or "").strip()
    try:
        from .yoloe_backend import is_yoloe_text_cached
        text_ready = is_yoloe_text_cached([yolo_class])
    except Exception:
        text_ready = False
    print(f"[YOLOMEDIA] Starting with target: {target_name} -> YOLO class: {yolo_class}", flush=True)
    print(f"[YOLOMEDIA] Available mappings: {ITEM_TO_CLASS_MAP}", flush=True)  # 添加这行调试
    bridge_io.set_yolo_status(
        running=True,
        phase="starting" if text_ready else "lazy_prewarm",
        target=current_item_target_zh,
        model_target=yolo_class,
        backend="YOLOE",
        frames=0,
        inferences=0,
        detections=0,
        last_error="",
        technical_error="",
        started_at=time.time(),
    )
    
    yolomedia_stop_event.clear()
    yolomedia_running = True
    yolomedia_sending_frames = False  # 重置发送帧状态
    
    def _run():
        try:
            # 传递目标类别名和停止事件
            if not text_ready:
                bridge_io.set_yolo_status(
                    running=True,
                    phase="lazy_prewarm",
                    target=yolo_class,
                    backend="YOLOE",
                    last_error="",
                    technical_error="",
                )
                try:
                    from .yoloe_backend import prewarm_yoloe
                    print(f"[YOLOMEDIA] lazy prewarm started for: {yolo_class}", flush=True)
                    prewarm_yoloe([yolo_class])
                    print(f"[YOLOMEDIA] lazy prewarm finished for: {yolo_class}", flush=True)
                except Exception as exc:
                    print(f"[YOLOMEDIA] lazy prewarm failed for {yolo_class}: {exc}", flush=True)
                    bridge_io.set_yolo_status(
                        running=True,
                        phase="lazy_prewarm_failed",
                        target=yolo_class,
                        backend="YOLOE",
                        last_error=str(exc),
                    )
                if yolomedia_stop_event.is_set():
                    print(f"[YOLOMEDIA] lazy prewarm cancelled for: {yolo_class}", flush=True)
                    return
            yolomedia.main(headless=True, prompt_name=yolo_class, stop_event=yolomedia_stop_event)
        except Exception as e:
            technical_error = str(e)
            if "same dtype" in technical_error or "Half != float" in technical_error:
                user_error = "视觉模型精度不兼容，已自动恢复；请重新开始寻找。"
            elif "Inference tensors do not track version counter" in technical_error:
                user_error = "视觉特征缓存异常，已自动清理；请重新开始寻找。"
            else:
                user_error = "视觉模型运行异常，找物已停止并恢复原模式。"
            print(f"[YOLOMEDIA] worker stopped: {technical_error}", flush=True)
            bridge_io.set_yolo_status(
                running=False,
                phase="failed",
                target=current_item_target_zh,
                model_target=yolo_class,
                last_error=user_error,
                technical_error=technical_error,
            )
            bridge_io.send_ui_final(f"[找物品] {user_error}")
        finally:
            global yolomedia_running, yolomedia_sending_frames
            yolomedia_running = False
            yolomedia_sending_frames = False
            status = bridge_io.get_yolo_status()
            if status.get("phase") == "completed":
                if orchestrator:
                    try:
                        orchestrator.stop_item_search(restore_nav=True)
                        print(f"[ITEM_SEARCH] completed by vision, state={orchestrator.get_state()}", flush=True)
                    except Exception as exc:
                        print(f"[ITEM_SEARCH] completed state restore failed: {exc}", flush=True)
                bridge_io.set_yolo_status(running=False, phase="completed")
            elif status.get("phase") == "failed":
                if orchestrator:
                    try:
                        orchestrator.stop_item_search(restore_nav=True)
                        print(f"[ITEM_SEARCH] failed, restored state={orchestrator.get_state()}", flush=True)
                    except Exception as exc:
                        print(f"[ITEM_SEARCH] failed state restore error: {exc}", flush=True)
            else:
                bridge_io.set_yolo_status(running=False, phase="stopped")
    
    yolomedia_thread = threading.Thread(target=_run, daemon=True)
    yolomedia_thread.start()
    print(f"[YOLOMEDIA] background worker started for: {yolo_class}（正在初始化，暂时显示原始画面）", flush=True)

def stop_yolomedia():
    """停止yolomedia线程"""
    global yolomedia_thread, yolomedia_stop_event, yolomedia_running, yolomedia_sending_frames
    
    if yolomedia_running:
        print("[YOLOMEDIA] Stopping worker...", flush=True)
        yolomedia_stop_event.set()
        
        # 等待线程结束（最多等5秒）
        if yolomedia_thread and yolomedia_thread.is_alive():
            yolomedia_thread.join(timeout=5.0)
        
        yolomedia_running = False
        yolomedia_sending_frames = False
        bridge_io.set_yolo_status(running=False, phase="stopping", technical_error="")
        
        # 【新增】如果orchestrator在找物品模式，结束时不自动恢复（由命令控制）
        # 只清理标志位即可
        print("[YOLOMEDIA] Worker stopped, 等待状态切换.", flush=True)

# ========= 自定义的 start_ai_with_text，支持识别特殊命令 =========
async def start_ai_with_text_custom(user_text: str):
    """扩展版的AI启动函数，支持识别特殊命令"""
    global navigation_active, blind_path_navigator, cross_street_active, cross_street_navigator, orchestrator

    if "找到了" in user_text or "拿到了" in user_text or "收到" in user_text:
        print("[ITEM_SEARCH] Found/received command detected", flush=True)
        stop_yolomedia()
        if orchestrator:
            orchestrator.stop_item_search(restore_nav=True)
            current_state = orchestrator.get_state()
            print(f"[ITEM_SEARCH] 找物品结束，当前状态: {current_state}")
            if current_state in ["BLINDPATH_NAV", "SEEKING_CROSSWALK", "WAIT_TRAFFIC_LIGHT", "CROSSING", "SEEKING_NEXT_BLINDPATH"]:
                await ui_broadcast_final("[找物品] 已找到物品，继续导航。")
            else:
                await ui_broadcast_final("[找物品] 已找到物品。")
        else:
            await ui_broadcast_final("[找物品] 已找到物品。")
        return
    
    # 【修改】在导航模式和红绿灯检测模式下，只有特定词才进入omni对话
    if orchestrator:
        current_state = orchestrator.get_state()
        # 如果在导航模式或红绿灯检测模式（非CHAT模式）
        if current_state not in ["CHAT", "IDLE"]:
            # 检查是否是允许的对话触发词
            allowed_keywords = ["帮我看", "帮我看下", "帮我找", "找一下", "看看", "识别一下"]
            is_allowed_query = any(keyword in user_text for keyword in allowed_keywords)
            
            # 检查是否是导航控制命令
            nav_control_keywords = ["开始过马路", "过马路结束", "开始导航", "盲道导航", "停止导航", "结束导航", 
                                   "检测红绿灯", "看红绿灯", "停止检测", "停止红绿灯"]
            is_nav_control = any(keyword in user_text for keyword in nav_control_keywords)
            
            # 如果既不是允许的查询，也不是导航控制命令，则丢弃
            if not is_allowed_query and not is_nav_control:
                mode_name = "红绿灯检测" if current_state == "TRAFFIC_LIGHT_DETECTION" else "导航"
                print(f"[{mode_name}模式] 丢弃非对话语音: {user_text}")
                return  # 直接丢弃，不进入omni
    
    # 【修改】检查是否是过马路相关命令 - 使用orchestrator控制
    if "开始过马路" in user_text or "帮我过马路" in user_text:
        # 【新增】如果正在找物品，先停止
        if yolomedia_running:
            stop_yolomedia()
            print("[ITEM_SEARCH] 从找物品模式切换到过马路")
        
        if orchestrator:
            orchestrator.start_crossing()
            print(f"[CROSS_STREET] 过马路模式已启动，状态: {orchestrator.get_state()}")
            # 播放启动语音并广播到UI
            play_voice_text("过马路模式已启动。")
            await ui_broadcast_final("[系统] 过马路模式已启动")
        else:
            print("[CROSS_STREET] 警告：导航统领器未初始化！")
            play_voice_text("启动过马路模式失败，请稍后重试。")
            await ui_broadcast_final("[系统] 导航系统未就绪")
        return
    
    if "过马路结束" in user_text or "结束过马路" in user_text:
        if orchestrator:
            orchestrator.stop_navigation()
            print(f"[CROSS_STREET] 导航已停止，状态: {orchestrator.get_state()}")
            # 播放停止语音并广播到UI
            play_voice_text("已停止导航。")
            await ui_broadcast_final("[系统] 过马路模式已停止")
        else:
            await ui_broadcast_final("[系统] 导航系统未运行")
        return
    
    # 【修改】检查是否是红绿灯检测命令 - 实现与盲道导航互斥
    if "检测红绿灯" in user_text or "看红绿灯" in user_text:
        try:
            from . import trafficlight_detection
            
            # 切换orchestrator到红绿灯检测模式（暂停盲道导航）
            if orchestrator:
                orchestrator.start_traffic_light_detection()
                print(f"[TRAFFIC] 切换到红绿灯检测模式，状态: {orchestrator.get_state()}")
            
            # 【改进】使用主线程模式而不是独立线程，避免掉帧
            success = trafficlight_detection.init_model()  # 只初始化模型，不启动线程
            trafficlight_detection.reset_detection_state()  # 重置状态
            
            if success:
                await ui_broadcast_final("[系统] 红绿灯检测已启动")
            else:
                await ui_broadcast_final("[系统] 红绿灯模型加载失败")
        except Exception as e:
            print(f"[TRAFFIC] 启动红绿灯检测失败: {e}")
            await ui_broadcast_final(f"[系统] 启动失败: {e}")
        return
    
    if "停止检测" in user_text or "停止红绿灯" in user_text:
        try:
            # 恢复到对话模式
            if orchestrator:
                orchestrator.stop_navigation()  # 回到CHAT模式
                print(f"[TRAFFIC] 红绿灯检测停止，恢复到{orchestrator.get_state()}模式")
            
            await ui_broadcast_final("[系统] 红绿灯检测已停止")
        except Exception as e:
            print(f"[TRAFFIC] 停止红绿灯检测失败: {e}")
            await ui_broadcast_final(f"[系统] 停止失败: {e}")
        return
    
    # 【修改】检查是否是导航相关命令 - 使用orchestrator控制
    if "开始导航" in user_text or "盲道导航" in user_text or "帮我导航" in user_text:
        # 【新增】如果正在找物品，先停止
        if yolomedia_running:
            stop_yolomedia()
            print("[ITEM_SEARCH] 从找物品模式切换到盲道导航")
        
        if orchestrator:
            orchestrator.start_blind_path_navigation()
            print(f"[NAVIGATION] 盲道导航已启动，状态: {orchestrator.get_state()}")
            await ui_broadcast_final("[系统] 盲道导航已启动")
        else:
            print("[NAVIGATION] 警告：导航统领器未初始化！")
            await ui_broadcast_final("[系统] 导航系统未就绪")
        return
    
    if "停止导航" in user_text or "结束导航" in user_text:
        if orchestrator:
            orchestrator.stop_navigation()
            print(f"[NAVIGATION] 导航已停止，状态: {orchestrator.get_state()}")
            await ui_broadcast_final("[系统] 盲道导航已停止")
        else:
            await ui_broadcast_final("[系统] 导航系统未运行")
        return

    nav_cmd_keywords = ["开始过马路", "过马路结束", "开始导航", "盲道导航", "停止导航", "结束导航", "立即通过", "现在通过", "继续"]
    if any(k in user_text for k in nav_cmd_keywords):
        if orchestrator:
            orchestrator.on_voice_command(user_text)
            await ui_broadcast_final("[系统] 导航模式已更新")
        else:
            await ui_broadcast_final("[系统] 导航统领器未初始化")
        return    

    # 检查是否是"帮我找/识别一下xxx"的命令
    # 扩展正则表达式，支持更多关键词
    find_pattern = r"(?:^\s*(?:帮我|请|麻烦)?\s*(?:找一下|找一找|找一个|找找|寻找|搜索|识别一下|检测一下|找)\s*(.+?)(?:在哪里|在哪儿|在哪|哪里|的位置)?(?:。|！|？|\?|$))|(?:^\s*(.+?)(?:在哪里|在哪儿|在哪|哪里|的位置)(?:。|！|？|\?|$))"
    match = re.search(find_pattern, user_text)
        
    if match:
        # 提取中文物品名称
        item_cn = (match.group(1) or match.group(2) or "").strip()
        if item_cn:
            # 【新增】用本地映射 + Qwen 提取英文类名
            label_en, src = extract_english_label(item_cn)
            print(f"[COMMAND] Finder request: '{item_cn}' -> '{label_en}' (src={src})", flush=True)

            # 【新增】切换到找物品模式（暂停导航）
            if orchestrator:
                orchestrator.start_item_search()
                print(f"[ITEM_SEARCH] 已切换到找物品模式，状态: {orchestrator.get_state()}")
            
            # 【关键】把英文类名传给 yolomedia（它会在找不到类时自动切 YOLOE）
            start_yolomedia_with_target(label_en, display_name=item_cn)

            # 给前端/语音来个确认反馈
            try:
                await ui_broadcast_final(f"[找物品] 正在寻找 {item_cn}...")
            except Exception:
                pass

            return
    
    # 【修改】omni对话开始时，切换到CHAT模式
    global omni_conversation_active, omni_previous_nav_state
    omni_conversation_active = True
    
    # 保存当前导航状态并切换到CHAT模式
    if orchestrator:
        current_state = orchestrator.get_state()
        # 只有在导航模式下才需要保存和切换
        if current_state not in ["CHAT", "IDLE"]:
            omni_previous_nav_state = current_state
            orchestrator.force_state("CHAT")
            print(f"[OMNI] 对话开始，从{current_state}切换到CHAT模式")
        else:
            omni_previous_nav_state = None
            print(f"[OMNI] 对话开始（当前已在{current_state}模式）")
    
    # 如果不是特殊命令，执行原有的AI对话逻辑
    # 但如果yolomedia正在运行，暂时不处理普通对话
    if yolomedia_running:
        print("[AI] YOLO media is running, skipping normal AI response", flush=True)
        return
    
    # 原有的AI对话逻辑
    await start_ai_with_text(user_text)

# ========= Omni 播放启动 =========
async def start_ai_with_text(user_text: str):
    """硬重置后，开启新的 AI 语音输出。"""
    async def _runner():
        txt_buf: List[str] = []
        rate_state = None

        # 组装（图像+文本）
        content_list = []
        if last_frames:
            try:
                _, jpeg_bytes = last_frames[-1]
                img_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                })
            except Exception:
                pass
        content_list.append({"type": "text", "text": user_text})

        try:
            async for piece in stream_chat(content_list, voice="Cherry", audio_format="wav"):
                # 文本增量（仅 UI）
                if piece.text_delta:
                    txt_buf.append(piece.text_delta)
                    try:
                        await ui_broadcast_partial("[AI] " + "".join(txt_buf))
                    except Exception:
                        pass

                # 音频分片：Omni 返回 24k (PCM16) 的 wav audio.data（Base64）；下行需要 8k PCM16
                if piece.audio_b64:
                    try:
                        pcm24 = base64.b64decode(piece.audio_b64)
                    except Exception:
                        pcm24 = b""
                    if pcm24:
                        # 24k → 8k (使用ratecv保证音调和速度不变)
                        pcm8k, rate_state = audioop.ratecv(pcm24, 2, 1, 24000, 8000, rate_state)
                        pcm8k = audioop.mul(pcm8k, 2, 0.60)
                        if pcm8k:
                            await broadcast_pcm16_realtime(pcm8k)

        except asyncio.CancelledError:
            # 被新一轮打断
            raise
        except Exception as e:
            try:
                await ui_broadcast_final(f"[AI] 发生错误：{e}")
            except Exception:
                pass
        finally:
            await finish_pcm16_stream()
            # 【修改】标记omni对话结束，恢复之前的导航模式
            global omni_conversation_active, omni_previous_nav_state
            omni_conversation_active = False
            
            # 恢复之前的导航状态
            if orchestrator and omni_previous_nav_state:
                orchestrator.force_state(omni_previous_nav_state)
                print(f"[OMNI] 对话结束，恢复到{omni_previous_nav_state}模式")
                omni_previous_nav_state = None
            else:
                print(f"[OMNI] 对话结束（无需恢复导航状态）")
            
            final_text = ("".join(txt_buf)).strip() or "（空响应）"
            try:
                await ui_broadcast_final("[AI] " + final_text)
            except Exception:
                pass

    # 真正启动前先硬重置，保证**绝无**旧音频残留
    await soft_reset_audio("start_ai_with_text")
    loop = asyncio.get_running_loop()
    from .audio_stream import current_ai_task as _task_holder  # 读写模块内全局
    from .audio_stream import __dict__ as _as_dict
    # 设置模块内的 current_ai_task
    task = loop.create_task(_runner())
    _as_dict["current_ai_task"] = task

# ---------- 页面 / 健康 ----------
@app.get("/", response_class=HTMLResponse)
def root():
    with open(os.path.join(APP_DIR, "templates", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/health", response_class=PlainTextResponse)
def health():
    return "OK"

def _device_status_payload() -> dict:
    def _ws_connected(ws: Optional[WebSocket]) -> bool:
        try:
            return bool(ws and ws.client_state == WebSocketState.CONNECTED)
        except Exception:
            return False

    last_frame_age = None
    if last_frames:
        try:
            last_frame_age = max(0.0, time.time() - last_frames[-1][0])
        except Exception:
            last_frame_age = None
    yolo_status = bridge_io.get_yolo_status()
    if current_item_target_zh:
        yolo_status["target_zh"] = current_item_target_zh
    return {
        "camera_connected": _ws_connected(esp32_camera_ws),
        "audio_connected": _ws_connected(esp32_audio_ws),
        "viewer_count": len(camera_viewers),
        "imu_viewer_count": len(imu_ws_clients),
        "last_frame_age_sec": last_frame_age,
        "asr_streaming": bool(asr_diag.get("streaming")),
        "asr_audio_chunks": int(asr_diag.get("audio_chunks") or 0),
        "asr_last_error": asr_diag.get("last_error") or "",
        "mode": _current_mode(),
        "item_search_running": bool(yolomedia_running),
        "item_search_target": current_item_target_zh,
        "audio_stream": get_stream_status(),
        "yolo": yolo_status,
        "pipeline": pipeline_metrics.snapshot(),
        "frame_buffer": bridge_io.get_frame_stats(),
        "performance_profile": profile_payload(PERFORMANCE_PROFILE),
    }


@app.get("/api/device-status")
def device_status():
    return _device_status_payload()


@app.get("/api/asr-status")
def asr_status():
    now = time.time()
    def age(ts):
        return None if not ts else max(0.0, now - float(ts))
    return {
        "api_key_configured": bool(API_KEY),
        "audio_ws_connected": bool(asr_diag.get("audio_ws_connected")),
        "streaming": bool(asr_diag.get("streaming")),
        "audio_chunks": int(asr_diag.get("audio_chunks") or 0),
        "started_age_sec": age(asr_diag.get("started_at")),
        "last_audio_age_sec": age(asr_diag.get("last_audio_at")),
        "last_partial_age_sec": age(asr_diag.get("last_partial_at")),
        "last_final_age_sec": age(asr_diag.get("last_final_at")),
        "last_command": asr_diag.get("last_command") or "",
        "last_error": asr_diag.get("last_error") or "",
    }

def _local_ipv4s() -> List[str]:
    ips: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        except Exception:
            pass
    def _score(ip: str) -> int:
        parts = ip.split(".")
        try:
            first = int(parts[0])
            second = int(parts[1])
        except Exception:
            return 99
        if first == 192 and second == 168:
            return 0
        if first == 10:
            return 1
        if first == 172 and 16 <= second <= 31:
            return 2
        if first == 169 and second == 254:
            return 8
        if first == 198 and second in (18, 19):
            return 9
        return 5

    return sorted(ips, key=_score)

def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}***{key[-4:]}"

def _set_asr_diag(**kwargs) -> None:
    asr_diag.update(kwargs)

def _persist_env_value(key: str, value: str) -> None:
    if not key or not value:
        return
    env_path = os.path.join(APP_DIR, ".env")
    lines: List[str] = []
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []

    updated = False
    prefix = f"{key}="
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'{key}="{escaped}"'
    for idx, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[idx] = new_line
            updated = True
            break
    if not updated:
        lines.append(new_line)

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
    except Exception as exc:
        print(f"[CONFIG] failed to persist {key} to .env: {exc}", flush=True)

@app.get("/api/runtime-config")
def runtime_config(request: Request):
    host_header = request.headers.get("host", "127.0.0.1:8081")
    port = host_header.split(":")[-1] if ":" in host_header else "8081"
    local_ips = _local_ipv4s()
    recommended_host = local_ips[0] if local_ips else host_header.split(":")[0]
    base_host = f"{recommended_host}:{port}"
    return {
        "api_key_configured": bool(API_KEY),
        "api_key_masked": _mask_key(API_KEY),
        "http_url": f"http://{base_host}/",
        "server_host": recommended_host,
        "server_port": port,
        "local_ips": local_ips,
        "endpoints": {
            "camera_ws": f"ws://{base_host}/ws/camera",
            "audio_ws": f"ws://{base_host}/ws_audio",
            "viewer_ws": f"ws://{base_host}/ws/viewer",
            "ui_ws": f"ws://{base_host}/ws_ui",
            "imu_ws": f"ws://{base_host}/ws",
            "audio_stream": f"http://{base_host}/stream.wav",
            "imu_udp": f"{recommended_host}:{UDP_PORT}",
        },
        "notes": {
            "camera_ws": "ESP32 摄像头 JPEG 二进制上传",
            "audio_ws": "ESP32 麦克风 PCM16 上传",
            "imu_udp": "ESP32 IMU UDP JSON 发送目标",
        },
        "performance_profile": PERFORMANCE_PROFILE,
        "performance_profiles": {
            key: profile_payload(key)
            for key in PROFILES
        },
        "models": {
            "blind_path_model": os.getenv("BLIND_PATH_MODEL", DEFAULT_NAV_SEG_MODEL),
            "obstacle_model": os.getenv("OBSTACLE_MODEL", DEFAULT_OBSTACLE_MODEL),
            "trafficlight_model": os.getenv("TRAFFICLIGHT_MODEL", DEFAULT_TRAFFIC_MODEL),
            "hand_task_path": os.getenv("HAND_TASK_PATH", DEFAULT_HAND_TASK),
            "item_search_model": os.getenv("YOLOE_MODEL_PATH", DEFAULT_ITEM_MODEL),
        },
    }

@app.post("/api/runtime-config")
async def update_runtime_config(request: Request):
    global API_KEY
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_key = str(body.get("dashscope_api_key") or "").strip()
    if new_key:
        API_KEY = new_key
        os.environ["DASHSCOPE_API_KEY"] = new_key
        _persist_env_value("DASHSCOPE_API_KEY", new_key)
        try:
            from . import omni_client as _omni_client
            if hasattr(_omni_client, "set_api_key"):
                _omni_client.set_api_key(new_key)
        except Exception:
            pass
    requested_profile = str(body.get("performance_profile") or PERFORMANCE_PROFILE)
    profile = _apply_performance_profile(requested_profile)
    model_updates = {
        field: (env_key, str(body.get(field) or "").strip())
        for field, (env_key, _default) in MODEL_CONFIG_FIELDS.items()
    }
    persisted_models = {}
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        existing = {}
    if isinstance(existing, dict) and isinstance(existing.get("models"), dict):
        persisted_models.update(existing["models"])

    saved_models = {}
    for field, (env_key, value) in model_updates.items():
        if value:
            os.environ[env_key] = value
            persisted_models[field] = value
            saved_models[field] = value

    for field, (env_key, default) in MODEL_CONFIG_FIELDS.items():
        persisted_models.setdefault(field, os.getenv(env_key, default))
    _persist_runtime_config(persisted_models)
    if esp32_camera_ws is not None:
        try:
            await _send_camera_profile(esp32_camera_ws)
        except Exception:
            pass
    return {
        "ok": True,
        "api_key_configured": bool(API_KEY),
        "api_key_masked": _mask_key(API_KEY),
        "saved_models": saved_models,
        "performance_profile": profile,
    }


def _dev_control_allowed(request: Request) -> bool:
    if os.getenv("AIGLASS_ALLOW_DEV_CONTROL", "0").strip().lower() in ("1", "true", "yes", "on"):
        return True
    host = str(request.client.host if request.client else "").split("%", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.post("/api/dev/command")
async def dev_command(request: Request):
    """本机硬件测试入口；不会启动 ASR，也不会产生语音识别调用费用。"""
    global current_item_target_zh
    if not _dev_control_allowed(request):
        raise HTTPException(
            status_code=403,
            detail="开发控制接口仅允许本机访问；远程调试请设置 AIGLASS_ALLOW_DEV_CONTROL=1。",
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    command = str(body.get("command") or "").strip().lower()
    target = str(body.get("target") or "手机").strip() or "手机"

    if command in ("find", "item_search"):
        label_en, source = extract_english_label(target)
        if orchestrator is not None:
            orchestrator.start_item_search()
        start_yolomedia_with_target(label_en, display_name=target)
        await ui_broadcast_final(f"[找物品] 正在寻找“{target}”")
        message = f"已开始寻找“{target}”"
        extra = {"target": target, "model_target": label_en, "label_source": source}
    elif command in ("stop_find", "stop_item_search"):
        stop_yolomedia()
        if orchestrator is not None:
            orchestrator.stop_item_search(restore_nav=False)
        bridge_io.set_yolo_status(running=False, phase="stopped", target=current_item_target_zh)
        await ui_broadcast_final(f"[找物品] 已停止寻找“{current_item_target_zh or target}”")
        message = "已停止找物"
        extra = {}
        current_item_target_zh = ""
    elif command in ("blindpath", "blind_path"):
        stop_yolomedia()
        current_item_target_zh = ""
        if orchestrator is None:
            raise HTTPException(status_code=409, detail="导航器尚未就绪，请先连接摄像头。")
        orchestrator.start_blind_path_navigation()
        await ui_broadcast_final("[系统] 盲道导航已启动")
        message = "已启动盲道导航"
        extra = {}
    elif command in ("traffic", "traffic_light"):
        stop_yolomedia()
        current_item_target_zh = ""
        if orchestrator is None:
            raise HTTPException(status_code=409, detail="导航器尚未就绪，请先连接摄像头。")
        from . import trafficlight_detection
        if not trafficlight_detection.init_model():
            raise HTTPException(status_code=503, detail="红绿灯模型加载失败。")
        trafficlight_detection.reset_detection_state()
        orchestrator.start_traffic_light_detection()
        await ui_broadcast_final("[系统] 红绿灯检测已启动")
        message = "已启动红绿灯检测"
        extra = {}
    elif command in ("crossing", "cross"):
        stop_yolomedia()
        current_item_target_zh = ""
        if orchestrator is None:
            raise HTTPException(status_code=409, detail="导航器尚未就绪，请先连接摄像头。")
        orchestrator.start_crossing()
        await ui_broadcast_final("[系统] 过马路模式已启动")
        message = "已启动过马路模式"
        extra = {}
    elif command in ("chat", "idle"):
        stop_yolomedia()
        current_item_target_zh = ""
        if orchestrator is not None:
            orchestrator.stop_navigation()
        bridge_io.set_yolo_status(running=False, phase="idle", target="", last_error="")
        await ui_broadcast_final("[系统] 已返回聊天模式")
        message = "已返回聊天模式"
        extra = {}
    else:
        raise HTTPException(
            status_code=400,
            detail="未知命令，可用命令：find、stop_find、blindpath、traffic、crossing、chat。",
        )
    return {"ok": True, "message": message, "status": _device_status_payload(), **extra}


# 注册 /stream.wav
register_stream_route(app)

# ---------- WebSocket：WebUI 文本（ASR/AI 状态推送） ----------
@app.websocket("/ws_ui")
async def ws_ui(ws: WebSocket):
    await ws.accept()
    ui_clients[id(ws)] = ws
    ui_client_locks[id(ws)] = asyncio.Lock()
    try:
        init = {"partial": current_partial, "finals": recent_finals[-10:]}
        await _send_ui_message(ws, "INIT:" + json.dumps(init, ensure_ascii=False))
        while True:
            await _send_ui_message(
                ws,
                "STATUS:" + json.dumps(_device_status_payload(), ensure_ascii=False),
            )
            await asyncio.sleep(0.75)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ui_clients.pop(id(ws), None)
        ui_client_locks.pop(id(ws), None)

# ---------- WebSocket：ESP32 音频入口（ASR 上行） ----------
@app.websocket("/ws_audio")
async def ws_audio(ws: WebSocket):
    global esp32_audio_ws
    esp32_audio_ws = ws
    await ws.accept()
    _set_asr_diag(audio_ws_connected=True, streaming=False, last_error="")
    print("\n[AUDIO] client connected")
    recognition = None
    streaming = False
    last_ts = time.monotonic()
    keepalive_task: Optional[asyncio.Task] = None

    async def stop_rec(send_notice: Optional[str] = None):
        nonlocal recognition, streaming, keepalive_task
        if keepalive_task and not keepalive_task.done():
            keepalive_task.cancel()
            try: await keepalive_task
            except Exception: pass
        keepalive_task = None
        if recognition:
            try: recognition.stop()
            except Exception: pass
            recognition = None
        await set_current_recognition(None)
        streaming = False
        _set_asr_diag(streaming=False)
        if send_notice:
            try: await ws.send_text(send_notice)
            except Exception: pass

    async def on_sdk_error(_msg: str):
        print(f"[ASR ERROR] {_msg}", flush=True)
        _set_asr_diag(last_error=str(_msg), streaming=False)
        await stop_rec(send_notice="RESTART")

    async def keepalive_loop():
        nonlocal last_ts, recognition, streaming
        try:
            while streaming and recognition is not None:
                idle = time.monotonic() - last_ts
                if idle > 0.35:
                    try:
                        for _ in range(30):  # ~600ms 静音
                            recognition.send_audio_frame(SILENCE_20MS)
                        last_ts = time.monotonic()
                    except Exception:
                        await on_sdk_error("keepalive send failed")
                        return
                await asyncio.sleep(0.10)
        except asyncio.CancelledError:
            return

    try:
        while True:
            if WebSocketState and ws.client_state != WebSocketState.CONNECTED:
                break
            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError as e:
                if "Cannot call \"receive\"" in str(e):
                    break
                raise

            if "text" in msg and msg["text"] is not None:
                raw = (msg["text"] or "").strip()
                cmd = raw.upper()

                if cmd == "START":
                    print("[AUDIO] START received")
                    _set_asr_diag(last_command="START", last_error="", audio_chunks=0)
                    if not API_KEY:
                        msg = "missing DASHSCOPE_API_KEY"
                        print(f"[ASR ERROR] {msg}", flush=True)
                        _set_asr_diag(last_error=msg, streaming=False)
                        await ws.send_text("ERR:NO_API_KEY")
                        continue
                    await stop_rec()
                    loop = asyncio.get_running_loop()
                    def post(coro):
                        asyncio.run_coroutine_threadsafe(coro, loop)

                    # 组装 ASR 回调（把依赖都注入）
                    cb = ASRCallback(
                        on_sdk_error=lambda s: post(on_sdk_error(s)),
                        post=post,
                        ui_broadcast_partial=ui_broadcast_partial,
                        ui_broadcast_final=ui_broadcast_final,
                        is_playing_now_fn=is_playing_now,
                        start_ai_with_text_fn=start_ai_with_text_custom,  # 使用自定义版本
                        full_system_reset_fn=full_system_reset,
                        interrupt_lock=interrupt_lock,
                    )

                    try:
                        recognition = dash_audio.asr.Recognition(
                            api_key=API_KEY, model=MODEL, format=AUDIO_FMT,
                            sample_rate=SAMPLE_RATE, callback=cb
                        )
                        recognition.start()
                    except Exception as exc:
                        msg = f"recognition start failed: {exc}"
                        print(f"[ASR ERROR] {msg}", flush=True)
                        _set_asr_diag(last_error=msg, streaming=False)
                        recognition = None
                        await ws.send_text("ERR:START_FAILED")
                        continue
                    await set_current_recognition(recognition)
                    streaming = True
                    last_ts = time.monotonic()
                    _set_asr_diag(streaming=True, started_at=time.time(), last_audio_at=None, audio_chunks=0, last_error="")
                    keepalive_task = asyncio.create_task(keepalive_loop())
                    await ui_broadcast_partial("（已开始接收音频…）")
                    await ws.send_text("OK:STARTED")

                elif cmd == "STOP":
                    print("[AUDIO] STOP received", flush=True)
                    _set_asr_diag(last_command="STOP")
                    if recognition:
                        for _ in range(15):  # ~300ms 静音
                            try: recognition.send_audio_frame(SILENCE_20MS)
                            except Exception: break
                    await stop_rec(send_notice="OK:STOPPED")

                elif raw.startswith("PROMPT:"):
                    # 设备端主动发起一轮：同样使用“先硬重置后播放”的强语义
                    text = raw[len("PROMPT:"):].strip()
                    if text:
                        async with interrupt_lock:
                            await start_ai_with_text_custom(text) # 使用自定义的启动函数
                        await ws.send_text("OK:PROMPT_ACCEPTED")
                    else:
                        await ws.send_text("ERR:EMPTY_PROMPT")

            elif "bytes" in msg and msg["bytes"] is not None:
                if streaming and recognition:
                    try:
                        recognition.send_audio_frame(msg["bytes"])
                        last_ts = time.monotonic()
                        chunks = int(asr_diag.get("audio_chunks") or 0) + 1
                        if chunks % 250 == 0:
                            print(f"[AUDIO] ASR received {chunks} chunks", flush=True)
                        _set_asr_diag(audio_chunks=chunks, last_audio_at=time.time())
                    except Exception:
                        await on_sdk_error("send_audio_frame failed")

    except Exception as e:
        print(f"\n[WS ERROR] {e}")
    finally:
        await stop_rec()
        try:
            if WebSocketState is None or ws.client_state == WebSocketState.CONNECTED:
                await ws.close(code=1000)
        except Exception:
            pass
        if esp32_audio_ws is ws:
            esp32_audio_ws = None
        _set_asr_diag(audio_ws_connected=False, streaming=False)
        print("[WS] connection closed")

# ---------- WebSocket：ESP32 相机入口（JPEG 二进制） ----------
@app.websocket("/ws/camera")
async def ws_camera_esp(ws: WebSocket):
    global esp32_camera_ws, blind_path_navigator, cross_street_navigator, cross_street_active, navigation_active, orchestrator, camera_stats
    if esp32_camera_ws is not None:
        await ws.close(code=1013)
        return
    esp32_camera_ws = ws
    await ws.accept()
    print("[CAMERA] ESP32 connected")
    
    # 【新增】初始化盲道导航器
    if blind_path_navigator is None and yolo_seg_model is not None:
        blind_path_navigator = BlindPathNavigator(yolo_seg_model, obstacle_detector)
        print("[NAVIGATION] 盲道导航器已初始化")
    else:
        if blind_path_navigator is not None:
            print("[NAVIGATION] 导航器已存在，无需重新初始化")
        elif yolo_seg_model is None:
            print("[NAVIGATION] 警告：YOLO模型未加载，无法初始化导航器")
    
    # 【新增】初始化过马路导航器
    if cross_street_navigator is None:
        if yolo_seg_model:
            # CrossStreetNavigator defaults to auto-loading the YOLOE obstacle detector
            # when obs_model is None. Keep it off here so camera connect never blocks on
            # the large MobileCLIP text-feature download.
            os.environ.setdefault("AIGLASS_OBS_AUTO", "0")
            cross_street_navigator = CrossStreetNavigator(
                seg_model=yolo_seg_model,
                coco_model=None,  # 不使用交通灯检测
                obs_model=None    # 暂时也不用障碍物检测，让它更快
            )
            print("[CROSS_STREET] 过马路导航器已初始化（简化版 - 仅斑马线检测）")
        else:
            print("[CROSS_STREET] 错误：缺少分割模型，无法初始化过马路导航器")
            
            if not yolo_seg_model:
                print("[CROSS_STREET] - 缺少分割模型 (yolo_seg_model)")
            if not obstacle_detector:
                print("[CROSS_STREET] - 缺少障碍物检测器 (obstacle_detector)")
    
    if orchestrator is None and blind_path_navigator is not None and cross_street_navigator is not None:
        orchestrator = NavigationMaster(blind_path_navigator, cross_street_navigator)
        print("[NAV MASTER] 统领状态机已初始化（托管模式）")
    _apply_performance_profile(PERFORMANCE_PROFILE)
    await _send_camera_profile(ws)
    frame_counter = 0
    last_stat_log = time.time()

    try:
        while True:
            msg = await ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                data = msg["bytes"]
                frame_counter += 1
                now = time.time()
                pipeline_metrics.on_capture(len(data), now)
                if ENABLE_SYNC_RECORDING:
                    try:
                        sync_recorder.record_frame(data)
                    except Exception as e:
                        if frame_counter % 100 == 0:
                            print(f"[RECORDER] 录制帧失败: {e}")
                last_frames.append((now, data))
                bridge_io.push_raw_jpeg(data)
                # 原始 JPEG 始终作为低延迟底图；推理完成后再用处理帧覆盖。
                # 找物进入跟踪后由 yolomedia 连续输出，此时停止原始帧覆盖。
                if not yolomedia_sending_frames and camera_viewers:
                    _offer_viewer_frame(data)
                if now - last_stat_log >= 5.0:
                    snap = pipeline_metrics.snapshot()
                    print(
                        f"[CAMERA] 接收={snap['capture_fps']:.1f} FPS "
                        f"处理={snap['processed_fps']:.1f} FPS "
                        f"显示={snap['broadcast_fps']:.1f} FPS "
                        f"延迟={snap['latency_ms']:.0f}ms",
                        flush=True,
                    )
                    last_stat_log = now

            elif "text" in msg and msg["text"] is not None:
                text = str(msg["text"]).strip()
                if text.startswith("STAT:"):
                    try:
                        camera_stats = json.loads(text[5:])
                        pipeline_metrics.update_esp32_camera(camera_stats)
                    except Exception:
                        pass

            elif "type" in msg and msg["type"] in ("websocket.close", "websocket.disconnect"):
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[CAMERA ERROR] {e}")
    finally:
        try:
            if WebSocketState is None or ws.client_state == WebSocketState.CONNECTED:
                await ws.close(code=1000)
        except Exception:
            pass
        esp32_camera_ws = None
        bridge_io.clear_raw_frames()
        bridge_io.set_yolo_status(
            running=bool(yolomedia_running),
            phase="camera_waiting" if yolomedia_running else "idle",
            last_error="",
        )
        print("[CAMERA] ESP32 disconnected")

# ---------- WebSocket：浏览器订阅相机帧 ----------
@app.websocket("/ws/viewer")
async def ws_viewer(ws: WebSocket):
    await ws.accept()
    camera_viewers.add(ws)
    print(f"[VIEWER] Browser connected. Total viewers: {len(camera_viewers)}", flush=True)
    try:
        while True:
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=15.0)
                if message.get("type") in ("websocket.close", "websocket.disconnect"):
                    break
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        print("[VIEWER] Browser disconnected", flush=True)
    finally:
        try: 
            camera_viewers.remove(ws)
        except Exception: 
            pass
        print(f"[VIEWER] Removed. Total viewers: {len(camera_viewers)}", flush=True)

# ---------- WebSocket：浏览器订阅 IMU ----------
@app.websocket("/ws")
async def ws_imu(ws: WebSocket):
    await ws.accept()
    imu_ws_clients.add(ws)
    try:
        while True:
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        pass
    finally:
        imu_ws_clients.discard(ws)

async def imu_broadcast(msg: str):
    if not imu_ws_clients: return
    dead = []
    for ws in list(imu_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        imu_ws_clients.discard(ws)

# ---------- 服务端 IMU 估计（原样保留） ----------
from math import atan2, hypot, pi
GRAV_BETA   = 0.98
STILL_W     = 0.4
YAW_DB      = 0.08
YAW_LEAK    = 0.2
ANG_EMA     = 0.15
AUTO_REZERO = True
USE_PROJ    = True
FREEZE_STILL= True
G     = 9.807
A_TOL = 0.08 * G
gLP = {"x":0.0, "y":0.0, "z":0.0}
gOff= {"x":0.0, "y":0.0, "z":0.0}
BIAS_ALPHA = 0.002
yaw  = 0.0
Rf = Pf = Yf = 0.0
ref = {"roll":0.0, "pitch":0.0, "yaw":0.0}
holdStart = 0.0
isStill   = False
last_ts_imu = 0.0
last_wall = 0.0
imu_store: List[Dict[str, Any]] = []

def _wrap180(a: float) -> float:
    a = a % 360.0
    if a >= 180.0: a -= 360.0
    if a < -180.0: a += 360.0
    return a

def process_imu_and_maybe_store(d: Dict[str, Any]):
    global gLP, gOff, yaw, Rf, Pf, Yf, ref, holdStart, isStill, last_ts_imu, last_wall

    t_ms = float(d.get("ts", 0.0))
    now_wall = time.monotonic()
    if t_ms <= 0.0:
        t_ms = (now_wall * 1000.0)
    if last_ts_imu <= 0.0 or t_ms <= last_ts_imu or (t_ms - last_ts_imu) > 3000.0:
        dt = 0.02
    else:
        dt = (t_ms - last_ts_imu) / 1000.0
    last_ts_imu = t_ms

    ax = float(((d.get("accel") or {}).get("x", 0.0)))
    ay = float(((d.get("accel") or {}).get("y", 0.0)))
    az = float(((d.get("accel") or {}).get("z", 0.0)))
    wx = float(((d.get("gyro")  or {}).get("x", 0.0)))
    wy = float(((d.get("gyro")  or {}).get("y", 0.0)))
    wz = float(((d.get("gyro")  or {}).get("z", 0.0)))

    gLP["x"] = GRAV_BETA * gLP["x"] + (1.0 - GRAV_BETA) * ax
    gLP["y"] = GRAV_BETA * gLP["y"] + (1.0 - GRAV_BETA) * ay
    gLP["z"] = GRAV_BETA * gLP["z"] + (1.0 - GRAV_BETA) * az
    gmag = hypot(gLP["x"], gLP["y"], gLP["z"]) or 1.0
    gHat = {"x": gLP["x"]/gmag, "y": gLP["y"]/gmag, "z": gLP["z"]/gmag}

    roll  = (atan2(az, ay)   * 180.0 / pi)
    pitch = (atan2(-ax, ay)  * 180.0 / pi)

    aNorm = hypot(ax, ay, az); wNorm = hypot(wx, wy, wz)
    nearFlat = (abs(roll) < 2.0 and abs(pitch) < 2.0)
    stillCond = (abs(aNorm - G) < A_TOL) and (wNorm < STILL_W)

    if stillCond:
        if holdStart <= 0.0: holdStart = t_ms
        if not isStill and (t_ms - holdStart) > 350.0: isStill = True
        gOff["x"] = (1.0 - BIAS_ALPHA)*gOff["x"] + BIAS_ALPHA*wx
        gOff["y"] = (1.0 - BIAS_ALPHA)*gOff["y"] + BIAS_ALPHA*wy
        gOff["z"] = (1.0 - BIAS_ALPHA)*gOff["z"] + BIAS_ALPHA*wz
    else:
        holdStart = 0.0; isStill = False

    if USE_PROJ:
        yawdot = ((wx - gOff["x"])*gHat["x"] + (wy - gOff["y"])*gHat["y"] + (wz - gOff["z"])*gHat["z"])
    else:
        yawdot = (wy - gOff["y"])

    if abs(yawdot) < YAW_DB: yawdot = 0.0
    if FREEZE_STILL and stillCond: yawdot = 0.0

    yaw = _wrap180(yaw + yawdot * dt)

    if (YAW_LEAK > 0.0) and nearFlat and stillCond and abs(yaw) > 0.0:
        step = YAW_LEAK * dt * (-1.0 if yaw > 0 else (1.0 if yaw < 0 else 0.0))
        if abs(yaw) <= abs(step): yaw = 0.0
        else: yaw += step

    global Rf, Pf, Yf, ref, last_wall
    Rf = ANG_EMA * roll  + (1.0 - ANG_EMA) * Rf
    Pf = ANG_EMA * pitch + (1.0 - ANG_EMA) * Pf
    Yf = ANG_EMA * yaw   + (1.0 - ANG_EMA) * Yf

    if AUTO_REZERO and nearFlat and (wNorm < STILL_W):
        if holdStart <= 0.0: holdStart = t_ms
        if not isStill and (t_ms - holdStart) > 350.0:
            ref.update({"roll": Rf, "pitch": Pf, "yaw": Yf})
            isStill = True

    R = _wrap180(Rf - ref["roll"])
    P = _wrap180(Pf - ref["pitch"])
    Y = _wrap180(Yf - ref["yaw"])

    now_wall = time.monotonic()
    if last_wall <= 0.0 or (now_wall - last_wall) >= 0.100:
        last_wall = now_wall
        item = {
            "ts": t_ms/1000.0,
            "angles": {"roll": R, "pitch": P, "yaw": Y},
            "accel":  {"x": ax, "y": ay, "z": az},
            "gyro":   {"x": wx, "y": wy, "z": wz},
        }
        imu_store.append(item)

# ---------- UDP 接收 IMU 并转发 ----------
class UDPProto(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        print(f"[UDP] listening on {UDP_IP}:{UDP_PORT}")
    def datagram_received(self, data, addr):
        try:
            s = data.decode('utf-8', errors='ignore').strip()
            d = json.loads(s)
            if 'ts' not in d and 'timestamp_ms' in d:
                d['ts'] = d.pop('timestamp_ms')
            process_imu_and_maybe_store(d)
            asyncio.create_task(imu_broadcast(json.dumps(d)))
        except Exception:
            pass



# === 注册 bridge_io 回调并启动低延迟视觉管线 ===
@app.on_event("startup")
async def on_startup_register_bridge_sender():
    global _output_event, _viewer_broadcast_task, _visual_worker_thread
    main_loop = asyncio.get_running_loop()
    _output_event = asyncio.Event()
    _viewer_broadcast_task = asyncio.create_task(
        _viewer_broadcast_loop(),
        name="viewer-latest-frame-broadcaster",
    )
    _visual_worker_stop.clear()
    if _visual_worker_thread is None or not _visual_worker_thread.is_alive():
        _visual_worker_thread = threading.Thread(
            target=_visual_worker,
            name="visual-worker",
            daemon=True,
        )
        _visual_worker_thread.start()

    def _sender(jpeg_bytes: bytes):
        if main_loop.is_closed():
            return

        def _offer():
            global yolomedia_sending_frames
            if yolomedia_running and not yolomedia_sending_frames:
                yolomedia_sending_frames = True
                print("[YOLOMEDIA] 已切换到处理后画面", flush=True)
            _offer_viewer_frame(jpeg_bytes, processed=True)

        try:
            main_loop.call_soon_threadsafe(_offer)
        except RuntimeError:
            pass

    def _ui_sender(text: str):
        if main_loop.is_closed():
            return

        def _schedule():
            asyncio.create_task(ui_broadcast_final(text))

        try:
            main_loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            pass

    bridge_io.set_sender(_sender)
    bridge_io.set_ui_sender(_ui_sender)

@app.on_event("startup")
async def on_startup_init_audio():
    """启动时初始化音频系统"""
    # 在后台线程中初始化，避免阻塞启动
    def _init():
        try:
            initialize_audio_system()
        except Exception as e:
            print(f"[AUDIO] 初始化失败: {e}")
    
    threading.Thread(target=_init, daemon=True).start()

@app.on_event("startup")
async def on_startup_prewarm_yoloe():
    def _prewarm():
        try:
            from .yoloe_backend import prewarm_yoloe
            raw = os.getenv("AIGLASS_YOLOE_PREWARM_CLASSES", DEFAULT_YOLOE_PREWARM_CLASSES)
            names = [x.strip() for x in raw.split(",") if x.strip()]
            if os.getenv("AIGLASS_YOLOE_PREWARM_CUSTOM", "0").strip().lower() in ("1", "true", "yes", "on"):
                names.extend(ITEM_TO_CLASS_MAP.values())
            prewarm_yoloe(names)
        except Exception as e:
            print(f"[YOLOE] prewarm failed: {e}", flush=True)

    if os.getenv("AIGLASS_YOLOE_PREWARM", "1").strip().lower() not in ("0", "false", "no", "off"):
        threading.Thread(target=_prewarm, name="yoloe-prewarm", daemon=True).start()

@app.on_event("startup")
async def on_startup():
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: UDPProto(), local_addr=(UDP_IP, UDP_PORT))

@app.on_event("shutdown")
async def on_shutdown():
    """应用关闭时的清理工作"""
    global _viewer_broadcast_task, _visual_worker_thread, _output_event
    print("[SHUTDOWN] 开始清理资源...")
    
    # 停止YOLO媒体处理
    stop_yolomedia()
    _visual_worker_stop.set()
    bridge_io.clear_raw_frames()
    if _visual_worker_thread is not None and _visual_worker_thread.is_alive():
        await asyncio.to_thread(_visual_worker_thread.join, 2.0)
    _visual_worker_thread = None
    bridge_io.set_sender(None)
    bridge_io.set_ui_sender(None)
    if _viewer_broadcast_task is not None:
        _viewer_broadcast_task.cancel()
        try:
            await _viewer_broadcast_task
        except asyncio.CancelledError:
            pass
        _viewer_broadcast_task = None
    _output_event = None
    
    # 停止音频和AI任务
    await hard_reset_audio("shutdown")
    
    print("[SHUTDOWN] 资源清理完成")

# app_main.py —— 在文件里已有的 @app.on_event("startup") 之后，再加一个新的 startup 钩子


# --- 导出接口（可选） ---
def get_last_frames():
    return last_frames

def get_camera_ws():
    return esp32_camera_ws

if __name__ == "__main__":
    uvicorn.run(
        app, host="0.0.0.0", port=8081,
        log_level="warning", access_log=False,
        loop="asyncio", workers=1, reload=False
    )
