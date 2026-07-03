@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "APP_NAME=OpenAIglasses_for_Navigation"
set "DIST_DIR=dist"
set "PACKAGE_ROOT=%TEMP%\%APP_NAME%_package_%RANDOM%%RANDOM%"
set "STAGE_DIR=%PACKAGE_ROOT%\%APP_NAME%"
set "INCLUDE_RECORDINGS=0"
set "INCLUDE_ENV=0"
set "NO_PAUSE=0"
set "SHOW_HELP=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--help" set "SHOW_HELP=1" & shift & goto parse_args
if /I "%~1"=="/?" set "SHOW_HELP=1" & shift & goto parse_args
if /I "%~1"=="--include-recordings" set "INCLUDE_RECORDINGS=1"
if /I "%~1"=="--include-env" set "INCLUDE_ENV=1"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
shift
goto parse_args

:args_done
if "%SHOW_HELP%"=="1" (
    call :print_usage
    exit /b 0
)

echo ============================================================
echo   %APP_NAME% - Windows server package
echo ============================================================
echo Project: %CD%
echo.

call :find_powershell
if errorlevel 1 goto failed

call :make_package_name
if errorlevel 1 goto failed

call :clean_stage
if errorlevel 1 goto failed

call :copy_payload
if errorlevel 1 goto failed

call :write_server_notes
if errorlevel 1 goto failed

call :create_zip
if errorlevel 1 goto failed

call :cleanup_stage

echo.
echo ============================================================
echo   Package ready
echo ============================================================
echo ZIP: %ZIP_PATH%
echo.
echo Server usage:
echo   1. Copy ZIP to the Windows server.
echo   2. Extract it.
echo   3. Double-click setup.bat in the extracted folder.
echo.
echo Optional package commands:
echo   package_win_server.bat --include-env          Include local .env ^(not recommended^)
echo   package_win_server.bat --include-recordings   Include recordings folder
echo   package_win_server.bat --no-pause             Exit without waiting for a key
echo   package_win_server.bat --help                 Show usage only
echo.
if "%NO_PAUSE%"=="0" pause
exit /b 0

:failed
echo.
echo ============================================================
echo   Packaging failed
echo ============================================================
echo.
call :cleanup_stage
if "%NO_PAUSE%"=="0" pause
exit /b 1

:print_usage
echo Usage:
echo   package_win_server.bat [options]
echo.
echo Options:
echo   --include-env          Include local .env ^(not recommended^)
echo   --include-recordings   Include recordings folder
echo   --no-pause             Exit without waiting for a key
echo   --help                 Show usage only
echo.
echo Output:
echo   dist\OpenAIglasses_for_Navigation_YYYYMMDD_HHMMSS_win_server.zip
exit /b 0

:find_powershell
where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] PowerShell was not found. Windows PowerShell is required for Compress-Archive.
    exit /b 1
)
echo [OK] PowerShell detected.
exit /b 0

:make_package_name
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"
for /f "usebackq delims=" %%T in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "STAMP=%%T"
if not defined STAMP (
    echo [ERROR] Failed to create timestamp.
    exit /b 1
)
set "ZIP_PATH=%CD%\%DIST_DIR%\%APP_NAME%_%STAMP%_win_server.zip"
echo [OK] Output: %ZIP_PATH%
exit /b 0

:clean_stage
if exist "%PACKAGE_ROOT%" rmdir /s /q "%PACKAGE_ROOT%"
mkdir "%STAGE_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create staging directory: %STAGE_DIR%
    exit /b 1
)
exit /b 0

:copy_payload
echo.
echo [1/3] Copying project files...

robocopy "%CD%\aiglasses" "%STAGE_DIR%\aiglasses" /E /XD "__pycache__" /XF "*.pyc" "*.pyo" >nul
if %ERRORLEVEL% GEQ 8 exit /b 1

robocopy "%CD%\compile" "%STAGE_DIR%\compile" /E /XD "__pycache__" >nul
if %ERRORLEVEL% GEQ 8 exit /b 1

robocopy "%CD%\static" "%STAGE_DIR%\static" /E /XD "__pycache__" >nul
if %ERRORLEVEL% GEQ 8 exit /b 1

robocopy "%CD%\templates" "%STAGE_DIR%\templates" /E /XD "__pycache__" >nul
if %ERRORLEVEL% GEQ 8 exit /b 1

robocopy "%CD%\tools" "%STAGE_DIR%\tools" /E /XD "__pycache__" >nul
if %ERRORLEVEL% GEQ 8 exit /b 1

if exist "%CD%\model" (
    robocopy "%CD%\model" "%STAGE_DIR%\model" /E /XD ".git" "__pycache__" >nul
    if %ERRORLEVEL% GEQ 8 exit /b 1
)

if exist "%CD%\music" (
    robocopy "%CD%\music" "%STAGE_DIR%\music" /E >nul
    if %ERRORLEVEL% GEQ 8 exit /b 1
)

if exist "%CD%\voice" (
    robocopy "%CD%\voice" "%STAGE_DIR%\voice" /E >nul
    if %ERRORLEVEL% GEQ 8 exit /b 1
)

if "%INCLUDE_RECORDINGS%"=="1" if exist "%CD%\recordings" (
    robocopy "%CD%\recordings" "%STAGE_DIR%\recordings" /E >nul
    if %ERRORLEVEL% GEQ 8 exit /b 1
)

for %%F in (
    "app_main.py"
    "desktop_esp32_simulator.py"
    "prepare_models.py"
    "requirements.txt"
    "setup.bat"
    "README.md"
    "CHANGELOG.md"
    "PROJECT_STRUCTURE.md"
    "FUNCTION_FRAMEWORK.md"
    "LICENSE"
    "Dockerfile"
    "docker-compose.yml"
    "setup.sh"
) do (
    if exist "%CD%\%%~F" copy /Y "%CD%\%%~F" "%STAGE_DIR%\%%~F" >nul
)

if exist "%CD%\mobileclip_blt.ts" copy /Y "%CD%\mobileclip_blt.ts" "%STAGE_DIR%\mobileclip_blt.ts" >nul

if "%INCLUDE_ENV%"=="1" (
    if exist "%CD%\.env" (
        copy /Y "%CD%\.env" "%STAGE_DIR%\.env" >nul
        echo [WARN] Included local .env. Make sure it is intended for this server package.
    )
) else (
    echo [OK] Local .env excluded. setup.bat will create a blank one on the server.
)

echo [OK] Payload copied.
exit /b 0

:write_server_notes
echo.
echo [2/3] Writing server notes...
(
    echo # Windows Server Deployment
    echo.
    echo 1. Extract this ZIP on the Windows server.
    echo 2. Run setup.bat from this folder.
    echo 3. Fill DASHSCOPE_API_KEY in .env or in the web UI runtime config.
    echo.
    echo Important:
    echo - .env is excluded by default to avoid leaking local secrets.
    echo - runtime_config.json is excluded because it may contain local absolute paths.
    echo - .venv, .venv-run, logs, recordings, .git, .vs, and __pycache__ are excluded.
    echo - setup.bat will create .venv-run, install dependencies, prepare models, and start the backend.
    echo.
    echo Default server endpoints after setup:
    echo - UI: http://SERVER_IP:8081/
    echo - Camera WS: ws://SERVER_IP:8081/ws/camera
    echo - Audio WS: ws://SERVER_IP:8081/ws_audio
) > "%STAGE_DIR%\SERVER_DEPLOY.md"
exit /b 0

:create_zip
echo.
echo [3/3] Creating ZIP archive...
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -LiteralPath '%STAGE_DIR%' -DestinationPath '%ZIP_PATH%' -Force"
if errorlevel 1 (
    echo [ERROR] Compress-Archive failed.
    exit /b 1
)

for %%Z in ("%ZIP_PATH%") do set "ZIP_SIZE=%%~zZ"
echo [OK] ZIP size: %ZIP_SIZE% bytes
exit /b 0

:cleanup_stage
if defined PACKAGE_ROOT if exist "%PACKAGE_ROOT%" rmdir /s /q "%PACKAGE_ROOT%"
exit /b 0
