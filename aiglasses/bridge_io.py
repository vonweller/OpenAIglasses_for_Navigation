# bridge_io.py
# 极简桥：接原始JPEG → 提供BGR帧给外部算法；外部算法产出BGR → 广播给前端
import threading
from collections import deque
import time
from dataclasses import dataclass
import cv2
import numpy as np

# 原始JPEG帧缓冲（只保留最新 N 帧）
_MAX_BUF = 1
_frames = deque(maxlen=_MAX_BUF)
_cond = threading.Condition()
_frame_seq = 0
_frame_drop_count = 0
_last_frame_at = None
_consumer_state = threading.local()

# 向前端发送JPEG的回调，由 app_main.py 在启动时注册
_sender_lock = threading.Lock()
_sender_cb = None

# 向前端发送UI文本的回调（由 app_main.py 在启动时注册）
_ui_sender_lock = threading.Lock()
_ui_sender_cb = None

_yolo_status_lock = threading.Lock()
_yolo_status = {
    "running": False,
    "phase": "idle",
    "target": "",
    "backend": "",
    "device": "",
    "model_path": "",
    "frames": 0,
    "inferences": 0,
    "detections": 0,
    "last_error": "",
    "started_at": None,
    "updated_at": None,
}


@dataclass(frozen=True)
class RawFrame:
    seq: int
    captured_at: float
    bgr: np.ndarray

def set_sender(cb):
    """由 app_main.py 调用，注册一个函数：cb(jpeg_bytes)->None"""
    global _sender_cb
    with _sender_lock:
        _sender_cb = cb

def set_ui_sender(cb):
    """由 app_main.py 调用，注册一个函数：cb(text:str)->None"""
    global _ui_sender_cb
    with _ui_sender_lock:
        _ui_sender_cb = cb

def set_yolo_status(**kwargs):
    """Thread-safe status channel for item-search/YOLO diagnostics."""
    with _yolo_status_lock:
        _yolo_status.update(kwargs)
        _yolo_status["updated_at"] = time.time()

def get_yolo_status():
    with _yolo_status_lock:
        return dict(_yolo_status)

def push_raw_jpeg(jpeg_bytes: bytes):
    """由 app_main.py 在收到 /ws/camera 帧时调用"""
    global _frame_seq, _frame_drop_count, _last_frame_at
    if not jpeg_bytes:
        return
    with _cond:
        if _frames:
            _frame_drop_count += 1
        now = time.time()
        _frame_seq += 1
        _last_frame_at = now
        _frames.append((_frame_seq, now, jpeg_bytes))
        _cond.notify_all()


def clear_raw_frames():
    """摄像头断开或管线重置时清除旧画面。"""
    with _cond:
        _frames.clear()
        _cond.notify_all()


def get_frame_stats():
    with _cond:
        return {
            "latest_seq": _frame_seq,
            "last_frame_at": _last_frame_at,
            "buffered": len(_frames),
            "dropped": _frame_drop_count,
        }


def wait_next_raw_bgr(last_seq: int = 0, timeout_sec: float = 0.5):
    """等待序号大于 ``last_seq`` 的新画面，超时返回 None。"""
    t_end = time.time() + timeout_sec
    while time.time() < t_end:
        with _cond:
            if _frames and _frames[-1][0] > last_seq:
                seq, ts, jpeg = _frames[-1]
            else:
                remaining = max(0.0, t_end - time.time())
                _cond.wait(timeout=min(0.05, remaining))
                continue
        # 解码JPEG为BGR
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is not None:
            return RawFrame(seq=seq, captured_at=ts, bgr=bgr)
        # 解码失败，稍等重试
        time.sleep(0.01)
    return None


def wait_raw_bgr(timeout_sec: float = 0.5):
    """兼容旧调用；新循环应使用 wait_next_raw_bgr 并保存序号。"""
    last_seq = int(getattr(_consumer_state, "last_seq", 0))
    packet = wait_next_raw_bgr(last_seq=last_seq, timeout_sec=timeout_sec)
    if packet is None:
        return None
    _consumer_state.last_seq = packet.seq
    return packet.bgr

def send_vis_bgr(bgr, quality: int = 88):
    """被 YOLO/MediaPipe 脚本调用：把处理后画面推给前端 viewer"""
    if bgr is None:
        return
    
    # 直接编码，不做任何增强处理
    ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return
    with _sender_lock:
        cb = _sender_cb
    if cb:
        try:
            cb(enc.tobytes())
        except Exception:
            pass

def send_ui_final(text: str):
    """把一条UI文案作为 final answer 推给前端（线程安全回调）"""
    if not text:
        return
    with _ui_sender_lock:
        cb = _ui_sender_cb
    if cb:
        try:
            cb(str(text))
        except Exception:
            pass
