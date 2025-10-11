"""Paraformer ÍÆÀí·â×°¡£"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from ..core.schema import Segment, Transcript, Word

if TYPE_CHECKING:
    from .models import ModelConfig


@dataclass
class ParaformerBundle:
    """°ü×° funasr Paraformer ÍÆÀí×é¼þ¡£"""

    model: Any
    device: str
    sample_rate: Optional[int] = None
    default_language: str = "zh"

    @classmethod
    def from_config(
        cls, config: "ModelConfig", *, device: str
    ) -> ParaformerBundle:
        """¸ù¾ÝÅäÖÃ¼ÓÔØ Paraformer Ä£ÐÍ¡£"""

        try:
            from funasr import AutoModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Paraformer ÒÀÀµ funasr£¬ÇëÖ´ÐÐ `pip install -e \".[paraformer]\"` °²×°ºóÔÙÊÔ¡£"
            ) from exc

        normalized_device = "cuda" if device.startswith("cuda") else "cpu"
        model_name_lower = config.name.strip().lower()
        allowed_models = {"paraformer-zh"}
        if model_name_lower not in allowed_models:
            if model_name_lower in {"fa-zh", "fa-en"} or "timestamp" in model_name_lower:
                raise ValueError(
                    f"Ä£ÐÍ {config.name} ÊôÓÚÊ±¼ä´Á¶ÔÆëÆ÷£¬ÐèÒªÊÂÏÈÌá¹©²Î¿¼ÎÄ±¾£¨µ±Ç° UI/CLI ÔÝÎ´Ö§³Ö£©£»ÈçÐèÈÈ´Ê»ò¶ÔÆëÄÜÁ¦£¬ÇëÊ¹ÓÃ¹Ù·½ seaco-paraformer ÍÆÀí½Å±¾ÔÚÍâ²¿¼¯³É¡£"
                )
            raise ValueError(
                f"Ä£ÐÍ {config.name} µ±Ç°Î´ÔÚ funasr=1.2.7 µÄ AutoModel ÖÐ×¢²á£¬ÔÝ²»Ö§³ÖÔÚ±¾ÏµÍ³Ö±½ÓÍÆÀí£»Çë¸ÄÓÃ paraformer-zh¡£"
            )
        extra_kwargs = dict(config.options or {})
        hub = extra_kwargs.pop("hub", "modelscope")
        base_kwargs: Dict[str, Any] = {
            "model": config.name,
            "device": normalized_device,
            "hub": hub,
            "vad_model": extra_kwargs.pop("vad_model", "fsmn-vad"),
            "punc_model": extra_kwargs.pop("punc_model", "ct-punc"),
            "disable_update": True,
        }
        model_kwargs: Dict[str, Any] = {**base_kwargs, **extra_kwargs}

        auto_model = AutoModel(**model_kwargs)

        sample_rate = getattr(auto_model, "sample_rate", None)
        language = (
            getattr(auto_model, "language", None)
            or getattr(auto_model, "lang", None)
            or "zh"
        )

        return cls(
            model=auto_model,
            device=normalized_device,
            sample_rate=sample_rate if isinstance(sample_rate, int) else None,
            default_language=str(language),
        )

    def transcribe(
        self, input_path: str | Path, *, language_hint: Optional[str] = None
    ) -> Transcript:
        """Ö´ÐÐ×ªÂ¼²¢·µ»ØÍ³Ò» Transcript ½á¹¹¡£"""

        media_path = Path(input_path)
        if not media_path.exists():
            raise FileNotFoundError(f"Input media not found: {media_path}")

        request_language = (language_hint or "auto").strip() or "auto"

        generate_kwargs: Dict[str, Any] = {
            "sentence_timestamp": True,
            "return_raw_text": True,
            "language": request_language,
        }

        try:
            results = self.model.generate(str(media_path), **generate_kwargs)
        except TypeError:
            results = self.model.generate(
                str(media_path), sentence_timestamp=True, language=request_language
            )

        payloads: List[Dict[str, Any]]
        if isinstance(results, list):
            payloads = [item for item in results if isinstance(item, dict)]
        elif isinstance(results, dict):
            payloads = [results]
        else:
            payloads = []

        segments = _extract_segments(payloads)
        language = _resolve_language(payloads, request_language, self.default_language)
        return Transcript(segments=segments, language=language)


def _extract_segments(items: Sequence[Dict[str, Any]]) -> List[Segment]:
    segments: List[Segment] = []
    for item in items:
        segments.extend(_segments_from_item(item))
    segments = [segment for segment in segments if segment is not None]
    segments.sort(key=lambda segment: segment.start)
    return segments


def _segments_from_item(item: Dict[str, Any]) -> List[Segment]:
    sentences = item.get("sentence_info")
    if isinstance(sentences, list) and sentences:
        candidates = (
            _build_segment(sentence)
            for sentence in sentences
            if isinstance(sentence, dict)
        )
        return [segment for segment in candidates if segment is not None]

    segment = _build_segment(item)
    return [segment] if segment is not None else []


def _build_segment(data: Dict[str, Any]) -> Optional[Segment]:
    text = _extract_text(data)
    if not text:
        return None

    words = _build_words(text, data.get("timestamp"))
    if not words:
        words = _distribute_without_timestamps(
            text,
            _to_seconds(data.get("start")),
            _to_seconds(data.get("end")),
        )

    if not words:
        return None

    segment_start = words[0].start
    segment_end = words[-1].end
    if segment_end < segment_start:
        segment_end = segment_start

    return Segment(start=segment_start, end=segment_end, text=text, words=words)


def _extract_text(data: Dict[str, Any]) -> str:
    for key in ("raw_text", "sentence", "text"):
        value = data.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return ""


def _build_words(text: str, timestamps: Any) -> List[Word]:
    pairs = _normalize_timestamp_pairs(timestamps)
    if not pairs:
        return []

    tokens = _tokenize_text(text)
    if len(tokens) != len(pairs):
        char_tokens = [char for char in text if not char.isspace()]
        if len(char_tokens) == len(pairs):
            tokens = char_tokens
        else:
            tokens = tokens[: len(pairs)]

    words: List[Word] = []
    for token, (start, end) in zip(tokens, pairs):
        words.append(Word(text=token, start=start, end=end, conf=None))
    return words


def _distribute_without_timestamps(
    text: str, start: Optional[float], end: Optional[float]
) -> List[Word]:
    tokens = _tokenize_text(text)
    if not tokens:
        return []

    if start is None and end is None:
        start = 0.0
        end = start
    elif start is None:
        start = max((end or 0.0) - len(tokens) * 0.05, 0.0)
    elif end is None:
        end = start + len(tokens) * 0.05

    if end is None:
        end = start

    if end < start:
        end = start

    duration = end - start
    if duration <= 0.0:
        return [Word(text=token, start=start, end=start, conf=None) for token in tokens]

    step = duration / len(tokens)
    words: List[Word] = []
    cursor = start
    for index, token in enumerate(tokens):
        word_end = cursor + step
        if index == len(tokens) - 1:
            word_end = end
        words.append(Word(text=token, start=cursor, end=word_end, conf=None))
        cursor = word_end
    return words


def _normalize_timestamp_pairs(timestamps: Any) -> List[Tuple[float, float]]:
    pairs: List[Tuple[float, float]] = []
    if isinstance(timestamps, dict):
        iterable = list(timestamps.values())
    elif isinstance(timestamps, (list, tuple)):
        iterable = list(timestamps)
    else:
        iterable = []

    for entry in iterable:
        if isinstance(entry, dict):
            start = entry.get("start")
            end = entry.get("end")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            start, end = entry[0], entry[1]
        else:
            continue

        start_sec = _to_seconds(start)
        end_sec = _to_seconds(end)
        if start_sec is None or end_sec is None:
            continue
        if end_sec < start_sec:
            end_sec = start_sec
        pairs.append((start_sec, end_sec))

    return pairs


def _to_seconds(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if numeric > 10.0:
        return numeric / 1000.0
    return numeric


def _resolve_language(
    payloads: Sequence[Dict[str, Any]], requested: str, default_language: str
) -> str:
    if requested and requested != "auto":
        return requested

    for item in payloads:
        language = item.get("language") or item.get("lang")
        if isinstance(language, str) and language.strip():
            return language.strip()

    return default_language or "auto"


def _tokenize_text(text: str) -> List[str]:
    stripped = text.strip()
    if not stripped:
        return []

    if any(char.isspace() for char in stripped):
        tokens: List[str] = []
        buffer = ""
        for char in stripped:
            if char.isspace():
                if buffer:
                    tokens.append(buffer)
                    buffer = ""
                continue
            buffer += char
        if buffer:
            tokens.append(buffer)
        return tokens

    return [char for char in stripped]

