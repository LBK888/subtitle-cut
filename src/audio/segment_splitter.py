"""音频分割模块 - 在静音处分割长音频"""

import logging
import re
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional

LOGGER = logging.getLogger(__name__)


def detect_silence_points(
    audio_path: Path,
    target_segment_duration: float = 900.0,  # 15分钟
    silence_threshold: str = "-30dB",
    silence_duration: float = 0.5,
    search_window: float = 60.0,  # 在目标点前后60秒内搜索
) -> List[float]:
    """检测适合分割的静音点
    
    Args:
        audio_path: 音频文件路径
        target_segment_duration: 目标分段时长（秒）
        silence_threshold: 静音阈值
        silence_duration: 静音最小持续时间（秒）
        search_window: 搜索窗口（秒）
    
    Returns:
        split_points: 分割时间点列表，包含0和总时长
    """
    LOGGER.info("Detecting silence points in %s", audio_path)
    
    # 1. 获取音频总时长
    duration = get_audio_duration(audio_path)
    LOGGER.info("Audio duration: %.2f seconds (%.2f minutes)", duration, duration / 60)
    
    # 2. 如果音频短于目标时长，不需要分割
    if duration <= target_segment_duration:
        LOGGER.info("Audio is shorter than target segment duration, no split needed")
        return [0.0, duration]
    
    # 3. 使用FFmpeg检测所有静音点
    silence_ranges = _detect_all_silence(audio_path, silence_threshold, silence_duration)
    LOGGER.info("Found %d silence ranges", len(silence_ranges))
    
    # 4. 计算理想的分割点
    num_segments = int(duration / target_segment_duration) + 1
    ideal_points = [i * target_segment_duration for i in range(1, num_segments)]
    LOGGER.info("Target %d segments, ideal split points: %s", num_segments, ideal_points)
    
    # 5. 为每个理想点找到最近的静音点
    split_points = [0.0]
    for ideal_point in ideal_points:
        # 在理想点前后search_window内找最长的静音
        best_silence = _find_best_silence_near(
            silence_ranges,
            ideal_point,
            search_window
        )
        if best_silence:
            # 使用静音的中点作为分割点
            split_point = (best_silence[0] + best_silence[1]) / 2
            split_points.append(split_point)
            LOGGER.info("Split point at %.2f (silence: %.2f-%.2f)", split_point, best_silence[0], best_silence[1])
        else:
            # 没找到静音，使用理想点
            split_points.append(ideal_point)
            LOGGER.warning("No silence found near %.2f, using ideal point", ideal_point)
    
    split_points.append(duration)
    
    LOGGER.info("Final split points: %s", split_points)
    return split_points


def _detect_all_silence(
    audio_path: Path,
    threshold: str,
    duration: float
) -> List[Tuple[float, float]]:
    """检测所有静音区间"""
    cmd = [
        "ffmpeg",
        "-i", str(audio_path),
        "-af", f"silencedetect=noise={threshold}:d={duration}",
        "-f", "null",
        "-"
    ]
    
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace"
    )
    
    # 解析输出
    silence_ranges = []
    silence_start = None
    
    for line in result.stderr.split('\n'):
        # silence_start: 123.456
        match_start = re.search(r'silence_start:\s*([\d.]+)', line)
        if match_start:
            silence_start = float(match_start.group(1))
        
        # silence_end: 125.678
        match_end = re.search(r'silence_end:\s*([\d.]+)', line)
        if match_end and silence_start is not None:
            silence_end = float(match_end.group(1))
            silence_ranges.append((silence_start, silence_end))
            silence_start = None
    
    return silence_ranges


def _find_best_silence_near(
    silence_ranges: List[Tuple[float, float]],
    target_point: float,
    window: float
) -> Optional[Tuple[float, float]]:
    """在目标点附近找最长的静音"""
    candidates = [
        (start, end)
        for start, end in silence_ranges
        if abs((start + end) / 2 - target_point) <= window
    ]
    
    if not candidates:
        return None
    
    # 返回最长的静音
    return max(candidates, key=lambda x: x[1] - x[0])


def get_audio_duration(audio_path: Path) -> float:
    """获取音频时长"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path)
    ]
    
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace"
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get audio duration: {result.stderr}")
    
    return float(result.stdout.strip())


def split_audio_at_points(
    audio_path: Path,
    split_points: List[float],
    output_dir: Path,
    prefix: str = "segment"
) -> List[dict]:
    """在指定点分割音频
    
    Args:
        audio_path: 输入音频文件
        split_points: 分割点列表（包含0和总时长）
        output_dir: 输出目录
        prefix: 输出文件前缀
    
    Returns:
        segments: 片段信息列表
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    segments = []
    
    for i in range(len(split_points) - 1):
        start = split_points[i]
        end = split_points[i + 1]
        duration = end - start
        
        output_file = output_dir / f"{prefix}_{i}.mp3"
        
        LOGGER.info("Splitting segment %d: %.2f - %.2f (%.2f seconds)", i, start, end, duration)
        
        # 使用copy模式快速分割
        cmd = [
            "ffmpeg",
            "-ss", str(start),
            "-t", str(duration),
            "-i", str(audio_path),
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
            LOGGER.error("Failed to split segment %d: %s", i, result.stderr[:200])
            raise RuntimeError(f"Failed to split segment {i}")
        
        segments.append({
            "index": i,
            "file": str(output_file),
            "start_time": start,
            "end_time": end,
            "duration": duration
        })
        
        LOGGER.info("Segment %d created: %s", i, output_file)
    
    LOGGER.info("Split complete: %d segments created", len(segments))
    return segments
