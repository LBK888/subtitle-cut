"""音频分块多线程剪辑工具"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, List, Optional, Sequence, Tuple, Union

from .cutter import (
    FilterPlan,
    PathLike,
    TimeRange,
    _AUDIO_OUTPUT_PROFILES,
    _DEFAULT_AUDIO_PROFILE,
    _create_filter_plan,
)
from .utils import run_ffmpeg

LOGGER = logging.getLogger(__name__)


def _execute_audio_chunked_cut(
    input_path: PathLike,
    output_path: PathLike,
    keep_list: Sequence[TimeRange],
    *,
    chunk_size: int,
    total_duration: float,
    ffmpeg_binary: str,
    audio_profile: Tuple[str, Tuple[str, ...], Tuple[str, ...]],
    progress_callback: Callable[[float], None] | None,
    progress_start: float,
    progress_span: float,
) -> None:
    """音频分块多线程剪辑
    
    Args:
        input_path: 输入音频文件
        output_path: 输出音频文件
        keep_list: 保留的时间区间列表
        chunk_size: 每个分块包含的区间数量
        total_duration: 总时长
        ffmpeg_binary: FFmpeg可执行文件路径
        audio_profile: 音频编码配置
        progress_callback: 进度回调
        progress_start: 进度起始值
        progress_span: 进度跨度
    """
    if chunk_size < 2:
        raise ValueError("chunk_size must be at least 2 for chunked execution")
    if total_duration <= 0.0:
        raise ValueError("无有效保留区间时长")

    def _emit(fraction: float) -> None:
        if progress_callback is None:
            return
        progress_callback(
            progress_start + progress_span * max(0.0, min(1.0, fraction))
        )

    # 1. 分块
    chunks: list[dict[str, object]] = []
    index = 0
    while index < len(keep_list):
        group = keep_list[index : index + chunk_size]
        duration = float(sum(max(0.0, end - start) for start, end in group))
        if duration > 0.0:
            chunks.append(
                {"index": len(chunks), "ranges": group, "duration": duration}
            )
        index += chunk_size

    if not chunks:
        raise ValueError("无任何可执行的剪辑子任务")

    LOGGER.info("Audio chunked cut: %d chunks, %d ranges total", len(chunks), len(keep_list))

    chunk_progress = [0.0 for _ in chunks]
    output_files: List[Optional[Path]] = [None for _ in chunks]
    progress_lock = threading.Lock()

    def update_progress_locked() -> None:
        aggregated = sum(
            chunks[i]["duration"] * chunk_progress[i]  # type: ignore[index]
            for i in range(len(chunks))
        )
        fraction = aggregated / total_duration if total_duration > 0.0 else 1.0
        _emit(fraction)

    _emit(0.0)

    # 2. 确定并行度
    cpu_count = os.cpu_count() or 1
    max_parallel = min(len(chunks), max(1, cpu_count - 1))  # 留一个核心给系统
    max_parallel = max(1, max_parallel)
    LOGGER.info("Using %d parallel workers for audio encoding", max_parallel)

    # 3. 处理每个分块
    def process_chunk(entry: dict[str, object]) -> tuple[int, Path]:
        chunk_index = int(entry["index"])  # type: ignore[arg-type]
        ranges = entry["ranges"]  # type: ignore[assignment]
        duration = float(entry["duration"])  # type: ignore[arg-type]
        
        # 创建临时输出文件
        with NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            temp_output = Path(tmp.name)
        
        try:
            plan = _create_filter_plan(
                ranges,  # type: ignore[arg-type]
                reencode="auto",
                xfade_ms=0.0,
                has_video=False,
                has_audio=True,
                frame_rate_expr=None,
            )
            
            try:
                container, codec_args, post_args = audio_profile
                command = [
                    "-i",
                    str(input_path),
                    "-filter_complex_script",
                    str(plan.script_path),
                    "-map",
                    "[aout]",
                ]
                if container:
                    command.extend(["-f", container])
                command.extend(codec_args)
                if post_args:
                    command.extend(post_args)
                command.append(str(temp_output))
                
                # 进度回调
                progress_duration = plan.expected_duration if plan.expected_duration > 0.0 else duration
                LOGGER.info(
                    "Processing audio chunk %d/%d: duration=%.2fs, expected_duration=%.2fs",
                    chunk_index + 1,
                    len(chunks),
                    duration,
                    progress_duration,
                )
                
                def on_local(value: float) -> None:
                    clamped = max(0.0, min(1.0, value))
                    with progress_lock:
                        chunk_progress[chunk_index] = max(chunk_progress[chunk_index], clamped)
                        update_progress_locked()
                
                run_ffmpeg(
                    command,
                    binary=ffmpeg_binary,
                    progress_callback=on_local if progress_callback is not None else None,
                    progress_duration=progress_duration,
                )
                
                with progress_lock:
                    chunk_progress[chunk_index] = 1.0
                    update_progress_locked()
                
                return chunk_index, temp_output
            finally:
                try:
                    plan.script_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            try:
                temp_output.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # 4. 并行处理
    futures: list[Future] = []
    executor = ThreadPoolExecutor(max_workers=max_parallel)
    try:
        for item in chunks:
            futures.append(executor.submit(process_chunk, item))
        for future in as_completed(futures):
            idx, temp_file = future.result()
            output_files[idx] = temp_file
    except Exception:
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    _emit(1.0)

    # 5. 拼接所有分块
    LOGGER.info("Concatenating %d audio chunks", len(output_files))
    try:
        _concat_audio_files(output_files, Path(output_path), ffmpeg_binary)  # type: ignore[arg-type]
    finally:
        # 清理临时文件
        for temp_file in output_files:
            if temp_file:
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError:
                    pass


def _concat_audio_files(
    input_files: Sequence[Path],
    output_path: Path,
    ffmpeg_binary: str,
) -> None:
    """拼接多个音频文件"""
    with NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as f:
        for file in input_files:
            abs_path = file.resolve()
            # 转义路径中的特殊字符
            escaped = str(abs_path).replace("\\", "/")
            f.write(f"file '{escaped}'\n")
        concat_file = Path(f.name)
    
    try:
        command = [
            "-fflags", "+genpts",  # 重新生成时间戳,消除DTS警告
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output_path),
        ]
        
        run_ffmpeg(command, binary=ffmpeg_binary)
        LOGGER.info("Audio concatenation completed: %s", output_path)
    finally:
        try:
            concat_file.unlink(missing_ok=True)
        except OSError:
            pass
