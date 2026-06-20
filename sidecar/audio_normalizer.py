"""Audio pre-processing: high-pass filter and loudness normalisation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from config import PostProcessConfig

logger = logging.getLogger(__name__)


class AudioNormalizer:
    """Per-chunk and per-window audio normaliser.

    apply_highpass  — called per incoming chunk before VAD
    normalize_loudness — called on the assembled decode window before Parakeet
    """

    def __init__(self, config: "PostProcessConfig | None" = None) -> None:
        self._config = config
        self._cutoff_hz: int = getattr(config, "audio_highpass_cutoff_hz", 80) if config else 80
        self._enabled: bool = getattr(config, "audio_normalizer_enabled", True) if config else True
        self._meter = None
        self._meter_attempted = False
        # Streaming high-pass filter state. The filter is run statefully
        # (sosfilt + carried zi) so consecutive 80 ms chunks form one
        # continuous filtered stream — no per-chunk edge transients.
        self._hp_sos = None
        self._hp_sos_sr: int | None = None
        self._hp_zi = None

    def reset(self) -> None:
        """Clear streaming high-pass state between recordings.

        Must be called at the start of each recording so the filter's memory
        from the previous session does not leak a startup transient into the
        new one.
        """
        self._hp_zi = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _get_hp_sos(self, sr: int):
        """Lazily build and cache the high-pass SOS for the given rate."""
        if self._hp_sos is not None and self._hp_sos_sr == sr:
            return self._hp_sos
        try:
            from scipy.signal import butter  # type: ignore

            nyq = sr / 2.0
            normalized_cutoff = max(1e-4, min(self._cutoff_hz / nyq, 0.9999))
            self._hp_sos = butter(4, normalized_cutoff, btype="high", output="sos")
            self._hp_sos_sr = sr
        except Exception:
            logger.debug("High-pass filter design unavailable")
            self._hp_sos = None
        return self._hp_sos

    def apply_highpass(self, audio: np.ndarray, sr: int = 16000) -> np.ndarray:
        """Stateful streaming Butterworth high-pass at self._cutoff_hz.

        Runs ``sosfilt`` with carried ``zi`` so successive chunks form one
        continuous filtered stream. The previous implementation ran a
        zero-phase ``sosfiltfilt`` independently on every 80 ms chunk, which
        injected edge transients at each chunk boundary — a buzz the VAD and
        live preview then had to cope with. The causal stateful filter has no
        boundary artefacts (the minor causal phase shift is irrelevant to
        ASR/VAD). Also removes DC as a side effect of the high-pass.

        Returns int16 array same shape as input. No-op when disabled or empty.
        Call :meth:`reset` between recordings.
        """
        if not self._enabled or audio is None or audio.size == 0:
            return audio

        sos = self._get_hp_sos(sr)
        if sos is None:
            # scipy unavailable: best-effort stateless DC removal.
            float_audio = audio.astype(np.float32)
            float_audio -= float_audio.mean()
            return np.clip(float_audio, -32768, 32767).astype(np.int16)

        from scipy.signal import sosfilt, sosfilt_zi  # type: ignore

        float_audio = audio.astype(np.float32)
        if self._hp_zi is None:
            # Initialise to the filter's steady-state response for the first
            # sample so we don't ring at recording start.
            self._hp_zi = sosfilt_zi(sos) * float_audio[0]
        filtered, self._hp_zi = sosfilt(sos, float_audio, zi=self._hp_zi)
        return np.clip(filtered, -32768, 32767).astype(np.int16)

    def filter_session(self, audio: np.ndarray, sr: int = 16000) -> np.ndarray:
        """Zero-phase high-pass for the full-session offline decode.

        The streaming :meth:`apply_highpass` runs per 80 ms chunk and only
        feeds the live preview / VAD. The authoritative final transcript is
        decoded from the full raw session buffer, which never saw any
        high-pass at all. This applies the same rumble/DC removal to the whole
        buffer in one zero-phase pass (``sosfiltfilt`` — no boundary
        transients, no streaming state), so the final decode benefits from the
        same cleanup the preview path does.

        Returns int16 array same shape as input. No-op when disabled or empty.
        """
        if not self._enabled or audio is None or audio.size == 0:
            return audio

        float_audio = audio.astype(np.float32)
        float_audio -= float_audio.mean()  # DC offset removal

        try:
            from scipy.signal import butter, sosfiltfilt  # type: ignore

            nyq = sr / 2.0
            normalized_cutoff = max(1e-4, min(self._cutoff_hz / nyq, 0.9999))
            sos = butter(4, normalized_cutoff, btype="high", output="sos")
            # sosfiltfilt needs enough samples for its edge padding; fall back
            # to the DC-removed signal for very short buffers.
            if float_audio.size > 3 * sos.shape[0] * 2:
                float_audio = sosfiltfilt(sos, float_audio).astype(np.float32)
        except Exception:
            logger.debug("Session high-pass unavailable; returning DC-removed audio")

        return np.clip(float_audio, -32768, 32767).astype(np.int16)

    def normalize_loudness(self, audio: np.ndarray, sr: int = 16000) -> np.ndarray:
        """Normalise loudness conservatively so Parakeet sees consistent gain.

        Key guarantees:
          - Never amplify into clipping (caps gain so post-gain peak ≤ 0.95)
          - Limit absolute gain to ±6 dB so we never distort already-healthy audio
          - Skip normalisation entirely when peak is already in [-14, -1] dBFS
            (Parakeet handles that range fine; touching it can introduce
            clipping artefacts that produce empty transcripts)
          - Fall back to gentle RMS scaling on silence, NaN, or pyloudnorm failure

        Returns int16 array same shape as input. No-op when disabled or empty.
        """
        if not self._enabled or audio is None or audio.size == 0:
            return audio

        # Healthy-range bypass: peak between -14 and -1 dBFS → leave alone.
        peak = int(np.max(np.abs(audio))) if audio.size else 0
        if 6500 <= peak <= 31000:  # ~ -14 dBFS .. -0.5 dBFS
            return audio

        float_audio = audio.astype(np.float32) / 32768.0

        # Too short for pyloudnorm (needs ≥400 ms): RMS only
        if audio.size < int(0.4 * sr):
            return self._rms_normalize(audio)

        meter = self._get_meter(sr)
        if meter is not None:
            try:
                loudness = meter.integrated_loudness(float_audio)
                # Silent / NaN guard
                if not np.isfinite(loudness) or loudness < -70.0:
                    return self._rms_normalize(audio)

                # Compute gain in dB; clamp to ±6 dB; also clamp so peak ≤ 0.95
                gain_db = -23.0 - loudness
                gain_db = max(-6.0, min(6.0, gain_db))
                gain = 10.0 ** (gain_db / 20.0)
                # Anti-clip ceiling
                peak_float = float(np.max(np.abs(float_audio)))
                if peak_float > 0:
                    max_gain = 0.95 / peak_float
                    gain = min(gain, max_gain)

                normalized = float_audio * gain
                if not np.all(np.isfinite(normalized)):
                    return self._rms_normalize(audio)
                return np.clip(normalized * 32768.0, -32768, 32767).astype(np.int16)
            except Exception:
                logger.debug("pyloudnorm normalisation failed; falling back to RMS")

        return self._rms_normalize(audio)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rms_normalize(self, audio: np.ndarray) -> np.ndarray:
        """Scale so RMS matches a target ~−23 LUFS equivalent (~0.07 full-scale).

        Caps gain to ±6 dB and enforces post-gain peak ≤ 0.95 so we never
        clip into Parakeet's input.
        """
        float_audio = audio.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(float_audio ** 2)))
        peak_float = float(np.max(np.abs(float_audio)))
        if rms < 1e-6 or peak_float < 1e-6:
            return audio
        target_rms = 0.07
        scale = target_rms / rms
        # Cap absolute gain to ±6 dB
        scale = max(0.5, min(scale, 2.0))
        # Anti-clip ceiling
        scale = min(scale, 0.95 / peak_float)
        return np.clip(float_audio * scale * 32768.0, -32768, 32767).astype(np.int16)

    def _get_meter(self, sr: int):
        if self._meter is not None:
            return self._meter
        if self._meter_attempted:
            return None
        self._meter_attempted = True
        try:
            import pyloudnorm as pyln  # type: ignore

            self._meter = pyln.Meter(sr)
            logger.debug("pyloudnorm meter initialised at %d Hz", sr)
        except Exception:
            logger.debug("pyloudnorm not available; will use RMS normalisation")
        return self._meter
