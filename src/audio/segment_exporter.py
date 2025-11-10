"""分段并行导出模块"""

import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import List, Tuple, Callable, Optional

LOGGER = logging.getLogger(__name__)

# 常量：边界容差和时间精度
BOUNDARY_TOLERANCE = 0.001  # 1ms容差，避免浮点数精度问题
TIME_PRECISION = 6  # 时间精度：6位小数（微秒级）


def export_with_segments(
    segments: List[dict],
    delete_ranges: List[Tuple[float, float]],
    output_path: Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    audio_codec: str = "libmp3lame",
    audio_bitrate: str = "192k",
    progress_callback: Optional[Callable[[float], None]] = None,
    ramdisk_path: Optional[Path] = None
) -> None:
    """基于预分割片段并行导出
    
    Args:
        segments: 片段信息列表
        delete_ranges: 全局删除区间列表
        output_path: 输出文件路径
        ffmpeg_binary: FFmpeg可执行文件
        audio_codec: 音频编码器
        audio_bitrate: 音频比特率
        progress_callback: 进度回调
        ramdisk_path: RAM盘路径（如果提供，将片段复制到RAM盘以加速访问）
    """
    LOGGER.info("Starting segment-based export: %d segments", len(segments))
    
    def emit_progress(fraction: float):
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 创建临时目录（使用output_path的父目录，避免路径过长）
    temp_dir = output_path.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Temporary directory: %s", temp_dir)
    
    # 如果提供了RAM盘路径，将所有片段复制到RAM盘
    staged_segments = []
    if ramdisk_path and ramdisk_path.exists():
        LOGGER.info("Staging %d segments to RAM disk: %s", len(segments), ramdisk_path)
        try:
            for seg in segments:
                src_file = Path(seg["file"])
                if not src_file.exists():
                    LOGGER.warning("Segment file not found: %s, using original path", src_file)
                    staged_segments.append(seg)
                    continue
                
                # 复制到RAM盘
                dst_file = ramdisk_path / src_file.name
                shutil.copy2(src_file, dst_file)
                
                # 创建新的segment字典，使用RAM盘路径
                staged_seg = seg.copy()
                staged_seg["file"] = str(dst_file)
                staged_segments.append(staged_seg)
                
                LOGGER.debug("Staged segment %d to RAM disk: %s", seg["index"], dst_file)
            
            LOGGER.info("All segments staged to RAM disk successfully")
            segments = staged_segments  # 使用RAM盘中的片段
        except Exception as e:
            LOGGER.warning("Failed to stage segments to RAM disk: %s, using original paths", e)
            # 如果复制失败，继续使用原始路径
    else:
        LOGGER.info("No RAM disk provided, using original segment paths")
    
    # 记录删除区间诊断信息
    LOGGER.info("=" * 60)
    LOGGER.info("EXPORT DELETION DIAGNOSTICS")
    LOGGER.info("Total delete ranges: %d", len(delete_ranges))
    for i, (d_start, d_end) in enumerate(delete_ranges[:10]):  # 只显示前10个
        LOGGER.info("  Delete %d: [%.6f - %.6f] (%.3fs)", 
                   i, d_start, d_end, d_end - d_start)
    if len(delete_ranges) > 10:
        LOGGER.info("  ... and %d more delete ranges", len(delete_ranges) - 10)
    LOGGER.info("=" * 60)
    
    # 1. 为每个片段计算其范围内的删除区间
    segment_tasks = []
    for seg in segments:
        seg_start = seg["start_time"]
        seg_end = seg["end_time"]
        seg_duration = seg["duration"]
        
        # 找到这个片段范围内的删除区间（转换为片段本地时间）
        seg_deletes = []
        for d_start, d_end in delete_ranges:
            # 检查是否有重叠（添加容差避免边界问题）
            if d_start < seg_end + BOUNDARY_TOLERANCE and d_end > seg_start - BOUNDARY_TOLERANCE:
                # 转换为片段本地时间，并四舍五入到微秒级
                local_start = round(max(d_start - seg_start, 0), TIME_PRECISION)
                local_end = round(min(d_end - seg_start, seg_duration), TIME_PRECISION)
                
                # 确保区间有效（至少1ms）
                if local_end - local_start >= 0.001:
                    seg_deletes.append((local_start, local_end))
                    
                    LOGGER.debug(
                        "Segment %d: delete global [%.6f-%.6f] -> local [%.6f-%.6f]",
                        seg["index"], d_start, d_end, local_start, local_end
                    )
        
        # 计算保留区间
        seg_keeps = _invert_ranges(seg_duration, seg_deletes)
        
        segment_tasks.append({
            "index": seg["index"],
            "input": seg["file"],
            "duration": seg_duration,
            "keep_ranges": seg_keeps,
            "output": temp_dir / f"s{seg['index']}.mp3"  # 使用超短文件名
        })
        
        LOGGER.info(
            "Segment %d [%.2f-%.2f]: %d delete ranges, %d keep ranges",
            seg["index"],
            seg_start,
            seg_end,
            len(seg_deletes),
            len(seg_keeps)
        )
        
        # 记录详细的删除和保留区间（仅对有删除的片段）
        if seg_deletes:
            LOGGER.debug("  Delete ranges (local time):")
            for d_start, d_end in seg_deletes[:5]:  # 最多显示5个
                LOGGER.debug("    [%.6f - %.6f]", d_start, d_end)
            LOGGER.debug("  Keep ranges (local time):")
            for k_start, k_end in seg_keeps[:5]:  # 最多显示5个
                LOGGER.debug("    [%.6f - %.6f]", k_start, k_end)
    
    # 2. 并行处理所有片段
    cpu_count = os.cpu_count() or 4
    max_workers = min(len(segments), cpu_count)
    
    LOGGER.info("Processing %d segments with %d workers", len(segments), max_workers)
    
    completed = [0]
    outputs = [None] * len(segments)
    
    def process_segment_task(task: dict) -> Tuple[int, Path]:
        """处理单个片段"""
        index = task["index"]
        input_file = Path(task["input"])
        output_file = task["output"]
        keep_ranges = task["keep_ranges"]
        duration = task["duration"]
        
        LOGGER.info("Processing segment %d: %d keep ranges", index, len(keep_ranges))
        
        try:
            if len(keep_ranges) == 0:
                # 整段删除，创建空文件（实际上不应该发生）
                LOGGER.warning("Segment %d has no keep ranges, skipping", index)
                return index, None
            
            elif len(keep_ranges) == 1 and keep_ranges[0] == (0.0, duration):
                # 整段保留，直接复制
                LOGGER.info("Segment %d: fully kept, copying", index)
                shutil.copy(input_file, output_file)
            
            else:
                # 需要剪辑，使用filter_complex
                LOGGER.info("Segment %d: cutting %d ranges", index, len(keep_ranges))
                _cut_segment(
                    input_file,
                    output_file,
                    keep_ranges,
                    ffmpeg_binary,
                    audio_codec,
                    audio_bitrate
                )
            
            completed[0] += 1
            emit_progress(0.1 + 0.8 * completed[0] / len(segments))
            
            return index, output_file
        
        except Exception as e:
            LOGGER.error("Failed to process segment %d: %s", index, e)
            raise
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_segment_task, task): task
            for task in segment_tasks
        }
        
        for future in as_completed(futures):
            try:
                index, output_file = future.result()
                outputs[index] = output_file
            except Exception as e:
                LOGGER.error("Segment processing failed: %s", e)
                raise
    
    # 3. 过滤掉None（被完全删除的片段）
    valid_outputs = [out for out in outputs if out is not None]
    
    if not valid_outputs:
        raise RuntimeError("All segments were deleted, no output to generate")
    
    LOGGER.info("All segments processed, concatenating %d files", len(valid_outputs))
    emit_progress(0.9)
    
    # 4. 按顺序合并
    _concat_files(valid_outputs, output_path, ffmpeg_binary)
    
    # 5. 清理临时文件和目录
    for output_file in valid_outputs:
        try:
            output_file.unlink(missing_ok=True)
        except:
            pass
    
    try:
        temp_dir.rmdir()  # 删除临时目录
    except:
        pass
    
    emit_progress(1.0)
    LOGGER.info("Export complete: %s", output_path)


def _invert_ranges(
    total_duration: float,
    delete_ranges: List[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """反转删除区间为保留区间"""
    if not delete_ranges:
        return [(0.0, total_duration)]
    
    # 排序并合并重叠的删除区间
    sorted_deletes = sorted(delete_ranges)
    merged_deletes = []
    
    for start, end in sorted_deletes:
        if merged_deletes and start <= merged_deletes[-1][1]:
            # 重叠，合并
            merged_deletes[-1] = (merged_deletes[-1][0], max(merged_deletes[-1][1], end))
        else:
            merged_deletes.append((start, end))
    
    # 计算保留区间
    keep_ranges = []
    prev_end = 0.0
    
    for start, end in merged_deletes:
        if start > prev_end:
            keep_ranges.append((prev_end, start))
        prev_end = max(prev_end, end)
    
    if prev_end < total_duration:
        keep_ranges.append((prev_end, total_duration))
    
    return keep_ranges


def _cut_segment(
    input_file: Path,
    output_file: Path,
    keep_ranges: List[Tuple[float, float]],
    ffmpeg_binary: str,
    audio_codec: str,
    audio_bitrate: str
) -> None:
    """剪辑单个片段"""
    # 构建filter_complex（使用高精度时间格式）
    filter_parts = []
    for i, (start, end) in enumerate(keep_ranges):
        # 使用6位小数精度（微秒级）
        filter_parts.append(
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]"
        )
    
    # 拼接
    concat_inputs = "".join(f"[a{i}]" for i in range(len(keep_ranges)))
    filter_parts.append(
        f"{concat_inputs}concat=n={len(keep_ranges)}:v=0:a=1[out]"
    )
    
    filter_complex = ";".join(filter_parts)
    
    # 执行FFmpeg
    cmd = [
        ffmpeg_binary,
        "-i", str(input_file),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", audio_codec,
        "-b:a", audio_bitrate,
        "-y",
        str(output_file)
    ]
    
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace"
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[:200]}")


def _concat_files(
    input_files: List[Path],
    output_file: Path,
    ffmpeg_binary: str
) -> None:
    """拼接多个文件"""
    if len(input_files) == 1:
        # 只有一个文件，直接移动
        shutil.move(input_files[0], output_file)
        return
    
    # 创建concat列表文件（使用output_file的父目录，避免路径过长）
    concat_file = output_file.parent / "c.txt"  # 超短文件名
    
    try:
        with open(concat_file, "w", encoding="utf-8") as f:
            for input_file in input_files:
                escaped_path = str(input_file.resolve()).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")
        
        # 执行拼接
        cmd = [
            ffmpeg_binary,
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-y",
            str(output_file)
        ]
        
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace"
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"Concat failed: {result.stderr[:200]}")
    
    finally:
        concat_file.unlink(missing_ok=True)
