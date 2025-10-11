@echo off
setlocal
title Subtitle-Cut Installer (Python 3.11 required)

rem ==========================================
rem  Subtitle-Cut Installation Script (Win)
rem  - CMD / PowerShell compatible
rem  - Forces Python 3.11 for venv
rem ==========================================

cd /d "%~dp0"

echo ==========================================
echo  Subtitle-Cut Installation Started
echo  Python 3.11 is REQUIRED
echo ==========================================

rem Step 0: locate Python 3.11 via py launcher
set "PY311_EXE="
for /f "usebackq delims=" %%P in (`py -3.11 -c "import sys; print(sys.executable)" 2^>nul`) do set "PY311_EXE=%%P"

rem Fallback: plain python but version must be exactly 3.11.x
if "%PY311_EXE%"=="" (
    for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
    for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do ( set "MAJOR=%%a" & set "MINOR=%%b" )
    if "%MAJOR%"=="3" if "%MINOR%"=="11" (
        set "PY311_EXE=python"
    )
)

if "%PY311_EXE%"=="" (
    echo [ERROR] Python 3.11 not found.
    echo         Install Python 3.11 and ensure either:
    echo           - py -3.11 works, or
    echo           - python --version is 3.11.x
    echo         Download: https://www.python.org/downloads/release/python-3110/
    pause
    exit /b 1
)

echo [OK] Using interpreter: %PY311_EXE%

rem Step 1: create virtual environment with Python 3.11
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment .venv using Python 3.11 ...
    "%PY311_EXE%" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [INFO] Virtual environment already exists.
)

rem Step 2: activate venv
call ".\.venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

rem Step 3: verify venv Python is 3.11.x
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "VENVVER=%%v"
for /f "tokens=1,2 delims=." %%a in ("%VENVVER%") do ( set "VMAJOR=%%a" & set "VMINOR=%%b" )
if not "%VMAJOR%"=="3" goto :bad_ver
if not "%VMINOR%"=="11" goto :bad_ver
goto :ok_ver

:bad_ver
echo [ERROR] The active venv is not Python 3.11 (detected %VENVVER%).
echo         Delete the .venv folder and run this installer again.
pause
exit /b 1

:ok_ver
echo [OK] Virtualenv Python version: %VENVVER%

rem Step 4: upgrade pip (quiet)
echo [INFO] Upgrading pip ...
python -m pip install --upgrade pip >nul 2>nul

rem Step 5: install dependencies
if exist "requirements.txt" (
    echo [INFO] Installing dependencies from requirements.txt ...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [WARN] Some packages failed to install. You can rerun this script later.
    )
) else (
    echo [WARN] requirements.txt not found. Skipping package installation.
)

echo.
echo ==========================================
echo  Installation finished.
echo  To start the app, run:  run_webapp.bat
echo ==========================================
pause
endlocal
exit /b 0
