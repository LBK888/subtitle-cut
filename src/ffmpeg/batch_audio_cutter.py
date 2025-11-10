"""批处理音频剪辑器 - 使用filter_complex一次处理多个区间

针对大量小区间的场景优化，减少FFmpeg启动次数。
"""

import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Optional, Sequence, Tuple, List

LOGGER = logging.getLogger(__name__)


def batch_cut_audio(
    input_path: Path,
    output_path: Path,
    keep_ranges: Sequence[Tuple[float, float]],
    *,
    ffmpeg_binary: str = "ffmpeg",
    audio_codec: str = "libmp3lame",
    audio_bitrate: str = "192k",
    progress_callback: Optional[Callable[[float], None]] = None,
    batch_size: int = 50,  # 每批处理多少个区间
) -> None:
    """使用批处理方式剪辑音频
    
    Args:
        input_path: 输入音频文件路径
        output_path: 输出音频文件路径
        keep_ranges: 保留区间列表 [(start, end), ...]
        ffmpeg_binary: FFmpeg可执行文件路径
        audio_codec: 音频编码器
        audio_bitrate: 音频比特率
        progress_callback: 进度回调函数
        batch_size: 每批处理的区间数量
    """
    
    if not keep_ranges:
        raise ValueError("No ranges to keep")
    
    LOGGER.info("Batch audio cutting: %d ranges, batch_size=%d", len(keep_ranges), batch_size)
    
    def emit_progress(fraction: float) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 计算总时长
    total_duration = sum(end - start for start, end in keep_ranges)
    
    # 将区间分批
    batches = []
    for i in range(0, len(keep_ranges), batch_size):
        batch = keep_ranges[i:i + batch_size]
        batches.append(batch)
    
    LOGGER.info("Split into %d batches", len(batches))
    
    # 并行处理每批
    cpu_count = os.cpu_count() or 4
    max_workers = min(len(batches), max(2, cpu_count // 2))
    
    LOGGER.info("Using %d parallel workers for batch processing", max_workers)
    
    batch_outputs: List[Optional[Path]] = [None] * len(batches)
    progress_lock = threading.Lock()
    completed_count = [0]
    
    def process_batch(batch_index: int, batch_ranges: List[Tuple[float, float]]) -> Tuple[int, Path]:
        """处理一批区间"""
        import time
        thread_id = threading.current_thread().name
        
        LOGGER.info(
            "[Thread %s] Processing batch %d: %d ranges",
            thread_id,
            batch_index,
            len(batch_ranges),
        )
        
        start_time = time.time()
        
        # 创建临时输出文件
        temp_file = Path(
            NamedTemporaryFile(
                suffix=".mp3",
                prefix=f"batch_{batch_index}_",
                delete=False,
            ).name
        )
        
        try:
            # 构建filter_complex
            filter_parts = []
            for i, (start, end) in enumerate(batch_ranges):
                duration = end - start
                # 为每个区间创建一个输入流
                filter_parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]")
            
            # 拼接所有流
            concat_inputs = "".join(f"[a{i}]" for i in range(len(batch_ranges)))
            filter_parts.append(f"{concat_inputs}concat=n={len(batch_ranges)}:v=0:a=1[out]")
            
            filter_complex = ";".join(filter_parts)
            
            # 构建FFmpeg命令
            cmd = [
                ffmpeg_binary,
                "-threads", "2",
                "-i", str(input_path),
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-c:a", audio_codec,
                "-b:a", audio_bitrate,
                "-y",
                str(temp_file),
            ]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            
            elapsed = time.time() - start_time
            
            if result.returncode != 0:
                LOGGER.error("Failed to process batch %d: %s", batch_index, result.stderr[:500])
                raise RuntimeError(f"FFmpeg failed: {result.stderr[:200]}")
            
            batch_duration = sum(end - start for start, end in batch_ranges)
            speed_ratio = batch_duration / elapsed if elapsed > 0 else 0
            
            LOGGER.info(
                "[Thread %s] Batch %d completed: %.2fs duration in %.2fs (%.1fx speed)",
                thread_id,
                batch_index,
                batch_duration,
                elapsed,
                speed_ratio,
            )
            
            # 更新进度
            with progress_lock:
                completed_count[0] += 1
                fraction = completed_count[0] / len(batches)
                emit_progress(fraction * 0.9)  # 处理占90%进度
            
            return batch_index, temp_file
        
        except Exception as e:
            try:
                temp_file.unlink(missing_ok=True)
            except:
                pass
            raise
    
    # 并行处理所有批次
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, batch in enumerate(batches):
            future = executor.submit(process_batch, i, batch)
            futures.append(future)
        
        for future in as_completed(futures):
            try:
                index, temp_file = future.result()
                batch_outputs[index] = temp_file
            except Exception as e:
                LOGGER.error("Batch processing failed: %s", e)
                # 清理已处理的批次
                for f in batch_outputs:
                    if f:
                        try:
                            f.unlink(missing_ok=True)
                        except:
                            pass
                raise
    
    LOGGER.info("All batches processed successfully")
    
    # 拼接所有批次的输出
    if len(batch_outputs) == 1:
        # 只有一批，直接移动文件
        batch_outputs[0].rename(output_path)
    else:
        # 多批，需要拼接
        LOGGER.info("Concatenating %d batch outputs...", len(batch_outputs))
        
        try:
            # 创建concat文件列表
            concat_file = Path(
                NamedTemporaryFile(
                    mode="w",
                    suffix=".txt",
                    prefix="concat_",
                    delete=False,
                    encoding="utf-8",
                ).name
            )
            
            with open(concat_file, "w", encoding="utf-8") as f:
                for batch_file in batch_outputs:
                    # FFmpeg concat需要转义特殊字符
                    escaped_path = str(batch_file).replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{escaped_path}'\n")
            
            # 使用concat协议拼接
            cmd = [
                ffmpeg_binary,
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                "-y",
                str(output_path),
            ]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            
            if result.returncode != 0:
                LOGGER.error("Failed to concatenate batches: %s", result.stderr)
                raise RuntimeError(f"FFmpeg concat failed: {result.stderr[:200]}")
            
            LOGGER.info("Concatenation completed")
            
        finally:
            # 清理临时文件
            try:
                concat_file.unlink(missing_ok=True)
            except:
                pass
            
            for batch_file in batch_outputs:
                if batch_file:
                    try:
                        batch_file.unlink(missing_ok=True)
                    except:
                        pass
    
    emit_progress(1.0)
    LOGGER.info("Batch audio cutting completed: %s", output_path)
