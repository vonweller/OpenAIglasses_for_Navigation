@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "APP_NAME=OpenAI Glasses Navigation"
set "PORT=8081"
set "VENV_DIR=.venv-run"
set "FORCE_INSTALL=0"
set "CHECK_ONLY=0"
set "SKIP_MODELS=0"
set "NO_PAUSE=0"
set "KILL_PORT=0"
set "PYTHONNOUSERSITE=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--check" set "CHECK_ONLY=1"
if /I "%~1"=="--reinstall" set "FORCE_INSTALL=1"
if /I "%~1"=="--no-models" set "SKIP_MODELS=1"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if /I "%~1"=="--kill-port" set "KILL_PORT=1"
shift
goto parse_args

:args_done
echo ============================================================
echo   %APP_NAME% - one click setup and start
echo ============================================================
echo Project: %CD%
echo.

call :find_boot_python
if errorlevel 1 goto failed

call :detect_gpu
call :get_local_ip

if "%CHECK_ONLY%"=="1" (
    echo.
    echo [OK] Check finished. No install or startup was performed.
    echo      Local UI:     http://127.0.0.1:%PORT%/
    echo      LAN UI:       http://%LOCAL_IP%:%PORT%/
    echo      Camera WS:    ws://%LOCAL_IP%:%PORT%/ws/camera
    echo      Audio WS:     ws://%LOCAL_IP%:%PORT%/ws_audio
    exit /b 0
)

call :ensure_venv
if errorlevel 1 goto failed

call :install_dependencies_if_needed
if errorlevel 1 goto failed

call :ensure_runtime_files
if errorlevel 1 goto failed

if "%SKIP_MODELS%"=="0" (
    call :prepare_models
) else (
    echo.
    echo [SKIP] Model preparation was skipped by --no-models.
)

call :start_backend
if errorlevel 1 goto failed

echo.
echo ============================================================
echo   Ready
echo ============================================================
echo Local UI:     http://127.0.0.1:%PORT%/
echo LAN UI:       http://%LOCAL_IP%:%PORT%/
echo Camera WS:    ws://%LOCAL_IP%:%PORT%/ws/camera
echo Audio WS:     ws://%LOCAL_IP%:%PORT%/ws_audio
echo IMU WS:       ws://%LOCAL_IP%:%PORT%/ws
echo Logs:         %CD%\logs\backend.stdout.log
echo Errors:       %CD%\logs\backend.stderr.log
echo.
echo Optional commands:
echo   setup.bat --check       Environment check only
echo   setup.bat --reinstall   Force dependency reinstall
echo   setup.bat --no-models   Skip model download/check
echo   setup.bat --no-pause    Exit without waiting for a key
echo   setup.bat --kill-port   Kill non-backend process using port %PORT%
echo.
if "%NO_PAUSE%"=="0" pause
exit /b 0

:failed
echo.
echo ============================================================
echo   Setup failed
echo ============================================================
echo Check the messages above. If backend startup failed, also inspect:
echo   %CD%\logs\backend.stderr.log
echo.
if "%NO_PAUSE%"=="0" pause
exit /b 1

:find_boot_python
echo [1/7] Detecting Python 3.9-3.11...
set "BOOT_PY="

py -3.11 -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 set "BOOT_PY=py -3.11"

if not defined BOOT_PY (
    py -3.10 -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 set "BOOT_PY=py -3.10"
)

if not defined BOOT_PY (
    py -3.9 -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 set "BOOT_PY=py -3.9"
)

if not defined BOOT_PY (
    python -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 set "BOOT_PY=python"
)

if not defined BOOT_PY if "%CHECK_ONLY%"=="0" (
    where winget >nul 2>nul
    if not errorlevel 1 (
        echo [RUN] Python 3.9-3.11 was not found. Installing Python 3.10 via winget...
        winget install -e --id Python.Python.3.10 --scope user --accept-package-agreements --accept-source-agreements
        if not errorlevel 1 (
            py -3.10 -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>nul
            if not errorlevel 1 set "BOOT_PY=py -3.10"
            if not defined BOOT_PY (
                python -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,11) else 1)" >nul 2>nul
                if not errorlevel 1 set "BOOT_PY=python"
            )
        )
    )
)

if not defined BOOT_PY (
    echo [ERROR] Python 3.9, 3.10, or 3.11 was not found.
    echo         Install Python from https://www.python.org/downloads/ or run this script on Windows with winget available.
    exit /b 1
)

echo [OK] Python bootstrap command: %BOOT_PY%
%BOOT_PY% --version
exit /b 0

:detect_gpu
echo.
echo [2/7] Detecting NVIDIA GPU...
set "HAS_GPU=0"
nvidia-smi >nul 2>nul
if not errorlevel 1 (
    set "HAS_GPU=1"
    echo [OK] NVIDIA GPU detected.
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
) else (
    echo [INFO] NVIDIA GPU was not detected. CPU PyTorch will be used.
)
exit /b 0

:get_local_ip
set "LOCAL_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ip=(Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -and $_.IPv4Address} | Select-Object -First 1 -ExpandProperty IPv4Address).IPAddress; if(-not $ip){$ip=(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown'} | Select-Object -First 1 -ExpandProperty IPAddress)}; if($ip){$ip}"`) do set "LOCAL_IP=%%I"
if not defined LOCAL_IP set "LOCAL_IP=127.0.0.1"
echo [OK] LAN IP: %LOCAL_IP%
exit /b 0

:ensure_venv
echo.
echo [3/7] Preparing virtual environment...
set "PY=%CD%\%VENV_DIR%\Scripts\python.exe"

if exist "%PY%" (
    echo [OK] Reusing %VENV_DIR%.
) else (
    echo [RUN] Creating %VENV_DIR%...
    %BOOT_PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        exit /b 1
    )
)

"%PY%" --version
"%PY%" -c "import sys; print('[OK] venv:', sys.prefix); raise SystemExit(1 if sys.prefix == sys.base_prefix else 0)"
if errorlevel 1 (
    echo [ERROR] Python virtual environment is not active/correct.
    exit /b 1
)
exit /b 0

:install_dependencies_if_needed
echo.
echo [4/7] Checking Python dependencies...
if "%FORCE_INSTALL%"=="0" (
    call :check_runtime_deps
    if not errorlevel 1 (
        if "%HAS_GPU%"=="1" (
            "%PY%" -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" >nul 2>nul
            if errorlevel 1 (
                echo [INFO] Dependencies exist, but CUDA PyTorch is not active. Reinstalling torch.
                call :install_torch
                if errorlevel 1 exit /b 1
            ) else (
                echo [OK] Required Python dependencies are already available.
            )
        ) else (
            echo [OK] Required Python dependencies are already available.
        )
        exit /b 0
    )
)

echo [RUN] Installing dependencies. This can take several minutes...
"%PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1

call :install_torch
if errorlevel 1 exit /b 1

set "REQ_FILTERED=%TEMP%\aiglass_requirements_%RANDOM%.txt"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath 'requirements.txt' | Where-Object {$_ -notmatch '^\s*(torch|torchvision|pyaudio)\b'} | Set-Content -LiteralPath $env:REQ_FILTERED -Encoding ASCII"
if errorlevel 1 (
    echo [ERROR] Failed to prepare filtered requirements.
    exit /b 1
)

"%PY%" -m pip install -r "%REQ_FILTERED%"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    exit /b 1
)

echo [RUN] Enforcing NumPy 1.x ABI for OpenCV/MediaPipe compatibility...
"%PY%" -m pip install --force-reinstall --no-deps numpy==1.24.3
if errorlevel 1 exit /b 1

echo [RUN] Installing optional PyAudio...
"%PY%" -m pip install pyaudio==0.2.14
if errorlevel 1 (
    echo [WARN] PyAudio installation failed. ESP32 hardware streaming can still run.
)

call :check_runtime_deps
if errorlevel 1 (
    echo [ERROR] Dependency verification failed.
    exit /b 1
)

echo [OK] Dependencies are ready.
exit /b 0

:install_torch
if "%HAS_GPU%"=="1" (
    echo [RUN] Installing PyTorch 2.5.1 CUDA 12.1 wheel...
    "%PY%" -m pip install --upgrade --force-reinstall torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121
) else (
    echo [RUN] Installing PyTorch 2.5.1 CPU/default wheel...
    "%PY%" -m pip install --upgrade torch==2.5.1 torchvision==0.20.1
)
if errorlevel 1 (
    echo [ERROR] PyTorch installation failed.
    exit /b 1
)
exit /b 0

:check_runtime_deps
"%PY%" -c "import inspect, sys, fastapi, uvicorn, cv2, numpy, PIL, ultralytics, torch, mediapipe, dashscope, openai, dotenv, modelscope, clip, lap; from openai import OpenAI; c=OpenAI(api_key='dependency-check', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1'); sig=str(inspect.signature(c.chat.completions.create)); bad=(sys.prefix == sys.base_prefix or not (ultralytics.__version__ == '8.4.88') or 'modalities' not in sig or 'audio' not in sig); raise SystemExit(1 if bad else 0)" >nul 2>nul
exit /b %ERRORLEVEL%

:ensure_runtime_files
echo.
echo [5/7] Preparing runtime files...
if not exist "logs" mkdir "logs"
if not exist "recordings" mkdir "recordings"
if not exist "model" mkdir "model"
if not exist "music" mkdir "music"
if not exist "voice" mkdir "voice"

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    ) else (
        > ".env" echo DASHSCOPE_API_KEY=
    )
    echo [WARN] Created .env. Fill DASHSCOPE_API_KEY for ASR/Qwen features.
) else (
    echo [OK] .env exists.
)

findstr /R /C:"^DASHSCOPE_API_KEY=." ".env" >nul 2>nul
if errorlevel 1 echo [WARN] DASHSCOPE_API_KEY looks empty. Backend starts, but online AI/ASR may fail until configured.
exit /b 0

:prepare_models
echo.
echo [6/7] Preparing model files...
"%PY%" "tools\prepare_models.py"
if errorlevel 1 (
    echo [WARN] Model preparation did not fully complete. Existing local models will still be used if present.
)

set "MISSING_MODEL=0"
for %%F in (
    "model\yolo-seg.pt"
    "model\yoloe-11l-seg.pt"
    "model\yoloe-26s-seg.pt"
    "model\shoppingbest5.pt"
    "model\trafficlight.pt"
    "model\hand_landmarker.task"
    "mobileclip_blt.ts"
    "mobileclip2_b.ts"
) do (
    if exist "%%~F" (
        echo [OK] %%~F
    ) else (
        echo [MISS] %%~F
        set "MISSING_MODEL=1"
    )
)

if "%MISSING_MODEL%"=="1" (
    echo [ERROR] Some required model files are missing.
    echo         Re-run setup.bat after checking network access, or copy the missing files into this folder.
    exit /b 1
)

"%PY%" -c "from pathlib import Path; checks={'model/yoloe-26s-seg.pt':10*1024*1024,'mobileclip2_b.ts':200*1024*1024,'mobileclip_blt.ts':500*1024*1024}; bad=[f'{p} ({Path(p).stat().st_size if Path(p).exists() else 0} bytes)' for p,n in checks.items() if (not Path(p).exists()) or Path(p).stat().st_size < n]; print('[OK] model size check passed' if not bad else '[ERROR] incomplete model files: '+', '.join(bad)); raise SystemExit(1 if bad else 0)"
if errorlevel 1 (
    echo [ERROR] Model/text-encoder files are incomplete or corrupt.
    exit /b 1
)
exit /b 0

:start_backend
echo.
echo [7/7] Starting backend...
call :get_port_pid
if defined PORT_PID (
    call :is_our_backend
    if not errorlevel 1 (
        echo [OK] Port %PORT% is already used by this backend. PID: %PORT_PID%
        echo [INFO] Reusing the running backend/process.
        start "" "http://127.0.0.1:%PORT%/"
        exit /b 0
    )

    echo [ERROR] Port %PORT% is already used by another process.
    call :describe_port_owner
    if "%KILL_PORT%"=="1" (
        echo [RUN] Killing PID %PORT_PID% because --kill-port was provided...
        taskkill /PID %PORT_PID% /F
        if errorlevel 1 exit /b 1
        timeout /t 2 /nobreak >nul
        call :get_port_pid
        if defined PORT_PID (
            echo [ERROR] Port %PORT% is still busy after taskkill.
            call :describe_port_owner
            exit /b 1
        )
    ) else (
        echo.
        echo Close the process above, or run:
        echo   setup.bat --kill-port
        exit /b 1
    )
)

set "STDOUT_LOG=%CD%\logs\backend.stdout.log"
set "STDERR_LOG=%CD%\logs\backend.stderr.log"
echo [RUN] Launching app_main.py...
if exist "%STDOUT_LOG%" del /f /q "%STDOUT_LOG%"
if exist "%STDERR_LOG%" del /f /q "%STDERR_LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$app=Join-Path -Path '%CD%' -ChildPath 'app_main.py'; $p=Start-Process -FilePath '%PY%' -ArgumentList @($app) -WorkingDirectory '%CD%' -RedirectStandardOutput '%STDOUT_LOG%' -RedirectStandardError '%STDERR_LOG%' -WindowStyle Hidden -PassThru; if($p){exit 0}else{exit 1}"
if errorlevel 1 (
    echo [ERROR] Failed to launch backend process.
    exit /b 1
)

set /a WAIT_COUNT=0
:wait_backend
call :get_port_pid
if defined PORT_PID (
    call :is_our_backend
    if not errorlevel 1 goto backend_ready
    goto backend_port_busy
)
set /a WAIT_COUNT+=1
if !WAIT_COUNT! GEQ 60 goto backend_timeout
timeout /t 1 /nobreak >nul
goto wait_backend

:backend_ready
echo [OK] Backend is listening on port %PORT%. PID: %PORT_PID%
start "" "http://127.0.0.1:%PORT%/"
exit /b 0

:backend_timeout
echo [ERROR] Backend did not listen on port %PORT% within 60 seconds.
if exist "%STDERR_LOG%" (
    echo.
    echo Last backend errors:
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath $env:STDERR_LOG -Tail 30"
)
exit /b 1

:backend_port_busy
echo [ERROR] Another process took port %PORT% while backend was starting.
call :describe_port_owner
exit /b 1

:get_port_pid
set "PORT_PID="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){$c.OwningProcess}"`) do set "PORT_PID=%%P"
exit /b 0

:is_our_backend
if not defined PORT_PID exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$expected=[regex]::Escape((Join-Path -Path (Resolve-Path -LiteralPath '%CD%') -ChildPath 'app_main.py')); $p=Get-CimInstance Win32_Process -Filter 'ProcessId=%PORT_PID%' -ErrorAction SilentlyContinue; if($p -and $p.CommandLine -match $expected){exit 0}else{exit 1}" >nul 2>nul
exit /b %ERRORLEVEL%

:describe_port_owner
if not defined PORT_PID exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%PORT_PID%' -ErrorAction SilentlyContinue; if($p){Write-Host ('  PID:     ' + $p.ProcessId); Write-Host ('  Name:    ' + $p.Name); Write-Host ('  Path:    ' + $p.ExecutablePath); Write-Host ('  Command: ' + $p.CommandLine)}"
exit /b 0
