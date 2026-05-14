set PYTHONUNBUFFERED=1
@echo off
setlocal
title Subtitle-Cut Launcher

rem =====================================
rem  Subtitle-Cut Startup Script (Win)
rem  - CMD / PowerShell compatible
rem  - Sets MODELSCOPE_CACHE to project root
rem =====================================

rem 1) always run from the script directory
cd /d "%~dp0"

rem 2) set ModelScope cache to the project root
set "MODELSCOPE_CACHE=%~dp0"

rem 3) ensure virtual environment exists (Python 3.10-3.12 enforced in install.bat)
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found. Running install.bat ...
    if exist "install.bat" (
        call install.bat
    ) else (
        echo [ERROR] install.bat not found. Cannot continue.
        pause
        exit /b 1
    )
)

rem 4) activate venv
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

rem 5) check FFmpeg (not shipped)
if not exist "third_party\ffmpeg\bin\ffmpeg.exe" (
    echo [WARN] FFmpeg not found under third_party\ffmpeg\bin\
    echo       Please copy your ffmpeg\bin here or make sure ffmpeg is in PATH.
)

rem 5) Parse config.json
for /f "delims=" %%a in ('python -c "import json; c=json.load(open('config.json', encoding='utf-8')) if __import__('os').path.exists('config.json') else {}; print(c.get('enable_server', 'True'))"') do set ENABLE_SERVER=%%a
for /f "delims=" %%a in ('python -c "import json; c=json.load(open('config.json', encoding='utf-8')) if __import__('os').path.exists('config.json') else {}; print(c.get('qwen_asr_port', 8001))"') do set QWEN_PORT=%%a
for /f "delims=" %%a in ('python -c "import json; c=json.load(open('config.json', encoding='utf-8')) if __import__('os').path.exists('config.json') else {}; print(c.get('main_port', 5000))"') do set MAIN_PORT=%%a

if /I "%ENABLE_SERVER%"=="True" (
    set HOST=0.0.0.0
    echo [INFO] LAN Access is ENABLED - Server binding to 0.0.0.0
) else (
    set HOST=127.0.0.1
    echo [INFO] LAN Access is DISABLED - Server binding to 127.0.0.1
)

rem 5.5) Start QwenASRMiniTool API Server
echo [INFO] Starting QwenASRMiniTool API Server on port %QWEN_PORT%...
start "QwenAPI" /MIN cmd /c "cd QwenASRMiniTool && python -m uvicorn api_server:app --host %HOST% --port %QWEN_PORT%"

rem 6) launch web app
echo [INFO] Launching Subtitle-Cut Web App on port %MAIN_PORT%...
echo [INFO] Enabling detailed logging...
set PYTHONUNBUFFERED=1
set LOG_LEVEL=DEBUG

if /I "%HOST%"=="0.0.0.0" (
    start "" "http://127.0.0.1:%MAIN_PORT%/"
) else (
    start "" "http://%HOST%:%MAIN_PORT%/"
)

python -m src.webapp.app --server %HOST% --port %MAIN_PORT% %*
echo [INFO] Service stopped. Press any key to exit.
pause >nul
endlocal
exit /b 0
