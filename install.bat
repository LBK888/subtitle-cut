@echo off
setlocal enabledelayedexpansion

set "VENV_DIR=.venv"
set "REQ_FILE=requirements.txt"
set "PIP_INDEX_ARGS=--extra-index-url https://download.pytorch.org/whl/cu118"
set "PYTHON_CMD="

if not exist "%REQ_FILE%" (
    echo [ERROR] Missing requirements.txt. Create it before running this script.
    exit /b 1
)

py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.11"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python 3.11 interpreter not found. Install Python 3.11 or enable the py launcher.
        exit /b 1
    )
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Detected Python version is below 3.11. Install Python 3.11 and rerun this script.
        exit /b 1
    )
    set "PYTHON_CMD=python"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [INFO] Creating virtual environment with %PYTHON_CMD%...
    call %PYTHON_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. Check your Python 3.11 installation.
        exit /b 1
    )
)

set "ACTIVATE_FILE=%VENV_DIR%\Scripts\activate.bat"
if exist "%ACTIVATE_FILE%" (
    echo [INFO] Applying ModelScope cache hook to activate.bat...
    powershell -NoLogo -NoProfile -Command ^
        "$path = Get-Item -LiteralPath '%ACTIVATE_FILE%';" ^
        "$content = if (Test-Path $path) { Get-Content -LiteralPath $path -Raw -Encoding UTF8 } else { '' };" ^
        "if ($content -notmatch ':: MODELSCOPE cache hook') {" ^
        "  $lines = if ($content.Length -gt 0) { $content -split \"`r?`n\" } else { @() };" ^
        "  $pct = [char]37;" ^
        "  $insert = @(" ^
        "    ':: MODELSCOPE cache hook'," ^
        "    ('for {0}{0}i in (\"{0}{0}~dp0..\\..\") do set \"SUBTITLE_CUT_PROJECT_ROOT={0}{0}~fi\"' -f $pct)," ^
        "    ('if not defined MODELSCOPE_CACHE set \"MODELSCOPE_CACHE={0}SUBTITLE_CUT_PROJECT_ROOT{0}\"' -f $pct)," ^
        "    ('set \"FFMPEG_DIR={0}SUBTITLE_CUT_PROJECT_ROOT{0}\\third_party\\ffmpeg\\bin\"' -f $pct)," ^
        "    'if exist \"%FFMPEG_DIR%\\ffmpeg.exe\" ('" ^
        "    '    set \"PATH=%FFMPEG_DIR%;%PATH%\"'" ^
        "    '    set \"FFMPEG_BINARY=%FFMPEG_DIR%\\ffmpeg.exe\"'" ^
        "    '    set \"FFPROBE_BINARY=%FFMPEG_DIR%\\ffprobe.exe\"'" ^
        "    ')'," ^
        "    'set \"FFMPEG_DIR=\"'," ^
        "    'set \"SUBTITLE_CUT_PROJECT_ROOT=\"'" ^
        "  );" ^
        "  $insertAt = 0;" ^
        "  for ($i = 0; $i -lt $lines.Length; $i++) {" ^
        "    if ($lines[$i].Trim().ToLower() -eq '@echo off') { $insertAt = $i + 1; break }" ^
        "  }" ^
        "  $updated = @();" ^
        "  if ($insertAt -gt 0) { $updated += $lines[0..($insertAt-1)] }" ^
        "  $updated += $insert;" ^
        "  if ($insertAt -lt $lines.Length) { $updated += $lines[$insertAt..($lines.Length-1)] }" ^
        "  [System.IO.File]::WriteAllLines($path.FullName, $updated, [System.Text.Encoding]::UTF8);" ^
        "}"
)

echo [INFO] Upgrading pip...
call "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] pip upgrade failed.
    exit /b 1
)

echo [INFO] Installing dependencies (PyTorch CUDA 11.8 + project packages)...
call "%VENV_DIR%\Scripts\python.exe" -m pip install %PIP_INDEX_ARGS% -r "%REQ_FILE%"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Please review requirements.txt.
    exit /b 1
)

echo [INFO] Environment ready. Activate it with "%VENV_DIR%\Scripts\activate".
exit /b 0
