"""评估不同 ASR 引擎剪辑效果的对比脚本。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from src.asr.transcribe import transcribe_to_json
from src.core.schema import Segment, Transcript, Word


@dataclass
class AlignmentStats:
    matched: int
    avg_start_delta: float
    avg_end_delta: float
    p95_start_delta: float
    p95_end_delta: float
    large_start_shift: int
    large_end_shift: int


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 WhisperX 与 Paraformer 的剪辑效果")
    parser.add_argument("--orig", required=True, type=Path, help="原始媒体文件")
    parser.add_argument("--out_whisperx", required=True, type=Path, help="WhisperX 剪辑产出")
    parser.add_argument("--out_paraformer", required=True, type=Path, help="Paraformer 剪辑产出")
    parser.add_argument("--target_text", required=True, type=Path, help="编辑后的目标文本文件")
    parser.add_argument("--device", default="auto", help="运行设备 (auto/cpu/cuda)")
    parser.add_argument(
        "--boundary_threshold",
        type=float,
        default=0.12,
        help="判定边界偏差的阈值 (秒)",
    )

    args = parser.parse_args()

    target_text = args.target_text.read_text(encoding="utf-8").strip()

    print("[info] 开始转录原始媒体 (WhisperX)")
    orig_whisper = transcribe_to_json(
        args.orig,
        engine="whisperx",
        device=args.device,
    )

    print("[info] 开始转录原始媒体 (Paraformer)")
    orig_paraformer = transcribe_to_json(
        args.orig,
        engine="paraformer",
        device=args.device,
    )

    print("[info] 重新转录 WhisperX 剪辑")
    cut_whisper = transcribe_to_json(
        args.out_whisperx,
        engine="whisperx",
        device=args.device,
    )

    print("[info] 重新转录 Paraformer 剪辑")
    cut_paraformer = transcribe_to_json(
        args.out_paraformer,
        engine="paraformer",
        device=args.device,
    )

    whisper_alignment = compare_boundaries(
        orig_whisper,
        cut_whisper,
        threshold=args.boundary_threshold,
    )
    paraformer_alignment = compare_boundaries(
        orig_paraformer,
        cut_paraformer,
        threshold=args.boundary_threshold,
    )

    whisper_cer = cer(target_text, transcript_to_text(cut_whisper))
    paraformer_cer = cer(target_text, transcript_to_text(cut_paraformer))

    report = {
        "whisperx": {
            "char_error_rate": whisper_cer,
            "boundary": alignment_to_dict(whisper_alignment),
        },
        "paraformer": {
            "char_error_rate": paraformer_cer,
            "boundary": alignment_to_dict(paraformer_alignment),
        },
    }

    print(json.dumps(report, indent=2, ensure_ascii=False))


def alignment_to_dict(stats: AlignmentStats) -> dict[str, float | int]:
    return {
        "matched_words": stats.matched,
        "avg_start_delta": stats.avg_start_delta,
        "avg_end_delta": stats.avg_end_delta,
        "p95_start_delta": stats.p95_start_delta,
        "p95_end_delta": stats.p95_end_delta,
        "large_start_shift": stats.large_start_shift,
        "large_end_shift": stats.large_end_shift,
    }


def compare_boundaries(
    original: Transcript,
    candidate: Transcript,
    *,
    threshold: float,
) -> AlignmentStats:
    pairs = list(_align_words(original, candidate))
    if not pairs:
        return AlignmentStats(0, 0.0, 0.0, 0.0, 0.0, 0, 0)

    start_deltas: List[float] = []
    end_deltas: List[float] = []
    large_start = 0
    large_end = 0
    for ref, hyp in pairs:
        delta_start = abs((hyp.start or 0.0) - (ref.start or 0.0))
        delta_end = abs((hyp.end or 0.0) - (ref.end or ref.start or 0.0))
        start_deltas.append(delta_start)
        end_deltas.append(delta_end)
        if delta_start > threshold:
            large_start += 1
        if delta_end > threshold:
            large_end += 1

    return AlignmentStats(
        matched=len(pairs),
        avg_start_delta=float(sum(start_deltas) / len(start_deltas)),
        avg_end_delta=float(sum(end_deltas) / len(end_deltas)),
        p95_start_delta=float(_percentile(start_deltas, 95)),
        p95_end_delta=float(_percentile(end_deltas, 95)),
        large_start_shift=large_start,
        large_end_shift=large_end,
    )


def _align_words(reference: Transcript, candidate: Transcript) -> Iterable[Tuple[Word, Word]]:
    ref_words = _flatten_words(reference)
    hyp_words = _flatten_words(candidate)
    if not ref_words or not hyp_words:
        return []

    ref_texts = [word.text for word in ref_words]
    hyp_texts = [word.text for word in hyp_words]

    import difflib

    matcher = difflib.SequenceMatcher(None, ref_texts, hyp_texts)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                yield ref_words[i1 + offset], hyp_words[j1 + offset]


def _flatten_words(transcript: Transcript) -> List[Word]:
    words: List[Word] = []
    for segment in transcript.segments:
        words.extend(segment.words)
    return words


def transcript_to_text(transcript: Transcript) -> str:
    tokens: List[str] = []
    for segment in transcript.segments:
        if segment.text:
            tokens.append(segment.text)
        else:
            tokens.extend(word.text for word in segment.words)
    return " ".join(tokens).strip()


def cer(reference: str, hypothesis: str) -> float:
    ref = list(reference)
    hyp = list(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    rows = len(ref) + 1
    cols = len(hyp) + 1
    dp = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dp[i][0] = i
    for j in range(cols):
        dp[0][j] = j

    for i in range(1, rows):
        for j in range(1, cols):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )

    return dp[-1][-1] / len(ref)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return d0 + d1


if __name__ == "__main__":
    main()
