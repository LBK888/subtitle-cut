"""支持预分割的ASR转录流程"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..audio import (
    detect_silence_points,
    split_audio_at_points,
    get_audio_duration,
    parallel_transcribe,
    merge_transcripts
)
from ..video import (
    should_presplit_video,
    probe_keyframes,
    find_optimal_split_points,
    split_video_at_keyframes,
    save_presplit_metadata as save_video_presplit_metadata,
)
from ..core.schema import Transcript
from .transcribe import transcribe_to_json

LOGGER = logging.getLogger(__name__)

# 阈值：超过此时长的音频将被预分割（秒）
PRESPLIT_THRESHOLD = 1800  # 30分钟（音频）
# 视频使用更低的阈值（在video.presplit中定义为20分钟）


def is_video_file(file_path: Path) -> bool:
    """判断文件是否为视频文件
    
    Args:
        file_path: 文件路径
    
    Returns:
        是否为视频文件
    """
    video_extensions = {".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm", ".m4v"}
    return file_path.suffix.lower() in video_extensions


def get_video_duration(video_path: Path, ffprobe_binary: str = "ffprobe") -> float:
    """获取视频时长
    
    Args:
        video_path: 视频文件路径
        ffprobe_binary: ffprobe可执行文件路径
    
    Returns:
        视频时长（秒）
    """
    cmd = [
        ffprobe_binary,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
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
        duration = float(result.stdout.strip())
        return duration
    except (subprocess.CalledProcessError, ValueError) as e:
        LOGGER.error("Failed to get video duration: %s", e)
        raise RuntimeError(f"Failed to get video duration: {e}")


def transcribe_with_presplit(
    input_media: Path,
    output_dir: Path,
    *,
    engine: str = "whisperx",
    model: str = "large-v2",
    device: str = "auto",
    compute_type: str = "auto",
    options: Dict[str, Any] | None = None,
    progress_callback: Callable[[float], None] | None = None,
    enable_presplit: bool = True,
    target_segment_duration: float = 900.0,  # 15分钟
    custom_segment_count: Optional[int] = None,  # 用户自定义段数
) -> tuple[Transcript, Optional[dict]]:
    """转录音频，自动判断是否需要预分割
    
    Args:
        input_media: 输入音频文件
        output_dir: 输出目录（用于存储分段文件）
        engine: ASR引擎
        model: 模型名称
        device: 设备
        compute_type: 计算类型
        options: 额外选项
        progress_callback: 进度回调
        enable_presplit: 是否启用预分割
        target_segment_duration: 目标分段时长（秒）
    
    Returns:
        (transcript, presplit_metadata)
        - transcript: 完整的转写结果
        - presplit_metadata: 如果使用了预分割，返回分段元数据；否则返回None
    """
    
    def emit_progress(fraction: float):
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    # 1. 检测媒体类型并获取时长
    is_video = is_video_file(input_media)
    
    try:
        if is_video:
            duration = get_video_duration(input_media)
            LOGGER.info("Video duration: %.2f seconds (%.2f minutes)", duration, duration / 60)
        else:
            duration = get_audio_duration(input_media)
            LOGGER.info("Audio duration: %.2f seconds (%.2f minutes)", duration, duration / 60)
    except Exception as e:
        LOGGER.error("Failed to get media duration: %s", e)
        raise
    
    # 2. 判断是否需要预分割
    # 视频使用20分钟阈值，音频使用30分钟阈值
    if is_video:
        needs_presplit = enable_presplit and should_presplit_video(duration)
        threshold_name = "video (20 min)"
    else:
        needs_presplit = enable_presplit and duration > PRESPLIT_THRESHOLD
        threshold_name = "audio (30 min)"
    
    LOGGER.info(
        "Presplit check: enable_presplit=%s, is_video=%s, duration=%.2f, needs_presplit=%s (%s)",
        enable_presplit,
        is_video,
        duration,
        needs_presplit,
        threshold_name
    )
    
    if not enable_presplit:
        LOGGER.info("Presplit disabled, using standard transcription")
        emit_progress(0.05)
        transcript = transcribe_to_json(
            input_media,
            engine=engine,
            model=model,
            device=device,
            compute_type=compute_type,
            options=options,
            progress_callback=lambda p: emit_progress(0.05 + 0.95 * p)
        )
        return transcript, None
    
    if not needs_presplit:
        LOGGER.info("Media is short, using standard transcription")
        emit_progress(0.05)
        
        # 使用标准转录
        transcript = transcribe_to_json(
            input_media,
            engine=engine,
            model=model,
            device=device,
            compute_type=compute_type,
            options=options,
            progress_callback=lambda p: emit_progress(0.05 + 0.95 * p)
        )
        
        return transcript, None
    
    # 3. 需要预分割
    LOGGER.info("=" * 60)
    LOGGER.info("USING PRESPLIT TRANSCRIPTION")
    LOGGER.info("Media type: %s, Duration: %.2f minutes", 
                "VIDEO" if is_video else "AUDIO",
                duration / 60)
    LOGGER.info("=" * 60)
    
    try:
        # 创建分段目录
        segments_dir = output_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Segments directory: %s", segments_dir)
        
        # 3.1 根据媒体类型进行分割
        if is_video:
            # 视频：在关键帧处分割
            LOGGER.info("Probing keyframes...")
            emit_progress(0.05)
            
            keyframes = probe_keyframes(input_media)
            
            LOGGER.info("Finding optimal split points...")
            split_points = find_optimal_split_points(keyframes, duration, custom_segment_count=custom_segment_count)
            
            LOGGER.info("Splitting video at %d keyframes...", len(split_points) - 1)
            emit_progress(0.10)
            
            segments = split_video_at_keyframes(
                input_media,
                segments_dir,
                split_points
            )
            
            # 保存视频预分割元数据
            video_metadata_file = output_dir / "video_presplit_metadata.json"
            save_video_presplit_metadata(segments, video_metadata_file)
            LOGGER.info("Video presplit metadata saved to %s", video_metadata_file)
        else:
            # 音频：在静音点处分割
            LOGGER.info("Detecting silence points...")
            emit_progress(0.05)
            
            split_points = detect_silence_points(
                input_media,
                target_segment_duration=target_segment_duration
            )
            
            LOGGER.info("Splitting audio at %d points...", len(split_points) - 1)
            emit_progress(0.10)
            
            segments = split_audio_at_points(
                input_media,
                split_points,
                segments_dir,
                prefix="seg"  # 使用更短的前缀
            )
    except Exception as e:
        LOGGER.error("Presplit failed: %s, falling back to standard transcription", e)
        LOGGER.exception("Presplit error details:")
        
        # 回退到标准转录
        transcript = transcribe_to_json(
            input_media,
            engine=engine,
            model=model,
            device=device,
            compute_type=compute_type,
            options=options,
            progress_callback=lambda p: emit_progress(0.05 + 0.95 * p)
        )
        return transcript, None
    
    LOGGER.info("Created %d segments", len(segments))
    emit_progress(0.15)
    
    # 3.2 并行转录所有片段
    LOGGER.info("Starting parallel transcription of %d segments...", len(segments))
    
    # 优化：只加载一次模型，复用于所有片段
    from .models import ModelConfig, load_asr_components
    from .transcribe import _transcribe_with_bundle
    
    LOGGER.info("Loading ASR model once for all segments...")
    config = ModelConfig(
        engine=engine,
        name=model,
        device=device,
        compute_type=compute_type,
        options={} if options is None else dict(options),
    )
    bundle = load_asr_components(config)
    LOGGER.info("Model loaded successfully, will be reused for all %d segments", len(segments))
    
    def transcribe_segment(segment_file: str, **kwargs) -> dict:
        """转录单个片段的包装函数（复用已加载的模型）"""
        result = _transcribe_with_bundle(
            bundle,
            segment_file,
            engine=engine,
            progress_callback=None  # 单个片段不需要进度回调
        )
        return result.model_dump()
    
    segment_transcripts = parallel_transcribe(
        segments,
        transcribe_segment,
        segments_dir,
        progress_callback=lambda p: emit_progress(0.15 + 0.75 * p)
    )
    
    # 3.3 合并转写结果
    LOGGER.info("Merging transcripts...")
    emit_progress(0.90)
    
    merged_file = output_dir / "merged_transcript.json"
    merged_transcript_dict = merge_transcripts(segment_transcripts, merged_file)
    
    # 转换为Transcript对象
    transcript = Transcript.model_validate(merged_transcript_dict)
    
    # 3.4 构建元数据
    presplit_metadata = {
        "is_presplit": True,
        "media_type": "video" if is_video else "audio",
        "num_segments": len(segments),
        "segments": segments,
        "segment_transcripts": [
            {
                "index": st["index"],
                "start_offset": st["start_offset"],
                "transcript_file": st["transcript_file"]
            }
            for st in segment_transcripts
        ],
        "merged_transcript_file": str(merged_file),
        "split_points": split_points
    }
    
    # 保存元数据
    metadata_file = output_dir / "presplit_metadata.json"
    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump(presplit_metadata, f, ensure_ascii=False, indent=2)
    
    LOGGER.info("Presplit metadata saved to %s", metadata_file)
    
    emit_progress(1.0)
    
    return transcript, presplit_metadata
