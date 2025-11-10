"""优化的音频剪辑器 - 专为长音频文件设计

核心策略：
1. 先将输入文件物理分割成多个小文件（如每30分钟一个）
2. 每个小文件独立处理其范围内的保留片段
3. 多线程并行处理所有小文件
4. 最后拼接结果

优势：
- 避免多个进程同时从同一个大文件seek导致的I/O竞争
- 每个进程处理小文件，seek速度快
- 充分利用多核CPU
- 适合超长音频文件（如2小时以上）
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Callable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)


def optimized_cut_audio(
    input_path: Path,
    output_path: Path,
    keep_ranges: Sequence[Tuple[float, float]],
    *,
    ffmpeg_binary: str = "ffmpeg",
    audio_codec: str = "libmp3lame",
    audio_bitrate: str = "192k",
    progress_callback: Optional[Callable[[float], None]] = None,
    split_duration: float = 1800.0,  # 每30分钟分割一次
) -> None:
    """优化的音频剪辑 - 专为长音频文件设计
    
    策略：
    1. 先物理分割输入文件为多个小文件
    2. 每个小文件独立处理其范围内的保留片段
    3. 多线程并行处理
    4. 最后拼接结果
    
    Args:
        input_path: 输入音频文件
        output_path: 输出音频文件
        keep_ranges: 保留的时间区间 [(start, end), ...]
        ffmpeg_binary: FFmpeg可执行文件路径
        audio_codec: 音频编码器
        audio_bitrate: 音频码率
        progress_callback: 进度回调函数
        split_duration: 分割时长（秒）
    """
    if not keep_ranges:
        raise ValueError("保留区间列表为空")
    
    total_duration = sum(end - start for start, end in keep_ranges)
    if total_duration <= 0:
        raise ValueError("保留区间总时长为0")
    
    LOGGER.info(
        "Optimized audio cut: %d ranges, total %.2f seconds (%.1f minutes)",
        len(keep_ranges),
        total_duration,
        total_duration / 60,
    )
    
    def emit_progress(fraction: float) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 步骤1: 获取输入文件总时长
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
    
    import re
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", output)
    if not duration_match:
        LOGGER.warning("无法获取输入文件时长，使用最后一个区间的结束时间")
        input_total_duration = max(end for _, end in keep_ranges)
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
    
    # 如果文件不长，直接使用简单策略
    if input_total_duration < split_duration * 2:
        LOGGER.info("File is short, using simple extraction strategy")
        from .simple_audio_cutter import simple_cut_audio
        simple_cut_audio(
            input_path,
            output_path,
            keep_ranges,
            ffmpeg_binary=ffmpeg_binary,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            progress_callback=progress_callback,
        )
        return
    
    emit_progress(0.05)
    
    # 步骤2: 创建临时目录
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        # 步骤3: 物理分割输入文件
        LOGGER.info("Splitting input file into chunks of %.1f minutes...", split_duration / 60)
        
        num_splits = int(input_total_duration / split_duration) + 1
        split_files: List[Tuple[int, Path, float, float]] = []  # (index, path, start_time, end_time)
        
        split_start_time = time.time()
        for i in range(num_splits):
            chunk_start = i * split_duration
            chunk_end = min((i + 1) * split_duration, input_total_duration)
            
            if chunk_start >= input_total_duration:
                break
            
            split_file = temp_path / f"split_{i:04d}.mp3"
            
            # 使用copy模式快速分割
            split_cmd = [
                ffmpeg_binary,
                "-ss", str(chunk_start),
                "-t", str(chunk_end - chunk_start),
                "-i", str(input_path),
                "-c", "copy",
                "-y",
                str(split_file),
            ]
            
            LOGGER.info("Splitting chunk %d/%d: [%.1f-%.1f]s", i + 1, num_splits, chunk_start, chunk_end)
            
            result = subprocess.run(
                split_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            
            if result.returncode != 0:
                LOGGER.error("Failed to split chunk %d: %s", i, result.stderr)
                raise RuntimeError(f"FFmpeg split failed: {result.stderr[:200]}")
            
            split_files.append((i, split_file, chunk_start, chunk_end))
            emit_progress(0.05 + 0.15 * (i + 1) / num_splits)
        
        split_elapsed = time.time() - split_start_time
        LOGGER.info(
            "Split completed: %d chunks in %.2fs (%.2fs per chunk)",
            len(split_files),
            split_elapsed,
            split_elapsed / len(split_files) if split_files else 0,
        )
        
        emit_progress(0.2)
        
        # 步骤4: 为每个分割文件分配保留区间
        chunk_tasks: List[dict] = []
        for split_idx, split_file, chunk_start, chunk_end in split_files:
            # 找出这个时间段内的所有保留区间
            chunk_ranges = []
            for start, end in keep_ranges:
                # 如果区间与当前时间段有交集
                if end > chunk_start and start < chunk_end:
                    # 转换为相对于分块起始的时间
                    relative_start = max(0, start - chunk_start)
                    relative_end = min(end - chunk_start, chunk_end - chunk_start)
                    if relative_end > relative_start:
                        chunk_ranges.append((relative_start, relative_end))
            
            if chunk_ranges:
                chunk_duration = sum(end - start for start, end in chunk_ranges)
                chunk_tasks.append({
                    "index": split_idx,
                    "split_file": split_file,
                    "ranges": chunk_ranges,
                    "duration": chunk_duration,
                })
                LOGGER.info(
                    "Chunk %d: %d ranges, %.1f seconds to keep",
                    split_idx,
                    len(chunk_ranges),
                    chunk_duration,
                )
        
        if not chunk_tasks:
            raise ValueError("没有任何需要处理的分块")
        
        # 步骤5: 并行处理每个分块
        LOGGER.info("Processing %d chunks in parallel...", len(chunk_tasks))
        
        chunk_progress = [0.0] * len(chunk_tasks)
        output_files: List[Optional[Path]] = [None] * len(chunk_tasks)
        progress_lock = threading.Lock()
        
        def update_progress_locked() -> None:
            aggregated = sum(
                chunk_tasks[i]["duration"] * chunk_progress[i]
                for i in range(len(chunk_tasks))
            )
            fraction = aggregated / total_duration if total_duration > 0.0 else 1.0
            emit_progress(0.2 + 0.7 * fraction)
        
        def process_chunk(task: dict) -> Tuple[int, Path]:
            """处理单个分块"""
            chunk_index = task["index"]
            split_file = task["split_file"]
            ranges = task["ranges"]
            duration = task["duration"]
            
            # 创建输出文件
            chunk_output = temp_path / f"output_{chunk_index:04d}.mp3"
            
            task_start_time = time.time()
            
            # 如果只有一个区间，直接提取
            if len(ranges) == 1:
                start, end = ranges[0]
                cmd = [
                    ffmpeg_binary,
                    "-threads", "2",
                    "-ss", str(start),
                    "-t", str(end - start),
                    "-i", str(split_file),
                    "-c:a", audio_codec,
                    "-b:a", audio_bitrate,
                    "-y",
                    str(chunk_output),
                ]
            else:
                # 多个区间，需要拼接
                # 先提取每个片段
                segment_files = []
                for seg_idx, (start, end) in enumerate(ranges):
                    seg_file = temp_path / f"chunk_{chunk_index}_seg_{seg_idx}.mp3"
                    cmd = [
                        ffmpeg_binary,
                        "-threads", "2",
                        "-ss", str(start),
                        "-t", str(end - start),
                        "-i", str(split_file),
                        "-c:a", audio_codec,
                        "-b:a", audio_bitrate,
                        "-y",
                        str(seg_file),
                    ]
                    
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                        errors="replace",
                    )
                    
                    if result.returncode != 0:
                        # 清理
                        for f in segment_files:
                            try:
                                f.unlink(missing_ok=True)
                            except:
                                pass
                        raise RuntimeError(f"Failed to extract segment: {result.stderr[:200]}")
                    
                    segment_files.append(seg_file)
                
                # 拼接片段
                concat_file = temp_path / f"concat_{chunk_index}.txt"
                with open(concat_file, 'w', encoding='utf-8') as f:
                    for seg_file in segment_files:
                        escaped = str(seg_file.resolve()).replace("\\", "/")
                        f.write(f"file '{escaped}'\n")
                
                cmd = [
                    ffmpeg_binary,
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-c", "copy",
                    "-y",
                    str(chunk_output),
                ]
                
                # 清理片段文件
                try:
                    for seg_file in segment_files:
                        seg_file.unlink(missing_ok=True)
                    concat_file.unlink(missing_ok=True)
                except:
                    pass
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            
            task_elapsed = time.time() - task_start_time
            
            if result.returncode != 0:
                LOGGER.error("Failed to process chunk %d: %s", chunk_index, result.stderr)
                raise RuntimeError(f"FFmpeg failed: {result.stderr[:200]}")
            
            speed_ratio = duration / task_elapsed if task_elapsed > 0 else 0
            LOGGER.info(
                "Chunk %d processed: %.2fs duration in %.2fs (%.1fx speed)",
                chunk_index,
                duration,
                task_elapsed,
                speed_ratio,
            )
            
            with progress_lock:
                chunk_progress[chunk_index] = 1.0
                update_progress_locked()
            
            return chunk_index, chunk_output
        
        # 确定并行度
        cpu_count = os.cpu_count() or 1
        # 因为每个任务处理的是小文件，可以更激进
        max_workers = min(len(chunk_tasks), max(2, cpu_count - 1))
        max_workers = max(2, min(max_workers, 6))  # 最多6个并行
        
        LOGGER.info("Using %d parallel workers for chunk processing", max_workers)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for task in chunk_tasks:
                future = executor.submit(process_chunk, task)
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    idx, output_file = future.result()
                    output_files[idx] = output_file
                except Exception as e:
                    LOGGER.error("Chunk processing failed: %s", e)
                    raise
        
        emit_progress(0.9)
        
        # 步骤6: 拼接所有输出
        LOGGER.info("Concatenating %d output chunks...", len([f for f in output_files if f]))
        
        final_concat_file = temp_path / "final_concat.txt"
        with open(final_concat_file, 'w', encoding='utf-8') as f:
            for output_file in output_files:
                if output_file:
                    escaped = str(output_file.resolve()).replace("\\", "/")
                    f.write(f"file '{escaped}'\n")
        
        concat_cmd = [
            ffmpeg_binary,
            "-f", "concat",
            "-safe", "0",
            "-i", str(final_concat_file),
            "-c", "copy",
            "-y",
            str(output_path),
        ]
        
        result = subprocess.run(
            concat_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        
        if result.returncode != 0:
            LOGGER.error("Final concatenation failed: %s", result.stderr)
            raise RuntimeError(f"FFmpeg concat failed: {result.stderr[:200]}")
        
        emit_progress(1.0)
        LOGGER.info("Optimized audio cut completed: %s", output_path)
