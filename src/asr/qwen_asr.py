"""Qwen3-ASR 推理封装。"""

from __future__ import annotations

import gc
import logging
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import torch
import re

from ..core.schema import Segment, Transcript, Word

if TYPE_CHECKING:
    from .models import ModelConfig

LOGGER = logging.getLogger(__name__)


@dataclass
class QwenASRBundle:
    """封装 Qwen3-ASR 级联推理组件。"""

    name: str
    device: str
    chunk_sec: int = 200
    default_language: str = "zh"
    sample_rate: int = 16000

    @classmethod
    def from_config(cls, config: "ModelConfig", *, device: str) -> QwenASRBundle:
        """根据配置加载 Qwen3-ASR 捆绑信息。"""
        normalized_device = "cuda:0" if device.startswith("cuda") else "cpu"
        
        extra_kwargs = dict(config.options or {})
        chunk_sec = int(extra_kwargs.get("chunk_sec", 200))
        
        # Qwen models auto-download via from_pretrained when cache misses.
        return cls(
            name=config.name or "Qwen/Qwen3-ASR-1.7B",
            device=normalized_device,
            chunk_sec=chunk_sec,
            default_language="auto",
            sample_rate=16000,
        )

    def _load_audio(self, path: str, sr: int) -> np.ndarray:
        """Decode audio to a mono float32 numpy array using ffmpeg."""
        cmd = [
            "ffmpeg",
            "-y",
            "-i", path,
            "-f", "f32le",
            "-ac", "1",
            "-ar", str(sr),
            "-loglevel", "error",
            "-"
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, check=True)
            return np.frombuffer(proc.stdout, dtype=np.float32)
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.decode('utf-8', errors='ignore').strip() if e.stderr else str(e)
            raise RuntimeError(f"FFmpeg failed to load audio from {path}. Error: {err_msg}\nCommand: {' '.join(cmd)}") from e

    def transcribe(
        self, input_path: str | Path, *, language_hint: Optional[str] = None
    ) -> Transcript:
        """执行转录并返回统一 Transcript 结构。"""
        try:
            from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner
        except ImportError as exc:
            raise RuntimeError(
                "Qwen-ASR dependencies missing. Run `pip install qwen-asr transformers`."
            ) from exc

        media_path = Path(input_path)
        if not media_path.exists():
            raise FileNotFoundError(f"Input media not found: {media_path}")

        request_language = (language_hint or self.default_language).strip()
        
        lang_lower = request_language.lower()
        if lang_lower == "auto":
            qwen_lang = None
        elif lang_lower in ("zh", "zh-cn", "zh-tw", "chinese"):
            qwen_lang = "Chinese"
        elif lang_lower in ("en", "english"):
            qwen_lang = "English"
        elif lang_lower in ("ja", "japanese"):
            qwen_lang = "Japanese"
        elif lang_lower in ("ko", "korean"):
            qwen_lang = "Korean"
        elif lang_lower in ("yue", "cantonese"):
            qwen_lang = "Cantonese"
        else:
            qwen_lang = request_language.title()
            
        sr = self.sample_rate

        LOGGER.info(f"Loading audio: {media_path.name} …")
        full_wav = self._load_audio(str(media_path), sr)
        total_dur = len(full_wav) / sr
        LOGGER.info(f"Duration: {total_dur:.1f} s")

        chunk_samples = self.chunk_sec * sr
        audio_chunks = []
        for start in range(0, len(full_wav), chunk_samples):
            chunk = full_wav[start : start + chunk_samples]
            if len(chunk) < sr // 2:
                continue
            audio_chunks.append((float(start) / sr, chunk))

        LOGGER.info(f"Split into {len(audio_chunks)} chunks of <= {self.chunk_sec}s.")

        # ==========================================
        # Pass 1: Transcribe
        # ==========================================
        LOGGER.info(f"Loading Qwen3-ASR model: {self.name}")
        asr_model = Qwen3ASRModel.from_pretrained(
            self.name,
            dtype=torch.bfloat16 if self.device.startswith("cuda") else torch.float32,
            device_map=self.device,
            attn_implementation="sdpa",  # Fallback to sdpa as requested
        )

        all_texts = []
        all_langs = []
        for i, (offset, chunk_wav) in enumerate(audio_chunks):
            LOGGER.info(f"Transcribing chunk {i + 1}/{len(audio_chunks)} ...")
            with torch.inference_mode():
                r = asr_model.transcribe(
                    audio=(chunk_wav, sr),
                    language=qwen_lang,
                    return_time_stamps=False,
                )
            all_texts.append(r[0].text.strip())
            all_langs.append(r[0].language)

        del asr_model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # ==========================================
        # Pass 2: Align Timestamps
        # ==========================================
        aligner_name = "Qwen/Qwen3-ForcedAligner-0.6B"
        LOGGER.info(f"Loading ForcedAligner: {aligner_name}")
        aligner = Qwen3ForcedAligner.from_pretrained(
            aligner_name,
            dtype=torch.bfloat16 if self.device.startswith("cuda") else torch.float32,
            device_map=self.device,
            attn_implementation="sdpa",
        )

        time_stamps = []
        for i, (offset, chunk_wav) in enumerate(audio_chunks):
            chunk_text = all_texts[i]
            if not chunk_text.strip():
                continue
            
            chunk_detected_lang = all_langs[i] if len(all_langs) > i else "Chinese"
            # Qwen aligner needs a concrete language; if it's returning empty for some reason, fallback to Chinese
            align_lang = qwen_lang if qwen_lang else (chunk_detected_lang or "Chinese")
            
            LOGGER.info(f"Aligning chunk {i + 1}/{len(audio_chunks)} (lang: {align_lang})...")
            with torch.inference_mode():
                alignment = aligner.align(
                    audio=(chunk_wav, sr),
                    text=chunk_text,
                    language=align_lang,
                )
            
            stamps_with_punc = self._restore_punctuation(chunk_text, alignment[0])
            
            for stamp in stamps_with_punc:
                shifted = replace(
                    stamp,
                    start_time=stamp.start_time + offset,
                    end_time=stamp.end_time + offset,
                )
                time_stamps.append(shifted)
            del alignment

        del aligner
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        segments = self._build_segments(time_stamps)
        
        final_lang = request_language
        if not qwen_lang and all_langs:
            # Pick a valid detected language or fallback
            valid_langs = [l for l in all_langs if l]
            if valid_langs:
                final_lang = valid_langs[0]
                
        return Transcript(segments=segments, language=final_lang)

    def _build_segments(
        self, stamps, max_chars: int = 40, max_duration: float = 7.0, gap_threshold: float = 0.6
    ) -> List[Segment]:
        if not stamps:
            return []

        subtitles = []
        current_words = []
        current_text = ""
        current_start = stamps[0].start_time
        current_end = stamps[0].end_time

        for i, stamp in enumerate(stamps):
            start_new = False
            word_obj = Word(text=stamp.text, start=stamp.start_time, end=stamp.end_time, conf=1.0)
            
            if i == 0:
                current_text = stamp.text
                current_start = stamp.start_time
                current_end = stamp.end_time
                current_words.append(word_obj)
                continue

            gap = stamp.start_time - current_end
            new_duration = stamp.end_time - current_start
            new_len = len(current_text) + len(stamp.text)

            if gap > gap_threshold or new_duration > max_duration or new_len > max_chars:
                start_new = True
            else:
                clean_current = current_text.strip()
                if clean_current:
                    last_char = clean_current[-1]
                    # 強斷句標點 (句子結束)
                    if last_char in "。！？.!?":
                        start_new = True
                    # 弱斷句標點 (逗號、分號等)，當長度或時間達到一半時才斷句
                    elif last_char in "，；,;：:" and (len(clean_current) >= max_chars * 0.5 or (current_end - current_start) >= max_duration * 0.5):
                        start_new = True

            if start_new:
                subtitles.append(Segment(
                    start=current_start,
                    end=current_end,
                    text=current_text.strip(),
                    words=current_words
                ))
                current_text = stamp.text
                current_start = stamp.start_time
                current_end = stamp.end_time
                current_words = [word_obj]
            else:
                if current_text and stamp.text:
                    # Always add a space between English words or when transitioning to/from English words
                    curr_ends_en = bool(re.search(r'[a-zA-Z0-9][^\w\s]*$', current_text))
                    next_starts_en = bool(re.search(r'^[^\w\s]*[a-zA-Z0-9]', stamp.text))
                    
                    stamp_text = stamp.text
                    # If stamp is purely punctuation, strip existing trailing punctuation from current_text
                    if re.match(r'^[^\w\s]+$', stamp_text):
                        import string
                        while current_text and current_text[-1] in string.punctuation + "。，、！？；：":
                            current_text = current_text[:-1]
                        
                        # Normalize full-width to half-width if previous word was English
                        if curr_ends_en:
                            trans_table = str.maketrans("。，、！？；：", ".,,!?;:")
                            stamp_text = stamp_text.translate(trans_table)

                    if (curr_ends_en or next_starts_en) and not current_text.endswith(" "):
                        current_text += " "
                    current_text += stamp_text
                current_end = stamp.end_time
                current_words.append(word_obj)

        if current_text.strip() or current_words:
            subtitles.append(Segment(
                start=current_start,
                end=current_end,
                text=current_text.strip(),
                words=current_words
            ))

        return subtitles

    def _restore_punctuation(self, chunk_text: str, stamps: List[Any]) -> List[Any]:
        if not stamps:
            return stamps
            
        result = []
        text_idx = 0
        from dataclasses import replace
        
        for i, stamp in enumerate(stamps):
            search_text = chunk_text[text_idx:].lower()
            word_lower = stamp.text.lower()
            idx = search_text.find(word_lower)
            
            if idx != -1:
                actual_idx = text_idx + idx
                end_idx = actual_idx + len(stamp.text)
                
                next_idx = len(chunk_text)
                if i + 1 < len(stamps):
                    next_word_lower = stamps[i+1].text.lower()
                    next_search = chunk_text[end_idx:].lower()
                    next_rel_idx = next_search.find(next_word_lower)
                    if next_rel_idx != -1:
                        next_idx = end_idx + next_rel_idx
                
                gap_text = chunk_text[end_idx:next_idx]
                punct_match = re.search(r'^[^\w\s]+', gap_text)
                new_text = stamp.text
                if punct_match:
                    punct_str = punct_match.group(0)
                    punct_char = punct_str[0]
                    
                    is_english = bool(re.search(r'[a-zA-Z0-9]$', stamp.text))
                    if is_english:
                        punct_char = punct_char.translate(str.maketrans("。，、！？；：", ".,,!?;:"))
                        
                    import string
                    while new_text and new_text[-1] in string.punctuation + "。，、！？；：":
                        new_text = new_text[:-1]
                    new_text += punct_char
                    next_idx = end_idx + len(punct_str)
            
                result.append(replace(stamp, text=new_text))
                text_idx = next_idx
            else:
                result.append(stamp)
                
        return result
