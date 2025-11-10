"""并行ASR处理模块"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional

LOGGER = logging.getLogger(__name__)


def parallel_transcribe(
    segments: List[dict],
    transcribe_func: Callable,
    output_dir: Path,
    progress_callback: Optional[Callable[[float], None]] = None,
    max_workers: Optional[int] = None,
    **transcribe_kwargs
) -> List[dict]:
    """并行转写所有片段
    
    Args:
        segments: 片段信息列表
        transcribe_func: 转写函数（如whisper_model.transcribe）
        output_dir: 输出目录
        progress_callback: 进度回调
        max_workers: 最大并行worker数量（None=自动检测）
        **transcribe_kwargs: 传递给转写函数的参数
    
    Returns:
        segment_transcripts: 片段转写结果列表
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 如果用户没有指定max_workers，自动检测
    if max_workers is None:
        # GPU任务不应该并行太多，否则显存不足导致性能下降
        cpu_count = os.cpu_count() or 4
        
        # 检查是否使用GPU
        device = transcribe_kwargs.get("device", "auto")
        if device in ["cuda", "gpu", "auto"]:
            # GPU模式：默认串行，确保最佳性能
            # 用户可以通过环境变量或配置文件调整
            max_workers = 1
            LOGGER.info("GPU mode detected: using %d worker to avoid VRAM overflow", max_workers)
            LOGGER.info("Tip: If you have >12GB VRAM, you can set max_workers=2 for faster processing")
        else:
            # CPU模式：可以使用多个worker
            max_workers = min(len(segments), cpu_count)
            LOGGER.info("CPU mode: using %d workers", max_workers)
    else:
        LOGGER.info("Using user-specified max_workers: %d", max_workers)
    
    LOGGER.info("Starting parallel ASR: %d segments, %d workers", len(segments), max_workers)
    
    def emit_progress(fraction: float):
        if progress_callback:
            progress_callback(max(0.0, min(1.0, fraction)))
    
    emit_progress(0.0)
    
    completed = [0]
    results = [None] * len(segments)
    
    def transcribe_segment(seg: dict) -> tuple:
        """转写单个片段"""
        index = seg["index"]
        audio_file = seg["file"]
        start_offset = seg["start_time"]
        
        LOGGER.info("Transcribing segment %d: %s", index, audio_file)
        
        try:
            # 执行转写
            transcript = transcribe_func(audio_file, **transcribe_kwargs)
            
            # 调整时间戳
            adjusted_transcript = adjust_timestamps(transcript, start_offset)
            
            # 保存到文件
            output_file = output_dir / f"segment_{index}.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(adjusted_transcript, f, ensure_ascii=False, indent=2)
            
            LOGGER.info("Segment %d transcribed successfully", index)
            
            completed[0] += 1
            emit_progress(completed[0] / len(segments))
            
            return index, {
                "index": index,
                "start_offset": start_offset,
                "transcript": adjusted_transcript,
                "transcript_file": str(output_file)
            }
        
        except Exception as e:
            LOGGER.error("Failed to transcribe segment %d: %s", index, e)
            raise
    
    # 并行处理
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(transcribe_segment, seg): seg
            for seg in segments
        }
        
        for future in as_completed(futures):
            try:
                index, result = future.result()
                results[index] = result
            except Exception as e:
                LOGGER.error("Segment transcription failed: %s", e)
                raise
    
    emit_progress(1.0)
    LOGGER.info("Parallel ASR complete: %d segments", len(results))
    
    return results


def adjust_timestamps(transcript: dict, offset: float) -> dict:
    """调整转写结果的时间戳
    
    Args:
        transcript: Whisper转写结果
        offset: 时间偏移（秒）
    
    Returns:
        adjusted_transcript: 调整后的转写结果
    """
    adjusted = transcript.copy()
    
    # 调整segments
    if "segments" in adjusted:
        adjusted["segments"] = [
            {
                **seg,
                "start": seg["start"] + offset,
                "end": seg["end"] + offset,
                "words": [
                    {
                        **word,
                        "start": word.get("start", 0) + offset,
                        "end": word.get("end", 0) + offset
                    }
                    for word in seg.get("words", [])
                ] if "words" in seg else []
            }
            for seg in adjusted["segments"]
        ]
    
    # 调整words（如果有顶层words）
    if "words" in adjusted:
        adjusted["words"] = [
            {
                **word,
                "start": word.get("start", 0) + offset,
                "end": word.get("end", 0) + offset
            }
            for word in adjusted["words"]
        ]
    
    return adjusted


def merge_transcripts(segment_transcripts: List[dict], output_file: Path) -> dict:
    """合并所有片段的转写结果
    
    Args:
        segment_transcripts: 片段转写结果列表
        output_file: 输出文件路径
    
    Returns:
        merged_transcript: 合并后的完整转写
    """
    LOGGER.info("Merging %d segment transcripts", len(segment_transcripts))
    
    # 按index排序
    sorted_transcripts = sorted(segment_transcripts, key=lambda x: x["index"])
    
    # 合并segments
    all_segments = []
    for st in sorted_transcripts:
        transcript = st["transcript"]
        if "segments" in transcript:
            all_segments.extend(transcript["segments"])
    
    # 合并words（如果有）
    all_words = []
    for st in sorted_transcripts:
        transcript = st["transcript"]
        if "words" in transcript:
            all_words.extend(transcript["words"])
    
    # 合并text
    all_text = " ".join(
        st["transcript"].get("text", "")
        for st in sorted_transcripts
    )
    
    # 构建合并结果
    merged = {
        "text": all_text,
        "segments": all_segments,
        "language": sorted_transcripts[0]["transcript"].get("language", "zh"),
        "_metadata": {
            "is_presplit": True,
            "num_segments": len(sorted_transcripts),
            "segment_boundaries": [
                {
                    "index": st["index"],
                    "start_offset": st["start_offset"],
                    "transcript_file": st["transcript_file"]
                }
                for st in sorted_transcripts
            ]
        }
    }
    
    # 如果有words，也添加
    if all_words:
        merged["words"] = all_words
    
    # 保存
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    
    LOGGER.info("Merged transcript saved to %s", output_file)
    LOGGER.info("Total segments: %d, Total words: %d", len(all_segments), len(all_words))
    
    return merged
