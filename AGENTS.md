# Repository Guidelines

## Project Structure & Module Organization

This is a voice-controlled assistive-glasses system for visually impaired users. The root `app_main.py` launches the FastAPI backend in `aiglasses/`; it is not the backend implementation. Navigation lives in `aiglasses/navigation_master.py`, `workflow_blindpath.py`, and `workflow_crossstreet.py`; ASR, vision, audio, and model clients remain backend responsibilities. Use `aiglasses/paths.py` for host-side paths. Browser assets are in `templates/` and `static/`.

`compile/compile.ino` is the XIAO ESP32S3 Sense firmware; `firmware/k230/` is the CanMV v1.8 device client. Read its `README.md` and `VALIDATION.md` before hardware edits. `tools/` holds the desktop device simulator and model preparation utility; `tests/` contains `unittest` tests. Runtime media uses `voice/`, `music/`, and ignored `recordings/`; downloaded weights use ignored `model/` and root MobileCLIP files. Read `FUNCTION_FRAMEWORK.md` before changing voice/navigation flows, but verify protocol details against code: some documentation is stale.

## Build, Test, and Development Commands

Run from the repository root using one Python 3.9–3.11 environment (prefer 3.11). Windows `setup.bat` uses `.venv-run`; in Git Bash use `./.venv-run/Scripts/python.exe` in place of `python` when using that environment.

- `python -m pip install -r requirements.txt` — install the pinned dependencies; preserve the Torch/Ultralytics/NumPy compatibility constraints.
- `python tools/prepare_models.py` — download/copy model assets; this writes files and may access the network. `AIGLASS_MODEL_SNAPSHOT` selects a local snapshot.
- `python app_main.py` or `python -m aiglasses.app_main` — serve the backend/UI on `8081`. Do not run `python aiglasses/app_main.py`; its imports are package-relative. Importing the backend loads models, so it is not a lightweight smoke check.
- `python tools/desktop_esp32_simulator.py --host 127.0.0.1 --port 8081` — device simulator. Add `--synthetic --headless --no-audio --no-playback` for camera-only testing without physical A/V devices. Microphone capture is off by default; explicitly enable it for voice tests. `--self-test` requires a running backend and probes devices.
- `setup.bat --check` (CMD) — inspect setup. Normal `setup.bat` installs dependencies, prepares models, and may restart the backend; do not use it just to inspect the project. Linux `setup.sh` and Docker dependency pins lag behind `requirements.txt`; reconcile them before relying on those launch paths.

## Device Protocol Compatibility

- `/ws/camera`: one binary message per complete raw JPEG. Preserve `SET:FRAMESIZE`, `SET:QUALITY`, `SET:FPS` controls and `STAT:` replies; prefer the latest frame over accumulating stale video.
- `/ws_audio`: send text `START` before raw PCM16 little-endian, 16 kHz mono, normally 20 ms / 640 bytes per block. Preserve `RESTART` handling and independent audio/camera reconnection. Do not attach WAV headers to microphone data.
- `/stream.wav`: HTTP playback is WAV containing 8 kHz mono PCM16; handle WAV headers and HTTP chunked transfer. ESP32 firmware has `SPEAKER_ENABLED=0`; the K230 client enables playback. Backend code defaults to `server`; K230 identification selects `both` (computer and device). `device` remains normalized to `esp32` for compatibility.
- Playback-time microphone suppression is backend substitution of equal-length zero PCM, not a `MIC_MUTE` device command. Wake/sleep gating is also backend-side: ASR continues while asleep. Do not stop uploading audio/video just because interaction is asleep.
- `/ws_ui` carries UI text/status; `/ws` is the IMU subscription, despite the older diagram in `FUNCTION_FRAMEWORK.md`.

## CanMV K230 Device

- `firmware/k230/` implements hardware JPEG, microphone upload, WAV playback, controls and reconnects for v1.8-c2d1f5c. IMU requires real external hardware and is not emulated. Keep the backend, UI, and ESP32 baseline working; isolate K230-specific code from host CPython and Arduino code. Do not move ASR/model inference/navigation onto K230 merely because it has a KPU.
- Load the `canmv-k230` skill before K230 work. Use the [01Studio documentation](https://wiki.01studio.cc/docs/canmv_k230/intro/canmv_k230) and matching firmware examples to resolve gaps; never assume ESP32, OpenMV, or desktop Python APIs apply. The user permits supplementing the skill with verified findings and pitfalls; record the source and applicable firmware version.
- The user has connected a K230 for hands-on debugging. Re-detect its exact variant, firmware, serial/storage interfaces, camera, and audio wiring instead of assuming a COM port or drive letter. Read/back up existing board files before replacement; do not erase/reflash storage merely because the board is connected.
- Board files use `/sdcard/`, with `/sdcard/main.py` for offline startup. On verified v1.8, `PyAudio.initialize` is absent and `MediaManager.init/deinit` are deprecated: explicitly `link.destroy()` once. Input/output share a clock; use 16 kHz for both and resample 8 kHz playback. Verify fixed right-channel capture. Preserve interruptible loops via `os.exitpoint()` and bounded buffering, and measure latency/frame rate rather than assuming higher resolution or more threads is faster.
- K230 defaults to 1536×864/25fps, with 720p/1280×960/1080p alternatives; see `firmware/k230/TUNING_VALIDATION.md` for measured limits. Audio uses native right mono input, ANS and gain60; preserve ordered PCM, never reuse the video latest-frame replacement policy.
- Validate JPEG upload, microphone ASR, speech playback, wake/sleep, playback suppression, camera controls, disconnect/reconnect, and offline boot on the actual board. Report hardware-unverified behavior separately from host tests.

## Coding Style & Naming Conventions

Use four-space indentation and UTF-8 source files. Follow Python conventions: `snake_case` for functions/modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Keep hardware, workflow, and transport concerns separate. Add type hints to new host-side public functions; keep board code compatible with its actual CanMV runtime. No lint, formatter, or typecheck command is configured; avoid unrelated mass formatting.

## Testing Guidelines

- Full suite: `python -m unittest discover -s tests -v`. Follow the existing `unittest` style in `tests/test_<module>.py`; mock model, network, audio, and hardware dependencies.
- K230 tests: `python -m unittest discover -s tests -p "test_k230*.py" -v`. `tools/k230_protocol_probe.py` exercises hardware without cloud ASR; `tools/k230_board.py` needs `pyserial` and interrupts the board before backup/deploy. In Git Bash set `MSYS_NO_PATHCONV=1` for `/sdcard/...` arguments.
- Focused image transport tests: `python -m unittest discover -s tests -p test_bridge_io.py -v` (OpenCV/NumPy required).
- Standard-library-only smoke test: `python -m unittest discover -s tests -p test_performance.py -k test_pipeline_metrics_snapshot -v`.
- The full suite includes heavier ML imports. Keep audio tests isolated from real speakers and cloud calls. Test `asr_transport.QueuedRecognition` without network: the installed SDK's legacy input generator busy-spins and can lose concurrent list additions, affecting both ASR and video FPS.
- For backend behavior changes, also start the service, open the web UI, and run the simulator. Verify affected voice-command flows with `FUNCTION_FRAMEWORK.md`; host tests do not verify K230 hardware.

## Commit & Pull Request Guidelines

History uses short, outcome-focused subjects in Chinese or English (for example, `优化性能问题` or `Add desktop simulator`). Keep each commit scoped to one change. Pull requests should describe behavior changes, setup/config impacts, and manual verification. Link relevant issues and include screenshots for UI changes or logs for device/runtime fixes.

## Security & Configuration

Never commit `.env`, API keys, Wi-Fi credentials, local IPs, recordings, logs, `runtime_config.json`, or large model binaries. Existing firmware/configuration can contain private connection values; do not copy them into docs or diagnostic output. Use host environment variables and ignored board-local configuration with sanitized examples.
