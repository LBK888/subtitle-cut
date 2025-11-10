"""简化的音频剪辑器 - 完全重写,避免复杂的filter_complex

核心思路:
1. 不使用filter_complex
2. 逐个提取保留片段
3. 直接拼接
4. 简单、快速、可靠
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)


def simple_cut_audio(
    input_path: Path,
    output_path: Path,
    keep_ranges: Sequence[Tuple[float, float]],
    *,
    ffmpeg_binary: str = "ffmpeg",
    audio_codec: str = "libmp3lame",
    audio_bitrate: str = "192k",
    progress_callback: Optional[Callable[[float], None]] = None,
) -> None:
    """简化的音频剪辑
    
    策略:
    1. 逐个提取保留片段 (使用-ss和-t,非常快)
    2. 多线程并行提取
    3. 拼接所有片段
    
    Args:
        input_path: 输入音频文件
        output_path: 输出音频文件
        keep_ranges: 保留的时间区间 [(start, end), ...]
        ffmpeg_binary: FFmpeg可执行文件路径
        audio_codec: 音频编码器
        audio_bitrate: 音频码率
        progress_callback: 进度回调函数
    """
    if not keep_ranges:
        raise ValueError("保留区间列表为空")
    
    total_duration = sum(end - start for start, end in keep_ranges)
    if total_duration <= 0:
        raise ValueError("保留区间总时长为0")
    
    LOGGER.info(
        "Simple audio cut: %d ranges, total %.2f seconds (%.1f minutes)",
        len(keep_ranges),
        total_duration,
        total_duration / 60,
    )
    
    def emit_progress(fraction: float) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 步骤1: 并行提取所有片段
    LOGGER.info("Extracting %d audio segments in parallel...", len(keep_ranges))
    
    segment_files: List[Optional[Path]] = [None] * len(keep_ranges)
    progress_lock = threading.Lock()
    completed_count = [0]
    
    def extract_segment(index: int, start: float, end: float) -> Tuple[int, Path]:
        """提取单个音频片段"""
        duration = end - start
        
        import time
        import threading
        thread_id = threading.current_thread().name
        
        LOGGER.debug(
            "[Thread %s] Starting segment %d: [%.2f-%.2f]s (%.2fs duration)",
            thread_id,
            index,
            start,
            end,
            duration,
        )
        
        # 创建临时文件
        temp_file = Path(
            NamedTemporaryFile(
                suffix=".mp3",
                prefix=f"segment_{index}_",
                delete=False,
            ).name
        )
        
        try:
            # 使用-ss和-t快速提取片段
            # -ss在-i之前:快速seek
            # 关键优化：使用copy模式无损复制，避免重新编码
            cmd = [
                ffmpeg_binary,
                "-ss", str(start),
                "-t", str(duration),
                "-i", str(input_path),
                "-c", "copy",  # 无损复制，不重新编码
                "-y",
                str(temp_file),
            ]
            
            start_time = time.time()
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            
            elapsed = time.time() - start_time
            
            if result.returncode != 0:
                LOGGER.error("Failed to extract segment %d: %s", index, result.stderr)
                raise RuntimeError(f"FFmpeg failed: {result.stderr[:200]}")
            
            # 记录性能数据
            speed_ratio = duration / elapsed if elapsed > 0 else 0
            LOGGER.info(
                "[Thread %s] Segment %d completed: %.2fs duration in %.2fs (%.1fx speed)",
                thread_id,
                index,
                duration,
                elapsed,
                speed_ratio,
            )
            
            # 更新进度
            with progress_lock:
                completed_count[0] += 1
                fraction = completed_count[0] / len(keep_ranges)
                emit_progress(fraction * 0.9)  # 提取占90%进度
            
            return index, temp_file
        
        except Exception as e:
            try:
                temp_file.unlink(missing_ok=True)
            except:
                pass
            raise
    
    # 并行提取
    # 策略：针对RAM Disk优化 - 没有I/O瓶颈，主要限制CPU使用
    # 关键：限制FFmpeg内部线程数，允许更多进程并行
    cpu_count = os.cpu_count() or 1
    
    # RAM Disk + 多核CPU场景：非常激进的并行度
    # 因为每个FFmpeg只用2个线程，20核CPU可以运行10个进程
    # 计算平均片段时长
    avg_segment_duration = total_duration / len(keep_ranges) if keep_ranges else 0
    
    # 根据片段时长动态调整并行度
    if avg_segment_duration < 5:  # 平均每段小于5秒
        # 短片段：FFmpeg启动开销大，需要更多并行来弥补
        max_workers = min(len(keep_ranges), max(6, cpu_count))
    elif avg_segment_duration < 30:  # 平均每段5-30秒
        # 中等片段：标准并行度
        max_workers = min(len(keep_ranges), max(4, cpu_count * 3 // 4))
    else:  # 平均每段超过30秒
        # 长片段：适中并行度
        max_workers = min(len(keep_ranges), max(3, cpu_count // 2))
    
    # 确保至少有4个worker，最多不超过cpu_count（因为每个FFmpeg用2线程）
    max_workers = max(4, min(max_workers, cpu_count))
    
    LOGGER.info(
        "Using %d parallel workers for segment extraction (total_duration=%.1f min, cpu_count=%d, ranges=%d, avg_segment=%.1fs)",
        max_workers,
        total_duration / 60,
        cpu_count,
        len(keep_ranges),
        avg_segment_duration,
    )
    LOGGER.info(
        "RAM Disk optimized: Each FFmpeg uses 2 threads, %d parallel processes should utilize ~%d cores (%.1f%% CPU)",
        max_workers,
        max_workers * 2,
        (max_workers * 2 / cpu_count) * 100,
    )
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, (start, end) in enumerate(keep_ranges):
            future = executor.submit(extract_segment, i, start, end)
            futures.append(future)
        
        for future in as_completed(futures):
            try:
                index, temp_file = future.result()
                segment_files[index] = temp_file
            except Exception as e:
                LOGGER.error("Segment extraction failed: %s", e)
                # 清理已提取的片段
                for f in segment_files:
                    if f:
                        try:
                            f.unlink(missing_ok=True)
                        except:
                            pass
                raise
    
    LOGGER.info("All segments extracted successfully")
    
    # 步骤2: 拼接所有片段
    LOGGER.info("Concatenating %d segments...", len(segment_files))
    
    try:
        # 创建concat文件列表
        concat_file = Path(
            NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                suffix='.txt',
                delete=False,
            ).name
        )
        
        try:
            with open(concat_file, 'w', encoding='utf-8') as f:
                for segment_file in segment_files:
                    if segment_file:
                        # 转义路径
                        escaped_path = str(segment_file.resolve()).replace("\\", "/")
                        f.write(f"file '{escaped_path}'\n")
            
            # 拼接
            concat_cmd = [
                ffmpeg_binary,
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
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
                LOGGER.error("Concatenation failed: %s", result.stderr)
                raise RuntimeError(f"FFmpeg concat failed: {result.stderr[:200]}")
            
            emit_progress(1.0)
            LOGGER.info("Audio cut completed: %s", output_path)
        
        finally:
            try:
                concat_file.unlink(missing_ok=True)
            except:
                pass
    
    finally:
        # 清理临时片段文件
        for segment_file in segment_files:
            if segment_file:
                try:
                    segment_file.unlink(missing_ok=True)
                except:
                    pass
