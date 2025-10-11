"""SRT/VTT 转换工具模块。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Tuple, Union

from .schema import Segment, Transcript, Word

PathLike = Union[str, Path]

_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
)


def load_srt(path: PathLike) -> Transcript:
    """读取 SRT 文件并转换为统一 Transcript。"""
    content = Path(path).read_text(encoding="utf-8")
    blocks = re.split(r"\r?\n\r?\n", content.strip())
    segments: List[Segment] = []

    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        index = 0
        if lines[index].isdigit():
            index += 1

        if index >= len(lines):
            continue

        match = _TIMESTAMP_PATTERN.match(lines[index])
        if not match:
            continue

        start = _parse_timestamp(match.group("start"))
        end = _parse_timestamp(match.group("end"))
        text_lines = lines[index + 1 :]
        text = "\n".join(text_lines).strip()

        segments.append(
            Segment(
                start=start,
                end=end,
                text=text,
                words=_derive_words_from_text(text_lines, start, end),
            )
        )

    return Transcript(segments=segments)


def dump_srt(transcript: Transcript, path: PathLike) -> None:
    """将 Transcript 导出为 SRT 文件。"""
    lines: List[str] = []
    for index, segment in enumerate(transcript.segments, start=1):
        start, end = _segment_time_span(segment)
        text = _segment_text(segment)
        lines.append(str(index))
        lines.append(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}")
        lines.extend(text.splitlines() or [""])
        lines.append("")

    Path(path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def dump_vtt(transcript: Transcript, path: PathLike) -> None:
    """将 Transcript 导出为 VTT 文件。"""
    lines: List[str] = ["WEBVTT", ""]
    for segment in transcript.segments:
        start, end = _segment_time_span(segment)
        text = _segment_text(segment)
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.extend(text.splitlines() or [""])
        lines.append("")

    Path(path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _parse_timestamp(value: str) -> float:
    hours, minutes, rest = value.split(":", 2)
    seconds, millis = rest.split(",")
    total_ms = (
        int(hours) * 3600 * 1000
        + int(minutes) * 60 * 1000
        + int(seconds) * 1000
        + int(millis)
    )
    return total_ms / 1000.0


def _format_srt_timestamp(value: float) -> str:
    hours, minutes, seconds, millis = _split_seconds(value)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _format_vtt_timestamp(value: float) -> str:
    hours, minutes, seconds, millis = _split_seconds(value)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def _split_seconds(value: float) -> Tuple[int, int, int, int]:
    total_millis = int(round(max(value, 0.0) * 1000))
    millis = total_millis % 1000
    total_seconds = total_millis // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return hours, minutes, seconds, millis


def _segment_time_span(segment: Segment) -> Tuple[float, float]:
    if segment.words:
        start = min(word.start for word in segment.words)
        end = max(word.end for word in segment.words)
        return start, end
    return segment.start, segment.end


def _segment_text(segment: Segment) -> str:
    if segment.text.strip():
        return segment.text
    if segment.words:
        return "".join(word.text for word in segment.words)
    return ""


def _derive_words_from_text(lines: Iterable[str], start: float, end: float) -> List[Word]:
    text = " ".join(lines).strip()
    if not text:
        return []
    duration = max(end - start, 0.0)
    tokens = text.split()
    if not tokens or duration <= 0:
        return []

    step = duration / len(tokens)
    words: List[Word] = []
    current_start = start
    for token in tokens:
        current_end = min(current_start + step, end)
        words.append(Word(text=token, start=current_start, end=current_end, conf=None))
        current_start = current_end
    if words:
        words[-1].end = end
    return words
