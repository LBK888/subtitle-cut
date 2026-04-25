"""静音区间检测与评分工具。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

import numpy as np

from .schema import Transcript, Word
from .transform import TimeRange, invert_ranges


@dataclass
class SilenceCandidate:
    start: float
    end: float
    gap_before: float
    gap_after: float
    motion_score: float | None = None
    frame_count: int | None = None
    classification: str = "unknown"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, float | None | str]:
        return {
            "start": round(self.start, 6),
            "end": round(self.end, 6),
            "duration": round(self.duration, 6),
            "gap_before": round(self.gap_before, 6),
            "gap_after": round(self.gap_after, 6),
            "motion_score": None if self.motion_score is None else round(self.motion_score, 4),
            "frame_count": self.frame_count,
            "classification": self.classification,
        }


def detect_silence_candidates(
    transcript: Transcript,
    *,
    min_duration: float = 1.2,
    epsilon: float = 1e-3,
) -> List[SilenceCandidate]:
    """根据 Transcript 计算语音空窗区间。"""

    speech_ranges = _collect_speech_ranges(transcript)
    total_duration = _transcript_duration(transcript)
    if not speech_ranges:
        return []

    silence_ranges = invert_ranges(total_duration, speech_ranges)
    candidates: List[SilenceCandidate] = []

    for index, silence in enumerate(silence_ranges):
        duration = silence.end - silence.start
        if duration + epsilon < min_duration:
            continue
        prev_end = speech_ranges[index - 1].end if index > 0 else silence.start
        next_start = speech_ranges[index].start if index < len(speech_ranges) else silence.end
        gap_before = silence.start - prev_end
        gap_after = next_start - silence.end
        candidates.append(
            SilenceCandidate(
                start=silence.start,
                end=silence.end,
                gap_before=max(0.0, gap_before),
                gap_after=max(0.0, gap_after),
            )
        )

    return candidates


def score_silence_candidates(
    media_path: Path,
    candidates: Sequence[SilenceCandidate],
    *,
    ffmpeg_binary: str = "ffmpeg",
    fps: float = 2.0,
    scale: int = 64,
    motion_static_threshold: float = 1.5,
    motion_active_threshold: float = 8.0,
) -> None:
    """为静音候选段计算画面运动评分。"""

    if not candidates:
        return

    for candidate in candidates:
        score, frames = _compute_motion_score(
            media_path,
            candidate.start,
            candidate.end,
            ffmpeg_binary=ffmpeg_binary,
            fps=fps,
            scale=scale,
        )
        candidate.motion_score = score
        candidate.frame_count = frames
        if score is None:
            candidate.classification = "unknown"
        elif score < motion_static_threshold:
            candidate.classification = "static"
        elif score > motion_active_threshold:
            candidate.classification = "active"
        else:
            candidate.classification = "review"


def analyze_silence(
    transcript: Transcript,
    media_path: Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    min_duration: float = 1.2,
    fps: float = 2.0,
    scale: int = 64,
    motion_static_threshold: float = 1.5,
    motion_active_threshold: float = 8.0,
) -> List[SilenceCandidate]:
    """一站式检测并评分静音候选段。"""

    candidates = detect_silence_candidates(transcript, min_duration=min_duration)
    if not candidates:
        return []
    score_silence_candidates(
        media_path,
        candidates,
        ffmpeg_binary=ffmpeg_binary,
        fps=fps,
        scale=scale,
        motion_static_threshold=motion_static_threshold,
        motion_active_threshold=motion_active_threshold,
    )
    return candidates


def _collect_speech_ranges(transcript: Transcript) -> List[TimeRange]:
    ranges: List[TimeRange] = []
    for segment in transcript.segments:
        if segment.words:
            for start, end in _iter_word_times(segment.words):
                if end > start:
                    ranges.append(TimeRange(start=start, end=end))
        else:
            if segment.end > segment.start:
                ranges.append(TimeRange(start=segment.start, end=segment.end))

    if not ranges:
        return []

    ranges.sort(key=lambda item: item.start)
    merged: List[TimeRange] = [ranges[0]]
    for current in ranges[1:]:
        last = merged[-1]
        if current.start <= last.end:
            merged[-1] = TimeRange(start=last.start, end=max(last.end, current.end))
        else:
            merged.append(current)
    return merged


def _iter_word_times(words: Iterable[Word]) -> Iterable[tuple[float, float]]:
    for word in words:
        try:
            start = float(word.start)
            end = float(word.end)
        except (TypeError, ValueError):
            continue
        yield start, end


def _transcript_duration(transcript: Transcript) -> float:
    if not transcript.segments:
        return 0.0
    return max(segment.end for segment in transcript.segments)


def _compute_motion_score(
    media_path: Path,
    start: float,
    end: float,
    *,
    ffmpeg_binary: str,
    fps: float,
    scale: int,
) -> tuple[float | None, int | None]:
    duration = max(0.0, end - start)
    if duration <= 0.0:
        return None, None

    frame_size = scale * scale
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(media_path),
        "-vf",
        f"fps={fps},scale={scale}:{scale},format=gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return None, None

    if result.returncode != 0 or not result.stdout:
        return None, None

    data = np.frombuffer(result.stdout, dtype=np.uint8)
    if data.size < frame_size:
        frames = data.size // frame_size
        return 0.0, frames

    try:
        frames = data.reshape((-1, scale, scale)).astype(np.int16)
    except ValueError:
        return None, None

    frame_count = frames.shape[0]
    if frame_count < 2:
        return 0.0, frame_count

    diffs = np.abs(np.diff(frames, axis=0))
    score = float(diffs.mean())
    return score, frame_count
