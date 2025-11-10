"""基于时间的音频分块多线程剪辑工具

核心思路:
1. 按时间将整个音频分成N个时间段(如每15分钟一段)
2. 每个时间段独立处理其范围内的保留片段
3. 多线程并行处理所有时间段
4. 最后拼接所有结果

优势:
- 每个线程只处理少量片段,filter_complex很小
- 充分利用多核CPU
- 避免单线程处理海量片段导致的性能瓶颈
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, List, Optional, Sequence, Tuple

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


def _execute_time_based_audio_chunked_cut(
    input_path: PathLike,
    output_path: PathLike,
    keep_list: Sequence[TimeRange],
    *,
    total_duration: float,
    ffmpeg_binary: str,
    audio_profile: Tuple[str, Tuple[str, ...], Tuple[str, ...]],
    progress_callback: Callable[[float], None] | None,
    progress_start: float,
    progress_span: float,
    max_chunk_duration: float = 900.0,  # 每个分块最多15分钟
) -> None:
    """基于时间的音频分块多线程剪辑
    
    策略:
    1. 获取输入文件的总时长
    2. 按时间分段(如每15分钟一段)
    3. 每段独立处理其范围内的保留片段
    4. 多线程并行处理
    5. 拼接结果
    
    Args:
        input_path: 输入音频文件
        output_path: 输出音频文件
        keep_list: 保留的时间区间列表
        total_duration: 保留区间的总时长
        ffmpeg_binary: FFmpeg可执行文件路径
        audio_profile: 音频编码配置
        progress_callback: 进度回调
        progress_start: 进度起始值
        progress_span: 进度跨度
        max_chunk_duration: 每个分块的最大时长(秒)
    """
    if total_duration <= 0.0:
        raise ValueError("无有效保留区间时长")
    
    if not keep_list:
        raise ValueError("保留区间列表为空")

    def _emit(fraction: float) -> None:
        if progress_callback is None:
            return
        progress_callback(
            progress_start + progress_span * max(0.0, min(1.0, fraction))
        )

    # 1. 获取输入文件的总时长
    import subprocess
    probe_cmd = [
        ffmpeg_binary,
        "-i", str(input_path),
        "-hide_banner",
    ]
    result = subprocess.run(
        probe_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stderr + result.stdout
    
    # 解析时长
    import re
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", output)
    if not duration_match:
        LOGGER.warning("无法获取输入文件时长,使用最后一个区间的结束时间")
        input_total_duration = max(end for _, end in keep_list)
    else:
        hours = int(duration_match.group(1))
        minutes = int(duration_match.group(2))
        seconds = float(duration_match.group(3))
        input_total_duration = hours * 3600 + minutes * 60 + seconds
    
    LOGGER.info(
        "Input file duration: %.2f seconds (%.1f minutes)",
        input_total_duration,
        input_total_duration / 60,
    )

    # 2. 按时间分段
    # 计算需要多少个分块
    num_chunks = max(2, int(input_total_duration / max_chunk_duration) + 1)
    chunk_duration = input_total_duration / num_chunks
    
    LOGGER.info(
        "Splitting into %d time-based chunks, ~%.1f minutes per chunk",
        num_chunks,
        chunk_duration / 60,
    )

    # 3. 为每个时间段分配保留区间
    chunks: List[dict] = []
    for i in range(num_chunks):
        chunk_start = i * chunk_duration
        chunk_end = (i + 1) * chunk_duration if i < num_chunks - 1 else input_total_duration
        
        # 找出这个时间段内的所有保留区间
        chunk_ranges = []
        for start, end in keep_list:
            # 如果区间与当前时间段有交集
            if end > chunk_start and start < chunk_end:
                # 裁剪区间到当前时间段内
                clipped_start = max(start, chunk_start)
                clipped_end = min(end, chunk_end)
                if clipped_end > clipped_start:
                    # 转换为相对于分块起始的时间
                    relative_start = clipped_start - chunk_start
                    relative_end = clipped_end - chunk_start
                    chunk_ranges.append((clipped_start, clipped_end, relative_start, relative_end))
        
        if chunk_ranges:
            chunk_keep_duration = sum(end - start for _, _, start, end in chunk_ranges)
            chunks.append({
                "index": i,
                "time_start": chunk_start,
                "time_end": chunk_end,
                "ranges": chunk_ranges,  # (原始start, 原始end, 相对start, 相对end)
                "duration": chunk_keep_duration,
            })
            LOGGER.info(
                "Chunk %d: time [%.1f-%.1f]s, %d ranges, %.1f seconds to keep",
                i,
                chunk_start,
                chunk_end,
                len(chunk_ranges),
                chunk_keep_duration,
            )

    if not chunks:
        raise ValueError("无任何可执行的剪辑子任务")

    LOGGER.info(
        "Time-based chunked cut: %d chunks, %d ranges total",
        len(chunks),
        len(keep_list),
    )

    # 4. 准备并行处理
    chunk_progress = [0.0 for _ in chunks]
    output_files: List[Optional[Path]] = [None for _ in chunks]
    progress_lock = threading.Lock()

    def update_progress_locked() -> None:
        aggregated = sum(
            chunks[i]["duration"] * chunk_progress[i]
            for i in range(len(chunks))
        )
        fraction = aggregated / total_duration if total_duration > 0.0 else 1.0
        _emit(fraction)

    _emit(0.0)

    # 5. 确定并行度
    # 策略：使用较少的worker避免I/O竞争
    # 每个分块都需要从原始文件seek，过多并行会导致I/O竞争
    cpu_count = os.cpu_count() or 1
    
    # 根据输入文件时长动态调整并行度
    if input_total_duration > 7200:  # 超过2小时
        max_parallel = min(len(chunks), max(2, cpu_count // 4))
    elif input_total_duration > 3600:  # 1-2小时
        max_parallel = min(len(chunks), max(2, cpu_count // 3))
    else:
        max_parallel = min(len(chunks), max(2, cpu_count // 2))
    
    # 限制最大并行数，避免过度并行
    max_parallel = max(2, min(max_parallel, 3))
    
    LOGGER.info(
        "Using %d parallel workers for time-based audio encoding (input_duration=%.1f min)",
        max_parallel,
        input_total_duration / 60,
    )

    # 6. 定义处理单个时间分块的函数
    def process_time_chunk(chunk_info: dict) -> Tuple[int, Path]:
        chunk_index = chunk_info["index"]
        time_start = chunk_info["time_start"]
        time_end = chunk_info["time_end"]
        ranges = chunk_info["ranges"]
        duration = chunk_info["duration"]
        
        container, codec_args, post_args = audio_profile
        
        from tempfile import NamedTemporaryFile
        temp_output = Path(
            NamedTemporaryFile(
                suffix=f".{container}",
                prefix=f"tmp_audio_time_chunk_{chunk_index}_",
                delete=False,
            ).name
        )
        
        try:
            # 先提取这个时间段
            temp_segment = Path(
                NamedTemporaryFile(
                    suffix=f".{container}",
                    prefix=f"tmp_segment_{chunk_index}_",
                    delete=False,
                ).name
            )
            
            try:
                # 提取时间段
                # 限制线程数以减少资源竞争
                extract_cmd = [
                    "-threads", "2",
                    "-ss", str(time_start),
                    "-t", str(time_end - time_start),
                    "-i", str(input_path),
                    "-c", "copy",
                    str(temp_segment),
                ]
                
                LOGGER.info(
                    "Extracting time segment %d: [%.1f-%.1f]s",
                    chunk_index,
                    time_start,
                    time_end,
                )
                
                run_ffmpeg(extract_cmd, binary=ffmpeg_binary)
                
                # 在提取的片段上应用保留区间
                # 构建filter_complex
                if len(ranges) == 1:
                    # 只有一个区间,直接裁剪
                    _, _, rel_start, rel_end = ranges[0]
                    command = [
                        "-i", str(temp_segment),
                        "-af", f"atrim=start={rel_start}:end={rel_end},asetpts=PTS-STARTPTS",
                    ]
                else:
                    # 多个区间,需要拼接
                    filter_parts = []
                    for idx, (_, _, rel_start, rel_end) in enumerate(ranges):
                        filter_parts.append(
                            f"[0:a]atrim=start={rel_start}:end={rel_end},asetpts=PTS-STARTPTS[a{idx}]"
                        )
                    
                    # 拼接
                    concat_inputs = "".join(f"[a{idx}]" for idx in range(len(ranges)))
                    filter_parts.append(f"{concat_inputs}concat=n={len(ranges)}:v=0:a=1[aout]")
                    
                    filter_script = Path(
                        NamedTemporaryFile(
                            mode='w',
                            encoding='utf-8',
                            suffix='.txt',
                            prefix=f'filter_time_chunk_{chunk_index}_',
                            delete=False,
                        ).name
                    )
                    
                    try:
                        filter_script.write_text("\n".join(filter_parts), encoding='utf-8')
                        
                        command = [
                            "-i", str(temp_segment),
                            "-filter_complex_script", str(filter_script),
                            "-map", "[aout]",
                        ]
                    finally:
                        try:
                            filter_script.unlink(missing_ok=True)
                        except OSError:
                            pass
                
                if container:
                    command.extend(["-f", container])
                command.extend(codec_args)
                if post_args:
                    command.extend(post_args)
                command.append(str(temp_output))
                
                # 进度回调
                progress_duration = duration if duration > 0.0 else (time_end - time_start)
                LOGGER.info(
                    "Processing time chunk %d/%d: %d ranges, %.2fs duration",
                    chunk_index + 1,
                    len(chunks),
                    len(ranges),
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
                    temp_segment.unlink(missing_ok=True)
                except OSError:
                    pass
        
        except Exception:
            try:
                temp_output.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # 7. 并行处理所有时间分块
    futures: list[Future] = []
    executor = ThreadPoolExecutor(max_workers=max_parallel)
    try:
        for chunk_info in chunks:
            futures.append(executor.submit(process_time_chunk, chunk_info))
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

    # 8. 拼接所有分块
    LOGGER.info("Concatenating %d time-based audio chunks", len(output_files))
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
