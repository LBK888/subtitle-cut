"""波形数据生成辅助模块。"""

from __future__ import annotations

import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import numpy as np

from ..ffmpeg.utils import ensure_ffmpeg_available

# 单次读取 256KB，既能保持流式处理，又不会造成过多系统调用。
_READ_CHUNK_BYTES = 256 * 1024


class WaveformGenerationError(RuntimeError):
    """波形生成失败时的异常类型。"""


def generate_waveform_payload(
    media_path: Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    target_points: int = 2000,
    sample_rate: int = 400,
) -> Dict[str, Any]:
    """生成媒体文件的波形概览数据。

    参数:
        media_path: 媒体文件路径。
        ffmpeg_binary: ffmpeg 可执行文件路径。
        target_points: 期望压缩后的波形点数量。
        sample_rate: ffmpeg 下采样的采样率，越小越省内存。
    """

    media_path = media_path.expanduser().resolve()
    if not media_path.exists():
        raise FileNotFoundError(f"媒体文件不存在: {media_path}")
    if target_points <= 0:
        raise ValueError("target_points 必须为正数")
    if sample_rate <= 0:
        raise ValueError("sample_rate 必须为正数")

    ensure_ffmpeg_available(ffmpeg_binary)

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-filter:a",
        f"aresample={int(sample_rate)}",
        "-f",
        "f32le",
        "pipe:1",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    try:
        samples, sample_count = _read_waveform_samples(process.stdout)
        stderr_output = process.stderr.read()
        return_code = process.wait()
    finally:
        process.stdout.close()
        process.stderr.close()

    if return_code != 0:
        stderr_text = stderr_output.decode("utf-8", errors="ignore").strip()
        raise WaveformGenerationError(f"FFmpeg 生成波形失败: {stderr_text or return_code}")

    if sample_count == 0:
        return {
            "values": [],
            "duration": 0.0,
            "sample_rate": sample_rate,
            "min": 0.0,
            "max": 0.0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": str(media_path),
        }

    duration = float(sample_count) / float(sample_rate)
    values, min_value, max_value = _compress_waveform(samples, sample_count, target_points)

    return {
        "values": values,
        "duration": duration,
        "sample_rate": sample_rate,
        "min": min_value,
        "max": max_value,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(media_path),
    }


def _read_waveform_samples(stream: Any) -> Tuple[np.ndarray, int]:
    """读取 FFmpeg 输出的浮点 PCM 数据。"""

    chunks: Iterable[bytes]
    chunks = iter(lambda: stream.read(_READ_CHUNK_BYTES), b"")
    buffers = []
    total_bytes = 0
    for chunk in chunks:
        if not chunk:
            break
        buffers.append(chunk)
        total_bytes += len(chunk)

    if total_bytes == 0:
        return np.empty(0, dtype=np.float32), 0

    combined = b"".join(buffers)
    remainder = len(combined) % 4
    if remainder:
        combined = combined[: len(combined) - remainder]
        total_bytes = len(combined)

    samples = np.frombuffer(combined, dtype=np.float32, count=total_bytes // 4)
    return samples, samples.size


def _compress_waveform(
    samples: np.ndarray,
    sample_count: int,
    target_points: int,
) -> Tuple[list[float], float, float]:
    """对波形采样进行压缩，返回最大值序列及统计信息。"""

    if sample_count == 0:
        return [], 0.0, 0.0

    cleaned = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
    abs_samples = np.abs(cleaned)
    if abs_samples.size:
        min_value = float(abs_samples.min())
        max_value = float(abs_samples.max())
    else:
        min_value = 0.0
        max_value = 0.0

    if sample_count <= target_points:
        clipped = np.clip(abs_samples, 0.0, 1.0)
        min_value = float(clipped.min()) if clipped.size else 0.0
        max_value = float(clipped.max()) if clipped.size else 0.0
        return clipped.astype(np.float32).tolist(), min_value, max_value

    window = int(math.ceil(sample_count / target_points))
    indices = np.arange(0, sample_count, window, dtype=np.int64)
    reduced = np.maximum.reduceat(abs_samples, indices)
    reduced = reduced[:target_points]
    clipped = np.clip(reduced, 0.0, 1.0)
    min_value = float(clipped.min()) if clipped.size else 0.0
    max_value = float(clipped.max()) if clipped.size else 0.0
    return clipped.astype(np.float32).tolist(), min_value, max_value
