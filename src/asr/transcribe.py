"""ASR 转录流程与分发。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Union

from ..core.schema import Segment, Transcript, Word
from .models import ModelBundle, ModelConfig, load_asr_components

PathLike = Union[str, Path]


def transcribe_to_json(
    input_media: PathLike,
    *,
    engine: str = "whisperx",
    model: str = "large-v2",
    device: str = "auto",
    compute_type: str = "auto",
    options: Dict[str, Any] | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> Transcript:
    """根据配置执行转录并返回 Transcript。"""

    def _emit(progress: float) -> None:
        if progress_callback is None:
            return
        progress_callback(max(0.0, min(1.0, progress)))

    media_path = Path(input_media)
    if not media_path.exists():
        raise FileNotFoundError(f"Input media not found: {media_path}")
    _emit(0.05)

    config = ModelConfig(
        engine=engine,
        name=model,
        device=device,
        compute_type=compute_type,
        options={} if options is None else dict(options),
    )
    bundle = load_asr_components(config)
    engine_key = (config.engine or "whisperx").strip().lower()
    _emit(0.1)

    return _transcribe_with_bundle(bundle, media_path, engine=engine_key, progress_callback=_emit)


def _transcribe_with_bundle(
    bundle: Any,
    input_media: PathLike,
    *,
    engine: str = "whisperx",
    progress_callback: Callable[[float], None] | None = None,
) -> Transcript:
    """使用已加载的模型bundle执行转录（避免重复加载模型）"""
    
    def _emit(progress: float) -> None:
        if progress_callback is None:
            return
        progress_callback(max(0.0, min(1.0, progress)))
    
    media_path = Path(input_media)
    if not media_path.exists():
        raise FileNotFoundError(f"Input media not found: {media_path}")
    
    engine_key = (engine or "whisperx").strip().lower()

    if engine_key == "whisperx":
        if not isinstance(bundle, ModelBundle):
            raise RuntimeError("WhisperX 加载失败，ModelBundle 不可用。")
        return _transcribe_whisperx(bundle, media_path, progress_callback=_emit)

    transcribe_fn = getattr(bundle, "transcribe", None)
    if callable(transcribe_fn):
        _emit(0.2)
        result = transcribe_fn(str(media_path))
        _emit(0.9)
        return result

    raise RuntimeError(f"Unknown ASR engine: {engine_key}")


def _transcribe_whisperx(
    bundle: ModelBundle,
    media_path: Path,
    *,
    progress_callback: Callable[[float], None] | None = None,
) -> Transcript:
    try:
        import whisperx  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "WhisperX 未安装，请先执行 `pip install whisperx`。"
        ) from exc

    def _emit(progress: float) -> None:
        if progress_callback is None:
            return
        progress_callback(max(0.0, min(1.0, progress)))

    _emit(0.2)
    result = bundle.model.transcribe(str(media_path))
    _emit(0.6)
    language = result.get("language", "auto")
    segments = result.get("segments", [])

    align_model, metadata = whisperx.load_align_model(
        language_code=language, device=bundle.device
    )
    _emit(0.7)

    aligned = whisperx.align(
        segments,
        align_model,
        metadata,
        str(media_path),
        device=bundle.device,
        return_char_alignments=False,
    )
    _emit(0.85)

    diarize_segments: Iterable[Dict[str, Any]] = aligned.get("segments", [])
    transcript_segments = [
        segment
        for segment in (
            _convert_segment(entry, bundle.device) for entry in diarize_segments
        )
        if segment is not None
    ]
    _emit(0.95)

    return Transcript(segments=transcript_segments, language=language)


def _convert_segment(segment: Dict[str, Any], device: str) -> Segment | None:
    start = float(segment.get("start", 0.0) or 0.0)
    end = float(segment.get("end", start))
    text = (segment.get("text") or "").strip()
    words_data = segment.get("words") or []

    words: List[Word] = []
    for item in words_data:
        word_text = (item.get("word") or item.get("text") or "").strip()
        try:
            word_start = float(item.get("start"))
            word_end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        conf = item.get("confidence")
        try:
            confidence = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            confidence = None
        words.append(
            Word(text=word_text, start=word_start, end=word_end, conf=confidence)
        )

    if not text and words:
        text = "".join(word.text for word in words)

    if not words and not text:
        return None

    return Segment(start=start, end=end if end > start else start, text=text, words=words)
