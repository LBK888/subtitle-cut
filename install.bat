@echo off
setlocal
title Subtitle-Cut Installer (Python 3.10-3.12 required)

rem ==========================================
rem  Subtitle-Cut Installation Script (Win)
rem  - CMD / PowerShell compatible
rem  - Forces Python 3.10 - 3.12 for venv
rem ==========================================

cd /d "%~dp0"

echo ==========================================
echo  Subtitle-Cut Installation Started
echo  Python 3.10 - 3.12 is REQUIRED
echo ==========================================

rem Step 0: locate Python 3.10~3.12 via py launcher
set "PY_EXE="
for %%v in (3.12 3.11 3.10) do (
    for /f "usebackq delims=" %%P in (`py -%%v -c "import sys; print(sys.executable)" 2^>nul`) do (
        set "PY_EXE=%%P"
        goto :found_py
    )
)
:found_py

rem Fallback: plain python but version must be 3.10-3.12
if not "%PY_EXE%"=="" goto :check_py_done
set "MAJOR="
set "MINOR="
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set "MAJOR=%%a"
    set "MINOR=%%b"
)
if "%MAJOR%"=="" goto :check_py_done
if not "%MAJOR%"=="3" goto :check_py_done
if "%MINOR%"=="" goto :check_py_done
if %MINOR% lss 10 goto :check_py_done
if %MINOR% gtr 12 goto :check_py_done
set "PY_EXE=python"
:check_py_done

if "%PY_EXE%"=="" (
    echo [ERROR] Compatible Python not found.
    echo         Install Python 3.10, 3.11, or 3.12 and ensure either:
    echo           - py -3.x works, or
    echo           - python --version is between 3.10.x and 3.12.x
    echo         Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Using interpreter: %PY_EXE%

rem Step 1: create virtual environment with compatible Python
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment .venv ...
    "%PY_EXE%" -m venv .venv
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

rem Step 3: verify venv Python is 3.1x
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "VENVVER=%%v"
set "VMAJOR="
set "VMINOR="
for /f "tokens=1,2 delims=." %%a in ("%VENVVER%") do ( set "VMAJOR=%%a" & set "VMINOR=%%b" )
if "%VMAJOR%"=="" goto :bad_ver
if not "%VMAJOR%"=="3" goto :bad_ver
if "%VMINOR%"=="" goto :bad_ver
if %VMINOR% lss 10 goto :bad_ver
if %VMINOR% gtr 12 goto :bad_ver
goto :ok_ver

:bad_ver
echo [ERROR] The active venv is not Python 3.10-3.12 (detected %VENVVER%).
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
