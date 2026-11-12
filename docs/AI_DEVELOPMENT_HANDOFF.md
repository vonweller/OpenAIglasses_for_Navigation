# AI 开发交接：K230、后端、网络配置与排障

> 更新：2026-09-23；按提交 `c20839f` 的代码核实。面向下一位 AI 编程助手及人工维护者。
>
> 本文是操作与架构交接，不是实时运行证明。接手时重新检查 Git、进程、串口、IP、固件和接口状态，不能根据上一轮聊天断言服务仍在运行。真实 IP、Wi-Fi 密码、API Key、录音和私有配置不得写入 Git。

## 1. 接手先读与版本基线

建议顺序：

1. [AGENTS.md](../AGENTS.md)：仓库规则、安全与测试约束。
2. 本文，尤其第 4 节“电脑 IP / Wi-Fi 变更”。
3. [K230 部署说明](../firmware/k230/README.md)。
4. [高清与语音专项验证](../firmware/k230/TUNING_VALIDATION.md)、[播报节拍修复](PLAYBACK_PACING_FIX.md)。
5. [功能框架](../FUNCTION_FRAMEWORK.md)：语音指令和导航流程；旧图示或旧命令与源码不一致时以源码为准。

已经推送的基线：

| 提交 | 内容 |
|---|---|
| `a6b47bc` | K230 高清音视频、ASR 有序队列、电脑和设备双端播报；当时 66 项测试通过 |
| `c20839f` | 云端语音突发导致首段跳读的播放节拍修复；72 项测试通过 |

当时分支为 `codex/optimize-local-simulator-and-vision`。接手后先执行 `git status --short --branch`、`git log -5 --oneline`、`git remote -v`，不要假设当前分支、远程或未提交修改归属。不要将用户正在改的文件 reset 掉。

**尚未修复**：云端 `Connection error.` 的可靠诊断/恢复，以及报错后又显示“（空响应）”；见第 9 节。不要把播报修复提交描述成已解决云端连接问题。

## 2. 当前架构与不能改坏的边界

```text
K230 摄像头 ── JPEG /ws/camera ──┐
K230 麦克风 ── 16k PCM /ws_audio ├─ FastAPI 后端 ── 浏览器 /ws/viewer、/ws_ui
                               ├─ Paraformer ASR、Qwen-Omni、导航与视觉模型
                               └─ 8k PCM ── 电脑扬声器
                                         └─ /stream.wav ── K230 音频输出
```

- 根目录 `app_main.py` 只是启动器；业务实现是 `aiglasses/app_main.py`。
- `firmware/k230/` 是板端 MicroPython 源码，**不是板上文件的自动同步目录**。
- ESP32 原实现仍在 `compile/compile.ino`。K230 移植没有将后端模型搬到 KPU，也没有伪造 IMU。
- 板型实测：01Studio `k230_canmv_01studio`，GC2093 CSI2；固件 `v1.8-0-gc2d1f5c`，构建日期 2026-07-03。换镜像必须重新验证 API。
- 用户明确当前 K230 **没有外接喇叭**：电脑必须能播报。K230 相机连接带 `?device=k230`，后端识别后自动使用 `both`。不要再次改成仅设备输出后宣称播放正常。
- K230 默认 1536×864、目标 25fps。此前网页绘制约 24fps；这是特定环境实测，不是任何 Wi-Fi/场景下的保证。“1.5K”指这里的像素尺寸，不是 2560×1440。

### 关键代码入口

| 文件 | 职责 |
|---|---|
| `aiglasses/app_main.py` | 设备接入、状态/API、命令路由、云端回答启动、独立网页发送队列 |
| `aiglasses/omni_client.py` | Qwen-Omni HTTP 流式请求；不是 ASR 客户端 |
| `aiglasses/asr_transport.py` | `QueuedRecognition`：修复所装 SDK 空输入忙等及共享列表并发丢帧 |
| `aiglasses/audio_ingress.py` | PCM 整帧重组、保序送流、输入音量/丢帧诊断 |
| `aiglasses/audio_stream.py`、`playback_clock.py` | 双端回放、PCM 时钟调度、队列背压、中断与静音状态 |
| `aiglasses/performance.py`、`bridge_io.py` | 高清档位、latest frame、叠加图像与流水线指标 |
| `static/main.js`、`templates/index.html` | 网页显示、真实绘制帧率、设置；修改脚本后更新 HTML 缓存版本 |
| `firmware/k230/main.py`、`camera.py`、`audio_io.py` | 板端协调、硬件 JPEG、采音/重采样回放 |
| `tools/k230_board.py` | 串口备份、执行、上传；进入工具即可能中断板端程序 |

## 3. 配置到底存在哪里

| 配置 | 真正生效位置 | 注意事项 |
|---|---|---|
| 板卡 Wi-Fi、电脑地址、服务端口 | **板上 `/sdcard/k230_secrets.py`** | 电脑换 IP 后主要改这里；Git 中没有真实文件 |
| 板端公开默认参数/协议路径 | `firmware/k230/config.py` → 部署至 `/sdcard/config.py` | 本地修改后必须上传；私有 `SERVER_PORT` 会覆盖此默认端口 |
| 后端 API Key | 主机进程环境、根目录忽略的 `.env` | 代码会加载 `.env`；不要把内容贴进聊天或日志 |
| 档位、播放目标、模型路径等 | 主机忽略的 `runtime_config.json`，通过 `/api/runtime-config` 更新 | 持久配置可能覆盖启动环境；模型路径变更不等于已热加载模型 |
| 后端监听地址/端口 | 实际启动命令；默认 `0.0.0.0:8081` | `0.0.0.0` 是监听通配符，不是板端连接目标 |
| 网页推荐连接地址 | `/api/runtime-config` 的 `server_host/server_port` 等 | 是电脑网卡推导出的提示，**不会写到板上** |

私有配置格式，全部为占位符：

```python
WIFI_SSID = "YOUR_2_4_GHZ_WIFI"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"
SERVER_HOST = "YOUR_COMPUTER_LAN_IPV4"
SERVER_PORT = 8081
```

- `SERVER_HOST` 只填电脑可达的局域网 IPv4，不带 `http://`、路径或 `:端口`。
- 不要填写板卡自身 IP、路由器网关、VPN/虚拟网卡地址、`127.0.0.1` 或 `0.0.0.0`。只有电脑本机浏览器才通常用 `http://127.0.0.1:8081/`。
- 板卡 DHCP 地址变化，电脑后端通常无需跟着改：连接由板卡主动发起。
- `/api/runtime-config` 的 POST 不提供“下发板卡 Wi-Fi / IP”的功能；往里面传 `server_host` 不会改板上连接目标。
- 私有文件 import 后有缓存，换文件不等于正在运行的客户端已更新，成功上传后还要重新启动板端程序。

## 4. 电脑 IP / Wi-Fi 变更：完整操作流程

以下是**Windows PowerShell、仓库根目录**下的维护步骤。仅在实际需要换网络时执行；本文编写时没有改动任何板端配置。`COM_PORT`、`YOUR_COMPUTER_LAN_IPV4` 是占位符，不可直接照抄为真实值。

### 4.1 找到正确网卡与后端

```powershell
$py = (Resolve-Path '.\.venv-run\Scripts\python.exe').Path
Get-NetIPConfiguration
Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

选择能与板卡 Wi-Fi 网络互通的电脑网卡；电脑接有线、板卡接同一路由器 Wi-Fi 也可以，但访客网络/AP 隔离可能禁止访问。多网卡时不要默认选第一项，网页推荐地址也要核实。

- K230 连接 2.4GHz Wi-Fi；换热点时检查频段、SSID、密码。
- 优先在路由器为电脑网卡设置 DHCP 地址保留，减少 IP 反复变化；这是网络配置变更，须获得授权，不替用户擅自修改。
- 防火墙只对确认可信的网络/接口放行对应 TCP 端口和 Python 程序；**不要关闭整个防火墙或把服务直接暴露到公网**。当前配置/调试接口没有面向公网部署的完整认证设计。
- `ping` 通不等于 TCP 8081 通；应从同网络另一设备访问 `http://YOUR_COMPUTER_LAN_IPV4:8081/api/health`。响应 `OK` 只证明 HTTP 可达，不证明模型/云服务全部可用。

### 4.2 重新枚举串口并先备份

如果环境尚未装串口库，先安装到实际使用的虚拟环境：

```powershell
& $py -m pip install pyserial
& $py -m serial.tools.list_ports -v
```

核对新出现的 CanMV USB 串口，不能沿用历史 COM 编号。关闭占用该端口的 IDE/串口监视器。然后：

```powershell
$ComPort = Read-Host '输入已确认的板卡串口，例如 COM 后接编号'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backupDir = Join-Path '.codex_tmp\k230\network-backups' $stamp
& $py tools/k230_board.py --port $ComPort backup --output $backupDir /sdcard/k230_secrets.py
if ($LASTEXITCODE -ne 0) { throw '备份失败，停止操作，不覆盖或重启板卡。' }
$editFile = Join-Path $backupDir 'k230_secrets.edit.py'
Copy-Item (Join-Path $backupDir 'k230_secrets.py') $editFile
notepad $editFile
```

**这个 backup 命令会先 Ctrl-C 停止板端程序，结束后不会自动恢复。** 备份目录必须不存在；旧配置不存在时不要强行继续，首次配置可走 4.5。

在本地编辑器中修改并保存：

- 仅电脑 IP 变化：只改 `SERVER_HOST`，保留 Wi-Fi 和端口。
- Wi-Fi 变化：改 `WIFI_SSID`、`WIFI_PASSWORD`，并重新确认该网络上的电脑地址。
- 服务端口变化：改 `SERVER_PORT`，电脑端也要按第 5 节更换监听端口。

不要把真实值写进 PowerShell 命令行、聊天或版本化文档。完成保存后再执行下一段。

### 4.3 只上传私有配置，不重复刷写固件

现有 `deploy` **排除** `k230_secrets.py` 和 `secrets.example.py`，所以不能用普通 deploy 来更新网络。下面通过现有 `Board` API 上传一份已经编辑好的文件，额外备份写入前版本并核对最终路径。

```powershell
@'
import ast
from datetime import datetime
import ipaddress
from pathlib import Path
import re
import sys

stage = "validate-local-file"
try:
    com, filename = sys.argv[1:3]
    if not re.fullmatch(r"COM[1-9][0-9]*", com):
        raise ValueError()
    local = Path(filename).resolve()
    safe_root = (Path.cwd() / ".codex_tmp").resolve()
    if safe_root not in local.parents:
        raise ValueError()
    payload = local.read_bytes()
    tree = ast.parse(payload.decode("utf-8-sig"), filename="<private-config>")
    values = {}
    for node in tree.body:
        if (not isinstance(node, ast.Assign) or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)):
            raise ValueError()
        key = node.targets[0].id
        if key in values:
            raise ValueError()
        values[key] = ast.literal_eval(node.value)
    for key in ("WIFI_SSID", "WIFI_PASSWORD", "SERVER_HOST"):
        if not isinstance(values.get(key), str) or not values[key]:
            raise ValueError()
    address = ipaddress.IPv4Address(values["SERVER_HOST"])
    if (address.is_loopback or address.is_unspecified or address.is_multicast
            or address.is_link_local or address.is_reserved):
        raise ValueError()
    port = values.get("SERVER_PORT")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError()
    payload = payload.decode("utf-8-sig").encode("utf-8")
    from tools.k230_board import Board
    stage = "stop-and-backup-board"
    with Board(com, timeout=30) as board:
        remote = "/sdcard/k230_secrets.py"
        original = board.read_file(remote)
        backup = local.parent / ("before-write-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".py")
        with backup.open("xb") as stream:
            stream.write(original)
        if backup.read_bytes() != original:
            raise RuntimeError()
        stage = "upload-and-verify"
        board.write_file(remote, payload)
        if board.read_file(remote) != payload:
            raise RuntimeError()
    print("OK: private configuration verified; board remains stopped until restart.")
except Exception:
    print("FAILED at " + stage + "; values suppressed. Keep the backup and do not reset blindly.")
    raise SystemExit(1)
'@ | & $py -B - $ComPort $editFile
```

说明：

- 在**电脑**执行上面脚本，`Board.execute()` 内的代码才在板上执行。不要把此脚本交给 `k230_board.py exec`；CLI `exec` 会把整份脚本发给板端 MicroPython。
- 示例只支持顶层字面量赋值；遇到动态生成配置、重复赋值会拒绝，不执行私有 Python 文件。IP 格式校验不代表此地址一定属于目标电脑。
- 备份与上传文件都含私密信息，`.codex_tmp/` 只是不进 Git，不是加密保险箱。
- `write_file` 先校验临时文件，再将旧文件改名为 `.previous-时间戳`，最后安装新文件。它不是整个部署的原子事务；失败时可能留下 `.upload-*` 或缺少正式文件。**先检查/恢复备份再启动，不要把失败当成功。**
- 此示例不自动复位；不会打印配置值或异常里的源代码。

### 4.4 重新启动并验收

1. 启动电脑后端并确认健康接口。
2. 上传成功后，在适合暂停硬件的时机使用板卡复位键/IDE 重启，让 `/sdcard/main.py` 重新加载私有配置。
3. 复位可能使串口重新枚举；需要后续调试时重新确认 COM。
4. 检查 `/api/device-status`：`device_type=k230`、相机与麦克风连接成立、最近帧年龄小、`playback_target=both`。
5. 检查 `/api/asr-status` 和真实语音；只看到 HTTP `OK` 或 `audio_connected=true` 不代表 ASR 已启动。
6. 检查画面、电脑测试音及唤醒/休眠。板卡无喇叭时不要要求板上可听到声音。

退出 `with Board(...)` 只关闭串口、回到 REPL，**不会恢复 main.py**。不要使用空 `Board.execute("")` 当探活：空 raw REPL Ctrl-D 可能 soft reboot 并运行已有 main。将 `machine.reset()` 放进 `execute()` 后也不应期待正常串口应答；断连不等于复位失败。

### 4.5 首次配置或换了一块空板

只有确认原私有文件不存在、且已有正确固件时，才从 [secrets.example.py](../firmware/k230/secrets.example.py) 制作私有文件并上传 `/sdcard/k230_secrets.py`；使用 CanMV 文件管理器或经过同等校验的 `Board.write_file`。

不要为了“只换 IP”格式化 SD 卡或重刷镜像。首次配置与“保留旧 Wi-Fi，仅改 host”是两件不同的事。

## 5. 更换服务端口、开发电脑与启动方式

默认推荐8081。**只换电脑 IP 不需要改监听 `0.0.0.0`**；板端只要改新的电脑地址。

标准启动，从仓库根目录：

```powershell
& $py app_main.py
```

需要临时或新端口时，用显式启动参数比改多处常量更清楚：

```powershell
$TcpPort = [int](Read-Host '输入已确定的后端 TCP 端口')
& $py -m uvicorn aiglasses.app_main:app --host 0.0.0.0 --port $TcpPort --workers 1 --ws-per-message-deflate false
```

- 板端 `SERVER_PORT`、浏览器地址、防火墙规则也要一致；用此命令时不要再开第二份 `setup.bat` 服务。
- 不要用多个 workers：设备连接、导航状态与模型对象是本进程状态。
- 保留 `--ws-per-message-deflate false`，避免已经压缩的 JPEG 再被压缩。
- 根 `app_main.py` 与 `python -m aiglasses.app_main` 的入口各自写有默认端口；修改一个不会自动修改另一个。
- 若坚持修改 `setup.bat` 默认端口，须同时核对它的 `PORT` 与实际 Python 启动入口；batch 的 `PORT` 只用于探测/等待/显示，**不是传给 Python 的启动参数**。没有 `setup.bat --port` 功能。
- `tools/k230_board.py --port` 指 COM 串口，不是这个 TCP 端口。

新电脑准备：同一 Python 3.9–3.11 虚拟环境（推荐3.11），安装 `requirements.txt` 和额外 `pyserial`，准备被忽略的模型文件，使用安全方式转移 `.env` 和必要本地配置。Git clone 不会带来密钥、Wi-Fi 配置、模型、`.venv-run` 或旧板备份。

`setup.bat --check` 用于检查；普通 `setup.bat` 会安装/下载并可能重启服务，不能当只读检查。`tools/prepare_models.py` 会联网/写文件。启动/导入完整后端会加载模型，不是轻量语法校验。

## 6. 更新板端代码与回退

网络配置更新与固件代码更新分开进行。需要更新代码时：

```powershell
& $py tools/k230_board.py --port $ComPort deploy firmware/k230 --backup .codex_tmp/k230/deploy-backups
```

- `deploy` 仅处理源目录顶层 `*.py`，不会上传 README；不会上传也不会备份被排除的 secrets，私有文件应另行备份。
- 先备份旧目标，逐文件临时上传并读回校验，`main.py` 最后安装；不删除无关板载文件，不创建不存在的远端目录。
- 隔离试验可先建立 `/sdcard/k230_probe`，再指定 `--destination /sdcard/k230_probe`。不要把残留测试目录/模块缓存当成最终 `/sdcard/` 版本。
- Git Bash 调 Windows Python且参数含 `/sdcard/...` 时，命令前加 `MSYS_NO_PATHCONV=1`；本文PowerShell命令不需要该设置。
- 回退后端：确认工作区/用户改动后使用审查过的 revert 或独立工作树，不直接 `reset --hard`。回退板端：从对应备份恢复程序，最后恢复入口；不要将另一网络的旧 secrets 无条件覆盖当前配置。
- 原诊断备份可能在维护电脑忽略的 `.codex_tmp/k230/`；别假定新机器上存在，交接时另行安全转移。

## 7. 协议与容易再次踩的坑

- 相机：`/ws/camera?device=k230`，每条二进制消息一个完整 JPEG；同一时刻只允许一个设备相机，不要让桌面模拟器占住它。
- 麦克风：`/ws_audio?device=k230`，`START` → `OK:STARTED` 后上传16kHz mono PCM16LE，每20ms为640B。音频必须保序，不能复用视频latest覆盖策略。
- 播放：`/stream.wav` 是8kHz mono PCM16 WAV + HTTP流。板内输入/输出都用16kHz共享codec时钟，网络8k音频重采样后播放；直接16k输入+8k输出会把采音减半。
- 语音生成可以比实时快数倍。`PlaybackClock` 做背压，正常语音不能因4.8秒队列满就丢开头。20.32秒云端样本已验证约20.34秒播放、两端完整保序。
- `MediaManager.init/deinit` 在实测v1.8已弃用，`PyAudio.initialize` 不存在。媒体绑定须 `link.destroy()` 一次，删除Python引用不等于解绑。
- `Sensor.set_framerate` 在此版本无实现；当前板端使用对应版本的通道帧率接口。不要将新文档API不经验证套给旧镜像。
- Windows计时器、SDK忙等、网页慢消费者都会拖帧；看实际接收/绘制fps，不只看入队数。
- 板端 `RESET` 协议是音频会话恢复，不是板卡复位。休眠是后端交互门控，麦克风仍须接收唤醒词。

这些约束来自配套源码和真机验证。用户级 `canmv-k230` 技能曾补充v1.8参考，但它**不在此Git仓库中**；换电脑不能依赖该绝对用户目录必然存在。本仓库文档应足以理解已实现行为，技能缺失时核对 [01Studio文档](https://wiki.01studio.cc/docs/canmv_k230/intro/canmv_k230) 和实物固件版本。

## 8. 分层验收与常用检查

| 检查 | 说明 |
|---|---|
| `GET /api/health` | HTTP返回OK；仅服务可达 |
| `GET /api/device-status` | 相机、音频、模式、档位、双端播报及pipeline状态 |
| `GET /api/asr-status` | ASR状态、输入RMS/dBFS、包间隔、队列丢帧、补静音 |
| `POST /api/dev/audio-test` | 本机半秒测试音，无云费用；会实际播放声音 |
| `GET/POST /api/runtime-config` | 配置查询/更改；响应可能含本地地址、路径、掩码密钥，勿原样贴到公开报告 |
| `GET /`、`/ws/viewer`、`/ws_ui` | 网页、画面、识别/状态；`/ws`是IMU订阅，不是设备上行入口 |

PowerShell本机测试：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8081/api/health'
Invoke-RestMethod 'http://127.0.0.1:8081/api/device-status' |
    Select-Object device_type, camera_connected, audio_connected, asr_streaming, playback_target
Invoke-RestMethod -Method Post 'http://127.0.0.1:8081/api/dev/audio-test'
& $py -m unittest discover -s tests -v
```

当前基线为72项测试通过。文档改动不代表重新跑过真机；代码改动需补实际运行证明。

- 先测网络，再测JPEG/采音，再测云ASR，再测对话，最后测试导航/找物；不要看到云错误就重刷相机。
- 本地 `tools/k230_protocol_probe.py` 默认8082，不调用云ASR；会发送测试音和控制/断线请求，**不是只读服务**。临时修改板目标后测试完恢复8081，并关闭额外进程。
- 真人/固定测试音都要记录音量、距离、背景声；SDK会话启动成功不等于识别率达标。当前不是完整声学回声消除，播报时麦克风保护可能屏蔽插话。
- 实测软件复位自启，物理拔电再上电、长时数小时和各种网络环境不应冒称已覆盖。

## 9. 已知未解决事项：云端连接错误与重复空响应

2026-09-23曾出现网页“你好”识别成功，随后 `Connection error.` 和“（空响应）”两条消息。

**已确认：**

- 失败阶段是后端请求Qwen-Omni，不等同于K230断网/ASR听错，也不同于已修复的播放首段丢失。
- 诊断曾观察到失效本机代理造成连接拒绝；另一次检查时系统代理与直连均可达，带认证的模型列表返回200并含目标模型。因此不能将所有Connection error永久归因为代理或密钥。
- `aiglasses/omni_client.py` 使用默认OpenAI/httpx客户端；系统代理可能在没有显式环境变量时参与路由。测试进程直连成功不会修改已运行后端的客户端。
- `aiglasses/app_main.py::start_ai_with_text` 的异常分支发错误，`finally` 仍无条件发送最终文本，没文本时又发“（空响应）”。这是明确的展示/收尾缺陷。

**尚未实现：**

- 记录脱敏异常类型、底层cause、超时阶段及实际代理选择；目前用户面板只见泛化错误。
- 有界、可取消的连接恢复策略；已开始播放/输出后不得盲目重试并重复播报。
- 错误/取消与正常完成分流，避免重复“空响应”。

排障不先换密钥、不关闭TLS校验、不修改全局代理。先按后端相同环境分别核对DNS/TCP/TLS/代理；无认证查询返回401说明到达接口，不能据此判断密钥；带认证模型列表200也不能证明一轮流式语音生成已经成功。云端调用可能产生费用，使用合成测试语句，不擅自上传用户录音和相机图像。

## 10. 下一位 AI 的交付要求

1. 明确此次是文档/诊断/代码/板端配置哪一类任务；不要仅见截图就擅改系统。
2. 记录开始的Git状态；备份后再改板，保留私有配置，不沿用旧IP/COM。
3. 只提交源码、脱敏示例、测试和验证文档。`.env`、`runtime_config.json`、`k230_secrets.py`、`.codex_tmp/`、录音日志和模型不得提交。
4. `.previous-*`、`.upload-*`等私有文件若复制到电脑，放忽略目录；仅名为`k230_secrets.py`的忽略规则不覆盖任意重命名副本。
5. 给出实际修改、测试结果、部署/重启结果、未验证项和已知问题。不能用旧测试记录冒充本轮实测。
6. 用户要求推送时核对分支、远程、暂存差异和隐私后commit/push，不force、不夹带无关配置。环境地址变化只进本地配置，不进此次Git提交。

**可复制给新AI的开场指令：**

> 请先读AGENTS.md与docs/AI_DEVELOPMENT_HANDOFF.md，核对当前Git状态、后端进程、实际COM和网络，不输出私有配置。K230连接目标在板上/sdcard/k230_secrets.py，普通deploy不上传该文件。默认1536×864/25fps、右mono/ANS/gain60、电脑和设备both播放。c20839f已修复播报首段跳读；Connection error及错误后重复空响应仍待处理。先根据本次请求定位，不盲刷板、不重置无关配置，完成后分清主机测试和真机验证。
