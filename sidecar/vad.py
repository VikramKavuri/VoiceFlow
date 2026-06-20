"""
VoiceFlow Sidecar - Voice Activity Detection (VAD).

Uses the Silero VAD model (via the ``silero-vad`` package) to classify
audio chunks as speech or silence.  A simple state machine tracks speech
onset / offset so that complete speech segments (with pre-roll context)
are emitted to the caller.

No audio is written to disk; all buffers are kept in memory only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import torch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SILERO_SR: int = 16_000
_SILERO_WINDOW: int = 512  # 32 ms at 16 kHz (model native window)


@dataclass
class VADResult:
    """Result returned by :meth:`VoiceActivityDetector.process_chunk`."""

    is_speech: bool
    probability: float
    segment_complete: bool
    speech_audio: Optional[np.ndarray] = field(default=None, repr=False)


class VoiceActivityDetector:
    """Silero-based VAD with a speech / silence state machine."""

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 500,
        pre_roll_ms: int = 200,
    ) -> None:
        self._threshold = threshold
        self._min_speech_samples = int(_SILERO_SR * min_speech_ms / 1000)
        self._min_silence_samples = int(_SILERO_SR * min_silence_ms / 1000)
        self._pre_roll_samples = int(_SILERO_SR * pre_roll_ms / 1000)

        # State machine
        self._in_speech = False
        self._speech_samples = 0
        self._silence_samples = 0

        # Accumulators
        self._speech_buffer: list[np.ndarray] = []
        self._pre_roll_buffer: list[np.ndarray] = []
        self._pre_roll_total: int = 0
        # Carry leftover raw samples across calls so boundary windows are never zero-padded mid-recording
        self._leftover: np.ndarray = np.empty(0, dtype=np.int16)

        # Silero model (lazy loaded)
        self._model: Optional[torch.jit.RecursiveScriptModule] = None

    # ------------------------------------------------------------------
    # Lazy model loading
    # ------------------------------------------------------------------

    def _ensure_model(self) -> torch.jit.RecursiveScriptModule:
        if self._model is None:
            from silero_vad import load_silero_vad
            self._model = load_silero_vad()
            logger.info("Silero VAD model loaded via silero-vad package")
        return self._model

    def warmup(self) -> None:
        """Pre-load the model and run a dummy inference to warm it up."""
        model = self._ensure_model()
        dummy = torch.zeros(_SILERO_WINDOW, dtype=torch.float32)
        model(dummy, _SILERO_SR)
        logger.info("VAD warmup complete")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_chunk(self, chunk: np.ndarray) -> VADResult:
        """Process an int16 audio chunk and return a :class:`VADResult`.

        The chunk may be any length; it is internally split into
        Silero-native windows of 512 samples (32 ms).
        """
        model = self._ensure_model()

        # Prepend any leftover samples from the previous call before slicing
        if len(self._leftover) > 0:
            chunk = np.concatenate([self._leftover, chunk])
        self._leftover = np.empty(0, dtype=np.int16)

        # Convert int16 -> float32 normalised to [-1, 1]
        audio = chunk.astype(np.float32) / 32768.0

        # Process in 512-sample windows
        prob = 0.0
        offset = 0
        while offset + _SILERO_WINDOW <= len(audio):
            window = audio[offset : offset + _SILERO_WINDOW]
            prob = self._infer(model, window)
            self._update_state(prob, chunk[offset : offset + _SILERO_WINDOW])
            offset += _SILERO_WINDOW

        # Stash incomplete window for the next call rather than zero-padding now
        if offset < len(chunk):
            self._leftover = chunk[offset:].copy()

        # Check if a complete segment should be emitted
        segment_complete = False
        speech_audio: Optional[np.ndarray] = None

        if (
            not self._in_speech
            and self._speech_buffer
            and self._silence_samples >= self._min_silence_samples
        ):
            # Silence long enough after speech -> emit segment
            if self._speech_samples >= self._min_speech_samples:
                segment_complete = True
                speech_audio = np.concatenate(self._speech_buffer)
            self._speech_buffer.clear()
            self._speech_samples = 0

        return VADResult(
            is_speech=self._in_speech,
            probability=prob,
            segment_complete=segment_complete,
            speech_audio=speech_audio,
        )

    def flush(self) -> Optional[np.ndarray]:
        """Return any accumulated speech that hasn't been emitted as a segment.

        Called when recording stops to capture trailing speech that hadn't
        reached the silence threshold for segment completion.  Returns
        ``None`` if there is no pending speech.
        """
        # Zero-pad the leftover once at end-of-recording (only safe place to do so)
        if len(self._leftover) > 0:
            model = self._ensure_model()
            padded = np.zeros(_SILERO_WINDOW, dtype=np.float32)
            padded[: len(self._leftover)] = self._leftover.astype(np.float32) / 32768.0
            prob = self._infer(model, padded)
            self._update_state(prob, self._leftover)
            self._leftover = np.empty(0, dtype=np.int16)

        if self._speech_buffer:
            audio = np.concatenate(self._speech_buffer)
            self._speech_buffer.clear()
            self._speech_samples = 0
            self._silence_samples = 0
            return audio
        return None

    def reset(self) -> None:
        """Reset all internal state between recordings."""
        self._in_speech = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._speech_buffer.clear()
        self._pre_roll_buffer.clear()
        self._pre_roll_total = 0
        self._leftover = np.empty(0, dtype=np.int16)
        if self._model is not None:
            # Some model-loading paths (torch.hub vs silero-vad wrapper)
            # may not implement `reset_states`. Guard the call.
            try:
                if hasattr(self._model, "reset_states"):
                    self._model.reset_states()
            except Exception:
                logger.debug("VAD model reset_states() not available or failed; continuing")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _infer(self, model: torch.jit.RecursiveScriptModule, window: np.ndarray) -> float:
        """Run a single Silero forward pass and return speech probability."""
        chunk_tensor = torch.FloatTensor(window)
        prob = model(chunk_tensor, _SILERO_SR)
        return float(prob.squeeze().item())

    def _update_state(self, prob: float, raw_chunk: np.ndarray) -> None:
        """Update the speech/silence state machine with one window result."""
        is_speech_frame = prob >= self._threshold

        if is_speech_frame:
            if not self._in_speech:
                # Transition: silence -> speech
                self._in_speech = True
                # Prepend pre-roll context
                if self._pre_roll_buffer:
                    self._speech_buffer.extend(self._pre_roll_buffer)
                    self._pre_roll_buffer.clear()
                    self._pre_roll_total = 0
                logger.debug("VAD: speech onset (prob=%.3f)", prob)

            self._speech_buffer.append(raw_chunk.copy())
            self._speech_samples += len(raw_chunk)
            self._silence_samples = 0
        else:
            if self._in_speech:
                # Still accumulating trailing audio into the speech buffer
                self._speech_buffer.append(raw_chunk.copy())
                self._silence_samples += len(raw_chunk)
                if self._silence_samples >= self._min_silence_samples:
                    self._in_speech = False
                    logger.debug("VAD: speech offset (silence=%d samples)", self._silence_samples)
            else:
                # Maintain pre-roll ring
                self._pre_roll_buffer.append(raw_chunk.copy())
                self._pre_roll_total += len(raw_chunk)
                while self._pre_roll_total > self._pre_roll_samples and self._pre_roll_buffer:
                    removed = self._pre_roll_buffer.pop(0)
                    self._pre_roll_total -= len(removed)
                self._silence_samples += len(raw_chunk)
