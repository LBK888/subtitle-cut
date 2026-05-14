@echo off
chcp 65001 >nul
echo ==================================================
echo QwenASRMiniTool API Server (Port 8001)
echo ==================================================
echo.

:: 檢查是否有 venv
if exist "venv\Scripts\python.exe" (
    echo [INFO] Found local virtual environment (venv)
    set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
    echo [INFO] No local venv found, using system python
    set "PYTHON_EXE=python"
)

:: 確認 uvicorn 是否已安裝
%PYTHON_EXE% -m pip show uvicorn >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing required dependencies (uvicorn, fastapi, pydantic)...
    %PYTHON_EXE% -m pip install uvicorn fastapi pydantic
)

:: 啟動 API Server
echo [INFO] Starting API server on http://127.0.0.1:8001
%PYTHON_EXE% -m uvicorn api_server:app --host 127.0.0.1 --port 8001
pause
