"""
VoiceFlow Sidecar - Automatic Speech Recognition engine.

Wraps the ONNX Parakeet TDT model via ``onnx_asr`` for both
segment-level and streaming (incremental) transcription.

No audio or text is written to disk (HIPAA compliance).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import sys
import time
from dataclasses import dataclass
from typing import Optional

from model_paths import models_root

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE: int = 16_000
_DEFAULT_MODEL_NAME = "istupakov/parakeet-tdt-0.6b-v3-onnx"
_DEFAULT_QUANTIZATION = "int8"


@dataclass
class TranscriptionResult:
    """Container for a completed transcription.

    ``confidence`` is ``None`` when the backend does not expose a real
    per-utterance confidence. The ONNX Parakeet ``recognize`` call returns
    text only, so we do not fabricate one. (See ``transcribe_segment``.)
    """

    text: str
    confidence: Optional[float]
    latency_ms: float
    audio_duration_ms: float


@dataclass
class WordConfidence:
    """One word and the model's confidence in it (0..1, higher = surer).

    Confidence is ``exp(min token log-prob)`` over the word's subword tokens,
    ignoring pure-punctuation tokens. The min (not mean) is deliberate: a word
    is only as trustworthy as its shakiest piece, which is exactly the signal a
    confidence gate wants for span-scoped correction.
    """

    word: str
    confidence: float


class ASREngine:
    """ONNX-based speech recognition engine backed by Parakeet TDT v3."""

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL_NAME,
        num_threads: int = 4,
    ) -> None:
        self._model_name = model_name
        self._num_threads = num_threads
        self._quantization = os.environ.get(
            "VOICEFLOW_ASR_QUANTIZATION",
            _DEFAULT_QUANTIZATION,
        )
        self._model_dir = self._resolve_model_dir()

        self._model = None

        # Thread-safety: prevent concurrent model loads
        self._load_lock = threading.Lock()

        # Streaming state: accumulated chunks for incremental decoding
        self._stream_chunks: list[np.ndarray] = []
        self._last_partial: str = ""

    # ------------------------------------------------------------------
    # Model loading (thread-safe)
    # ------------------------------------------------------------------

    @staticmethod
    def _runtime_root() -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return Path(__file__).resolve().parent

    def _resolve_model_dir(self) -> Path:
        override = os.environ.get("VOICEFLOW_ASR_MODEL_DIR")
        if override:
            return Path(override).expanduser().resolve()

        return models_root() / self._model_name.rsplit("/", 1)[-1]

    @staticmethod
    def _extract_text(result: object) -> str:
        if result is None:
            return ""

        if isinstance(result, str):
            return result.strip()

        if isinstance(result, (list, tuple)):
            parts = [ASREngine._extract_text(item) for item in result]
            return " ".join(part for part in parts if part).strip()

        text = getattr(result, "text", None)
        if isinstance(text, str):
            return text.strip()

        transcript = getattr(result, "transcript", None)
        if isinstance(transcript, str):
            return transcript.strip()

        return str(result).strip()

    @staticmethod
    def _normalize_audio(audio: np.ndarray) -> np.ndarray:
        if audio.dtype == np.float32:
            return audio

        if np.issubdtype(audio.dtype, np.integer):
            return audio.astype(np.float32) / 32768.0

        return audio.astype(np.float32)

    def _load_onnx_model(self):
        try:
            import onnx_asr
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Parakeet ONNX backend is unavailable. Install 'onnx-asr[cpu,hub]' "
                "before starting the sidecar."
            ) from exc

        if os.environ.get("HF_HUB_OFFLINE") == "1" and not self._model_dir.exists():
            raise FileNotFoundError(
                "Offline Parakeet model bundle not found at "
                f"'{self._model_dir}'. Download the model into that directory "
                "before starting VoiceFlow."
            )

        self._model_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Loading Parakeet ONNX model '%s' from %s (quantization=%s, threads=%d)",
            self._model_name,
            self._model_dir,
            self._quantization,
            self._num_threads,
        )

        if "OMP_NUM_THREADS" not in os.environ:
            os.environ["OMP_NUM_THREADS"] = str(self._num_threads)

        return onnx_asr.load_model(
            self._model_name,
            str(self._model_dir),
            quantization=self._quantization,
        )

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        with self._load_lock:
            # Double-check after acquiring the lock
            if self._model is not None:
                return self._model

            self._model = self._load_onnx_model()
            logger.info("Parakeet ONNX model loaded")

        return self._model

    def _recognize_audio(self, audio_float: np.ndarray) -> str:
        model = self._ensure_model()
        result = model.recognize(audio_float, sample_rate=_SAMPLE_RATE)
        return self._extract_text(result)

    def warmup(self) -> None:
        """Pre-load the model and run a tiny dummy inference."""
        self._ensure_model()
        dummy_audio = np.zeros(1600, dtype=np.float32)  # 0.1s silence
        self._recognize_audio(dummy_audio)

        logger.info("ASR warmup complete")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def transcribe_segment(self, audio: np.ndarray) -> TranscriptionResult:
        """Transcribe a complete audio segment (int16, 16 kHz mono).

        Returns a :class:`TranscriptionResult` with the final text,
        confidence score, latency, and audio duration.
        """
        audio_float = self._normalize_audio(audio)
        audio_duration_ms = len(audio) / _SAMPLE_RATE * 1000.0

        t0 = time.perf_counter()
        text = self._recognize_audio(audio_float)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # No real confidence available. The previous code returned
        # min(1.0, audio_duration_ms / latency_ms) — that is the inverse
        # real-time factor (decode speed), not transcription confidence, and
        # it pinned to 1.0 on any machine that decodes faster than real time.
        # Reporting None is honest; downstream treats it as "unknown".
        confidence = None

        result = TranscriptionResult(
            text=text.strip(),
            confidence=confidence,
            latency_ms=round(latency_ms, 1),
            audio_duration_ms=round(audio_duration_ms, 1),
        )
        logger.info(
            "Transcribed %.1fs audio in %.1fms: '%s'",
            audio_duration_ms / 1000.0,
            latency_ms,
            result.text[:80],
        )
        return result

    def transcribe_segment_with_words(
        self, audio: np.ndarray
    ) -> "tuple[str, list[WordConfidence]]":
        """Transcribe a segment and return ``(text, per-word confidences)``.

        Uses the onnx_asr ``with_timestamps()`` adapter, which exposes
        per-token ``logprobs``. Subword tokens are merged into words (a new
        word begins on a leading-space token) and scored by the min token
        probability. Falls back to ``([recognize text], [])`` if the backend
        does not provide token logprobs.
        """
        model = self._ensure_model()
        audio_float = self._normalize_audio(audio)

        try:
            res = model.with_timestamps().recognize(audio_float, sample_rate=_SAMPLE_RATE)
        except Exception:
            logger.debug("with_timestamps decode failed; no word confidence", exc_info=True)
            return self._recognize_audio(audio_float).strip(), []

        text = (getattr(res, "text", "") or "").strip()
        tokens = getattr(res, "tokens", None)
        logprobs = getattr(res, "logprobs", None)
        if not tokens or not logprobs or len(tokens) != len(logprobs):
            return text, []

        return text, self._aggregate_word_confidences(tokens, logprobs)

    @staticmethod
    def _aggregate_word_confidences(
        tokens: "list[str]", logprobs: "list[float]"
    ) -> "list[WordConfidence]":
        """Merge subword tokens into words and score each by min token prob.

        A new word starts on a token with a leading space (Parakeet's
        word-boundary convention). Pure-punctuation tokens attach to the
        current word but are excluded from the confidence min so trailing
        '?'/'.' don't make a confidently-decoded word look uncertain.
        """
        import math

        words: list[WordConfidence] = []
        cur_chars: list[str] = []
        cur_lp: Optional[float] = None  # min log-prob over scored tokens

        def _flush() -> None:
            nonlocal cur_chars, cur_lp
            word = "".join(cur_chars).strip()
            if word:
                lp = cur_lp if cur_lp is not None else 0.0
                words.append(WordConfidence(word=word, confidence=math.exp(lp)))
            cur_chars = []
            cur_lp = None

        for i, (tok, lp) in enumerate(zip(tokens, logprobs)):
            if tok.startswith(" ") and i > 0:
                _flush()
            cur_chars.append(tok)
            # Only count tokens that carry a letter/digit toward confidence.
            if any(ch.isalnum() for ch in tok):
                cur_lp = lp if cur_lp is None else min(cur_lp, lp)

        _flush()
        return words

    def transcribe_streaming(self, chunk: np.ndarray) -> Optional[str]:
        """Feed an incremental chunk and return partial text, or ``None``
        if the accumulated audio is too short for a meaningful decode.

        Designed for real-time preview in the UI.
        """
        self._stream_chunks.append(chunk)

        # Only attempt decoding every ~0.5 s of accumulated audio
        total_samples = sum(len(c) for c in self._stream_chunks)
        if total_samples < _SAMPLE_RATE // 2:
            return None

        # Cap at last 10 seconds to keep latency bounded
        max_stream_samples = _SAMPLE_RATE * 10
        if total_samples > max_stream_samples:
            # Keep only the tail
            combined_all = np.concatenate(self._stream_chunks)
            combined_all = combined_all[-max_stream_samples:]
            self._stream_chunks = [combined_all]
            total_samples = max_stream_samples

        combined = np.concatenate(self._stream_chunks)
        try:
            result = self.transcribe_segment(combined)
            if result.text and result.text != self._last_partial:
                self._last_partial = result.text
                return result.text
        except Exception:
            logger.exception("Streaming transcription error")

        return None

    def reset_stream(self) -> None:
        """Reset the streaming accumulator between utterances."""
        self._stream_chunks.clear()
        self._last_partial = ""
