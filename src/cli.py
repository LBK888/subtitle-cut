"""命令行入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import click

from .asr.transcribe import transcribe_to_json
from .core.keyframes import probe_keyframes, snap_ranges_to_keyframes
from .core.schema import Transcript
from .core.silence import analyze_silence
from .core.srt_vtt import dump_srt, dump_vtt, load_srt
from .core.transform import (
    TimeRange,
    compute_delete_ranges,
    derive_keep_ranges,
    invert_ranges,
    rebase_transcript_after_cuts,
)
from .ffmpeg.cutter import cut_video
from .ffmpeg.utils import ensure_ffmpeg_available


@click.group()
@click.option("--ffmpeg", "ffmpeg_binary", default="ffmpeg", show_default=True, help="FFmpeg 可执行文件路径")
@click.pass_context
def cli(ctx: click.Context, ffmpeg_binary: str) -> None:
    """字幕剪辑工具的命令行界面。"""

    ctx.ensure_object(dict)
    ctx.obj["ffmpeg_binary"] = ffmpeg_binary


@cli.command()
@click.option("--input", "input_path", required=True, type=click.Path(path_type=Path))
@click.option(
    "--engine",
    type=click.Choice(["whisperx", "qwen3-asr"]),
    default="whisperx",
    show_default=True,
)
@click.option("--model", default="large-v2")
@click.option("--device", default="auto")
@click.option("--out", "output_path", type=click.Path(path_type=Path), required=True)
def asr(
    input_path: Path,
    engine: str,
    model: str,
    device: str,
    output_path: Path,
) -> None:
    """执行 ASR 转录并输出 JSON。"""

    transcript = transcribe_to_json(
        input_path,
        engine=engine,
        model=model,
        device=device,
    )
    output_path.write_text(
        json.dumps(transcript.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    click.echo(f"Saved transcript to {output_path}")


@cli.command()
@click.option("--transcript", "transcript_path", required=True, type=click.Path(path_type=Path))
@click.option("--delete-words", "delete_words", default="", help="以空格分隔的要删除词语")
@click.option("--delete-words-file", "delete_words_file", type=click.Path(path_type=Path))
@click.option("--merge-gap-ms", default=120.0, type=float)
@click.option("--padding-ms", default=80.0, type=float)
@click.option(
    "--out",
    "output_path",
    type=click.Path(path_type=Path),
    default=Path("delete_ranges.json"),
)
def plan(
    transcript_path: Path,
    delete_words: str,
    delete_words_file: Optional[Path],
    merge_gap_ms: float,
    padding_ms: float,
    output_path: Path,
) -> None:
    """根据要删除的词语生成删除时间段。"""

    transcript = _load_transcript(transcript_path)
    targets = _collect_delete_words(delete_words, delete_words_file)
    delete_ranges = compute_delete_ranges(
        transcript,
        targets,
        merge_gap_ms=merge_gap_ms,
        padding_ms=padding_ms,
    )

    total_duration = _transcript_duration(transcript)
    keep_ranges = derive_keep_ranges(transcript, delete_ranges)
    if not keep_ranges:
        keep_ranges = invert_ranges(total_duration, delete_ranges)

    payload = {
        "total_duration": total_duration,
        "delete_ranges": _ranges_to_dict(delete_ranges),
        "keep_ranges": _ranges_to_dict(keep_ranges),
    }

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    click.echo(f"Saved planning result to {output_path}")


@cli.command()
@click.option("--input", "input_path", required=True, type=click.Path(path_type=Path))
@click.option("--keep", "keep_ranges_path", required=True, type=click.Path(path_type=Path))
@click.option("--output", "output_path", required=True, type=click.Path(path_type=Path))
@click.option("--reencode", type=click.Choice(["auto", "copy", "reencode", "nvenc"]), default="auto")
@click.option("--xfade-ms", default=0.0, type=float, show_default=True)
@click.option("--chunk-size", default=0, type=int, show_default=True)
@click.option("--snap-zero-cross", type=bool, default=True, show_default=True)
@click.pass_context
def cut(
    ctx: click.Context,
    input_path: Path,
    keep_ranges_path: Path,
    output_path: Path,
    reencode: str,
    xfade_ms: float,
    chunk_size: int,
    snap_zero_cross: bool,
) -> None:
    """基于保留区间剪辑视频。"""

    ffmpeg_binary = ctx.obj["ffmpeg_binary"]
    ensure_ffmpeg_available(ffmpeg_binary)

    keep_ranges, _ = _load_ranges_file(keep_ranges_path, prefer="keep")
    if not keep_ranges:
        raise click.UsageError("Keep ranges are empty; nothing to cut.")

    keep_tuples = [(rng.start, rng.end) for rng in keep_ranges]
    encoder_note = cut_video(
        input_path,
        output_path,
        keep_tuples,
        reencode=reencode,
        ffmpeg_binary=ffmpeg_binary,
        xfade_ms=xfade_ms,
        chunk_size=chunk_size,
        snap_zero_cross=snap_zero_cross,
    )
    if encoder_note:
        click.echo(encoder_note)
    click.echo(f"Exported clipped video to {output_path}")


@cli.command()
@click.option("--input", "input_path", required=True, type=click.Path(path_type=Path))
@click.option("--output", "output_path", required=True, type=click.Path(path_type=Path))
@click.option(
    "--engine",
    type=click.Choice(["whisperx", "qwen3-asr"]),
    default="whisperx",
    show_default=True,
)
@click.option("--model", default="large-v2", show_default=True)
@click.option("--device", default="auto", show_default=True)
@click.option("--export-srt", "export_srt", type=click.Path(path_type=Path))
@click.option("--export-vtt", "export_vtt", type=click.Path(path_type=Path))
@click.option("--delete-words", "delete_words", default="")
@click.option("--delete-words-file", "delete_words_file", type=click.Path(path_type=Path))
@click.option("--merge-gap-ms", default=120.0, type=float)
@click.option("--padding-ms", default=80.0, type=float)
@click.option("--snap", type=click.Choice(["none", "keyframe"]), default="keyframe")
@click.option("--reencode", type=click.Choice(["auto", "copy", "reencode"]), default="auto")
@click.option("--xfade-ms", default=0.0, type=float, show_default=True)
@click.option("--chunk-size", default=0, type=int, show_default=True)
@click.option("--snap-zero-cross", type=bool, default=True, show_default=True)
@click.pass_context
def run(
    ctx: click.Context,
    input_path: Path,
    output_path: Path,
    engine: str,
    model: str,
    device: str,
    export_srt: Optional[Path],
    export_vtt: Optional[Path],
    delete_words: str,
    delete_words_file: Optional[Path],
    merge_gap_ms: float,
    padding_ms: float,
    snap: str,
    reencode: str,
    xfade_ms: float,
    chunk_size: int,
    snap_zero_cross: bool,
) -> None:
    """执行一条龙处理流程。"""

    ffmpeg_binary = ctx.obj["ffmpeg_binary"]
    ensure_ffmpeg_available(ffmpeg_binary)

    transcript = transcribe_to_json(
        input_path,
        engine=engine,
        model=model,
        device=device,
    )
    targets = _collect_delete_words(delete_words, delete_words_file)
    delete_ranges = compute_delete_ranges(
        transcript,
        targets,
        merge_gap_ms=merge_gap_ms,
        padding_ms=padding_ms,
    )

    total_duration = _transcript_duration(transcript)
    keep_ranges = derive_keep_ranges(transcript, delete_ranges)
    if not keep_ranges:
        keep_ranges = invert_ranges(total_duration, delete_ranges)

    if snap == "keyframe":
        keyframes = probe_keyframes(input_path)
        if keyframes:
            snapped = snap_ranges_to_keyframes(
                [(rng.start, rng.end) for rng in keep_ranges], keyframes
            )
            keep_ranges = [TimeRange(start=s, end=e) for s, e in snapped]
            delete_ranges = invert_ranges(total_duration, keep_ranges)

    keep_tuples = [(rng.start, rng.end) for rng in keep_ranges]
    encoder_note = cut_video(
        input_path,
        output_path,
        keep_tuples,
        reencode=reencode,
        ffmpeg_binary=ffmpeg_binary,
        xfade_ms=xfade_ms,
        chunk_size=chunk_size,
        snap_zero_cross=snap_zero_cross,
    )
    if encoder_note:
        click.echo(encoder_note)

    rebased = rebase_transcript_after_cuts(
        transcript,
        delete_ranges,
        keep_ranges=keep_ranges,
    )

    if export_srt:
        dump_srt(rebased, export_srt)
        click.echo(f"Exported rebased subtitles to {export_srt}")
    if export_vtt:
        dump_vtt(rebased, export_vtt)
        click.echo(f"Exported rebased subtitles to {export_vtt}")

    click.echo(f"Workflow completed. Output video: {output_path}")


@cli.command()
@click.option("--input", "input_path", required=True, type=click.Path(path_type=Path))
@click.option("--transcript", "transcript_path", required=True, type=click.Path(path_type=Path))
@click.option(
    "--out",
    "output_path",
    type=click.Path(path_type=Path),
    default=Path("silence_candidates.json"),
    show_default=True,
)
@click.option("--min-duration", default=1.2, type=float, show_default=True)
@click.option("--fps", default=2.0, type=float, show_default=True)
@click.option("--scale", default=64, type=int, show_default=True)
@click.pass_context
def silence(
    ctx: click.Context,
    input_path: Path,
    transcript_path: Path,
    output_path: Path,
    min_duration: float,
    fps: float,
    scale: int,
) -> None:
    """分析静音候选并生成报告。"""

    ffmpeg_binary = ctx.obj.get("ffmpeg_binary", "ffmpeg")
    ensure_ffmpeg_available(ffmpeg_binary)

    transcript = _load_transcript(transcript_path)
    candidates = analyze_silence(
        transcript,
        input_path,
        ffmpeg_binary=ffmpeg_binary,
        min_duration=max(0.1, min_duration),
        fps=max(0.5, fps),
        scale=max(16, scale),
    )

    payload = {
        "media_path": str(input_path),
        "min_duration": min_duration,
        "fps": fps,
        "scale": scale,
        "candidates": [candidate.to_dict() for candidate in candidates],
    }

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    click.echo(f"Generated {len(candidates)} silence candidates -> {output_path}")


def main() -> None:
    """命令行入口。"""

    cli()


def _load_transcript(path: Path) -> Transcript:
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return load_srt(path)

    content = path.read_text(encoding="utf-8")
    return Transcript.model_validate_json(content)


def _collect_delete_words(words: str, words_file: Optional[Path]) -> List[str]:
    tokens = [token.strip() for token in words.split() if token.strip()]
    if words_file and words_file.exists():
        file_tokens = [
            line.strip()
            for line in words_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        tokens.extend(file_tokens)
    return tokens


def _ranges_to_dict(ranges: Sequence[TimeRange]) -> List[dict[str, float]]:
    return [
        {"start": round(rng.start, 6), "end": round(rng.end, 6)} for rng in ranges
    ]


def _dicts_to_ranges(items: Iterable[dict[str, float]]) -> List[TimeRange]:
    ranges: List[TimeRange] = []
    for item in items:
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        ranges.append(TimeRange(start=start, end=end))
    return ranges


def _load_ranges_file(path: Path, *, prefer: str) -> tuple[List[TimeRange], float]:
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        total_duration = float(data.get("total_duration", 0.0) or 0.0)
        if prefer == "keep" and "keep_ranges" in data:
            return _dicts_to_ranges(data["keep_ranges"]), total_duration
        if prefer == "delete" and "delete_ranges" in data:
            return _dicts_to_ranges(data["delete_ranges"]), total_duration
        if "delete_ranges" in data:
            delete_ranges = _dicts_to_ranges(data["delete_ranges"])
            keep = invert_ranges(total_duration, delete_ranges)
            return keep, total_duration
        if "keep_ranges" in data:
            keep = _dicts_to_ranges(data["keep_ranges"])
            delete_ranges = invert_ranges(total_duration, keep)
            return delete_ranges, total_duration

    if isinstance(data, list):
        ranges = _dicts_to_ranges(data)
        total = max((rng.end for rng in ranges), default=0.0)
        if prefer == "keep":
            return ranges, total
        return invert_ranges(total, ranges), total

    raise click.UsageError(f"Unsupported range file format: {path}")


def _transcript_duration(transcript: Transcript) -> float:
    return max((segment.end for segment in transcript.segments), default=0.0)


if __name__ == "__main__":
    main()
