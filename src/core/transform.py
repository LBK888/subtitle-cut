"""转录变换与片段规划工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

from .schema import Segment, Transcript, Word


@dataclass
class TimeRange:
    start: float
    end: float

    def clamped(self, *, minimum: float = 0.0, maximum: float | None = None) -> TimeRange:
        upper = maximum if maximum is not None else self.end
        new_start = max(self.start, minimum)
        new_end = max(min(self.end, upper), new_start)
        return TimeRange(start=new_start, end=new_end)


def compute_delete_ranges(
    transcript: Transcript,
    delete_words: Sequence[str],
    *,
    merge_gap_ms: float = 120.0,
    padding_ms: float = 80.0,
) -> List[TimeRange]:
    """根据要删除的词返回时间段。"""

    normalized_targets = {
        _normalize_token(token)
        for token in delete_words
        if token and _normalize_token(token)
    }
    if not normalized_targets:
        return []

    padding = max(padding_ms, 0.0) / 1000.0
    merge_gap = max(merge_gap_ms, 0.0) / 1000.0
    total_duration = _transcript_duration(transcript)

    raw_ranges: List[TimeRange] = []
    for word in _iter_words(transcript):
        normalized = _normalize_token(word.text)
        if normalized and normalized in normalized_targets:
            raw_ranges.append(
                TimeRange(start=word.start - padding, end=word.end + padding)
            )

    if not raw_ranges:
        return []

    raw_ranges.sort(key=lambda r: r.start)

    merged: List[TimeRange] = []
    current = raw_ranges[0].clamped(minimum=0.0, maximum=total_duration)
    for next_range in raw_ranges[1:]:
        next_clamped = next_range.clamped(minimum=0.0, maximum=total_duration)
        if next_clamped.start <= current.end + merge_gap:
            current.end = max(current.end, next_clamped.end)
        else:
            merged.append(current)
            current = next_clamped
    merged.append(current)

    return [rng for rng in merged if rng.end > rng.start]


def invert_ranges(total_duration: float, delete_ranges: Iterable[TimeRange]) -> List[TimeRange]:
    """将删除区间转为保留区间。"""

    total = max(total_duration, 0.0)
    ranges = sorted(
        (rng.clamped(minimum=0.0, maximum=total) for rng in delete_ranges),
        key=lambda r: r.start,
    )

    keep: List[TimeRange] = []
    cursor = 0.0
    for rng in ranges:
        if rng.end <= cursor:
            continue
        if rng.start > cursor:
            keep.append(TimeRange(start=cursor, end=rng.start))
        cursor = max(cursor, rng.end)

    if cursor < total:
        keep.append(TimeRange(start=cursor, end=total))

    return [rng for rng in keep if rng.end > rng.start]


def derive_keep_ranges(
    transcript: Transcript,
    delete_ranges: Sequence[TimeRange],
    *,
    merge_gap: float = 0.05,
    coverage_tolerance: float = 1e-3,
) -> List[TimeRange]:
    """根据转写内容推导保留区间。"""

    total_duration = _transcript_duration(transcript)
    if total_duration <= 0.0:
        return []

    normalized_deletes = sorted(
        (
            rng.clamped(minimum=0.0, maximum=total_duration)
            for rng in delete_ranges
        ),
        key=lambda rng: rng.start,
    )

    def _is_fully_deleted(rng: TimeRange) -> bool:
        length = rng.end - rng.start
        if length <= 0.0:
            return True
        covered = 0.0
        for delete_rng in normalized_deletes:
            if delete_rng.end <= rng.start:
                continue
            if delete_rng.start >= rng.end:
                break
            overlap_start = max(rng.start, delete_rng.start)
            overlap_end = min(rng.end, delete_rng.end)
            if overlap_end > overlap_start:
                covered += overlap_end - overlap_start
                if covered >= length - coverage_tolerance:
                    return True
        return covered >= length - coverage_tolerance

    candidate: List[TimeRange] = []
    for segment in transcript.segments:
        for word in _segment_words(segment):
            start = float(word.start if word.start is not None else 0.0)
            end = float(word.end if word.end is not None else start)
            rng = TimeRange(start=start, end=end).clamped(minimum=0.0, maximum=total_duration)
            if rng.end <= rng.start:
                continue
            if _is_fully_deleted(rng):
                continue
            candidate.append(rng)

    if not candidate:
        return []

    candidate.sort(key=lambda rng: (rng.start, rng.end))
    gap = max(merge_gap, 0.0)
    merged: List[TimeRange] = []
    
    def _has_delete_overlap(start: float, end: float) -> bool:
        """检查指定区间是否与任何删除区间重叠"""
        for delete_rng in normalized_deletes:
            if delete_rng.start < end and delete_rng.end > start:
                return True
        return False
    
    for rng in candidate:
        if not merged:
            merged.append(TimeRange(start=rng.start, end=rng.end))
            continue
        prev = merged[-1]
        # 只有在间隔小于gap且合并后的整个区间都不与删除区间重叠时才合并
        if rng.start <= prev.end + gap:
            # 检查合并后的完整区间 [prev.start, rng.end] 是否与删除区间重叠
            if not _has_delete_overlap(prev.start, rng.end):
                prev.end = max(prev.end, rng.end)
            else:
                merged.append(TimeRange(start=rng.start, end=rng.end))
        else:
            merged.append(TimeRange(start=rng.start, end=rng.end))
    
    # 验证：确保合并后的区间不包含任何删除区间
    for merged_rng in merged:
        for delete_rng in normalized_deletes:
            # 检查是否有重叠
            if delete_rng.start < merged_rng.end and delete_rng.end > merged_rng.start:
                # 发现重叠！这不应该发生
                import logging
                logger = logging.getLogger(__name__)
                logger.error(
                    "CRITICAL: Merged range [%.2f-%.2f] overlaps with delete range [%.2f-%.2f]!",
                    merged_rng.start,
                    merged_rng.end,
                    delete_rng.start,
                    delete_rng.end,
                )
                raise RuntimeError(
                    f"Merge validation failed: keep range [{merged_rng.start}-{merged_rng.end}] "
                    f"overlaps with delete range [{delete_rng.start}-{delete_rng.end}]"
                )
    
    # 记录合并效果
    import logging
    logger = logging.getLogger(__name__)
    if len(candidate) > len(merged):
        logger.info(
            "Merged keep ranges: %d candidate ranges -> %d merged ranges (merge_gap=%.2fs, reduction=%.1f%%)",
            len(candidate),
            len(merged),
            gap,
            (1 - len(merged) / len(candidate)) * 100,
        )
    
    logger.info("Merge validation passed: no overlap with delete ranges")
    
    return merged


def rebase_transcript_after_cuts(
    transcript: Transcript,
    delete_ranges: Sequence[TimeRange],
    *,
    keep_ranges: Sequence[TimeRange] | None = None,
) -> Transcript:
    """删除指定时间段后重新映射转录。"""

    total_duration = _transcript_duration(transcript)
    resolved_keep = list(keep_ranges) if keep_ranges else derive_keep_ranges(transcript, delete_ranges)
    if not resolved_keep:
        resolved_keep = invert_ranges(total_duration, delete_ranges)
    if not resolved_keep:
        return Transcript(segments=[], language=transcript.language)

    offsets = []
    accumulated = 0.0
    for rng in resolved_keep:
        offsets.append((rng.start, rng.end, accumulated - rng.start))
        accumulated += rng.end - rng.start

    new_segments: List[Segment] = []
    for segment in transcript.segments:
        source_words = list(_segment_words(segment))
        if not source_words:
            continue

        transformed_words: List[Word] = []
        for word in source_words:
            for start, end, delta in offsets:
                if word.end <= start:
                    continue
                if word.start >= end:
                    continue
                clip_start = max(word.start, start)
                clip_end = min(word.end, end)
                if clip_end <= clip_start:
                    continue
                transformed_words.append(
                    Word(
                        text=word.text,
                        start=clip_start + delta,
                        end=clip_end + delta,
                        conf=word.conf,
                    )
                )

        if transformed_words:
            transformed_words.sort(key=lambda w: (w.start, w.end))
            new_text = " ".join(word.text for word in transformed_words)
            new_segments.append(
                Segment(
                    start=transformed_words[0].start,
                    end=transformed_words[-1].end,
                    text=new_text,
                    words=transformed_words,
                )
            )

    return Transcript(segments=new_segments, language=transcript.language)


def _normalize_token(text: str) -> str:
    return text.strip().lower().strip("，。,.!?！？；;：:…")


def _iter_words(transcript: Transcript) -> Iterable[Word]:
    for segment in transcript.segments:
        yield from _segment_words(segment)


def _segment_words(segment: Segment) -> Iterable[Word]:
    if segment.words:
        return segment.words
    text = segment.text.strip()
    if not text:
        return []
    duration = max(segment.end - segment.start, 0.0)
    tokens = text.split()
    if not tokens:
        return []
    step = duration / max(len(tokens), 1)
    words: List[Word] = []
    current = segment.start
    for token in tokens:
        end = current + step if duration > 0 else segment.end
        words.append(Word(text=token, start=current, end=end, conf=None))
        current = end
    if words:
        words[-1].end = segment.end
    return words


def _transcript_duration(transcript: Transcript) -> float:
    if not transcript.segments:
        return 0.0
    return max(segment.end for segment in transcript.segments)
