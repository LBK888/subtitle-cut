@echo off
REM Bootstrap subtitle-cut environment on Windows.
setlocal
set SCRIPT_DIR=%~dp0

set PYTHON=python

REM Allow callers to provide custom Python executable via SUBTITLE_CUT_PYTHON.
if defined SUBTITLE_CUT_PYTHON (
    set PYTHON=%SUBTITLE_CUT_PYTHON%
)

"%PYTHON%" "%SCRIPT_DIR%setup_environment.py" %*
set EXIT_CODE=%ERRORLEVEL%

if not "%EXIT_CODE%"=="0" (
    echo Setup script failed with exit code %EXIT_CODE%.
) else (
    echo Environment setup finished successfully.
)

endlocal & exit /b %EXIT_CODE%
