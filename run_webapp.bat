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

rem 6) launch web app
echo [INFO] Launching Subtitle-Cut Web App...
echo [INFO] Enabling detailed logging...
set PYTHONUNBUFFERED=1
set LOG_LEVEL=DEBUG
start "" "http://127.0.0.1:5000/"
python -m src.webapp.app

echo [INFO] Service stopped. Press any key to exit.
pause >nul
endlocal
exit /b 0
