# Repository Guidelines

## Project Structure & Module Organization

The root `app_main.py` is the compatibility launcher for the FastAPI backend in `aiglasses/`. Core navigation state and workflows live in `aiglasses/navigation_master.py`, `workflow_blindpath.py`, and `workflow_crossstreet.py`; vision, audio, ASR, and model clients are split into focused sibling modules. Browser UI files are under `templates/` and `static/`. ESP32 firmware is in `compile/`, while `tools/` contains the desktop device simulator and model preparation utility. Runtime media belongs in `voice/`, `music/`, and ignored `recordings/`; downloaded weights belong in ignored `model/`.

## Build, Test, and Development Commands

- `python -m venv .venv && .venv\Scripts\activate` — create and activate a Windows environment.
- `pip install -r requirements.txt` — install Python 3.9–3.11 dependencies.
- `python tools/prepare_models.py` — download and validate required model assets.
- `python app_main.py` — start the backend and web UI on port `8081`.
- `python tools/desktop_esp32_simulator.py --host 127.0.0.1 --port 8081` — exercise camera/audio flows without hardware.
- `setup.bat` or `bash setup.sh` — perform guided setup; the Windows script also launches the service.
- `docker compose up --build` — build and run the containerized service with GPU configuration.

## Coding Style & Naming Conventions

Use four-space indentation and UTF-8 source files. Follow Python conventions: `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Keep hardware, workflow, and transport concerns in separate modules. Add type hints to new public functions and concise docstrings where behavior is not obvious. No formatter is enforced; avoid unrelated mass formatting.

## Testing Guidelines

No automated tests are currently checked in. For backend changes, at minimum start the service, open the web UI, and run the desktop simulator. Verify affected voice-command flows using `FUNCTION_FRAMEWORK.md`. For pure logic, add `pytest` tests under `tests/` named `test_<module>.py`; mock model, network, audio, and hardware dependencies.

## Commit & Pull Request Guidelines

History uses short, outcome-focused subjects in Chinese or English (for example, `优化性能问题` or `Add desktop simulator`). Keep each commit scoped to one change. Pull requests should describe behavior changes, setup/config impacts, and manual verification. Link relevant issues and include screenshots for UI changes or logs for device/runtime fixes.

## Security & Configuration

Never commit `.env`, API keys, Wi-Fi credentials, local IPs, recordings, logs, runtime configuration, or large model binaries. Use environment variables and sanitized examples instead.
