# CanMV K230 智能眼镜设备端

用 K230 替换 `compile/compile.ino` 的采集/播放设备层，继续使用现有 FastAPI 后端、模型、ASR 和网页。**不在 KPU 上重复部署后端模型，也不模拟不存在的 IMU。**

## 已适配硬件与边界

- 真机：01Studio CanMV，`os.uname().machine == 'k230_canmv_01studio'`；GC2093 CSI2。
- 固件：`v1.8-0-gc2d1f5c`，2026-07-03 构建。其他版本需重新核对媒体 API；不能直接套最新网站的无通道编号编码器。
- 2.4 GHz Wi-Fi；板上麦克风为右声道。扬声器需接实际音频输出设备，USB 连电脑不等于已经接好扬声器。
- 设备上传画面和麦克风到配置的后端；后端按原有配置调用云端 ASR/模型，可能产生费用。

| 功能 | 实现 |
|---|---|
| 摄像头 | GC2093 → YUV420SP → VENC 硬件 JPEG；每条 `/ws/camera` 二进制消息一张完整图 |
| 相机控制 | `SET:FRAMESIZE/QUALITY/FPS`，兼容 ESP32 七档及 K230 720p/1280×960/1536×864/1080p；默认1536×864、25fps；`STAT.send_fps` 只计完整发出帧 |
| 高清抓拍 | `SNAP:HQ`；`SNAP:BEGIN`、1920×1080 JPEG、`SNAP:END`；随后恢复流设置 |
| 麦克风 | 固件原生右mono输入，增益60、ANS开启，16kHz PCM16 LE、20ms/640B；保序队列，不采用视频latest替换策略 |
| 设备回放 | `/stream.wav` HTTP chunked/WAV，8kHz mono PCM16；板内转换至16kHz共享音频时钟 |
| 恢复 | Wi-Fi重试、相机/音频/回放独立重连；`RESTART/RESET` 重建音频会话，不重启板子 |
| 停止 | Ctrl-C/IDE停止，等待工作线程退出再释放媒体；有界缓冲，不积压旧画面 |
| IMU | 未实现：未检测到已连接的 ICM42688，不能把模拟数据当真实姿态 |

`RESET` 在这个客户端表示重新建立后端音频会话；原 ESP 固件没有该分支。高清抓拍需要短暂切换相机输出，期间视频会暂停，麦克风线程继续。

## 目录与运行

将本目录的 `main.py`、`config.py`、`camera.py`、`audio_io.py`、`device_protocol.py`、`ws_client.py`、`wav_stream.py` 放到 `/sdcard/`。

将 `secrets.example.py` 复制为板上的 `/sdcard/k230_secrets.py`，填入实际 Wi-Fi 和电脑局域网地址。真实文件已加入 Git 忽略规则，部署工具不会上传/覆盖它。端口默认8081；不要使用其他会话遗留的服务器地址。

主机从仓库根目录运行：

```powershell
.\.venv-run\Scripts\python.exe app_main.py
```

K230 当前没有外接喇叭，电脑播报是必要输出。相机连接携带 `?device=k230`，后端自动识别并选择 `both`（电脑和板端同时输出）；原ESP32仍兼容server/device。仅打开 `/stream.wav` 不改变目标。可用配置 API：

```text
POST /api/runtime-config
{"playback_target":"both","performance_profile":"k230_1_5k"}
```

不要让桌面模拟器同时占用唯一的 `/ws/camera` 入口。网页：[本机控制页](http://127.0.0.1:8081/)。

## 安全部署与回退

主机串口工具额外需要 `python -m pip install pyserial`。先枚举端口，不假定 COM9 永久属于 K230。以下 `COM_PORT` 替换为实测端口。

```powershell
python tools/k230_board.py --port COM_PORT info
python tools/k230_board.py --port COM_PORT backup --output .codex_tmp/k230/manual-backup /sdcard/main.py /sdcard/k230_secrets.py
python tools/k230_board.py --port COM_PORT deploy firmware/k230 --backup .codex_tmp/k230/backups
```

- 工具先 Ctrl-C 停止当前程序；`info` 也会暂停运行，不能当无侵入监控。
- deploy先备份所有待替换文件，分块上传到临时路径，逐文件读回校验，旧版改名保留，`main.py` 最后安装。不擦盘、不刷镜像。
- Git Bash 调 Windows Python 且参数含 `/sdcard/` 时，加 `MSYS_NO_PATHCONV=1`；否则MSYS会改坏板路径。
- 可先建隔离 `/sdcard/k230_probe` 并用 `--destination /sdcard/k230_probe` 测试，不修改离线启动入口。
- 回退：停止客户端，将备份中的旧程序恢复到原路径；私有配置同样从本地保密备份恢复。不要把备份目录加入Git。
- `/sdcard/main.py` 在复位后自启；REPL soft reboot也可能运行main。诊断前不要盲发Ctrl-D。

## 测试

```powershell
python -m unittest discover -s tests -p "test_k230*.py" -v
python tools/k230_protocol_probe.py
```

本地协议验收服务默认8082，无云端ASR，不录制媒体；每隔数秒发送低幅440Hz测试音，切换VGA/SVGA、请求高清抓拍、断开相机/回放、发送RESTART/RESET。临时让板的SERVER_PORT指向8082，再检查 `http://127.0.0.1:8082/report`。测试结束恢复8081并关闭验收服务。

不要以“open成功”代替测试音频采样速度；分别测录音块数、摄像头真实接收帧率、回放写入及连接数。自动验证无法替代有人现场听扬声器与口述唤醒词。

## 已验证的固件坑点

1. v1.8没有 `PyAudio.initialize()`，`MediaManager.init/deinit()` 不负责清资源。`link.destroy()` 必须显式调用一次，`del link` 不解绑。
2. 输入16k、输出8k虽然都能open，但共用codec时钟后输入50块要2秒，变成半速。统一16k后50块为1秒；网络8k音频在输出前2倍重采样。
3. nonblocking recv可能用空bytes表示无数据，先poll再recv；没有getsockopt/SO_ERROR，连接采用有界timeout再切nonblocking。
4. bytearray不支持del切片，array不支持step切片；部署base64解码要传bytes；rename不覆盖目标。
5. WS必须mask；以原生大整数异或替代Python字节循环，但正确保留分块相位与尾长。未发出的旧视频可丢，已经发送的帧不能被控制帧插断。
6. 回放静音可能5秒只来10ms，不能等待攒满一整大块再判连接正常。队列限制200ms，断线后不回放旧积压。

## 后端对应修复

- `asr_transport.QueuedRecognition` 用阻塞有界队列替代所装SDK的空输入忙循环和共享列表clear，避免抢占CPU及并发漏音频；不修改site-packages。
- `audio_ingress` 按640B重组PCM并记录音量、包间隔、丢帧；keepalive一次仅20ms，禁止600ms突发静音插进语句。
- 每个网页独立latest队列；原始JPEG直通，canvas使用真实像素；关闭WebSocket对JPEG的额外deflate。
- 本机 `POST /api/dev/audio-test` 播放半秒低幅测试音，不调用云端；`/api/asr-status` 显示输入电平、送流队列、静音和丢帧指标。
- 明确的“进入休眠”在本地执行，不送多模态模型解释。

实测与未覆盖项目见 [VALIDATION.md](VALIDATION.md) 及 [高清和语音专项验证](TUNING_VALIDATION.md)。
