"""视频分段并行导出模块"""

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


def export_with_video_segments(
    segments: List[dict],
    keep_ranges: List[Tuple[float, float]],
    output_path: Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    video_codec: str = "libx264",
    audio_codec: str = "aac",
    crf: int = 23,
    preset: str = "medium",
    progress_callback: Optional[Callable[[float], None]] = None,
) -> None:
    """基于预分割片段并行导出视频
    
    Args:
        segments: 片段信息列表
        keep_ranges: 全局保留区间列表（相对于原始视频的时间）
        output_path: 输出文件路径
        ffmpeg_binary: FFmpeg可执行文件
        video_codec: 视频编码器
        audio_codec: 音频编码器
        crf: 视频质量（0-51，越小质量越好）
        preset: 编码预设
        progress_callback: 进度回调
    """
    LOGGER.info("Starting segment-based video export: %d segments", len(segments))
    
    def emit_progress(fraction: float):
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 创建临时目录
    temp_dir = output_path.parent / "temp_video"
    temp_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Temporary directory: %s", temp_dir)
    
    # 记录保留区间诊断信息
    LOGGER.info("=" * 60)
    LOGGER.info("VIDEO EXPORT DIAGNOSTICS")
    LOGGER.info("Total keep ranges: %d", len(keep_ranges))
    for i, (k_start, k_end) in enumerate(keep_ranges[:10]):
        LOGGER.info("  Keep %d: [%.6f - %.6f] (%.3fs)", 
                   i, k_start, k_end, k_end - k_start)
    if len(keep_ranges) > 10:
        LOGGER.info("  ... and %d more keep ranges", len(keep_ranges) - 10)
    LOGGER.info("=" * 60)
    
    # 1. 为每个片段计算其范围内的保留区间
    segment_tasks = []
    for seg in segments:
        seg_start = seg["start_time"]
        seg_end = seg["end_time"]
        seg_duration = seg["duration"]
        
        # 找到这个片段范围内的保留区间（转换为片段本地时间）
        seg_keeps = []
        for k_start, k_end in keep_ranges:
            # 检查是否有重叠（添加容差避免边界问题）
            if k_start < seg_end + BOUNDARY_TOLERANCE and k_end > seg_start - BOUNDARY_TOLERANCE:
                # 转换为片段本地时间，并四舍五入到微秒级
                local_start = round(max(k_start - seg_start, 0), TIME_PRECISION)
                local_end = round(min(k_end - seg_start, seg_duration), TIME_PRECISION)
                
                # 确保区间有效（至少1ms）
                if local_end - local_start >= 0.001:
                    seg_keeps.append((local_start, local_end))
                    
                    LOGGER.debug(
                        "Segment %d: keep global [%.6f-%.6f] -> local [%.6f-%.6f]",
                        seg["index"], k_start, k_end, local_start, local_end
                    )
        
        segment_tasks.append({
            "index": seg["index"],
            "input": seg["file"],
            "duration": seg_duration,
            "keep_ranges": seg_keeps,
            "output": temp_dir / f"seg_{seg['index']:03d}.mp4"
        })
        
        LOGGER.info(
            "Segment %d [%.2f-%.2f]: %d keep ranges",
            seg["index"],
            seg_start,
            seg_end,
            len(seg_keeps)
        )
        
        # 记录详细的保留区间
        if seg_keeps:
            LOGGER.debug("  Keep ranges (local time):")
            for k_start, k_end in seg_keeps[:5]:
                LOGGER.debug("    [%.6f - %.6f]", k_start, k_end)
    
    # 2. 并行处理所有片段
    cpu_count = os.cpu_count() or 4
    
    # NVENC有并发会话限制，消费级显卡通常只能2-3个并发
    if "nvenc" in video_codec.lower():
        max_workers = 2  # NVENC限制为2个并发
        LOGGER.info("Using NVENC: limiting parallel workers to 2 (driver restriction)")
        LOGGER.info("Strategy: Process %d segments in batches of 2", len(segments))
    else:
        max_workers = min(len(segments), cpu_count)
    
    LOGGER.info("=" * 60)
    LOGGER.info("PARALLEL VIDEO SEGMENT PROCESSING")
    LOGGER.info("Total segments: %d", len(segments))
    LOGGER.info("Parallel workers: %d", max_workers)
    LOGGER.info("Video codec: %s", video_codec)
    LOGGER.info("This will process %d segments in parallel", max_workers)
    LOGGER.info("=" * 60)
    
    completed = [0]
    outputs = [None] * len(segments)
    
    def process_segment_task(task: dict) -> Tuple[int, Optional[Path]]:
        """处理单个片段"""
        index = task["index"]
        input_file = Path(task["input"])
        output_file = task["output"]
        keep_ranges = task["keep_ranges"]
        duration = task["duration"]
        
        LOGGER.info("Processing segment %d: %d keep ranges", index, len(keep_ranges))
        
        try:
            if len(keep_ranges) == 0:
                # 整段删除，跳过
                LOGGER.warning("Segment %d has no keep ranges, skipping", index)
                return index, None
            
            elif len(keep_ranges) == 1 and abs(keep_ranges[0][0]) < 0.001 and abs(keep_ranges[0][1] - duration) < 0.001:
                # 整段保留，直接复制
                LOGGER.info("Segment %d: fully kept, copying", index)
                shutil.copy(input_file, output_file)
            
            else:
                # 需要剪辑，使用filter_complex
                LOGGER.info("Segment %d: cutting %d ranges (this may take a while...)", index, len(keep_ranges))
                
                # 报告开始处理这个片段
                LOGGER.info("Segment %d: Starting FFmpeg processing...", index)
                
                _cut_video_segment(
                    input_file,
                    output_file,
                    keep_ranges,
                    ffmpeg_binary,
                    video_codec,
                    audio_codec,
                    crf,
                    preset
                )
                
                LOGGER.info("Segment %d: FFmpeg processing completed", index)
            
            completed[0] += 1
            current_progress = 0.1 + 0.8 * completed[0] / len(segments)
            emit_progress(current_progress)
            LOGGER.info("Overall progress: %.1f%% (%d/%d segments completed)", 
                       current_progress * 100, completed[0], len(segments))
            
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
    
    LOGGER.info("=" * 60)
    LOGGER.info("ALL SEGMENTS PROCESSED - STARTING CONCATENATION")
    LOGGER.info("Valid segments to concatenate: %d", len(valid_outputs))
    LOGGER.info("=" * 60)
    emit_progress(0.9)
    
    # 4. 按顺序合并
    LOGGER.info("Concatenating video segments...")
    _concat_video_files(valid_outputs, output_path, ffmpeg_binary)
    LOGGER.info("Concatenation completed")
    
    # 5. 清理临时文件和目录
    for output_file in valid_outputs:
        try:
            output_file.unlink(missing_ok=True)
        except:
            pass
    
    try:
        temp_dir.rmdir()
    except:
        pass
    
    emit_progress(1.0)
    LOGGER.info("Video export complete: %s", output_path)


def _cut_video_segment(
    input_file: Path,
    output_file: Path,
    keep_ranges: List[Tuple[float, float]],
    ffmpeg_binary: str,
    video_codec: str,
    audio_codec: str,
    crf: int,
    preset: str
) -> None:
    """剪辑单个视频片段"""
    # 构建filter_complex（使用高精度时间格式）
    video_parts = []
    audio_parts = []
    
    for i, (start, end) in enumerate(keep_ranges):
        # 视频流：使用trim和setpts
        video_parts.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]"
        )
        # 音频流：使用atrim和asetpts
        audio_parts.append(
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{i}]"
        )
    
    # 拼接视频和音频
    video_concat = "".join(f"[v{i}]" for i in range(len(keep_ranges)))
    audio_concat = "".join(f"[a{i}]" for i in range(len(keep_ranges)))
    
    filter_parts = video_parts + audio_parts
    filter_parts.append(
        f"{video_concat}concat=n={len(keep_ranges)}:v=1:a=0[vout]"
    )
    filter_parts.append(
        f"{audio_concat}concat=n={len(keep_ranges)}:v=0:a=1[aout]"
    )
    
    filter_complex = ";".join(filter_parts)
    
    # 如果filter_complex太长，使用临时文件
    # Windows命令行限制约为8191字符
    if len(filter_complex) > 6000:
        # 使用临时文件存储filter_complex
        filter_file = output_file.parent / f"filter_{output_file.stem}.txt"
        try:
            with open(filter_file, "w", encoding="utf-8") as f:
                f.write(filter_complex)
            
            LOGGER.info("Filter complex too long (%d chars), using file: %s", 
                       len(filter_complex), filter_file)
            
            # 使用 -filter_complex_script 参数
            cmd = [
                ffmpeg_binary,
                "-i", str(input_file),
                "-filter_complex_script", str(filter_file),
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", video_codec,
            ]
            
            # 根据编码器类型添加质量参数
            if "nvenc" in video_codec.lower():
                # NVENC参数（与cutter.py保持一致）
                cmd.extend([
                    "-preset", preset,
                    "-rc", "vbr",
                    "-cq", str(crf),
                    "-b:v", "0"
                ])
            else:
                # libx264参数
                cmd.extend([
                    "-crf", str(crf),
                    "-preset", preset
                ])
            
            cmd.extend([
                "-c:a", audio_codec,
                "-b:a", "192k",
                "-movflags", "+faststart",
                "-y",
                str(output_file)
            ])
        finally:
            pass  # 稍后清理
    else:
        # filter_complex不长，直接使用命令行参数
        cmd = [
            ffmpeg_binary,
            "-i", str(input_file),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", video_codec,
        ]
        
        # 根据编码器类型添加质量参数
        if "nvenc" in video_codec.lower():
            # NVENC参数（与cutter.py保持一致）
            cmd.extend([
                "-preset", preset,
                "-rc", "vbr",
                "-cq", str(crf),
                "-b:v", "0"
            ])
        else:
            # libx264参数
            cmd.extend([
                "-crf", str(crf),
                "-preset", preset
            ])
        
        cmd.extend([
            "-c:a", audio_codec,
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-y",
            str(output_file)
        ])
        filter_file = None
    
    try:
        LOGGER.info("Running FFmpeg command: %s", ' '.join(cmd))
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace"
        )
        
        if result.returncode != 0:
            LOGGER.error("FFmpeg command failed: %s", ' '.join(cmd))
            LOGGER.error("FFmpeg stderr (full): %s", result.stderr)
            raise RuntimeError(f"FFmpeg failed with return code {result.returncode}. See logs for details.")
    finally:
        # 清理临时filter文件
        if filter_file and filter_file.exists():
            try:
                filter_file.unlink()
            except:
                pass


def _concat_video_files(
    input_files: List[Path],
    output_file: Path,
    ffmpeg_binary: str
) -> None:
    """拼接多个视频文件"""
    if len(input_files) == 1:
        # 只有一个文件，直接移动
        shutil.move(input_files[0], output_file)
        return
    
    # 创建concat列表文件
    concat_file = output_file.parent / "concat_list.txt"
    
    try:
        with open(concat_file, "w", encoding="utf-8") as f:
            for input_file in input_files:
                # 使用绝对路径并转义
                escaped_path = str(input_file.resolve()).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")
        
        # 执行拼接（使用concat demuxer，无损拼接）
        cmd = [
            ffmpeg_binary,
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",  # 无损复制
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
