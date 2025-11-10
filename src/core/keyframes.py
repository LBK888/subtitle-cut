"""关键帧相关工具。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Union

PathLike = Union[str, Path]


def probe_keyframes(path: PathLike) -> List[float]:
    """使用 ffprobe 获取关键帧时间。"""

    command = [
        "ffprobe",
        "-hide_banner",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=pkt_pts_time,best_effort_timestamp_time,key_frame",
        "-of",
        "json",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    frames = payload.get("frames", [])
    keyframes: List[float] = []
    for frame in frames:
        is_key_raw = frame.get("key_frame", 0)
        try:
            is_key = int(is_key_raw) == 1
        except (TypeError, ValueError):
            continue
        if not is_key:
            continue
        pts_raw = frame.get("pkt_pts_time")
        fallback_raw = frame.get("best_effort_timestamp_time")
        pts_value: float | None = None
        for candidate in (pts_raw, fallback_raw):
            if candidate is None:
                continue
            try:
                pts_value = float(candidate)
            except (TypeError, ValueError):
                continue
            else:
                break
        if pts_value is None:
            continue
        keyframes.append(max(pts_value, 0.0))

    keyframes.sort()
    return keyframes


def snap_ranges_to_keyframes(
    ranges: Sequence[Tuple[float, float]],
    keyframes: Iterable[float],
) -> List[Tuple[float, float]]:
    """将时间区间吸附到关键帧。"""

    keyframe_list = sorted({round(max(k, 0.0), 6) for k in keyframes})
    if not keyframe_list:
        return list(ranges)

    snapped: List[Tuple[float, float]] = []
    for start, end in ranges:
        if end <= start:
            continue
        snapped_start = _snap_to_previous(start, keyframe_list)
        snapped_end = _snap_to_next(end, keyframe_list)
        snapped.append((snapped_start, max(snapped_end, snapped_start)))

    return snapped


def _snap_to_previous(value: float, keyframes: Sequence[float]) -> float:
    candidate = keyframes[0]
    for frame in keyframes:
        if frame > value:
            break
        candidate = frame
    return candidate


def _snap_to_next(value: float, keyframes: Sequence[float]) -> float:
    for frame in keyframes:
        if frame >= value:
            return frame
    return keyframes[-1]

