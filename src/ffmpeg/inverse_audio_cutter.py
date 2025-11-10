"""反向音频剪辑器 - 基于删除区间而不是保留区间

核心思路：
1. 从原始音频中删除指定的片段
2. 而不是提取保留片段再拼接
3. 使用filter_complex一次性完成
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)


def inverse_cut_audio(
    input_path: Path,
    output_path: Path,
    keep_ranges: Sequence[Tuple[float, float]],
    *,
    ffmpeg_binary: str = "ffmpeg",
    audio_codec: str = "libmp3lame",
    audio_bitrate: str = "192k",
    progress_callback: Optional[Callable[[float], None]] = None,
) -> None:
    """基于保留区间的反向剪辑
    
    策略：
    1. 使用atrim过滤器提取每个保留区间
    2. 使用concat过滤器一次性拼接
    3. 只调用一次FFmpeg
    
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
        raise ValueError("No ranges to keep")
    
    LOGGER.info("Inverse audio cutting: %d keep ranges", len(keep_ranges))
    
    def emit_progress(fraction: float) -> None:
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 构建filter_complex
    # 为每个保留区间创建一个atrim
    filter_parts = []
    for i, (start, end) in enumerate(keep_ranges):
        # atrim: 提取指定时间段
        # asetpts: 重置时间戳
        filter_parts.append(
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        )
    
    # 拼接所有区间
    concat_inputs = "".join(f"[a{i}]" for i in range(len(keep_ranges)))
    filter_parts.append(
        f"{concat_inputs}concat=n={len(keep_ranges)}:v=0:a=1[out]"
    )
    
    filter_complex = ";".join(filter_parts)
    
    LOGGER.info("Filter complexity: %d ranges, %d characters", len(keep_ranges), len(filter_complex))
    
    # 构建FFmpeg命令
    cmd = [
        ffmpeg_binary,
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", audio_codec,
        "-b:a", audio_bitrate,
        "-y",
        str(output_path),
    ]
    
    LOGGER.info("Executing FFmpeg with filter_complex...")
    
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        
        if result.returncode != 0:
            LOGGER.error("FFmpeg failed: %s", result.stderr[:500])
            raise RuntimeError(f"FFmpeg failed: {result.stderr[:200]}")
        
        emit_progress(1.0)
        LOGGER.info("Inverse audio cutting completed: %s", output_path)
    
    except Exception as e:
        LOGGER.error("Inverse audio cutting failed: %s", e)
        raise
