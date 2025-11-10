"""音频处理模块"""

from .segment_splitter import (
    detect_silence_points,
    split_audio_at_points,
    get_audio_duration
)
from .parallel_asr import (
    parallel_transcribe,
    merge_transcripts,
    adjust_timestamps
)
from .segment_exporter import (
    export_with_segments
)

__all__ = [
    "detect_silence_points",
    "split_audio_at_points",
    "get_audio_duration",
    "parallel_transcribe",
    "merge_transcripts",
    "adjust_timestamps",
    "export_with_segments",
]
