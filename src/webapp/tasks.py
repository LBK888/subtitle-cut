"""后台任务接口（WhisperX 转录）。"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import re
import shutil
import string
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set

from ..asr.transcribe import transcribe_to_json
from ..core.schema import Transcript
from ..core.transform import TimeRange, rebase_transcript_after_cuts
from ..ffmpeg.cutter import cut_video
from ..webapp.storage import ProjectStorage


LOGGER = logging.getLogger(__name__)


def _merge_time_ranges(ranges: list[TimeRange]) -> list[TimeRange]:
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda rng: rng.start)
    merged: list[TimeRange] = []
    current = sorted_ranges[0]
    for rng in sorted_ranges[1:]:
        if rng.start <= current.end:
            current = TimeRange(start=current.start, end=max(current.end, rng.end))
        else:
            merged.append(current)
            current = rng
    merged.append(current)
    return merged


def _get_available_physical_memory() -> Optional[int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        result = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except AttributeError:
        return None
    if not result:
        return None
    return int(status.ullAvailPhys)


@dataclass
class TaskState:
    id: str
    status: str = "pending"
    message: str = ""
    progress: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None


class TaskManager:
    """简易后台任务管理器（单机）。"""

    def __init__(self, storage: ProjectStorage, working_dir: Path, exports_dir: Optional[Path] = None) -> None:
        self.storage = storage
        self.working_dir = working_dir
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir = exports_dir or (working_dir / "exports")
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: Dict[str, TaskState] = {}
        self.lock = threading.Lock()

    def submit_transcribe(
        self,
        media_path: Path,
        engine: str,
        model: str,
        device: str,
    ) -> TaskState:
        task_id = uuid.uuid4().hex
        state = TaskState(id=task_id, status="queued")
        with self.lock:
            self.tasks[task_id] = state
        LOGGER.info(
            "queued asr task %s (engine=%s, model=%s, device=%s) for %s",
            task_id,
            engine,
            model,
            device,
            media_path,
        )

        thread = threading.Thread(
            target=self._run_transcribe_task,
            args=(state, media_path, engine, model, device),
            daemon=True,
        )
        thread.start()
        return state

    def get_task(self, task_id: str) -> Optional[TaskState]:
        with self.lock:
            return self.tasks.get(task_id)

    def cleanup_project(self, project_id: int) -> Set[Path]:
        file_paths: Set[Path] = set()
        with self.lock:
            to_remove: list[str] = []
            for task_id, state in list(self.tasks.items()):
                metadata_project_id = state.metadata.get("project_id")
                if metadata_project_id == project_id:
                    to_remove.append(task_id)
                    file_paths.update(self._collect_state_paths(state))
            for task_id in to_remove:
                LOGGER.info("removing cached task %s for project %s", task_id, project_id)
                self.tasks.pop(task_id, None)
        return file_paths

    def _collect_state_paths(self, state: TaskState) -> Set[Path]:
        paths: Set[Path] = set()
        roots = (self.working_dir.resolve(), self.exports_dir.resolve())
        for payload in (state.metadata, state.result):
            if not payload:
                continue
            for path in self._collect_paths_from_mapping(payload):
                resolved = path.resolve()
                if any(self._is_within_root(resolved, root) for root in roots):
                    paths.add(resolved)
        return paths

    @staticmethod
    def _collect_paths_from_mapping(value: Any) -> Set[Path]:
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
                paths.update(TaskManager._collect_paths_from_mapping(item))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                paths.update(TaskManager._collect_paths_from_mapping(item))
        return paths

    @staticmethod
    def _is_within_root(path: Path, root: Path) -> bool:
        destination: Optional[Path] = None
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @contextlib.contextmanager
    def _stage_input_on_ramdisk(self, source: Path, state: TaskState) -> Iterator[Path]:
        imdisk_exe = shutil.which("imdisk")
        if not imdisk_exe:
            yield source
            return

        try:
            file_size = source.stat().st_size
        except OSError:
            yield source
            return

        available = _get_available_physical_memory()
        overhead = max(file_size // 5, 256 * 1024 * 1024)
        required = file_size + overhead
        if available is not None and available < required:
            LOGGER.info("available memory insufficient for RAM disk staging (need=%s, available=%s)", required, available)
            yield source
            return

        mount_letter: Optional[str] = None
        for letter in reversed(string.ascii_uppercase):
            if letter in {"A", "B"}:
                continue
            drive = Path(f"{letter}:\\")
            if not drive.exists():
                mount_letter = letter
                break
        if mount_letter is None:
            LOGGER.warning("no free drive letter available for RAM disk staging")
            yield source
            return

        mount_point = f"{mount_letter}:"
        mount_root = Path(f"{mount_letter}:\\")
        size_mb = max(64, int((required + (1024 * 1024 - 1)) // (1024 * 1024)))
        create_cmd = [
            imdisk_exe,
            "-a",
            "-s",
            f"{size_mb}M",
            "-m",
            mount_point,
            "-p",
            "/fs:ntfs /q /y",
        ]
        result = subprocess.run(create_cmd, capture_output=True)
        if result.returncode != 0:
            LOGGER.warning(
                "failed to create RAM disk via imdisk: %s",
                result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown error",
            )
            yield source
            return

        try:
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if mount_root.exists():
                    break
                time.sleep(0.1)
            else:
                LOGGER.warning("RAM disk mount point %s did not appear in time", mount_root)
                yield source
                return

            destination = mount_root / source.name
            LOGGER.info("staging input %s to RAM disk %s", source, destination)
            state.message = "正在将源视频加载到内存..."
            shutil.copy2(source, destination)
            state.metadata["ramdisk_used"] = True
            state.metadata["ramdisk_mount"] = mount_point
            state.metadata["ramdisk_path"] = str(destination)
            yield destination
        finally:
            if destination is not None:
                try:
                    destination.unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning("failed to remove staged file %s: %s", destination, exc)
            state.metadata.pop("ramdisk_path", None)
            state.metadata.pop("ramdisk_mount", None)
            state.metadata["ramdisk_used"] = False
            try:
                detach_cmd = [imdisk_exe, "-D", "-m", mount_point]
                for attempt in range(3):
                    result = subprocess.run(detach_cmd, capture_output=True, check=False)
                    if result.returncode == 0:
                        break
                    LOGGER.warning(
                        "attempt %s to detach RAM disk %s failed (exit=%s): %s",
                        attempt + 1,
                        mount_point,
                        result.returncode,
                        result.stderr.decode("utf-8", errors="replace") if result.stderr else "",
                    )
                    time.sleep(0.5)
                else:
                    LOGGER.error("unable to detach RAM disk %s after retries", mount_point)
            except Exception:
                LOGGER.exception("failed to detach RAM disk %s", mount_point)

    def _export_exists(self, stem: str) -> bool:
        suffixes = (".mp4", ".srt", ".vtt")
        for ext in suffixes:
            if (self.exports_dir / f"{stem}{ext}").exists():
                return True
        return False

    def resolve_export_stem(self, base_name: str, project_id: int) -> str:
        base = (base_name or "").strip()
        if not base:
            base = f"project_{project_id}"
        base = Path(base).stem
        base = re.sub(r"[\\/:*?\"<>|]+", "_", base).strip(" ._")
        if not base:
            base = f"project_{project_id}"

        candidate = base
        counter = 1
        while self._export_exists(candidate):
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def _run_transcribe_task(
        self,
        state: TaskState,
        media_path: Path,
        engine: str,
        model: str,
        device: str,
    ) -> None:
        engine_key = (engine or "whisperx").lower()
        engine_label = "WhisperX" if engine_key == "whisperx" else "Paraformer"

        state.status = "running"
        state.message = f"正在执行 {engine_label} 转录"
        state.metadata = {
            "engine": engine_key,
            "engine_label": engine_label,
            "model": model,
            "device": device,
        }
        state.progress = 0.05
        try:
            LOGGER.info(
                "task %s starting asr (engine=%s, model=%s, device=%s, media=%s)",
                state.id,
                engine_label,
                model,
                device,
                media_path,
            )

            def _update_transcribe_progress(fraction: float) -> None:
                clamped = max(0.0, min(1.0, fraction))
                state.progress = max(state.progress, min(0.95, clamped))

            transcript = transcribe_to_json(
                media_path,
                engine=engine_key,
                model=model,
                device=device,
                progress_callback=_update_transcribe_progress,
            )
            output_path = self.working_dir / f"transcript_{state.id}.json"
            output_path.write_text(
                json.dumps(transcript.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            state.progress = max(state.progress, 0.98)
            state.progress = 1.0
            state.status = "completed"
            state.result = {
                "transcript": transcript.model_dump(),
                "output_path": str(output_path),
                "media_path": str(media_path),
            }
            state.message = f"{engine_label} 转录完成"
            LOGGER.info(
                "task %s completed asr (engine=%s) -> %s",
                state.id,
                engine_label,
                output_path,
            )
        except Exception as exc:
            state.status = "failed"
            state.message = str(exc)
            LOGGER.exception(
                "task %s asr failed (engine=%s)",
                state.id,
                engine_label,
            )

    def submit_cut(
        self,
        project_id: int,
        input_path: Path,
        keep_ranges: list[tuple[float, float]],
        transcript_payload: Dict[str, Any],
        delete_ranges: list[dict[str, float]],
        base_name: str,
        reencode: str,
        snap_zero_cross: bool,
        xfade_ms: float,
        chunk_size: int,
    ) -> TaskState:
        task_id = uuid.uuid4().hex
        state = TaskState(id=task_id, status="queued")
        state.metadata = {
            "type": "cut",
            "project_id": project_id,
            "input_path": str(input_path),
            "snap_zero_cross": snap_zero_cross,
            "xfade_ms": xfade_ms,
            "chunk_size": chunk_size,
            "ramdisk_used": False,
        }
        with self.lock:
            self.tasks[task_id] = state
        LOGGER.info(
            "queued cut task %s for project %s, input=%s", task_id, project_id, input_path
        )

        thread = threading.Thread(
            target=self._run_cut_task,
            args=(
                state,
                project_id,
                input_path,
                keep_ranges,
                transcript_payload,
                delete_ranges,
                base_name,
                reencode,
                snap_zero_cross,
                xfade_ms,
                chunk_size,
            ),
            daemon=True,
        )
        thread.start()
        return state

    def _run_cut_task(
        self,
        state: TaskState,
        project_id: int,
        input_path: Path,
        keep_ranges: list[tuple[float, float]],
        transcript_payload: Dict[str, Any],
        delete_ranges: list[dict[str, float]],
        base_name: str,
        reencode: str,
        snap_zero_cross: bool,
        xfade_ms: float,
        chunk_size: int,
    ) -> None:
        state.status = "running"
        state.message = "正在剪辑视频"
        state.progress = 0.1

        try:
            output_stem = self.resolve_export_stem(base_name, project_id)
            state.metadata["output_stem"] = output_stem
            output_video = self.exports_dir / f"{output_stem}.mp4"

            keep_list = [
                (
                    round(max(0.0, start), 6),
                    round(max(0.0, end), 6),
                )
                for start, end in keep_ranges
                if end > start
            ]
            keep_list.sort(key=lambda item: item[0])
            deduped: list[list[float]] = []
            for rng in keep_list:
                if not deduped:
                    deduped.append(list(rng))
                    continue
                prev_start, prev_end = deduped[-1]
                start, end = rng
                if abs(start - prev_start) < 1e-4 and abs(end - prev_end) < 1e-4:
                    continue
                if start <= prev_end:
                    deduped[-1][1] = max(prev_end, end)
                else:
                    deduped.append(list(rng))
            keep_list = [(start, end) for start, end in deduped]
            state.progress = max(state.progress, 0.15)

            LOGGER.info("cut task %s keep ranges: %s", state.id, keep_list)
            if not keep_list:
                raise ValueError("无有效保留区间，无法执行剪辑")

            def _update_cut_progress(fraction: float) -> None:
                clamped = max(0.0, min(1.0, fraction))
                state.progress = max(state.progress, min(0.9, 0.1 + 0.75 * clamped))

            with self._stage_input_on_ramdisk(input_path, state) as staged_input:
                state.message = "正在剪辑视频"
                cut_video(
                    staged_input,
                    output_video,
                    keep_list,
                    reencode=reencode,
                    snap_zero_cross=snap_zero_cross,
                    xfade_ms=xfade_ms,
                    chunk_size=chunk_size,
                    progress_callback=_update_cut_progress,
                )
            state.progress = max(state.progress, 0.92)

            transcript_model = Transcript.model_validate(transcript_payload)
            time_ranges = _merge_time_ranges(
                [
                    TimeRange(start=round(item["start"], 6), end=round(item["end"], 6))
                    for item in delete_ranges
                ]
            )
            rebased = rebase_transcript_after_cuts(transcript_model, time_ranges)
            state.progress = max(state.progress, 0.95)

            outputs: Dict[str, Any] = {
                "output_video": str(output_video),
                "rebased_transcript": rebased.model_dump(),
            }

            state.progress = 1.0
            state.status = "completed"
            state.result = outputs
            state.message = "剪辑完成"
            LOGGER.info("task %s cut completed -> %s", state.id, output_video)
        except Exception as exc:
            state.status = "failed"
            state.message = str(exc)
            LOGGER.exception("task %s cut failed", state.id)
