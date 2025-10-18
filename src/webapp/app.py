"""Web UI 后端应用实现。"""

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

from flask import Flask, abort, jsonify, render_template, request, send_file
from pydantic import ValidationError

from ..core.schema import Transcript
from ..core.srt_vtt import dump_srt
from ..core.silence import analyze_silence
from ..core.transform import TimeRange, derive_keep_ranges, invert_ranges
from ..ffmpeg.utils import run_ffmpeg
from .storage import ProjectStorage
from .tasks import TaskManager, _merge_time_ranges
from .waveform import WaveformGenerationError, generate_waveform_payload


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
            text=True,
        )
    except FileNotFoundError:
        LOGGER.warning("未找到 FFmpeg 可执行文件 %s，跳过音频损坏检查", ffmpeg_binary)
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
        LOGGER.warning("未找到 FFmpeg 可执行文件 %s，无法执行音频修复", ffmpeg_binary)
        return False, "未找到 FFmpeg 可执行文件"
    except subprocess.CalledProcessError as exc:
        error_text = ""
        if exc.stderr:
            error_text = exc.stderr if isinstance(exc.stderr, str) else str(exc.stderr)
        return False, error_text or "FFmpeg 重编码失败"
    return True, ""


def create_app(config: Optional[Dict[str, Any]] = None) -> Flask:
    """Flask 应用工厂。"""

    app = Flask(__name__)
    if config:
        app.config.update(config)

    data_root = Path(app.config.get("SUBTITLE_CUT_WEB_ROOT", Path(__file__).resolve().parents[2] / "data"))
    database_path = Path(app.config.get("SUBTITLE_CUT_WEB_DB_PATH", data_root / "webapp.db"))
    uploads_dir = Path(app.config.get("SUBTITLE_CUT_WEB_UPLOAD_DIR", data_root / "uploads"))
    task_dir = Path(app.config.get("SUBTITLE_CUT_WEB_TASK_DIR", data_root / "tasks"))
    exports_dir = Path(app.config.get("SUBTITLE_CUT_WEB_EXPORT_DIR", data_root / "exports"))
    filler_path = Path(app.config.get("SUBTITLE_CUT_FILLER_PATH", data_root / "fillerwords_zh.txt"))
    log_dir = Path(app.config.get("SUBTITLE_CUT_WEB_LOG_DIR", data_root / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    log_path.write_text("", encoding="utf-8")

    if not logging.getLogger().handlers:
        handler = handlers.RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

    storage = ProjectStorage(database_path)
    storage.initialize()
    app.config["SUBTITLE_CUT_STORAGE"] = storage
    uploads_dir.mkdir(parents=True, exist_ok=True)
    task_manager = TaskManager(storage, task_dir, exports_dir)
    app.config["SUBTITLE_CUT_TASK_MANAGER"] = task_manager
    app.config["SUBTITLE_CUT_UPLOAD_DIR"] = uploads_dir
    app.config["SUBTITLE_CUT_LOG_PATH"] = log_path
    filler_path.parent.mkdir(parents=True, exist_ok=True)
    if not filler_path.exists():
        filler_path.write_text("", encoding="utf-8")
    app.config["SUBTITLE_CUT_FILLER_PATH"] = filler_path

    def _load_filler_words_file() -> list[str]:
        try:
            content = filler_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line.strip() for line in content.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------
    @app.route("/")
    def index() -> str:
        return render_template("index.html", log_path=str(app.config["SUBTITLE_CUT_LOG_PATH"]))

    # ------------------------------------------------------------------
    # 项目管理接口
    # ------------------------------------------------------------------
    @app.get("/api/projects")
    def list_projects() -> Any:
        storage_local = _get_storage()
        projects = storage_local.list_projects()
        return jsonify({"projects": projects})

    @app.delete("/api/projects/<int:project_id>")
    def delete_project(project_id: int) -> Any:
        storage_local = _get_storage()
        project = storage_local.get_project(project_id)
        if project is None:
            abort(404, description="项目不存在")

        task_manager = _get_task_manager()
        upload_dir = _get_upload_dir()
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
        return jsonify({"status": "deleted", "project_id": project_id})

    @app.post("/api/projects")
    def create_project() -> Any:
        payload = request.get_json(silent=True) or {}
        transcript_payload = payload.get("transcript")
        if transcript_payload is None:
            abort(400, description="缺少 transcript 字段")

        try:
            transcript_model = Transcript.model_validate(transcript_payload)
        except ValidationError as exc:
            return jsonify({"error": "transcript 格式不合法", "details": exc.errors()}), 400

        name = (payload.get("name") or transcript_model.language or "未命名项目").strip() or "未命名项目"
        storage_local = _get_storage()
        result = storage_local.create_project(name=name, transcript=transcript_model.model_dump())
        metadata_payload = payload.get("metadata")
        if metadata_payload:
            storage_local.save_metadata(result["id"], metadata_payload)
        return jsonify({"project": result}), 201

    # ------------------------------------------------------------------
    # 转录数据
    # ------------------------------------------------------------------
    @app.get("/api/projects/<int:project_id>/transcript")
    def fetch_transcript(project_id: int) -> Any:
        storage_local = _get_storage()
        version = request.args.get("version", type=int)
        snapshot = storage_local.get_snapshot(project_id, "transcript", version=version)
        if snapshot is None:
            abort(404, description="未找到对应的项目或版本")

        full_param = request.args.get("full", "")
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
        else:
            offset = request.args.get("offset", type=int, default=0)
            limit = request.args.get("limit", type=int)
            transcript_data = _slice_transcript(snapshot.payload, offset, limit)

        return jsonify({
            "project_id": project_id,
            "version": snapshot.version,
            "created_at": snapshot.created_at,
            "size_bytes": size_bytes,
            "transcript": transcript_data,
        })

    @app.post("/api/projects/<int:project_id>/transcript")
    def save_transcript(project_id: int) -> Any:
        payload = request.get_json(silent=True) or {}
        transcript_payload = payload.get("transcript")
        if transcript_payload is None:
            abort(400, description="缺少 transcript 字段")

        try:
            transcript_model = Transcript.model_validate(transcript_payload)
        except ValidationError as exc:
            return jsonify({"error": "transcript 格式不合法", "details": exc.errors()}), 400

        next_version = _get_storage().save_transcript(project_id, transcript_model.model_dump())
        return jsonify({"project_id": project_id, "version": next_version}), 201

    # ------------------------------------------------------------------
    # 选择集（删除计划）管理
    # ------------------------------------------------------------------
    @app.get("/api/projects/<int:project_id>/selection")
    def fetch_selection(project_id: int) -> Any:
        version = request.args.get("version", type=int)
        snapshot = _get_storage().get_snapshot(project_id, "selection", version=version)
        if snapshot is None:
            abort(404, description="未找到 selection 信息")
        return jsonify({
            "project_id": project_id,
            "version": snapshot.version,
            "created_at": snapshot.created_at,
            "selection": snapshot.payload,
        })

    @app.post("/api/projects/<int:project_id>/selection")
    def save_selection(project_id: int) -> Any:
        payload = request.get_json(silent=True) or {}
        delete_ranges_payload = payload.get("delete_ranges")
        if delete_ranges_payload is None:
            abort(400, description="缺少 delete_ranges 字段")

        delete_ranges = _normalize_delete_ranges(delete_ranges_payload)
        metadata = payload.get("metadata") or {}
        selection_payload = {"delete_ranges": delete_ranges, "metadata": metadata}
        version = _get_storage().save_selection(project_id, selection_payload)
        return jsonify({"project_id": project_id, "version": version}), 201

    # ------------------------------------------------------------------
    # 快照列表
    # ------------------------------------------------------------------
    @app.get("/api/projects/<int:project_id>/snapshots")
    def list_snapshots(project_id: int) -> Any:
        storage_local = _get_storage()
        transcript_snapshots = storage_local.list_snapshots(project_id, "transcript")
        selection_snapshots = storage_local.list_snapshots(project_id, "selection")
        return jsonify({
            "transcripts": [snapshot.__dict__ for snapshot in transcript_snapshots],
            "selections": [snapshot.__dict__ for snapshot in selection_snapshots],
        })

    @app.get("/api/projects/<int:project_id>/metadata")
    def fetch_metadata(project_id: int) -> Any:
        metadata = _get_storage().get_metadata(project_id)
        if metadata is None:
            return jsonify({"metadata": None}), 200
        return jsonify({"metadata": metadata})

    @app.post("/api/projects/<int:project_id>/metadata")
    def save_metadata(project_id: int) -> Any:
        payload = request.get_json(silent=True) or {}
        if not payload:
            abort(400, description="metadata 不能为空")
        version = _get_storage().save_metadata(project_id, payload)
        return jsonify({"project_id": project_id, "version": version}), 201

    # ------------------------------------------------------------------
    # 工程文件
    # ------------------------------------------------------------------
    @app.get("/api/project-files")
    def list_project_files() -> Any:
        project_id = request.args.get("project_id", type=int)
        if not project_id:
            abort(400, description="缺少 project_id")
        storage_local = _get_storage()
        files = storage_local.list_project_files(project_id)
        return jsonify({
            "project_id": project_id,
            "files": [file.to_dict() for file in files],
        })

    @app.get("/api/project-files/<int:file_id>")
    def fetch_project_file(file_id: int) -> Any:
        storage_local = _get_storage()
        project_file = storage_local.get_project_file(file_id)
        if project_file is None:
            abort(404, description="未找到工程文件")
        return jsonify({"file": project_file.to_dict()})

    @app.post("/api/project-files")
    def create_project_file() -> Any:
        payload = request.get_json(silent=True) or {}
        project_id = payload.get("project_id")
        if not isinstance(project_id, int):
            abort(400, description="缺少 project_id")
        name = (payload.get("name") or "").strip()
        if not name:
            abort(400, description="工程文件名称不能为空")
        selection_payload = payload.get("selection") or {}
        if not isinstance(selection_payload, dict):
            abort(400, description="selection 必须为对象")
        storage_local = _get_storage()
        project_file = storage_local.create_project_file(project_id, name, selection_payload)
        # 同步生成一份 selection 快照，保证现有流程兼容
        storage_local.save_selection(project_id, selection_payload)
        return jsonify({"file": project_file.to_dict()}), 201

    @app.post("/api/project-files/<int:file_id>/save")
    def save_project_file(file_id: int) -> Any:
        payload = request.get_json(silent=True) or {}
        selection_payload = payload.get("selection")
        if not isinstance(selection_payload, dict):
            abort(400, description="selection 必须为对象")
        name = payload.get("name")
        if name is not None:
            if not isinstance(name, str):
                abort(400, description="name 必须为字符串")
            name = name.strip()
            if not name:
                abort(400, description="工程文件名称不能为空")
        storage_local = _get_storage()
        project_file = storage_local.get_project_file(file_id)
        if project_file is None:
            abort(404, description="未找到工程文件")
        updated = storage_local.update_project_file(
            file_id,
            selection_payload,
            name=name,
        )
        if updated is None:
            abort(404, description="未找到工程文件")
        storage_local.save_selection(project_file.project_id, selection_payload)
        return jsonify({"file": updated.to_dict()}), 200

    @app.get("/api/projects/<int:project_id>/silence")
    def fetch_silence(project_id: int) -> Any:
        snapshot = _get_storage().latest_snapshot(project_id, "silence")
        if snapshot is None:
            return jsonify({"version": None, "candidates": []})
        payload = dict(snapshot.payload)
        payload["version"] = snapshot.version
        payload.setdefault("generated_at", snapshot.created_at)
        return jsonify(payload)

    @app.post("/api/projects/<int:project_id>/silence")
    def compute_silence(project_id: int) -> Any:
        storage_local = _get_storage()
        if storage_local.get_project(project_id) is None:
            abort(404, description="项目不存在")

        transcript_snapshot = storage_local.latest_snapshot(project_id, "transcript")
        if transcript_snapshot is None:
            abort(400, description="项目尚未导入转录")

        try:
            transcript = Transcript.model_validate(transcript_snapshot.payload)
        except ValidationError as exc:
            abort(400, description=f"转录数据无效: {exc}")

        metadata = storage_local.get_metadata(project_id) or {}
        media_path_value = metadata.get("media_path")
        if not media_path_value:
            abort(400, description="项目尚未记录媒体路径")

        media_path = Path(media_path_value)
        if not media_path.exists():
            abort(400, description=f"媒体文件不存在: {media_path}")

        params = request.get_json(silent=True) or {}
        min_duration = float(params.get("min_duration", 1.2))
        fps = float(params.get("fps", 2.0))
        scale = int(params.get("scale", 64))
        ffmpeg_binary = app.config.get("SUBTITLE_CUT_WEB_FFMPEG", "ffmpeg")

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

    @app.get("/api/projects/<int:project_id>/media")
    def fetch_media(project_id: int) -> Any:
        metadata = _get_storage().get_metadata(project_id) or {}
        media_path_value = metadata.get("media_path")
        if not media_path_value:
            abort(404, description="未记录媒体路径")

        media_path = Path(media_path_value)
        if not media_path.exists():
            abort(404, description=f"媒体文件不存在: {media_path}")

        return send_file(media_path, conditional=True)

    @app.get("/api/projects/<int:project_id>/waveform")
    def fetch_waveform(project_id: int) -> Any:
        storage_local = _get_storage()
        requested_version = request.args.get("version", type=int)
        refresh_flag = (request.args.get("refresh") or "").strip().lower()
        refresh = refresh_flag in {"1", "true", "yes"}
        if requested_version is not None and refresh:
            abort(400, description="version 与 refresh 参数不能同时使用")

        if not refresh:
            snapshot = storage_local.get_snapshot(project_id, "waveform", version=requested_version)
            if snapshot:
                return jsonify({
                    "project_id": project_id,
                    "version": snapshot.version,
                    "waveform": snapshot.payload,
                    "cached": True,
                })
            # 指定版本尚未生成时，回退到重新生成逻辑
            requested_version = None

        metadata = storage_local.get_metadata(project_id) or {}
        media_path_value = metadata.get("media_path")
        if not media_path_value:
            abort(404, description="未记录媒体路径")

        media_path = Path(media_path_value)
        if not media_path.exists():
            abort(404, description=f"媒体文件不存在 {media_path}")

        ffmpeg_binary = app.config.get("SUBTITLE_CUT_WEB_FFMPEG", "ffmpeg")
        try:
            waveform_payload = generate_waveform_payload(media_path, ffmpeg_binary=ffmpeg_binary)
        except FileNotFoundError:
            abort(404, description=f"媒体文件不存在 {media_path}")
        except WaveformGenerationError as exc:
            LOGGER.warning("生成波形失败，将返回空波形: %s", exc)
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
    # 错误处理
    # ------------------------------------------------------------------
    @app.errorhandler(400)
    def handle_bad_request(error: Exception) -> Any:
        message = getattr(error, "description", "请求无效")
        return jsonify({"error": message}), 400

    @app.errorhandler(404)
    def handle_not_found(error: Exception) -> Any:
        message = getattr(error, "description", "资源不存在")
        return jsonify({"error": message}), 404

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception) -> Any:
        LOGGER.exception("Web API 发生未预期异常")
        return jsonify({"error": "服务器内部错误"}), 500

    # ------------------------------------------------------------------
    # WhisperX 任务接口
    # ------------------------------------------------------------------
    @app.post("/api/uploads")
    def upload_media() -> Any:
        uploads_dir_local = _get_upload_dir()
        uploaded_file = request.files.get("file")
        if uploaded_file is None or not uploaded_file.filename:
            abort(400, description="缺少文件")

        extension = Path(uploaded_file.filename).suffix.lower()
        filename = f"upload_{uuid.uuid4().hex}{extension}"
        save_path = uploads_dir_local / filename
        uploaded_file.save(save_path)

        response_payload: Dict[str, Any] = {"path": str(save_path)}
        ffmpeg_binary = app.config.get("SUBTITLE_CUT_WEB_FFMPEG", "ffmpeg")
        if extension in AUDIO_EXTENSIONS:
            corrupted, detail = _detect_audio_corruption(save_path, ffmpeg_binary)
            if corrupted:
                LOGGER.warning("检测到音频包含损坏帧，开始尝试修复: %s", save_path)
                repaired_filename = f"upload_{uuid.uuid4().hex}.wav"
                repaired_path = uploads_dir_local / repaired_filename
                success, repair_error = _reencode_audio_file(save_path, repaired_path, ffmpeg_binary)
                if success:
                    try:
                        save_path.unlink(missing_ok=True)
                    except Exception as exc:  # pragma: no cover - 清理失败不影响主流程
                        LOGGER.warning("删除原始损坏音频失败 %s: %s", save_path, exc)
                    save_path = repaired_path
                    response_payload["path"] = str(save_path)
                    response_payload["repaired"] = True
                    notice = "检测到音频存在损坏帧，已使用 FFmpeg 重编码生成修复文件。"
                    response_payload["repair_notice"] = notice
                    if detail:
                        response_payload["repair_detail"] = detail
                    LOGGER.info("音频修复完成，已替换为修复版本: %s", save_path)
                else:
                    response_payload["repaired"] = False
                    response_payload["repair_notice"] = "检测到音频存在损坏帧，自动修复失败，请检查原始文件。"
                    if repair_error:
                        response_payload["repair_detail"] = repair_error
                    LOGGER.warning("音频修复失败: %s %s", save_path, repair_error or "")
        return jsonify(response_payload), 201

    @app.post("/api/tasks/transcribe")
    def submit_transcribe() -> Any:
        payload = request.get_json(silent=True) or {}
        media_path_value = payload.get("media_path")
        if not media_path_value:
            abort(400, description="缺少 media_path")
        media_path = Path(media_path_value)
        if not media_path.exists():
            abort(404, description="指定的媒体文件不存在")

        engine_value = (payload.get("engine") or "whisperx").strip().lower()
        if engine_value not in {"whisperx", "paraformer"}:
            abort(400, description="engine 取值必须为 whisperx 或 paraformer")

        model = str(payload.get("model", "large-v2"))
        device = str(payload.get("device", "auto"))

        task_state = _get_task_manager().submit_transcribe(
            media_path,
            engine=engine_value,
            model=model,
            device=device,
        )
        return jsonify({"task_id": task_state.id, "status": task_state.status}), 202

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> Any:
        task_state = _get_task_manager().get_task(task_id)
        if not task_state:
            abort(404, description="未找到任务")
        response = {
            "task_id": task_state.id,
            "status": task_state.status,
            "message": task_state.message,
            "progress": task_state.progress,
            "metadata": task_state.metadata,
        }
        if task_state.result:
            response["result"] = task_state.result
        return jsonify(response)

    @app.post("/api/tasks/cut")
    def submit_cut() -> Any:
        payload = request.get_json(silent=True) or {}
        project_id = payload.get("project_id")
        if project_id is None:
            abort(400, description="缺少 project_id")
        try:
            project_id = int(project_id)
        except ValueError as exc:
            abort(400, description="project_id 必须为整数")

        input_path_value = payload.get("input_path")
        if not input_path_value:
            abort(400, description="缺少 input_path")
        input_path = Path(input_path_value)
        if not input_path.exists():
            abort(404, description="输入视频不存在")

        storage_local = _get_storage()
        transcript_snapshot = storage_local.get_snapshot(project_id, "transcript")
        if transcript_snapshot is None:
            abort(404, description="项目尚未导入转录")

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
        keep_ranges = derive_keep_ranges(transcript_model, delete_time_ranges)
        if not keep_ranges:
            keep_ranges = invert_ranges(total_duration, delete_time_ranges)
        keep_tuples = [(round(rng.start, 6), round(rng.end, 6)) for rng in keep_ranges]
        if not keep_tuples:
            abort(400, description="无保留区间，无法剪辑")

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
            abort(400, description="xfade_ms 必须为数值")
        if xfade_ms < 0.0:
            xfade_ms = 0.0

        chunk_size_raw = payload.get("chunk_size", 20)
        try:
            chunk_size = int(chunk_size_raw)
        except (TypeError, ValueError):
            chunk_size = 20
        if chunk_size < 1:
            chunk_size = 1

        task_state = _get_task_manager().submit_cut(
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
        return jsonify({"task_id": task_state.id, "status": task_state.status}), 202

    @app.get("/api/common-fillers")
    def get_common_fillers() -> Any:
        words = _load_filler_words_file()
        return jsonify({"words": words})

    @app.post("/api/common-fillers")
    def update_common_fillers() -> Any:
        payload = request.get_json(silent=True) or {}
        words = payload.get("words", [])
        if not isinstance(words, list):
            return jsonify({"error": "无效的水词列表"}), 400
        normalized: list[str] = []
        for item in words:
            text = str(item or "").strip()
            if text:
                normalized.append(text)
        try:
            filler_path.write_text("\n".join(normalized), encoding="utf-8")
        except OSError:
            LOGGER.exception("failed to persist filler words")
            return jsonify({"error": "保存常用水词失败"}), 500
        return jsonify({"words": normalized})

    @app.post("/api/projects/<int:project_id>/export/srt")
    def export_project_srt(project_id: int) -> Any:
        payload = request.get_json(silent=True) or {}
        storage_local = _get_storage()
        snapshot = storage_local.get_snapshot(project_id, "transcript")
        if snapshot is None:
            abort(404, description="项目尚未生成转录")

        try:
            transcript_model = Transcript.model_validate(snapshot.payload)
        except ValidationError as exc:
            return jsonify({"error": "转录数据无效", "details": exc.errors()}), 400

        base_name = (payload.get("output_name") or "").strip()
        manager = _get_task_manager()
        stem = manager.resolve_export_stem(base_name, project_id)
        export_path = manager.exports_dir / f"{stem}.srt"
        dump_srt(transcript_model, export_path)
        LOGGER.info("exported srt for project %s -> %s", project_id, export_path)
        return jsonify({"status": "ok", "output_path": str(export_path), "file_name": f"{stem}.srt"})

    return app


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _get_storage() -> ProjectStorage:
    from flask import current_app

    storage = current_app.config.get("SUBTITLE_CUT_STORAGE")
    if storage is None:
        raise RuntimeError("存储尚未初始化")
    return storage


def _get_task_manager() -> TaskManager:
    from flask import current_app

    manager = current_app.config.get("SUBTITLE_CUT_TASK_MANAGER")
    if manager is None:
        raise RuntimeError("任务管理器尚未初始化")
    return manager


def _get_upload_dir() -> Path:
    from flask import current_app

    upload_dir = current_app.config.get("SUBTITLE_CUT_UPLOAD_DIR")
    if upload_dir is None:
        raise RuntimeError("上传目录尚未初始化")
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
            LOGGER.warning("删除文件失败 %s: %s", path, exc)
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
        abort(400, description="delete_ranges 必须是数组")

    normalized: list[dict[str, float]] = []
    for entry in raw_ranges:
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError) as exc:
            abort(400, description=f"delete_ranges 项格式错误: {entry}")
        if end <= start:
            continue
        normalized.append({"start": start, "end": end})

    if not normalized:
        abort(400, description="delete_ranges 不能为空")

    # 归并重叠段，保持有序
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
    app.run(debug=True)
