"""视频预分割模块 - 在关键帧处分割视频以支持并行处理"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional

LOGGER = logging.getLogger(__name__)

# 阈值：超过此时长的视频将被预分割（秒）
PRESPLIT_THRESHOLD = 1200  # 20分钟


def should_presplit_video(duration_seconds: float) -> bool:
    """判断视频是否应该预分割
    
    Args:
        duration_seconds: 视频时长（秒）
    
    Returns:
        是否应该预分割
    """
    return duration_seconds >= PRESPLIT_THRESHOLD


def calculate_segment_count(duration_seconds: float) -> int:
    """根据视频时长计算最优分段数
    
    策略：增加分段数，让每个片段的剪辑区间更少，从而可以多线程并行处理
    目标：每个片段2-3分钟，让CPU能多核并行处理filter_complex
    
    Args:
        duration_seconds: 视频时长（秒）
    
    Returns:
        分段数量
    """
    duration_minutes = duration_seconds / 60
    
    if duration_minutes < 20:
        return 1  # 不分割
    else:
        # 目标：每个片段2.5分钟，让每个片段的剪辑区间更少
        # 这样可以多线程并行处理，充分利用CPU多核
        target_segment_duration = 2.5  # 分钟
        segment_count = max(2, int(duration_minutes / target_segment_duration + 0.5))
        return segment_count


def probe_keyframes(video_path: Path, ffprobe_binary: str = "ffprobe") -> List[float]:
    """探测视频的所有关键帧时间戳
    
    Args:
        video_path: 视频文件路径
        ffprobe_binary: ffprobe可执行文件路径
    
    Returns:
        关键帧时间戳列表（秒）
    """
    cmd = [
        ffprobe_binary,
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=print_section=0",
        str(video_path)
    ]
    
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=True
        )
        
        keyframes = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                pts_time = parts[0]
                flags = parts[1]
                # K表示关键帧
                if 'K' in flags and pts_time != 'N/A':
                    try:
                        keyframes.append(float(pts_time))
                    except ValueError:
                        continue
        
        keyframes.sort()
        LOGGER.info("Found %d keyframes in video", len(keyframes))
        return keyframes
    
    except subprocess.CalledProcessError as e:
        LOGGER.error("Failed to probe keyframes: %s", e.stderr)
        raise RuntimeError(f"Failed to probe keyframes: {e.stderr[:200]}")


def find_optimal_split_points(
    keyframes: List[float],
    total_duration: float,
    min_duration: float = PRESPLIT_THRESHOLD,
    custom_segment_count: Optional[int] = None
) -> List[float]:
    """在关键帧中找到最接近目标时长的分割点
    
    Args:
        keyframes: 关键帧时间戳列表（秒）
        total_duration: 视频总时长（秒）
        min_duration: 最小触发分割的时长（秒）
    
    Returns:
        分割点时间戳列表（包含0和total_duration）
    """
    # 如果视频时长小于阈值，不分割
    if total_duration < min_duration:
        LOGGER.info("Video duration %.2fs < threshold %.2fs, no split needed", 
                   total_duration, min_duration)
        return [0, total_duration]
    
    # 计算最优分段数和目标时长
    if custom_segment_count and custom_segment_count >= 2:
        segment_count = custom_segment_count
        LOGGER.info("Using custom segment count: %d", segment_count)
    else:
        segment_count = calculate_segment_count(total_duration)
    target_duration = total_duration / segment_count
    
    LOGGER.info(
        "Video duration: %.2fs (%.2f min), target %d segments, ~%.2fs each",
        total_duration,
        total_duration / 60,
        segment_count,
        target_duration
    )
    
    split_points = [0.0]
    current_time = 0.0
    
    for kf_time in keyframes:
        # 如果当前段时长接近目标时长（±10%容差）
        if kf_time - current_time >= target_duration * 0.9:
            split_points.append(kf_time)
            current_time = kf_time
            
            # 如果已经有足够的分割点，停止
            if len(split_points) >= segment_count:
                break
    
    # 确保最后一个点是视频结尾
    if split_points[-1] != total_duration:
        split_points.append(total_duration)
    
    LOGGER.info("Split points: %s", split_points)
    return split_points


def split_video_at_keyframes(
    input_video: Path,
    output_dir: Path,
    split_points: List[float],
    *,
    ffmpeg_binary: str = "ffmpeg"
) -> List[dict]:
    """在指定的关键帧时间点分割视频
    
    Args:
        input_video: 输入视频文件
        output_dir: 输出目录
        split_points: 分割点时间戳列表（必须包含0和结尾）
        ffmpeg_binary: ffmpeg可执行文件路径
    
    Returns:
        片段信息列表，每个元素包含：
        {
            "index": 片段索引,
            "file": 片段文件路径,
            "start_time": 起始时间（秒）,
            "end_time": 结束时间（秒）,
            "duration": 时长（秒）
        }
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    segments = []
    
    for i in range(len(split_points) - 1):
        start_time = split_points[i]
        end_time = split_points[i + 1]
        duration = end_time - start_time
        
        # 生成输出文件名
        output_file = output_dir / f"segment_{i:03d}.mp4"
        
        LOGGER.info(
            "Splitting segment %d: %.2fs - %.2fs (%.2fs)",
            i, start_time, end_time, duration
        )
        
        # 使用 -ss 和 -t 参数精确分割
        # -ss 在 -i 之前：快速定位（可能不精确）
        # -ss 在 -i 之后：精确定位（但慢）
        # 结合使用：先快速定位到接近位置，再精确定位
        
        # 计算快速定位点（提前5秒）
        fast_seek = max(0, start_time - 5)
        accurate_seek = start_time - fast_seek
        
        cmd = [
            ffmpeg_binary,
            "-ss", str(fast_seek),  # 快速定位
            "-i", str(input_video),
            "-ss", str(accurate_seek),  # 精确定位
            "-t", str(duration),  # 时长
            "-c", "copy",  # 无损复制（在关键帧处分割）
            "-avoid_negative_ts", "make_zero",  # 避免负时间戳
            "-y",
            str(output_file)
        ]
        
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                check=True
            )
            
            segments.append({
                "index": i,
                "file": str(output_file),
                "start_time": start_time,
                "end_time": end_time,
                "duration": duration
            })
            
            LOGGER.info("Segment %d created: %s", i, output_file)
        
        except subprocess.CalledProcessError as e:
            LOGGER.error("Failed to split segment %d: %s", i, e.stderr[:200])
            raise RuntimeError(f"Failed to split segment {i}: {e.stderr[:200]}")
    
    return segments


def save_presplit_metadata(
    segments: List[dict],
    output_path: Path
) -> None:
    """保存预分割元数据
    
    Args:
        segments: 片段信息列表
        output_path: 元数据文件路径
    """
    metadata = {
        "version": "1.0",
        "segments": segments
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    LOGGER.info("Presplit metadata saved to %s", output_path)


def load_presplit_metadata(metadata_path: Path) -> Optional[List[dict]]:
    """加载预分割元数据
    
    Args:
        metadata_path: 元数据文件路径
    
    Returns:
        片段信息列表，如果文件不存在或无效则返回None
    """
    if not metadata_path.exists():
        return None
    
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        
        segments = metadata.get("segments", [])
        LOGGER.info("Loaded presplit metadata: %d segments", len(segments))
        return segments
    
    except Exception as e:
        LOGGER.warning("Failed to load presplit metadata: %s", e)
        return None
