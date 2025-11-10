"""基于 FFmpeg 的裁剪与拼接工具。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import io
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, List, Optional, Sequence, Tuple, Union

import numpy as np

from .utils import run_ffmpeg

PathLike = Union[str, Path]
TimeRange = Tuple[float, float]

LOGGER = logging.getLogger(__name__)

NVENC_XFADE_FALLBACK_NOTE = "检测到启用了交叉淡化，当前版本暂不支持使用 NVIDIA NVENC，将自动改用 libx264 编码。"


@dataclass
class FilterPlan:
    script_path: Path
    modes: List[str]
    expected_duration: float
    has_video: bool
    has_audio: bool


_STREAM_KIND_RE = re.compile(
    r"Stream #\d+:\d+(?:\[[^\]]*\]|\([^\)]*\))*:\s*(Video|Audio)\b", re.IGNORECASE
)

_AUDIO_OUTPUT_PROFILES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    ".mp3": ("mp3", ("-c:a", "libmp3lame", "-b:a", "192k"), ()),
    ".wav": ("wav", ("-c:a", "pcm_s16le"), ()),
    ".flac": ("flac", ("-c:a", "flac"), ()),
    ".ogg": ("ogg", ("-c:a", "libvorbis", "-q:a", "5"), ()),
    ".aac": ("adts", ("-c:a", "aac", "-b:a", "192k"), ()),
    ".m4a": ("mp4", ("-c:a", "aac", "-b:a", "192k"), ("-movflags", "+faststart")),
}

_DEFAULT_AUDIO_PROFILE = ".mp3"


def probe_media_streams(input_path: PathLike, ffmpeg_binary: str) -> tuple[bool, bool]:
    """探测媒体文件的流类型
    
    优先根据文件扩展名判断,避免误识别(如音频文件的封面图片被识别为视频流)
    """
    input_path_obj = Path(input_path)
    file_ext = input_path_obj.suffix.lower()
    
    # 定义音频和视频扩展名
    audio_extensions = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus", ".ape", ".alac"}
    video_extensions = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
    
    # 根据扩展名硬性判定
    if file_ext in audio_extensions:
        # 音频文件:只有音频,没有视频
        import sys
        print(f"[PROBE] {input_path_obj.name} identified as AUDIO-ONLY (extension: {file_ext})", file=sys.stderr, flush=True)
        LOGGER.info("probe_media_streams: %s identified as audio-only by extension", input_path_obj.name)
        return False, True
    
    if file_ext in video_extensions:
        # 视频文件:默认有视频和音频(即使没有音频也无妨)
        LOGGER.info("probe_media_streams: %s identified as video by extension", input_path_obj.name)
        return True, True
    
    # 未知扩展名,使用FFmpeg探测
    LOGGER.info("probe_media_streams: %s has unknown extension, using FFmpeg detection", input_path_obj.name)
    command = [ffmpeg_binary, "-hide_banner", "-i", str(input_path)]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    output = ""
    if result.stderr:
        output += result.stderr
    if result.stdout:
        output += result.stdout
    text = output
    matches = _STREAM_KIND_RE.findall(text)
    has_video = any(kind.lower() == "video" for kind in matches)
    has_audio = any(kind.lower() == "audio" for kind in matches)
    
    LOGGER.info(
        "probe_media_streams: %s -> video=%s, audio=%s (detected by FFmpeg)",
        input_path_obj.name,
        has_video,
        has_audio,
    )
    
    return has_video, has_audio


def _probe_average_frame_rate(
    input_path: PathLike,
    ffmpeg_binary: str,
) -> Optional[str]:
    """尝试读取源视频的平均帧率（形如 30000/1001）。"""
    ffprobe_binary = "ffprobe"
    ffmpeg_path = shutil.which(ffmpeg_binary)
    if ffmpeg_path:
        candidate = Path(ffmpeg_path).with_name("ffprobe")
        if candidate.exists():
            ffprobe_binary = str(candidate)
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    value = result.stdout.strip()
    if not value or value in {"0/0", "0"}:
        return None
    return value


def _create_filter_plan(
    ranges: Sequence[TimeRange],
    *,
    reencode: str,
    xfade_ms: float,
    has_video: bool,
    has_audio: bool,
    frame_rate_expr: Optional[str],
) -> FilterPlan:
    filter_parts: List[str] = []
    segment_durations: List[float] = []
    video_labels: List[str] = []
    audio_labels: List[str] = []

    trim_margin = 0.008
    total_segments = len(ranges)

    for idx, (start, end) in enumerate(ranges):
        segment_start = float(start)
        segment_end = float(end)
        segment_length = max(0.0, segment_end - segment_start)

        if total_segments > 1 and segment_length > 0.0:
            if idx > 0:
                prev_end = ranges[idx - 1][1]
                gap = segment_start - prev_end
                adjust = min(trim_margin, max(gap / 2, 0.0), segment_length / 4)
                segment_start = min(segment_end, segment_start + adjust)
            if idx < total_segments - 1:
                next_start = ranges[idx + 1][0]
                gap = next_start - segment_end
                adjust = min(trim_margin, max(gap / 2, 0.0), (segment_end - segment_start) / 4)
                segment_end = max(segment_start, segment_end - adjust)

        if segment_end <= segment_start:
            continue

        segment_duration = segment_end - segment_start
        segment_durations.append(segment_duration)

        if has_video:
            filter_parts.append(
                f"[0:v]trim=start={segment_start:.6f}:end={segment_end:.6f},setpts=PTS-STARTPTS[v{idx}]"
            )
            video_labels.append(f"v{idx}")
        if has_audio:
            filter_parts.append(
                f"[0:a]atrim=start={segment_start:.6f}:end={segment_end:.6f},asetpts=PTS-STARTPTS[a{idx}]"
            )
            audio_labels.append(f"a{idx}")

    video_enabled = has_video and bool(video_labels)
    audio_enabled = has_audio and bool(audio_labels)

    if has_video and not video_enabled:
        raise ValueError("未生成有效的视频片段")
    if has_audio and not audio_enabled:
        raise ValueError("未生成有效的音频片段")
    if not video_enabled and not audio_enabled:
        raise ValueError("未生成有效的剪辑片段")

    xfade_seconds = max(xfade_ms, 0.0) / 1000.0
    use_audio_crossfade = audio_enabled and len(audio_labels) > 1 and xfade_seconds > 0.0
    use_video_crossfade = video_enabled and len(video_labels) > 1 and xfade_seconds > 0.0
    if use_audio_crossfade or use_video_crossfade:
        pair_limits = [
            min(segment_durations[i - 1], segment_durations[i])
            for i in range(1, len(segment_durations))
        ]
        max_allowed = max(min(pair_limits) - 1e-3, 0.0) if pair_limits else 0.0
        if max_allowed <= 0.0:
            use_audio_crossfade = False
            use_video_crossfade = False
        else:
            xfade_seconds = min(xfade_seconds, max_allowed)

    raw_duration = float(sum(segment_durations))
    overlap = xfade_seconds * (len(segment_durations) - 1) if (use_audio_crossfade or use_video_crossfade) else 0.0
    expected_duration = max(0.0, raw_duration - overlap)
    if expected_duration <= 0.0 and raw_duration > 0.0:
        expected_duration = raw_duration

    if video_enabled:
        if use_video_crossfade:
            if audio_enabled:
                audio_prev = audio_labels[0]
                for idx in range(1, len(audio_labels)):
                    current = audio_labels[idx]
                    out_label = f"af_{idx}"
                    filter_parts.append(
                        f"[{audio_prev}][{current}]acrossfade=d={xfade_seconds:.6f}:curve1=tri:curve2=tri[{out_label}]"
                    )
                    audio_prev = out_label

            video_prev = video_labels[0]
            accumulated = segment_durations[0]
            for idx in range(1, len(video_labels)):
                current = video_labels[idx]
                out_label = f"vf_{idx}"
                offset = max(accumulated - xfade_seconds, 0.0)
                filter_parts.append(
                    f"[{video_prev}][{current}]xfade=transition=fade:duration={xfade_seconds:.6f}:offset={offset:.6f}[{out_label}]"
                )
                video_prev = out_label
                accumulated = accumulated + segment_durations[idx] - xfade_seconds

            final_video_label = video_prev
            if frame_rate_expr:
                filter_parts.append(f"[{final_video_label}]fps={frame_rate_expr}[vf_rate]")
                final_video_label = "vf_rate"
            filter_parts.append(f"[{final_video_label}]format=yuv420p[vout]")
            if audio_enabled:
                filter_parts.append(f"[{audio_prev}]anull[aout]")
        else:
            if audio_enabled:
                concat_inputs = "".join(
                    f"[{video_labels[idx]}][{audio_labels[idx]}]" for idx in range(len(video_labels))
                )
                filter_parts.append(
                    f"{concat_inputs}concat=n={len(video_labels)}:v=1:a=1[vcat][acat]"
                )
                final_video_label = "vcat"
                if frame_rate_expr:
                    filter_parts.append(f"[{final_video_label}]fps={frame_rate_expr}[vf_rate]")
                    final_video_label = "vf_rate"
                filter_parts.append(f"[{final_video_label}]format=yuv420p[vout]")
                filter_parts.append(f"[acat]anull[aout]")
            else:
                concat_inputs = "".join(f"[{video_labels[idx]}]" for idx in range(len(video_labels)))
                filter_parts.append(
                    f"{concat_inputs}concat=n={len(video_labels)}:v=1:a=0[vcat]"
                )
                final_video_label = "vcat"
                if frame_rate_expr:
                    filter_parts.append(f"[{final_video_label}]fps={frame_rate_expr}[vf_rate]")
                    final_video_label = "vf_rate"
                filter_parts.append(f"[{final_video_label}]format=yuv420p[vout]")
    elif audio_enabled:
        if use_audio_crossfade:
            audio_prev = audio_labels[0]
            for idx in range(1, len(audio_labels)):
                current = audio_labels[idx]
                out_label = f"af_{idx}"
                filter_parts.append(
                    f"[{audio_prev}][{current}]acrossfade=d={xfade_seconds:.6f}:curve1=tri:curve2=tri[{out_label}]"
                )
                audio_prev = out_label
            filter_parts.append(f"[{audio_prev}]anull[aout]")
        else:
            if len(audio_labels) == 1:
                filter_parts.append(f"[{audio_labels[0]}]anull[aout]")
            else:
                concat_inputs = "".join(f"[{label}]" for label in audio_labels)
                filter_parts.append(
                    f"{concat_inputs}concat=n={len(audio_labels)}:v=0:a=1[aout]"
                )

    filter_complex = ";\n".join(filter_parts)
    script_path = _create_filter_script(filter_complex)

    codec = (reencode or "auto").lower()
    if codec not in {"auto", "copy", "reencode", "nvenc"}:
        codec = "auto"
    if codec == "copy":
        codec = "auto"

    if video_enabled:
        modes = [codec]
        if codec == "nvenc":
            modes.append("auto")
    else:
        modes = ["audio"]

    return FilterPlan(
        script_path=script_path,
        modes=modes,
        expected_duration=expected_duration,
        has_video=video_enabled,
        has_audio=audio_enabled,
    )


def _build_encoder_command(
    input_path: PathLike,
    plan: FilterPlan,
    codec_mode: str,
    *,
    container: str,
    output_target: str,
) -> List[str]:
    cmd: List[str] = [
        "-i",
        str(input_path),
        "-filter_complex_script",
        str(plan.script_path),
    ]

    if plan.has_video:
        cmd.extend(["-map", "[vout]"])
    else:
        cmd.append("-vn")

    if plan.has_audio:
        cmd.extend(["-map", "[aout]"])
    else:
        cmd.append("-an")

    if plan.has_video:
        if codec_mode == "nvenc":
            cmd.extend(
                [
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "p4",
                    "-rc",
                    "vbr",
                    "-cq",
                    "19",
                    "-b:v",
                    "0",
                ]
            )
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18"])

    if plan.has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])

    if container == "mp4":
        cmd.extend(["-movflags", "+faststart", output_target])
    elif container == "mpegts":
        cmd.extend(
            [
                "-f",
                "mpegts",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                "-reset_timestamps",
                "1",
                output_target,
            ]
        )
    else:
        raise ValueError(f"Unsupported container: {container}")

    return cmd


def _remux_ts_chunks(ffmpeg_binary: str, chunks: Sequence[bytes], output_path: Path) -> None:
    if not chunks:
        raise ValueError("无任何可拼接的片段")
    ts_stream = b"".join(chunks)
    command = [
        ffmpeg_binary,
        "-y",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(output_path),
    ]
    # 注意: input是二进制数据,但stderr需要文本模式
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_data, stderr_data = process.communicate(input=ts_stream)
    
    if process.returncode != 0:
        stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else None
        raise subprocess.CalledProcessError(process.returncode, command, stderr=stderr_text)


def _execute_chunked_cut(
    input_path: PathLike,
    output_path: PathLike,
    keep_list: Sequence[TimeRange],
    *,
    codec: str,
    chunk_size: int,
    total_duration: float,
    ffmpeg_binary: str,
    xfade_ms: float,
    has_video: bool,
    has_audio: bool,
    frame_rate_expr: Optional[str],
    progress_callback: Callable[[float], None] | None,
    progress_start: float,
    progress_span: float,
) -> None:
    if not has_video:
        raise ValueError("chunked execution requires video stream")
    if chunk_size < 2:
        raise ValueError("chunk_size must be at least 2 for chunked execution")
    if total_duration <= 0.0:
        raise ValueError("无有效保留区间时长")

    def _emit(fraction: float) -> None:
        if progress_callback is None:
            return
        progress_callback(
            progress_start + progress_span * max(0.0, min(1.0, fraction))
        )

    chunks: list[dict[str, object]] = []
    index = 0
    while index < len(keep_list):
        group = keep_list[index : index + chunk_size]
        duration = float(sum(max(0.0, end - start) for start, end in group))
        if duration > 0.0:
            chunks.append(
                {"index": len(chunks), "ranges": group, "duration": duration}
            )
        index += chunk_size

    if not chunks:
        raise ValueError("无任何可执行的剪辑子任务")

    chunk_progress = [0.0 for _ in chunks]
    outputs: list[bytes] = [b"" for _ in chunks]
    progress_lock = threading.Lock()

    def update_progress_locked() -> None:
        aggregated = sum(
            chunks[i]["duration"] * chunk_progress[i]  # type: ignore[index]
            for i in range(len(chunks))
        )
        fraction = aggregated / total_duration if total_duration > 0.0 else 1.0
        _emit(fraction)

    _emit(0.0)

    if codec == "nvenc":
        max_parallel = min(2, len(chunks))
    else:
        cpu_count = os.cpu_count() or 1
        max_parallel = min(len(chunks), max(1, cpu_count // 2))
    max_parallel = max(1, max_parallel)

    def run_plan(
        plan: FilterPlan,
        *,
        local_progress: Callable[[float], None] | None,
        capture_stdout: bool,
        progress_duration: float | None,
    ) -> subprocess.CompletedProcess:
        last_error: subprocess.CalledProcessError | None = None
        for mode in plan.modes:
            command = _build_encoder_command(
                input_path,
                plan,
                mode,
                container="mpegts",
                output_target="pipe:1",
            )
            try:
                return run_ffmpeg(
                    command,
                    binary=ffmpeg_binary,
                    progress_callback=local_progress,
                    progress_duration=progress_duration,
                    capture_stdout=capture_stdout,
                )
            except subprocess.CalledProcessError as error:
                last_error = error
                if plan.modes[0] == "nvenc" and mode == "nvenc" and len(plan.modes) > 1:
                    LOGGER.warning("NVENC encoding failed, falling back to libx264: %s", error)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("FFmpeg execution failed without raising an error")

    def process_chunk(entry: dict[str, object]) -> tuple[int, bytes]:
        chunk_index = int(entry["index"])  # type: ignore[arg-type]
        ranges = entry["ranges"]  # type: ignore[assignment]
        duration = float(entry["duration"])  # type: ignore[arg-type]
        plan = _create_filter_plan(
            ranges,  # type: ignore[arg-type]
            reencode=codec,
            xfade_ms=xfade_ms,
            has_video=has_video,
            has_audio=has_audio,
            frame_rate_expr=frame_rate_expr,
        )
        try:
            progress_duration = plan.expected_duration if plan.expected_duration > 0.0 else duration

            if progress_callback is not None:

                def on_local(value: float) -> None:
                    clamped = max(0.0, min(1.0, value))
                    with progress_lock:
                        chunk_progress[chunk_index] = max(chunk_progress[chunk_index], clamped)
                        update_progress_locked()

            else:
                on_local = None

            result = run_plan(
                plan,
                local_progress=on_local if progress_callback is not None else None,
                capture_stdout=True,
                progress_duration=progress_duration if progress_callback is not None else None,
            )
            data = result.stdout or b""
            with progress_lock:
                chunk_progress[chunk_index] = 1.0
                update_progress_locked()
            return chunk_index, data
        finally:
            try:
                plan.script_path.unlink(missing_ok=True)
            except OSError:
                pass

    futures: list[Future] = []
    executor = ThreadPoolExecutor(max_workers=max_parallel)
    try:
        for item in chunks:
            futures.append(executor.submit(process_chunk, item))
        for future in as_completed(futures):
            idx, data = future.result()
            outputs[idx] = data
    except Exception:
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    _emit(1.0)

    _remux_ts_chunks(ffmpeg_binary, outputs, Path(output_path))


def cut_video(
    input_path: PathLike,
    output_path: PathLike,
    keep_ranges: Sequence[TimeRange],
    *,
    reencode: str = "auto",
    ffmpeg_binary: str = "ffmpeg",
    snap_zero_cross: bool = True,
    zero_cross_window_ms: float = 20.0,
    zero_cross_max_shift_ms: float = 10.0,
    xfade_ms: float = 0.0,
    chunk_size: int = 0,
    forced_streams: tuple[bool, bool] | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> Optional[str]:
    """执行视频裁剪并输出结果。

    返回值:
        当启用视频交叉淡化且使用 NVENC 编码需要回退时，返回提示信息；否则返回 None。
    """

    def _emit_progress(value: float) -> None:
        if progress_callback is None:
            return
        progress_callback(max(0.0, min(1.0, value)))

    _emit_progress(0.0)
    
    # ==================== 关键修复：根据输出文件扩展名预判类型 ====================
    output_path_obj = Path(output_path)
    output_suffix = output_path_obj.suffix.lower()
    
    # 定义音频输出扩展名
    audio_output_extensions = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}
    # 定义视频输出扩展名
    video_output_extensions = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
    
    is_audio_output = output_suffix in audio_output_extensions
    is_video_output = output_suffix in video_output_extensions
    
    LOGGER.info(
        "Output file type detection: %s -> audio_output=%s, video_output=%s",
        output_suffix,
        is_audio_output,
        is_video_output,
    )

    keep_list = [rng for rng in keep_ranges if rng[1] > rng[0]]
    if not keep_list:
        raise ValueError("keep_ranges 不能为空")

    keep_list.sort(key=lambda item: item[0])
    _emit_progress(0.05)

    merged: List[TimeRange] = []
    for start, end in keep_list:
        if not merged:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1e-6:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    preliminary = [
        (round(start, 6), round(end, 6))
        for start, end in merged
        if end - start >= 0.05 or len(merged) == 1
    ]
    if not preliminary:
        raise ValueError("剪辑段过短，无法生成视频")

    if snap_zero_cross:
        _emit_progress(0.08)
        snapped = _snap_ranges_to_zero_crossings(
            input_path,
            preliminary,
            ffmpeg_binary=ffmpeg_binary,
            window_ms=zero_cross_window_ms,
            max_shift_ms=zero_cross_max_shift_ms,
        )
    else:
        snapped = preliminary

    keep_list = [
        (round(start, 6), round(end, 6))
        for start, end in snapped
        if end > start
    ]
    if not keep_list:
        raise ValueError("零交叉调整后无有效剪辑区间")
    _emit_progress(0.12)

    total_keep_duration = float(sum(max(0.0, end - start) for start, end in keep_list))
    if total_keep_duration <= 0.0:
        raise ValueError("无有效保留区间时长")

    encoder_note: Optional[str] = None

    codec = reencode.lower()
    if codec not in {"auto", "copy", "reencode", "nvenc"}:
        codec = "auto"
    if codec == "copy":
        codec = "auto"

    chunk_size_value = 0
    if chunk_size and chunk_size > 1:
        try:
            chunk_size_value = int(chunk_size)
        except (TypeError, ValueError):
            chunk_size_value = 0
        else:
            if chunk_size_value < 2:
                chunk_size_value = 0
    
    # 智能调整chunk_size:对于长文件,自动使用更小的chunk_size
    if chunk_size_value > 0 and total_keep_duration > 3600:  # 超过1小时
        # 确保每个分块不超过15分钟
        max_duration_per_chunk = 900  # 15分钟
        estimated_duration_per_range = total_keep_duration / len(keep_list) if keep_list else 0
        if estimated_duration_per_range > 0:
            optimal_chunk_size = max(2, int(max_duration_per_chunk / estimated_duration_per_range))
            if optimal_chunk_size < chunk_size_value:
                LOGGER.info(
                    "Auto-adjusting chunk_size from %d to %d for long file (%.1f minutes)",
                    chunk_size_value,
                    optimal_chunk_size,
                    total_keep_duration / 60,
                )
                chunk_size_value = optimal_chunk_size

    frame_rate_expr: Optional[str] = None
    if forced_streams is not None:
        has_video, has_audio = forced_streams
    else:
        has_video, has_audio = probe_media_streams(input_path, ffmpeg_binary)
        if not has_video and not has_audio:
            suffix = Path(input_path).suffix.lower()
            if suffix in _AUDIO_OUTPUT_PROFILES:
                has_audio = True
                LOGGER.warning(
                    "media stream probing failed for %s, assuming audio-only based on extension",
                    input_path,
                )
            else:
                has_video = True
                LOGGER.warning(
                    "media stream probing failed for %s, assuming video stream is present",
                    input_path,
                )
    if has_video:
        frame_rate_expr = _probe_average_frame_rate(input_path, ffmpeg_binary)

    # ==================== 音频输出专用处理路径（完全独立） ====================
    # 如果输出文件是音频格式，直接进入音频专用处理逻辑
    if is_audio_output:
        import sys
        print("=" * 80, file=sys.stderr, flush=True)
        print("AUDIO OUTPUT DETECTED - Using optimized audio processing", file=sys.stderr, flush=True)
        print(f"Output: {output_suffix}, Ranges: {len(keep_list)}, Duration: {total_keep_duration / 60:.1f} min", file=sys.stderr, flush=True)
        print("=" * 80, file=sys.stderr, flush=True)
        
        LOGGER.info("=" * 80)
        LOGGER.info("AUDIO OUTPUT DETECTED - Using optimized audio processing")
        LOGGER.info("Output: %s, Ranges: %d, Duration: %.1f min", output_suffix, len(keep_list), total_keep_duration / 60)
        LOGGER.info("=" * 80)
        
        audio_profile = _AUDIO_OUTPUT_PROFILES.get(output_suffix)
        if audio_profile is None:
            audio_profile = _AUDIO_OUTPUT_PROFILES[_DEFAULT_AUDIO_PROFILE]
            LOGGER.warning("unknown audio extension %s, defaulting to mp3 encoding", output_suffix)
        
        # 对于纯音频,根据区间数量选择合适的剪辑策略
        # 注意：不使用物理分割策略，因为会破坏ASR时间戳的准确性
        if len(keep_list) >= 2:
            # 根据区间数量选择策略
            if len(keep_list) > 100:
                # 大量区间：使用单个filter_complex一次性处理
                print("=" * 80, file=sys.stderr, flush=True)
                print("USING SINGLE FILTER_COMPLEX - FOR MANY RANGES", file=sys.stderr, flush=True)
                print(f"File duration: {total_keep_duration / 60:.1f} minutes, {len(keep_list)} ranges", file=sys.stderr, flush=True)
                print("Strategy: Single FFmpeg call with filter_complex (no startup overhead)", file=sys.stderr, flush=True)
                print("=" * 80, file=sys.stderr, flush=True)
                
                LOGGER.warning("=" * 80)
                LOGGER.warning("USING SINGLE FILTER_COMPLEX - FOR MANY RANGES")
                LOGGER.warning("File duration: %.1f minutes, %d ranges", total_keep_duration / 60, len(keep_list))
                LOGGER.warning("Strategy: Single FFmpeg call with filter_complex")
                LOGGER.warning("=" * 80)
                
                from .inverse_audio_cutter import inverse_cut_audio
                
                _emit_progress(0.1)
                inverse_cut_audio(
                    Path(input_path),
                    Path(output_path),
                    keep_list,
                    ffmpeg_binary=ffmpeg_binary,
                    audio_codec="libmp3lame",
                    audio_bitrate="192k",
                    progress_callback=progress_callback,
                )
                _emit_progress(1.0)
                return None
            else:
                # 少量大区间：使用简单剪辑策略
                print("=" * 80, file=sys.stderr, flush=True)
                print("USING SIMPLIFIED AUDIO CUTTER - OPTIMIZED FOR RAM DISK", file=sys.stderr, flush=True)
                print(f"File duration: {total_keep_duration / 60:.1f} minutes, {len(keep_list)} ranges", file=sys.stderr, flush=True)
                print("Strategy: Direct parallel extraction (preserves ASR timestamp accuracy)", file=sys.stderr, flush=True)
                print("=" * 80, file=sys.stderr, flush=True)
                
                LOGGER.warning("=" * 80)
                LOGGER.warning("USING SIMPLIFIED AUDIO CUTTER - OPTIMIZED FOR RAM DISK")
                LOGGER.warning("File duration: %.1f minutes, %d ranges", total_keep_duration / 60, len(keep_list))
                LOGGER.warning("Strategy: Direct parallel extraction (preserves ASR timestamp accuracy)")
                LOGGER.warning("=" * 80)
                
                from .simple_audio_cutter import simple_cut_audio
                
                _emit_progress(0.1)
                simple_cut_audio(
                    Path(input_path),
                    Path(output_path),
                    keep_list,
                    ffmpeg_binary=ffmpeg_binary,
                    audio_codec="libmp3lame",
                    audio_bitrate="192k",
                    progress_callback=progress_callback,
                )
                _emit_progress(1.0)
                return None
        else:
            # 只有1个区间，使用传统方法（filter_complex）
            LOGGER.info("Only 1 range, using traditional filter_complex method for audio")
            # 继续往下执行，使用传统的filter_complex处理
            # 注意：这里不return，让它继续执行后面的通用逻辑
            pass
        
        # 为音频输出准备audio_profile（用于后续的传统处理路径）
        # 这个分支只在单区间或其他特殊情况下才会执行到
        audio_profile = _AUDIO_OUTPUT_PROFILES.get(output_suffix)
        if audio_profile is None:
            audio_profile = _AUDIO_OUTPUT_PROFILES[_DEFAULT_AUDIO_PROFILE]
    
    # ==================== 视频输出专用处理路径（完全独立） ====================
    # 从这里开始是视频处理逻辑，音频输出已经在上面处理完并返回了
    
    if codec == "nvenc" and has_video and len(keep_list) > 1 and xfade_ms > 0.0:
        encoder_note = NVENC_XFADE_FALLBACK_NOTE
        LOGGER.warning(encoder_note)
        codec = "auto"

    # 视频多线程分块处理判断（恢复旧版本的简洁逻辑）
    use_video_chunk = (
        has_video
        and chunk_size_value >= 2
        and len(keep_list) > chunk_size_value
        and xfade_ms <= 0.0
    )
    
    if use_video_chunk:
        LOGGER.info(
            "Video multi-threading ENABLED: ranges=%d, chunk_size=%d, duration=%.1f min",
            len(keep_list),
            chunk_size_value,
            total_keep_duration / 60,
        )
        _emit_progress(0.2)
        _execute_chunked_cut(
            input_path,
            output_path,
            keep_list,
            codec=codec,
            chunk_size=chunk_size_value,
            total_duration=total_keep_duration,
            ffmpeg_binary=ffmpeg_binary,
            xfade_ms=xfade_ms,
            has_video=has_video,
            has_audio=has_audio,
            frame_rate_expr=frame_rate_expr,
            progress_callback=progress_callback,
            progress_start=0.25,
            progress_span=0.65,
        )
        _emit_progress(0.92)
        _emit_progress(1.0)
        return encoder_note

    # 非分块处理：使用传统的filter_complex方式
    _emit_progress(0.2)

    plan = _create_filter_plan(
        keep_list,
        reencode=codec,
        xfade_ms=xfade_ms,
        has_video=has_video,
        has_audio=has_audio,
        frame_rate_expr=frame_rate_expr,
    )

    _emit_progress(0.25)
    
    # 确保audio_profile已定义（用于纯音频单区间的情况）
    # 注意：如果前面is_audio_output分支已经设置过，这里不会重复设置
    if not has_video and has_audio:
        if 'audio_profile' not in locals():
            audio_profile = _AUDIO_OUTPUT_PROFILES.get(output_suffix)
            if audio_profile is None:
                audio_profile = _AUDIO_OUTPUT_PROFILES[_DEFAULT_AUDIO_PROFILE]
                LOGGER.warning("unknown audio extension %s, defaulting to mp3 encoding", output_suffix)

    last_error: subprocess.CalledProcessError | None = None
    ffmpeg_start = 0.25
    ffmpeg_span = 0.65

    def _on_ffmpeg_progress(value: float) -> None:
        clamped = max(0.0, min(1.0, value))
        _emit_progress(ffmpeg_start + ffmpeg_span * clamped)

    try:
        for mode in plan.modes:
            if plan.has_video:
                command = _build_encoder_command(
                    input_path,
                    plan,
                    mode,
                    container="mp4",
                    output_target=str(output_path_obj),
                )
            else:
                container, codec_args, post_args = audio_profile
                command = [
                    "-i",
                    str(input_path),
                    "-filter_complex_script",
                    str(plan.script_path),
                    "-map",
                    "[aout]",
                ]
                if container:
                    command.extend(["-f", container])
                command.extend(codec_args)
                if post_args:
                    command.extend(post_args)
                command.append(str(output_path_obj))
            try:
                run_ffmpeg(
                    command,
                    binary=ffmpeg_binary,
                    progress_callback=_on_ffmpeg_progress,
                    progress_duration=plan.expected_duration if plan.expected_duration > 0.0 else None,
                )
                last_error = None
                break
            except subprocess.CalledProcessError as error:
                last_error = error
                if (
                    plan.has_video
                    and plan.modes
                    and plan.modes[0] == "nvenc"
                    and mode == "nvenc"
                    and len(plan.modes) > 1
                ):
                    LOGGER.warning("NVENC encoding failed, falling back to libx264: %s", error)
                    continue
                raise
        if last_error is not None:
            raise last_error
        _emit_progress(0.92)
    finally:
        try:
            plan.script_path.unlink(missing_ok=True)
        except OSError:
            pass

    _emit_progress(1.0)
    return encoder_note


def _snap_ranges_to_zero_crossings(
    input_path: PathLike,
    ranges: Sequence[TimeRange],
    *,
    ffmpeg_binary: str,
    window_ms: float,
    max_shift_ms: float,
    sample_rate: int = 48000,
) -> List[TimeRange]:
    adjusted: List[TimeRange] = []
    window_seconds = max(window_ms, 1.0) / 1000.0
    max_shift_seconds = max(max_shift_ms, 0.0) / 1000.0

    for start, end in ranges:
        new_start = _nearest_zero_crossing(
            input_path,
            target_time=start,
            ffmpeg_binary=ffmpeg_binary,
            window_seconds=window_seconds,
            max_shift_seconds=max_shift_seconds,
            sample_rate=sample_rate,
        )
        new_end = _nearest_zero_crossing(
            input_path,
            target_time=end,
            ffmpeg_binary=ffmpeg_binary,
            window_seconds=window_seconds,
            max_shift_seconds=max_shift_seconds,
            sample_rate=sample_rate,
        )

        if new_end <= new_start:
            adjusted.append((start, end))
        else:
            adjusted.append((new_start, new_end))

    return adjusted


def _nearest_zero_crossing(
    input_path: PathLike,
    *,
    target_time: float,
    ffmpeg_binary: str,
    window_seconds: float,
    max_shift_seconds: float,
    sample_rate: int,
) -> float:
    search_start = max(target_time - window_seconds, 0.0)
    duration = max(window_seconds * 2.0, 0.02)

    audio = _extract_pcm_snippet(
        input_path,
        start_time=search_start,
        duration=duration,
        sample_rate=sample_rate,
        ffmpeg_binary=ffmpeg_binary,
    )

    if audio is None or audio.size < 2:
        return target_time

    target_offset = target_time - search_start
    target_sample = target_offset * sample_rate
    max_shift_samples = int(max_shift_seconds * sample_rate)

    start_idx = max(int(round(target_sample)) - max_shift_samples, 0)
    end_idx = min(int(round(target_sample)) + max_shift_samples + 1, audio.size - 1)
    if end_idx <= start_idx:
        return target_time

    window = audio[start_idx : end_idx + 1]
    zero_crossings = np.where(np.diff(np.sign(window)) != 0)[0]

    candidates: List[float] = []
    for idx in zero_crossings:
        global_idx = start_idx + idx
        sample_a = audio[global_idx]
        sample_b = audio[global_idx + 1]
        denom = abs(sample_a) + abs(sample_b)
        fraction = abs(sample_a) / denom if denom else 0.0
        candidates.append(global_idx + fraction)

    if not candidates:
        min_idx = start_idx + int(np.argmin(np.abs(window)))
        candidates.append(float(min_idx))

    def _candidate_time(index_value: float) -> float:
        return search_start + index_value / sample_rate

    best_index = min(candidates, key=lambda idx_val: abs(_candidate_time(idx_val) - target_time))
    snapped_time = _candidate_time(best_index)

    lower_bound = target_time - max_shift_seconds
    upper_bound = target_time + max_shift_seconds
    return float(min(max(snapped_time, lower_bound), upper_bound))


def _extract_pcm_snippet(
    input_path: PathLike,
    *,
    start_time: float,
    duration: float,
    sample_rate: int,
    ffmpeg_binary: str,
) -> np.ndarray | None:
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_time:.6f}",
        "-i",
        str(input_path),
        "-t",
        f"{duration:.6f}",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if result.returncode != 0 or not result.stdout:
        return None

    return np.frombuffer(result.stdout, dtype="<f4")


def _create_filter_script(content: str) -> Path:
    with NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, suffix=".ffilter") as tmp:
        tmp.write(content)
        tmp.flush()
        path = Path(tmp.name)
    _ = path.read_text(encoding="utf-8")
    return path
