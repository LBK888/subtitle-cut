"""Web UI 後端應用實現。"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from datetime import datetime
from logging import handlers
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..core.schema import Transcript
from ..core.srt_vtt import dump_srt
from ..core.silence import analyze_silence
from ..core.transform import TimeRange, derive_keep_ranges, invert_ranges, rebase_transcript_after_cuts
from ..ffmpeg.utils import run_ffmpeg
from .storage import ProjectStorage
from .tasks import TaskManager, _merge_time_ranges
from .ramdisk import get_ramdisk_manager
from .waveform import WaveformGenerationError, generate_waveform_payload
from .config import get_app_config


LOGGER = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mka",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}

def jsonify(*args, **kwargs):
    if args and isinstance(args[0], dict):
        return args[0]
    return kwargs

def send_file(path_or_file, **kwargs):
    return FileResponse(path_or_file)

def _detect_audio_corruption(media_path: Path, ffmpeg_binary: str) -> tuple[bool, str]:
    command = [
        ffmpeg_binary,
        "-v",
        "error",
        "-hide_banner",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(media_path),
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        LOGGER.warning("未找到 FFmpeg 可執行文件 %s，跳過音頻損壞檢查", ffmpeg_binary)
        return False, ""
    stderr_text = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return True, stderr_text
    return False, stderr_text


def _reencode_audio_file(
    source_path: Path,
    target_path: Path,
    ffmpeg_binary: str,
) -> tuple[bool, str]:
    command = [
        "-hide_banner",
        "-i",
        str(source_path),
        "-acodec",
        "pcm_s16le",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(target_path),
    ]
    try:
        run_ffmpeg(command, binary=ffmpeg_binary)
    except FileNotFoundError:
        LOGGER.warning("未找到 FFmpeg 可執行文件 %s，無法執行音頻修復", ffmpeg_binary)
        return False, "未找到 FFmpeg 可執行文件"
    except subprocess.CalledProcessError as exc:
        error_text = ""
        if exc.stderr:
            error_text = exc.stderr if isinstance(exc.stderr, str) else str(exc.stderr)
        return False, error_text or "FFmpeg 重編碼失敗"
    return True, ""


def create_app(config: Optional[Dict[str, Any]] = None) -> FastAPI:
    """FastAPI App"""

    app = FastAPI()
    
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    setattr(app.state, "templates", templates)
    if config:
        for k, v in config.items(): setattr(app.state, k, v)

    data_root = Path(getattr(app.state, "SUBTITLE_CUT_WEB_ROOT", Path(__file__).resolve().parents[2] / "data"))
    database_path = Path(getattr(app.state, "SUBTITLE_CUT_WEB_DB_PATH", data_root / "webapp.db"))
    
    # 初始化應用配置
    project_root = Path(__file__).resolve().parents[2]
    config_file = project_root / "config.json"
    if not config_file.exists():
        config_file = data_root / "config.json"
    app_config = get_app_config(config_file)
    
    # 初始化虛擬硬碟管理器(但不自動創建)
    # 從配置文件讀取虛擬硬碟設置
    ramdisk_enabled = app_config.ramdisk_enabled
    ramdisk_size_gb = app_config.ramdisk_size_gb
    
    # 支持從環境變量覆蓋配置
    import os
    if "SUBTITLE_CUT_RAMDISK_ENABLED" in os.environ:
        ramdisk_enabled = os.environ["SUBTITLE_CUT_RAMDISK_ENABLED"].lower() in ("true", "1", "yes")
    if "SUBTITLE_CUT_RAMDISK_SIZE_GB" in os.environ:
        try:
            ramdisk_size_gb = int(os.environ["SUBTITLE_CUT_RAMDISK_SIZE_GB"])
        except ValueError:
            pass
    
    LOGGER.info("虛擬硬碟配置: enabled=%s, size_gb=%s (不自動創建,需手動應用)", ramdisk_enabled, ramdisk_size_gb)
    
    # 創建管理器但不初始化(不調用initialize)
    from .ramdisk import RamDiskManager
    ramdisk_mgr = RamDiskManager(enabled=ramdisk_enabled, size_gb=ramdisk_size_gb)
    
    # 如果配置為啟用,嘗試查找已存在的虛擬硬碟
    if ramdisk_enabled:
        imdisk_exe = shutil.which("imdisk")
        if imdisk_exe:
            ramdisk_mgr.imdisk_exe = imdisk_exe
            existing_mount = ramdisk_mgr._find_existing_ramdisk()
            if existing_mount:
                ramdisk_mgr.mount_point = f"{existing_mount}:"
                ramdisk_mgr.mount_root = Path(f"{existing_mount}:\\")
                LOGGER.info("找到已存在的虛擬硬碟: %s", ramdisk_mgr.mount_point)
                ramdisk_mgr.ensure_directories()
    
    # 註冊到全局
    from . import ramdisk as ramdisk_module
    ramdisk_module._ramdisk_manager = ramdisk_mgr
    
    # 根據虛擬硬碟狀態設置初始路徑
    uploads_dir = ramdisk_mgr.get_uploads_dir()
    task_dir = ramdisk_mgr.get_tasks_dir()
    
    # exports目錄保持在本地硬碟，因為這是最終輸出
    exports_dir = Path(getattr(app.state, "SUBTITLE_CUT_WEB_EXPORT_DIR", data_root / "exports"))
    filler_path = Path(getattr(app.state, "SUBTITLE_CUT_FILLER_PATH", data_root / "fillerwords_zh.txt"))
    log_dir = Path(getattr(app.state, "SUBTITLE_CUT_WEB_LOG_DIR", data_root / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    log_path.write_text("", encoding="utf-8")

    if not logging.getLogger().handlers:
        # 文件日誌 - 強制刷新
        file_handler = handlers.RotatingFileHandler(
            log_path, 
            maxBytes=5 * 1024 * 1024, 
            backupCount=3, 
            encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        file_handler.setLevel(logging.DEBUG)  # 捕獲所有級別
        
        # 控制臺日誌
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        console_handler.setLevel(logging.INFO)
        
        # 配置root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)  # 捕獲所有級別
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        # 確保所有子模塊的日誌都被捕獲
        for logger_name in ['src.audio', 'src.asr', 'src.ffmpeg', 'src.webapp', 'src.core']:
            module_logger = logging.getLogger(logger_name)
            module_logger.setLevel(logging.DEBUG)
            module_logger.propagate = True  # 確保傳播到root logger
        
        LOGGER.info("=" * 80)
        LOGGER.info("Subtitle-Cut Web App Starting")
        LOGGER.info("Logging to: %s", log_path)
        LOGGER.info("Log level: DEBUG (all messages will be captured)")
        LOGGER.info("=" * 80)
    
    # 輸出虛擬硬碟信息
    if ramdisk_mgr.mount_root:
        LOGGER.info("=" * 80)
        LOGGER.info("RAM DISK CONFIGURATION")
        LOGGER.info("Mount point: %s", ramdisk_mgr.mount_point)
        LOGGER.info("Uploads dir: %s", uploads_dir)
        LOGGER.info("Tasks dir: %s", task_dir)
        LOGGER.info("Exports dir: %s (local disk)", exports_dir)
        LOGGER.info("=" * 80)
    else:
        LOGGER.warning("=" * 80)
        LOGGER.warning("RAM DISK NOT AVAILABLE - Using local disk")
        LOGGER.warning("Uploads dir: %s", uploads_dir)
        LOGGER.warning("Tasks dir: %s", task_dir)
        LOGGER.warning("=" * 80)

    storage = ProjectStorage(database_path)
    storage.initialize()
    setattr(app.state, "SUBTITLE_CUT_STORAGE", storage)
    
    # 確保目錄存在
    uploads_dir.mkdir(parents=True, exist_ok=True)
    task_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)
    
    # 初始化TaskManager
    task_manager = TaskManager(storage, task_dir, exports_dir)
    setattr(app.state, "SUBTITLE_CUT_RAMDISK_MANAGER", ramdisk_mgr)
    setattr(app.state, "SUBTITLE_CUT_TASK_MANAGER", task_manager)
    setattr(app.state, "SUBTITLE_CUT_UPLOAD_DIR", uploads_dir)
    setattr(app.state, "SUBTITLE_CUT_DATA_ROOT", data_root)
    setattr(app.state, "SUBTITLE_CUT_LOG_PATH", log_path)
    filler_path.parent.mkdir(parents=True, exist_ok=True)
    if not filler_path.exists():
        filler_path.write_text("", encoding="utf-8")
    setattr(app.state, "SUBTITLE_CUT_FILLER_PATH", filler_path)

    def _load_filler_words_file() -> list[str]:
        try:
            content = filler_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line.strip() for line in content.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # 頁面
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> str:
        return templates.TemplateResponse(request, "index.html", {
            "log_path": str(getattr(app.state, "SUBTITLE_CUT_LOG_PATH", "")),
            "ui_language": app_config.get("ui_language", "zh-TW")
        })

    # ------------------------------------------------------------------
    # 項目管理接口
    # ------------------------------------------------------------------
    @app.get("/api/projects")
    async def list_projects(request: Request) -> Any:
        storage_local = _get_storage(request)
        projects = storage_local.list_projects()
        return {"projects": projects}

    @app.delete("/api/projects/{project_id}")
    async def delete_project(request: Request, project_id: int) -> Any:
        storage_local = _get_storage(request)
        project = storage_local.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="項目不存在")

        task_manager = _get_task_manager(request)
        upload_dir = _get_upload_dir(request)
        cleanup_roots = {
            upload_dir.resolve(),
            task_manager.working_dir.resolve(),
            task_manager.exports_dir.resolve(),
        }

        payloads: list[Dict[str, Any]] = []
        for kind in ("metadata", "selection", "silence", "waveform"):
            snapshots = storage_local.list_snapshots(project_id, kind)
            payloads.extend(snapshot.payload for snapshot in snapshots)

        paths_to_remove: Set[Path] = set(task_manager.cleanup_project(project_id))
        task_output_root = task_manager.exports_dir.resolve()

        for payload in payloads:
            paths_to_remove.update(_collect_file_paths(payload))

        filtered_paths = {path for path in paths_to_remove if not _is_path_in_roots(path, [task_output_root])}
        _remove_files_within_roots(filtered_paths, cleanup_roots)

        storage_local.delete_project(project_id)
        return {"status": "deleted", "project_id": project_id}

    @app.post("/api/projects")
    async def create_project(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        transcript_payload = payload.get("transcript")
        if transcript_payload is None:
            raise HTTPException(status_code=400, detail="缺少 transcript 欄位")

        try:
            transcript_model = Transcript.model_validate(transcript_payload)
        except ValidationError as exc:
            return JSONResponse(content={"error": "transcript 格式不合法", "details": exc.errors()}, status_code=400)

        name = (payload.get("name") or transcript_model.language or "未命名項目").strip() or "未命名項目"
        
        # 保存transcript，同時保留_metadata（包括presplit_metadata）
        transcript_dict = transcript_model.model_dump()
        
        # 如果原始payload包含_metadata，保留它
        if "_metadata" in transcript_payload:
            transcript_dict["_metadata"] = transcript_payload["_metadata"]
            LOGGER.info("Creating project with _metadata")
            if "presplit_metadata" in transcript_payload.get("_metadata", {}):
                num_segments = transcript_payload["_metadata"]["presplit_metadata"].get("num_segments", 0)
                LOGGER.info("Project includes presplit_metadata: %d segments", num_segments)
        else:
            LOGGER.info("Creating project without _metadata")
        
        storage_local = _get_storage(request)
        result = storage_local.create_project(name=name, transcript=transcript_dict)
        metadata_payload = payload.get("metadata")
        if metadata_payload:
            storage_local.save_metadata(result["id"], metadata_payload)
        return JSONResponse(content={"project": result}, status_code=201)

    # ------------------------------------------------------------------
    # 轉錄數據
    # ------------------------------------------------------------------
    @app.get("/api/projects/{project_id}/transcript")
    async def fetch_transcript(request: Request, project_id: int) -> Any:
        storage_local = _get_storage(request)
        v_str = request.query_params.get("version")
        version = int(v_str) if v_str else None
        snapshot = storage_local.get_snapshot(project_id, "transcript", version=version)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="未找到對應的項目或版本")

        full_param = request.query_params.get("full", "")
        full_requested = str(full_param).lower() in {"1", "true", "yes"}
        size_bytes = len(json.dumps(snapshot.payload, ensure_ascii=False).encode("utf-8"))

        if full_requested:
            transcript_data = dict(snapshot.payload)
            segments = transcript_data.get("segments", [])
            transcript_data["pagination"] = {
                "offset": 0,
                "limit": None,
                "total_segments": len(segments),
                "returned": len(segments),
            }
            offset_str = request.query_params.get("offset")
            limit_str = request.query_params.get("limit")
            offset = int(offset_str) if offset_str else 0
            limit = int(limit_str) if limit_str else None
            transcript_data = _slice_transcript(snapshot.payload, offset, limit)

        # 日誌：檢查是否包含_metadata
        if "_metadata" in transcript_data:
            if "presplit_metadata" in transcript_data.get("_metadata", {}):
                num_segments = transcript_data["_metadata"]["presplit_metadata"].get("num_segments", 0)
                LOGGER.info("Fetching transcript with presplit_metadata: %d segments", num_segments)
            else:
                LOGGER.info("Fetching transcript with _metadata (no presplit)")
        else:
            LOGGER.info("Fetching transcript without _metadata")

        return jsonify({
            "project_id": project_id,
            "version": snapshot.version,
            "created_at": snapshot.created_at,
            "size_bytes": size_bytes,
            "transcript": transcript_data,
        })

    @app.post("/api/projects/{project_id}/transcript")
    async def save_transcript(request: Request, project_id: int) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        transcript_payload = payload.get("transcript")
        if transcript_payload is None:
            raise HTTPException(status_code=400, detail="缺少 transcript 欄位")

        try:
            transcript_model = Transcript.model_validate(transcript_payload)
        except ValidationError as exc:
            return JSONResponse(content={"error": "transcript 格式不合法", "details": exc.errors()}, status_code=400)

        # 保存transcript，同時保留_metadata（包括presplit_metadata）
        transcript_dict = transcript_model.model_dump()
        
        # 如果原始payload包含_metadata，保留它
        if "_metadata" in transcript_payload:
            transcript_dict["_metadata"] = transcript_payload["_metadata"]
            LOGGER.info("Preserving _metadata in transcript (project %d)", project_id)
            if "presplit_metadata" in transcript_payload.get("_metadata", {}):
                num_segments = transcript_payload["_metadata"]["presplit_metadata"].get("num_segments", 0)
                LOGGER.info("Preserved presplit_metadata: %d segments", num_segments)
        
        next_version = _get_storage(request).save_transcript(project_id, transcript_dict)
        return JSONResponse(content={"project_id": project_id, "version": next_version}, status_code=201)

    # ------------------------------------------------------------------
    # 選擇集（刪除計劃）管理
    # ------------------------------------------------------------------
    @app.get("/api/projects/{project_id}/selection")
    async def fetch_selection(request: Request, project_id: int) -> Any:
        v_str = request.query_params.get("version")
        version = int(v_str) if v_str else None
        snapshot = _get_storage(request).get_snapshot(project_id, "selection", version=version)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="未找到 selection 信息")
        return jsonify({
            "project_id": project_id,
            "version": snapshot.version,
            "created_at": snapshot.created_at,
            "selection": snapshot.payload,
        })

    @app.post("/api/projects/{project_id}/selection")
    async def save_selection(request: Request, project_id: int) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        delete_ranges_payload = payload.get("delete_ranges")
        if delete_ranges_payload is None:
            raise HTTPException(status_code=400, detail="缺少 delete_ranges 欄位")

        delete_ranges = _normalize_delete_ranges(delete_ranges_payload)
        metadata = payload.get("metadata") or {}
        selection_payload = {"delete_ranges": delete_ranges, "metadata": metadata}
        version = _get_storage(request).save_selection(project_id, selection_payload)
        return JSONResponse(content={"project_id": project_id, "version": version}, status_code=201)

    # ------------------------------------------------------------------
    # 快照列表
    # ------------------------------------------------------------------
    @app.get("/api/projects/{project_id}/snapshots")
    async def list_snapshots(request: Request, project_id: int) -> Any:
        storage_local = _get_storage(request)
        transcript_snapshots = storage_local.list_snapshots(project_id, "transcript")
        selection_snapshots = storage_local.list_snapshots(project_id, "selection")
        return jsonify({
            "transcripts": [snapshot.__dict__ for snapshot in transcript_snapshots],
            "selections": [snapshot.__dict__ for snapshot in selection_snapshots],
        })

    @app.get("/api/projects/{project_id}/metadata")
    async def fetch_metadata(request: Request, project_id: int) -> Any:
        metadata = _get_storage(request).get_metadata(project_id)
        if metadata is None:
            return JSONResponse(content={"metadata": None}, status_code=200)
        return {"metadata": metadata}

    @app.post("/api/projects/{project_id}/metadata")
    async def save_metadata(request: Request, project_id: int) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not payload:
            raise HTTPException(status_code=400, detail="metadata 不能為空")
        version = _get_storage(request).save_metadata(project_id, payload)
        return JSONResponse(content={"project_id": project_id, "version": version}, status_code=201)

    # ------------------------------------------------------------------
    # 工程文件
    # ------------------------------------------------------------------
    @app.get("/api/project-files")
    async def list_project_files(request: Request) -> Any:
        pid_str = request.query_params.get("project_id")
        project_id = int(pid_str) if pid_str else 0
        if not project_id:
            raise HTTPException(status_code=400, detail="缺少 project_id")
        storage_local = _get_storage(request)
        files = storage_local.list_project_files(project_id)
        return jsonify({
            "project_id": project_id,
            "files": [file.to_dict() for file in files],
        })

    @app.get("/api/project-files/{file_id}")
    async def fetch_project_file(request: Request, file_id: int) -> Any:
        storage_local = _get_storage(request)
        project_file = storage_local.get_project_file(file_id)
        if project_file is None:
            raise HTTPException(status_code=404, detail="未找到工程文件")
        return {"file": project_file.to_dict()}

    @app.post("/api/project-files")
    async def create_project_file(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            raise HTTPException(status_code=400, detail="缺少 project_id")
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="工程文件名稱不能為空")
        selection_payload = payload.get("selection") or {}
        if not isinstance(selection_payload, dict):
            raise HTTPException(status_code=400, detail="selection 必須為對象")
        storage_local = _get_storage(request)
        project_file = storage_local.create_project_file(project_id, name, selection_payload)
        # 同步生成一份 selection 快照，保證現有流程兼容
        storage_local.save_selection(project_id, selection_payload)
        return JSONResponse(content={"file": project_file.to_dict()}, status_code=201)

    @app.post("/api/project-files/{file_id}/save")
    async def save_project_file(request: Request, file_id: int) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        selection_payload = payload.get("selection")
        if not isinstance(selection_payload, dict):
            raise HTTPException(status_code=400, detail="selection 必須為對象")
        name = payload.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise HTTPException(status_code=400, detail="name 必須為字符串")
            name = name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="工程文件名稱不能為空")
        storage_local = _get_storage(request)
        project_file = storage_local.get_project_file(file_id)
        if project_file is None:
            raise HTTPException(status_code=404, detail="未找到工程文件")
        updated = storage_local.update_project_file(
            file_id,
            selection_payload,
            name=name,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="未找到工程文件")
        storage_local.save_selection(project_file.project_id, selection_payload)
        return JSONResponse(content={"file": updated.to_dict()}, status_code=200)

    @app.get("/api/projects/{project_id}/silence")
    async def fetch_silence(request: Request, project_id: int) -> Any:
        snapshot = _get_storage(request).latest_snapshot(project_id, "silence")
        if snapshot is None:
            return {"version": None, "candidates": []}
        payload = dict(snapshot.payload)
        payload["version"] = snapshot.version
        payload.setdefault("generated_at", snapshot.created_at)
        return payload

    @app.post("/api/projects/{project_id}/silence")
    async def compute_silence(request: Request, project_id: int) -> Any:
        storage_local = _get_storage(request)
        if storage_local.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="項目不存在")

        transcript_snapshot = storage_local.latest_snapshot(project_id, "transcript")
        if transcript_snapshot is None:
            raise HTTPException(status_code=400, detail="項目尚未導入轉錄")

        try:
            transcript = Transcript.model_validate(transcript_snapshot.payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=f"轉錄數據無效: {exc}")

        metadata = storage_local.get_metadata(project_id) or {}
        media_path_value = metadata.get("media_path")
        if not media_path_value:
            raise HTTPException(status_code=400, detail="項目尚未記錄媒體路徑")

        media_path = Path(media_path_value)
        if not media_path.exists():
            raise HTTPException(status_code=400, detail=f"媒體文件不存在: {media_path}")

        try:
            params = await request.json()
        except Exception:
            params = {}
        min_duration = float(params.get("min_duration", 1.2))
        fps = float(params.get("fps", 2.0))
        scale = int(params.get("scale", 64))
        ffmpeg_binary = getattr(app.state, "SUBTITLE_CUT_WEB_FFMPEG", "ffmpeg")

        candidates = analyze_silence(
            transcript,
            media_path,
            ffmpeg_binary=ffmpeg_binary,
            min_duration=max(0.1, min_duration),
            fps=max(0.5, fps),
            scale=max(16, scale),
        )

        serialized = [candidate.to_dict() for candidate in candidates]
        payload = {
            "media_path": str(media_path),
            "generated_at": datetime.utcnow().isoformat(),
            "min_duration": min_duration,
            "fps": fps,
            "scale": scale,
            "ffmpeg_binary": ffmpeg_binary,
            "candidates": serialized,
        }

        version = storage_local.save_snapshot(project_id, "silence", payload)
        return jsonify({
            "version": version,
            "generated_at": payload["generated_at"],
            "candidates": serialized,
        })

    @app.get("/api/projects/{project_id}/media")
    async def fetch_media(request: Request, project_id: int) -> Any:
        metadata = _get_storage(request).get_metadata(project_id) or {}
        media_path_value = metadata.get("media_path")
        if not media_path_value:
            raise HTTPException(status_code=404, detail="未記錄媒體路徑")

        media_path = Path(media_path_value)
        if not media_path.exists():
            raise HTTPException(status_code=404, detail=f"媒體文件不存在: {media_path}")

        return send_file(media_path, conditional=True)

    @app.get("/api/projects/{project_id}/waveform")
    async def fetch_waveform(request: Request, project_id: int) -> Any:
        storage_local = _get_storage(request)
        v_str = request.query_params.get("version")
        requested_version = int(v_str) if v_str else None
        refresh_flag = (request.query_params.get("refresh") or "").strip().lower()
        refresh = refresh_flag in {"1", "true", "yes"}
        if requested_version is not None and refresh:
            raise HTTPException(status_code=400, detail="version 與 refresh 參數不能同時使用")

        if not refresh:
            snapshot = storage_local.get_snapshot(project_id, "waveform", version=requested_version)
            if snapshot:
                return jsonify({
                    "project_id": project_id,
                    "version": snapshot.version,
                    "waveform": snapshot.payload,
                    "cached": True,
                })
            # 指定版本尚未生成時，回退到重新生成邏輯
            requested_version = None

        metadata = storage_local.get_metadata(project_id) or {}
        media_path_value = metadata.get("media_path")
        if not media_path_value:
            raise HTTPException(status_code=404, detail="未記錄媒體路徑")

        media_path = Path(media_path_value)
        if not media_path.exists():
            raise HTTPException(status_code=404, detail=f"媒體文件不存在 {media_path}")

        ffmpeg_binary = getattr(app.state, "SUBTITLE_CUT_WEB_FFMPEG", "ffmpeg")
        try:
            waveform_payload = generate_waveform_payload(media_path, ffmpeg_binary=ffmpeg_binary)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"媒體文件不存在 {media_path}")
        except WaveformGenerationError as exc:
            LOGGER.warning("生成波形失敗，將返回空波形: %s", exc)
            waveform_payload = {
                "values": [],
                "duration": 0.0,
                "sample_rate": 0,
                "min": 0.0,
                "max": 0.0,
                "generated_at": datetime.now().isoformat(),
                "source": str(media_path),
                "error": str(exc),
            }

        version = storage_local.save_snapshot(project_id, "waveform", waveform_payload)
        return jsonify({
            "project_id": project_id,
            "version": version,
            "waveform": waveform_payload,
            "cached": False,
        })

    # ------------------------------------------------------------------
    # 錯誤處理
    # ------------------------------------------------------------------
    @app.exception_handler(400)
    async def handle_bad_request(request: Request, error: Exception) -> Any:
        message = getattr(error, "description", "請求無效")
        return JSONResponse(content={"error": message}, status_code=400)

    @app.exception_handler(404)
    async def handle_not_found(request: Request, error: Exception) -> Any:
        message = getattr(error, "description", "資源不存在")
        return JSONResponse(content={"error": message}, status_code=404)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception) -> Any:
        LOGGER.exception("Web API 發生未預期異常")
        return JSONResponse(content={"error": "伺服器內部錯誤"}, status_code=500)

    # ------------------------------------------------------------------
    # WhisperX 任務接口
    # ------------------------------------------------------------------
    @app.post("/api/uploads")
    async def upload_media(request: Request, file: UploadFile = File(...)) -> Any:
        uploads_dir_local = _get_upload_dir(request)
        if file is None or not file.filename:
            raise HTTPException(status_code=400, detail="缺少文件")

        extension = Path(file.filename).suffix.lower()
        filename = f"upload_{uuid.uuid4().hex}{extension}"
        save_path = uploads_dir_local / filename
        
        with open(save_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        response_payload: Dict[str, Any] = {"path": str(save_path)}
        ffmpeg_binary = getattr(app.state, "SUBTITLE_CUT_WEB_FFMPEG", "ffmpeg")
        if extension in AUDIO_EXTENSIONS:
            corrupted, detail = _detect_audio_corruption(save_path, ffmpeg_binary)
            if corrupted:
                LOGGER.warning("檢測到音頻包含損壞幀，開始嘗試修復: %s", save_path)
                repaired_filename = f"upload_{uuid.uuid4().hex}.wav"
                repaired_path = uploads_dir_local / repaired_filename
                success, repair_error = _reencode_audio_file(save_path, repaired_path, ffmpeg_binary)
                if success:
                    try:
                        save_path.unlink(missing_ok=True)
                    except Exception as exc:  # pragma: no cover - 清理失敗不影響主流程
                        LOGGER.warning("刪除原始損壞音頻失敗 %s: %s", save_path, exc)
                    save_path = repaired_path
                    response_payload["path"] = str(save_path)
                    response_payload["repaired"] = True
                    notice = "檢測到音頻存在損壞幀，已使用 FFmpeg 重編碼生成修復文件。"
                    response_payload["repair_notice"] = notice
                    if detail:
                        response_payload["repair_detail"] = detail
                    LOGGER.info("音頻修復完成，已替換為修復版本: %s", save_path)
                else:
                    response_payload["repaired"] = False
                    response_payload["repair_notice"] = "檢測到音頻存在損壞幀，自動修復失敗，請檢查原始文件。"
                    if repair_error:
                        response_payload["repair_detail"] = repair_error
                    LOGGER.warning("音頻修復失敗: %s %s", save_path, repair_error or "")
        return JSONResponse(content=response_payload, status_code=201)

    @app.post("/api/tasks/transcribe")
    async def submit_transcribe(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        media_path_value = payload.get("media_path")
        if not media_path_value:
            raise HTTPException(status_code=400, detail="缺少 media_path")
        media_path = Path(media_path_value)
        if not media_path.exists():
            raise HTTPException(status_code=404, detail="指定的媒體文件不存在")

        engine_value = (payload.get("engine") or "whisperx").strip().lower()
        if engine_value not in {"whisperx", "qwen3-asr", "qwen-mini"}:
            raise HTTPException(status_code=400, detail="engine 取值必須為 whisperx, qwen3-asr 或 qwen-mini")

        model = str(payload.get("model", "large-v2"))
        language = str(payload.get("language", "auto")).strip().lower()
        device = str(payload.get("device", "auto"))
        presplit_mode = str(payload.get("presplit_mode", "auto"))
        presplit_segments = int(payload.get("presplit_segments", 10))
        diarize = bool(payload.get("diarize", False))
        simplified = bool(payload.get("simplified", False))

        task_state = _get_task_manager(request).submit_transcribe(
            media_path,
            engine=engine_value,
            model=model,
            language=language,
            device=device,
            presplit_mode=presplit_mode,
            presplit_segments=presplit_segments,
            diarize=diarize,
            simplified=simplified,
        )
        return JSONResponse(content={"task_id": task_state.id, "status": task_state.status}, status_code=202)

    @app.post("/api/tasks/edit_subtitle")
    async def open_subtitle_editor(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        srt_path = payload.get("srt_path")
        audio_path = payload.get("audio_path")
        if not srt_path or not audio_path:
            raise HTTPException(status_code=400, detail="缺少 srt_path 或 audio_path")
            
        import requests
        try:
            qwen_port = get_app_config().get("qwen_asr_port", 8001)
            resp = requests.post(f"http://127.0.0.1:{qwen_port}/api/editor", json={
                "srt_path": srt_path,
                "audio_path": audio_path
            }, timeout=5)
            resp.raise_for_status()
            return JSONResponse(content={"status": "Editor launched"}, status_code=200)
        except Exception as e:
            LOGGER.error(f"無法呼叫 QwenASRMiniTool API 啟動編輯器: {e}")
            raise HTTPException(status_code=500, detail=f"無法啟動字幕編輯器 (API 無回應): {e}")

    @app.get("/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> Any:
        task_state = _get_task_manager(request).get_task(task_id)
        if not task_state:
            raise HTTPException(status_code=404, detail="未找到任務")
        response = {
            "task_id": task_state.id,
            "status": task_state.status,
            "message": task_state.message,
            "progress": task_state.progress,
            "metadata": task_state.metadata,
        }
        if task_state.result:
            response["result"] = task_state.result
        return response

    @app.post("/api/tasks/cut")
    async def submit_cut(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        project_id = payload.get("project_id")
        if project_id is None:
            raise HTTPException(status_code=400, detail="缺少 project_id")
        try:
            project_id = int(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="project_id 必須為整數")

        input_path_value = payload.get("input_path")
        if not input_path_value:
            raise HTTPException(status_code=400, detail="缺少 input_path")
        input_path = Path(input_path_value)
        if not input_path.exists():
            raise HTTPException(status_code=404, detail="輸入視頻不存在")

        storage_local = _get_storage(request)
        transcript_snapshot = storage_local.get_snapshot(project_id, "transcript")
        if transcript_snapshot is None:
            raise HTTPException(status_code=404, detail="項目尚未導入轉錄")

        # 獲取預分割元數據（如果有）
        presplit_metadata = transcript_snapshot.payload.get("_metadata", {}).get("presplit_metadata")
        
        if presplit_metadata:
            LOGGER.info("Found presplit metadata: %d segments", presplit_metadata.get("num_segments", 0))
        else:
            LOGGER.info("No presplit metadata found in transcript")

        selection_snapshot = storage_local.get_snapshot(project_id, "selection")
        delete_ranges = []
        if selection_snapshot:
            delete_ranges = selection_snapshot.payload.get("delete_ranges", [])

        transcript_model = Transcript.model_validate(transcript_snapshot.payload)
        total_duration = max((segment.end for segment in transcript_model.segments), default=0.0)
        delete_time_ranges = _merge_time_ranges(
            [
                TimeRange(start=item["start"], end=item["end"]).clamped(minimum=0.0, maximum=total_duration)
                for item in delete_ranges
            ]
        )
        # 直接使用invert_ranges：從總時長中刪除標記的區間，保留其餘部分
        # 這比derive_keep_ranges高效得多：
        # - derive_keep_ranges: 逐字檢查 → 生成幾千個保留區間
        # - invert_ranges: 反向刪除 → 只生成少量保留區間
        keep_ranges = invert_ranges(total_duration, delete_time_ranges)
        keep_tuples = [(round(rng.start, 6), round(rng.end, 6)) for rng in keep_ranges]
        if not keep_tuples:
            raise HTTPException(status_code=400, detail="無保留區間，無法剪輯")

        base_name = payload.get("output_name") or f"project_{project_id}"
        reencode = payload.get("reencode", "nvenc")
        snap_zero_raw = payload.get("snap_zero_cross", True)
        if isinstance(snap_zero_raw, str):
            snap_zero_cross = snap_zero_raw.strip().lower() not in {"false", "0", "no"}
        else:
            snap_zero_cross = bool(snap_zero_raw)

        try:
            xfade_ms = float(payload.get("xfade_ms", 0.0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="xfade_ms 必須為數值")
        if xfade_ms < 0.0:
            xfade_ms = 0.0

        chunk_size_raw = payload.get("chunk_size", 10)  # 降低默認值,更容易觸發多線程
        try:
            chunk_size = int(chunk_size_raw)
        except (TypeError, ValueError):
            chunk_size = 10
        if chunk_size < 1:
            chunk_size = 1

        task_state = _get_task_manager(request).submit_cut(
            project_id=project_id,
            input_path=input_path,
            keep_ranges=keep_tuples,
            transcript_payload=transcript_snapshot.payload,
            delete_ranges=delete_ranges,
            base_name=base_name,
            reencode=reencode,
            snap_zero_cross=snap_zero_cross,
            xfade_ms=xfade_ms,
            chunk_size=chunk_size,
        )
        return JSONResponse(content={"task_id": task_state.id, "status": task_state.status}, status_code=202)

    @app.get("/api/common-fillers")
    async def get_common_fillers(request: Request) -> Any:
        words = _load_filler_words_file()
        return {"words": words}

    @app.post("/api/common-fillers")
    async def update_common_fillers(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        words = payload.get("words", [])
        if not isinstance(words, list):
            return JSONResponse(content={"error": "無效的水詞列表"}, status_code=400)
        normalized: list[str] = []
        for item in words:
            text = str(item or "").strip()
            if text:
                normalized.append(text)
        try:
            filler_path.write_text("\n".join(normalized), encoding="utf-8")
        except OSError:
            LOGGER.exception("failed to persist filler words")
            return JSONResponse(content={"error": "保存常用水詞失敗"}, status_code=500)
        return {"words": normalized}

    @app.put("/api/projects/{project_id}/transcript")
    async def update_project_transcript(request: Request, project_id: int) -> Any:
        payload = await request.json()
        storage_local = _get_storage(request)
        if not storage_local.get_project(project_id):
            raise HTTPException(status_code=404, detail="項目不存在")
            
        try:
            transcript_model = Transcript.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(content={"error": "轉錄數據無效", "details": exc.errors()}, status_code=400)
            
        transcript_dict = transcript_model.model_dump()
        if "_metadata" in payload:
            transcript_dict["_metadata"] = payload["_metadata"]
            
        storage_local.save_transcript(project_id, transcript_dict)
        return {"status": "ok"}

    @app.post("/api/projects/{project_id}/export/srt")
    async def export_project_srt(request: Request, project_id: int) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        storage_local = _get_storage(request)
        snapshot = storage_local.get_snapshot(project_id, "transcript")
        if snapshot is None:
            raise HTTPException(status_code=404, detail="項目尚未生成轉錄")

        try:
            transcript_model = Transcript.model_validate(snapshot.payload)
        except ValidationError as exc:
            return JSONResponse(content={"error": "轉錄數據無效", "details": exc.errors()}, status_code=400)

        selection_snapshot = storage_local.get_snapshot(project_id, "selection")
        delete_ranges = selection_snapshot.payload.get("delete_ranges", []) if selection_snapshot else []
        
        if delete_ranges:
            total_duration = max((segment.end for segment in transcript_model.segments), default=0.0)
            delete_time_ranges = _merge_time_ranges(
                [TimeRange(start=item["start"], end=item["end"]) for item in delete_ranges]
            )
            keep_ranges = invert_ranges(total_duration, delete_time_ranges)
            transcript_model = rebase_transcript_after_cuts(
                transcript_model,
                delete_time_ranges,
                keep_ranges=keep_ranges,
            )

        base_name = (payload.get("output_name") or "").strip()
        manager = _get_task_manager(request)
        stem = manager.resolve_export_stem(base_name, project_id, ".srt")
        export_path = manager.exports_dir / f"{stem}.srt"
        dump_srt(transcript_model, export_path)
        LOGGER.info("exported srt for project %s -> %s", project_id, export_path)
        return FileResponse(
            path=str(export_path),
            filename=f"{stem}.srt",
            media_type="application/x-subrip"
        )

    @app.get("/api/download")
    async def download_file(request: Request, path: str) -> Any:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
            
        data_root = getattr(app.state, "SUBTITLE_CUT_DATA_ROOT")
        try:
            file_path.resolve().relative_to(data_root.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")
            
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type="application/octet-stream"
        )

    # ------------------------------------------------------------------
    # 虛擬硬碟管理接口
    # ------------------------------------------------------------------
    @app.get("/api/ramdisk/status")
    async def get_ramdisk_status(request: Request) -> Any:
        """獲取虛擬硬碟狀態"""
        ramdisk_mgr = get_ramdisk_manager()
        app_config = get_app_config()
        
        # 返回配置文件中的值(下次啟動時會使用的配置)
        # 而不是當前運行時的狀態
        return jsonify({
            "enabled": app_config.ramdisk_enabled,  # 從配置文件讀取
            "mounted": ramdisk_mgr.mount_point is not None,
            "mount_point": ramdisk_mgr.mount_point,
            "size_gb": app_config.ramdisk_size_gb,  # 從配置文件讀取
            "current_enabled": ramdisk_mgr.enabled,  # 當前運行狀態
            "current_size_gb": ramdisk_mgr.size_gb,  # 當前運行容量
        })

    @app.post("/api/ramdisk/unmount")
    async def unmount_ramdisk(request: Request) -> Any:
        """卸載虛擬硬碟"""
        ramdisk_mgr = get_ramdisk_manager()
        success = ramdisk_mgr.unmount()
        if success:
            return {"status": "ok", "message": "虛擬硬碟已卸載"}
        else:
            return JSONResponse(content={"status": "error", "message": "卸載虛擬硬碟失敗"}, status_code=500)

    @app.post("/api/ramdisk/reset-size")
    async def reset_ramdisk_size(request: Request) -> Any:
        """重置虛擬硬碟容量"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            new_size_gb = int(payload.get("size_gb", 10))
        except (TypeError, ValueError):
            return JSONResponse(content={"status": "error", "message": "容量參數無效"}, status_code=400)
        
        if new_size_gb < 1 or new_size_gb > 64:
            return JSONResponse(content={"status": "error", "message": "容量必須在1-64GB之間"}, status_code=400)
        
        ramdisk_mgr = get_ramdisk_manager()
        success = ramdisk_mgr.reset_size(new_size_gb)
        if success:
            # 保存配置
            app_config = get_app_config()
            app_config.ramdisk_size_gb = new_size_gb
            app_config.save()
            
            return jsonify({
                "status": "ok", 
                "message": f"虛擬硬碟容量已重置為{new_size_gb}GB",
                "size_gb": new_size_gb,
                "mount_point": ramdisk_mgr.mount_point
            })
        else:
            return JSONResponse(content={"status": "error", "message": "重置虛擬硬碟容量失敗"}, status_code=500)

    @app.post("/api/ramdisk/save-config")
    async def save_ramdisk_config(request: Request) -> Any:
        """保存虛擬硬碟配置(僅保存,不應用)"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        
        try:
            enabled = payload.get("enabled")
            if enabled is not None:
                if isinstance(enabled, str):
                    enabled = enabled.lower() in ("true", "1", "yes")
                enabled = bool(enabled)
            
            size_gb = payload.get("size_gb")
            if size_gb is not None:
                size_gb = int(size_gb)
                if size_gb < 1 or size_gb > 64:
                    return JSONResponse(content={"status": "error", "message": "容量必須在1-64GB之間"}, status_code=400)
        except (TypeError, ValueError) as e:
            return JSONResponse(content={"status": "error", "message": f"參數無效: {e}"}, status_code=400)
        
        # 保存配置
        app_config = get_app_config()
        if enabled is not None:
            app_config.ramdisk_enabled = enabled
        if size_gb is not None:
            app_config.ramdisk_size_gb = size_gb
        
        try:
            app_config.save()
            return jsonify({
                "status": "ok",
                "message": "配置已保存,重啟應用後生效",
                "config": {
                    "enabled": app_config.ramdisk_enabled,
                    "size_gb": app_config.ramdisk_size_gb
                }
            })
        except Exception as e:
            LOGGER.error("保存配置失敗: %s", e)
            return JSONResponse(content={"status": "error", "message": f"保存配置失敗: {e}"}, status_code=500)

    @app.post("/api/ramdisk/apply-config")
    async def apply_ramdisk_config(request: Request) -> Any:
        """保存並立即應用虛擬硬碟配置"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        
        try:
            enabled = payload.get("enabled")
            if enabled is not None:
                if isinstance(enabled, str):
                    enabled = enabled.lower() in ("true", "1", "yes")
                enabled = bool(enabled)
            else:
                return JSONResponse(content={"status": "error", "message": "缺少enabled參數"}, status_code=400)
            
            size_gb = payload.get("size_gb")
            if size_gb is not None:
                size_gb = int(size_gb)
                if size_gb < 1 or size_gb > 64:
                    return JSONResponse(content={"status": "error", "message": "容量必須在1-64GB之間"}, status_code=400)
            else:
                size_gb = 10
        except (TypeError, ValueError) as e:
            return JSONResponse(content={"status": "error", "message": f"參數無效: {e}"}, status_code=400)
        
        # 保存配置
        app_config = get_app_config()
        app_config.ramdisk_enabled = enabled
        app_config.ramdisk_size_gb = size_gb
        
        try:
            app_config.save()
        except Exception as e:
            LOGGER.error("保存配置失敗: %s", e)
            return JSONResponse(content={"status": "error", "message": f"保存配置失敗: {e}"}, status_code=500)
        
        # 立即應用配置
        ramdisk_mgr = get_ramdisk_manager()
        
        if enabled:
            # 啟用虛擬硬碟
            if ramdisk_mgr.mount_point:
                # 已掛載,檢查容量是否需要調整
                if ramdisk_mgr.size_gb != size_gb:
                    LOGGER.info("虛擬硬碟容量變更: %dGB -> %dGB", ramdisk_mgr.size_gb, size_gb)
                    success = ramdisk_mgr.reset_size(size_gb)
                    if not success:
                        return JSONResponse(content={"status": "error", "message": "調整虛擬硬碟容量失敗"}, status_code=500)
                    ramdisk_mgr.ensure_directories()
                    message = f"虛擬硬碟容量已調整為{size_gb}GB"
                else:
                    message = f"虛擬硬碟已啟用 ({ramdisk_mgr.mount_point}, {size_gb}GB)"
            else:
                # 未掛載,創建虛擬硬碟
                ramdisk_mgr.enabled = True
                ramdisk_mgr.size_gb = size_gb
                success = ramdisk_mgr.initialize()
                if success:
                    ramdisk_mgr.ensure_directories()
                    message = f"虛擬硬碟已創建並掛載 ({ramdisk_mgr.mount_point}, {size_gb}GB)"
                else:
                    return JSONResponse(content={"status": "error", "message": "創建虛擬硬碟失敗"}, status_code=500)
            
            # 更新應用配置中的路徑
            new_uploads_dir = ramdisk_mgr.get_uploads_dir()
            new_tasks_dir = ramdisk_mgr.get_tasks_dir()
            new_uploads_dir.mkdir(parents=True, exist_ok=True)
            new_tasks_dir.mkdir(parents=True, exist_ok=True)
            
            
            setattr(request.app.state, "SUBTITLE_CUT_UPLOAD_DIR", new_uploads_dir)
            
            # 更新TaskManager的工作目錄
            task_manager = _get_task_manager(request)
            task_manager.update_working_dir(new_tasks_dir)
            
            # 輸出新的路徑信息
            LOGGER.info("=" * 80)
            LOGGER.info("虛擬硬碟已啟用")
            LOGGER.info("Uploads目錄: %s", new_uploads_dir)
            LOGGER.info("Tasks目錄: %s", new_tasks_dir)
            LOGGER.info("=" * 80)
        else:
            # 禁用虛擬硬碟
            if ramdisk_mgr.mount_point:
                success = ramdisk_mgr.unmount()
                if success:
                    ramdisk_mgr.enabled = False
                    message = "虛擬硬碟已卸載並禁用"
                else:
                    return JSONResponse(content={"status": "error", "message": "卸載虛擬硬碟失敗"}, status_code=500)
            else:
                ramdisk_mgr.enabled = False
                message = "虛擬硬碟已禁用"
            
            # 更新應用配置中的路徑(切換到本地)
            new_uploads_dir = ramdisk_mgr.get_uploads_dir()
            new_tasks_dir = ramdisk_mgr.get_tasks_dir()
            new_uploads_dir.mkdir(parents=True, exist_ok=True)
            new_tasks_dir.mkdir(parents=True, exist_ok=True)
            
            
            setattr(request.app.state, "SUBTITLE_CUT_UPLOAD_DIR", new_uploads_dir)
            
            # 更新TaskManager的工作目錄
            task_manager = _get_task_manager(request)
            task_manager.update_working_dir(new_tasks_dir)
            
            # 輸出降級後的路徑信息
            LOGGER.info("=" * 80)
            LOGGER.info("虛擬硬碟已禁用,使用本地存儲")
            LOGGER.info("Uploads目錄: %s", new_uploads_dir)
            LOGGER.info("Tasks目錄: %s", new_tasks_dir)
            LOGGER.info("=" * 80)
        
        return jsonify({
            "status": "ok",
            "message": message,
            "config": {
                "enabled": enabled,
                "size_gb": size_gb,
                "mounted": ramdisk_mgr.mount_point is not None,
                "mount_point": ramdisk_mgr.mount_point
            }
        })

    return app


# ----------------------------------------------------------------------
# 輔助函數
# ----------------------------------------------------------------------
def _get_storage(request: Request) -> ProjectStorage:
    

    storage = getattr(request.app.state, "SUBTITLE_CUT_STORAGE", None)
    if storage is None:
        raise RuntimeError("存儲尚未初始化")
    return storage


def _get_task_manager(request: Request) -> TaskManager:
    

    manager = getattr(request.app.state, "SUBTITLE_CUT_TASK_MANAGER", None)
    if manager is None:
        raise RuntimeError("任務管理器尚未初始化")
    return manager


def _get_upload_dir(request: Request) -> Path:
    """獲取上傳目錄"""
    
    
    upload_dir = getattr(request.app.state, "SUBTITLE_CUT_UPLOAD_DIR", None)
    if upload_dir is None:
        raise RuntimeError("上傳目錄尚未初始化")
    return Path(upload_dir)


def _slice_transcript(transcript: Dict[str, Any], offset: int, limit: Optional[int]) -> Dict[str, Any]:
    segments = transcript.get("segments", [])
    total_segments = len(segments)
    start_index = max(offset, 0)
    end_index = total_segments if limit is None else min(total_segments, start_index + max(limit, 0))
    sliced_segments = segments[start_index:end_index]
    result = dict(transcript)
    result["segments"] = sliced_segments
    result["pagination"] = {
        "offset": start_index,
        "limit": None if limit is None else max(limit, 0),
        "total_segments": total_segments,
        "returned": len(sliced_segments),
    }
    return result




def _collect_file_paths(value: Any) -> Set[Path]:
    paths: Set[Path] = set()
    if isinstance(value, str):
        try:
            candidate = Path(value).expanduser()
        except (TypeError, ValueError):
            return paths
        if candidate.is_absolute():
            paths.add(candidate)
        return paths
    if isinstance(value, dict):
        for item in value.values():
            paths.update(_collect_file_paths(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            paths.update(_collect_file_paths(item))
    return paths


def _remove_files_within_roots(paths: Iterable[Path], roots: Iterable[Path]) -> None:
    resolved_roots = [root.resolve() for root in roots]
    for original in set(paths):
        try:
            path = original if isinstance(original, Path) else Path(original)
        except (TypeError, ValueError):
            continue
        path = path.expanduser().resolve()
        if not _is_path_in_roots(path, resolved_roots):
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover - best effort cleanup
            LOGGER.warning("刪除文件失敗 %s: %s", path, exc)
        else:
            _prune_empty_parents(path.parent, resolved_roots)


def _is_path_in_roots(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _prune_empty_parents(start: Path, roots: Iterable[Path]) -> None:
    for parent in start.parents:
        if parent in roots:
            break
        if not _is_path_in_roots(parent, roots):
            break
        try:
            parent.rmdir()
        except OSError:
            break


def _normalize_delete_ranges(raw_ranges: Any) -> list[dict[str, float]]:
    if not isinstance(raw_ranges, list):
        raise HTTPException(status_code=400, detail="delete_ranges 必須是數組")

    normalized: list[dict[str, float]] = []
    for entry in raw_ranges:
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"delete_ranges 項格式錯誤: {entry}")
        if end <= start:
            continue
        normalized.append({"start": start, "end": end})

    if not normalized:
        raise HTTPException(status_code=400, detail="delete_ranges 不能為空")

    # 歸併重疊段，保持有序
    normalized.sort(key=lambda item: item["start"])
    merged: list[TimeRange] = []
    for item in normalized:
        current = TimeRange(start=item["start"], end=item["end"])
        if not merged or current.start > merged[-1].end:
            merged.append(current)
        else:
            merged[-1].end = max(merged[-1].end, current.end)

    return [{"start": rng.start, "end": rng.end} for rng in merged]


app = create_app()


if __name__ == "__main__":
    import argparse
    import uvicorn
    
    parser = argparse.ArgumentParser(description="Subtitle-Cut WebApp (FastAPI)")
    parser.add_argument("--server", type=str, default="127.0.0.1", help="Server host IP")
    parser.add_argument("--port", type=int, default=5000, help="Server port")
    args = parser.parse_args()
    
    uvicorn.run("src.webapp.app:app", host=args.server, port=args.port, reload=True)
