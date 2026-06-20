"""
VoiceFlow Sidecar - Audio capture module.

Records 16 kHz mono int16 audio from the system microphone using the
WASAPI host API on Windows. Session audio is kept in memory as chunked
buffers for the full recording duration, then explicitly zeroed on stop
(HIPAA compliance).

No audio is ever written to disk.
"""

from __future__ import annotations

import logging
import time
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
from scipy.signal import resample_poly
from math import gcd

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - exercised in test environments without audio deps
    sd = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_SAMPLE_RATE: int = 16_000
DEFAULT_CHUNK_MS: int = 80
_PROBE_DURATION_S: float = 0.18
_MAX_PROBED_DEVICES: int = 8

_SKIP_KEYWORDS = {
    "virtual",
    "cable",
    "stereo mix",
    "sound mapper",
    "primary sound",
    "line out",
    "line 1",
}
_REAL_MIC_KEYWORDS = {
    "microphone",
    "mic",
    "headset",
    "headphone",
    "earbud",
    "earbuds",
    "buds",
    "airpods",
    "hands-free",
    "ag audio",
}
_API_PRIORITY = {"windows wasapi": 0, "windows directsound": 1, "wdm-ks": 2, "mme": 3}


@dataclass
class AudioDevice:
    """Descriptor returned by :meth:`AudioCapture.list_devices`."""

    id: int
    name: str
    channels: int
    sample_rate: float
    host_api: str
    activity_score: float = 0.0
    recommended: bool = False


class AudioCapture:
    """Stream microphone audio and accumulate it for the full session."""

    # Cache the recommended-device probe across recordings. Re-probing on every
    # `start_recording` opens 0.18s WASAPI streams on every candidate device;
    # those handles do not always release in time for the real recording stream
    # to open, causing intermittent paInvalidDevice (-9996) failures on Windows.
    _cached_recommended_id: Optional[int] = None

    @classmethod
    def invalidate_recommendation_cache(cls) -> None:
        cls._cached_recommended_id = None

    def __init__(
        self,
        device_id: Optional[int] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        chunk_size_ms: int = DEFAULT_CHUNK_MS,
    ) -> None:
        if device_id is not None:
            self._device_id = device_id
        else:
            if AudioCapture._cached_recommended_id is None:
                AudioCapture._cached_recommended_id = self._find_real_mic()
            self._device_id = AudioCapture._cached_recommended_id
        logger.info("AudioCapture using device_id=%s", self._device_id)
        self._target_rate = sample_rate  # what the ASR model expects (16 kHz)
        self._device_rate: int = self._get_device_rate()
        self._needs_resample = self._device_rate != self._target_rate

        if self._needs_resample:
            g = gcd(self._target_rate, self._device_rate)
            self._resample_up = self._target_rate // g
            self._resample_down = self._device_rate // g
            logger.info(
                "Will resample %d → %d Hz (up=%d, down=%d)",
                self._device_rate, self._target_rate,
                self._resample_up, self._resample_down,
            )

        # Chunk size in samples at the *device* sample rate
        self._chunk_samples = int(self._device_rate * chunk_size_ms / 1000)

        self._sample_rate = sample_rate
        self._session_chunks: list[np.ndarray] = []
        self._session_samples: int = 0

        self._stream: Optional[sd.InputStream] = None
        self._chunk_callback: Optional[Callable[[np.ndarray], None]] = None
        self._lock = threading.Lock()
        self._recording = False

    def _get_device_rate(self) -> int:
        """Return the native sample rate of the selected device."""
        if sd is None:
            return self._target_rate
        try:
            if self._device_id is not None:
                info = sd.query_devices(self._device_id)
            else:
                info = sd.query_devices(kind="input")
            native = int(info["default_samplerate"])
            logger.info("Device native sample rate: %d Hz", native)
            return native
        except Exception:
            logger.debug("Could not query device rate, assuming %d", self._target_rate)
            return self._target_rate

    # ------------------------------------------------------------------
    # Smart default mic selection
    # ------------------------------------------------------------------

    @staticmethod
    def _find_real_mic() -> Optional[int]:
        """Find the best microphone for the default mode.

        The default path prefers the currently most active real microphone.
        If activity probing fails or everything is silent, we fall back to the
        best-ranked real input device by host API.
        """
        recommended = AudioCapture.recommended_device_id()
        if recommended is not None:
            logger.info("Auto-selected mic: [%d]", recommended)
            return recommended
        return None

    @staticmethod
    def _host_api_name(host_apis: list[dict[str, Any]], api_idx: int) -> str:
        if 0 <= api_idx < len(host_apis):
            return str(host_apis[api_idx].get("name", ""))
        return ""

    @staticmethod
    def _normalize_device_name(name: str) -> str:
        base = name.lower().strip()
        for separator in (" (", ","):
            if separator in base:
                base = base.split(separator, 1)[0].strip()
        return base

    @staticmethod
    def _is_real_mic_candidate(name: str) -> bool:
        name_lower = name.lower()
        if any(keyword in name_lower for keyword in _SKIP_KEYWORDS):
            return False
        if any(keyword in name_lower for keyword in _REAL_MIC_KEYWORDS):
            return True
        return "input" not in name_lower and "output" not in name_lower

    @staticmethod
    def _enumerate_input_devices() -> list[AudioDevice]:
        devices: list[AudioDevice] = []
        if sd is None:
            return devices
        try:
            all_devs = sd.query_devices()
            host_apis = sd.query_hostapis()
            for idx, dev in enumerate(all_devs):  # type: ignore[arg-type]
                if dev.get("max_input_channels", 0) <= 0:  # type: ignore[union-attr]
                    continue
                name = str(dev["name"])  # type: ignore[index]
                if not AudioCapture._is_real_mic_candidate(name):
                    continue
                host_api = AudioCapture._host_api_name(host_apis, int(dev.get("hostapi", -1)))  # type: ignore[arg-type]
                devices.append(
                    AudioDevice(
                        id=idx,
                        name=name,
                        channels=int(dev["max_input_channels"]),  # type: ignore[index]
                        sample_rate=float(dev["default_samplerate"]),  # type: ignore[index]
                        host_api=host_api,
                    )
                )
        except Exception:
            logger.exception("Failed to enumerate audio devices")
        return devices

    @staticmethod
    def _dedupe_devices(devices: list[AudioDevice]) -> list[AudioDevice]:
        deduped: dict[str, AudioDevice] = {}
        for device in devices:
            key = AudioCapture._normalize_device_name(device.name)
            existing = deduped.get(key)
            device_priority = _API_PRIORITY.get(device.host_api.lower(), 99)
            if existing is None:
                deduped[key] = device
                continue
            existing_priority = _API_PRIORITY.get(existing.host_api.lower(), 99)
            if device_priority < existing_priority:
                deduped[key] = device
        return sorted(
            deduped.values(),
            key=lambda dev: (_API_PRIORITY.get(dev.host_api.lower(), 99), dev.name.lower()),
        )

    @staticmethod
    def _probe_device_activity(device: AudioDevice) -> float:
        peaks: list[float] = []
        if sd is None:
            return 0.0

        def callback(indata: np.ndarray, _frames: int, _time_info: Any, status: sd.CallbackFlags) -> None:
            if status:
                logger.debug("Probe callback status for %s: %s", device.name, status)
            peaks.append(float(np.max(np.abs(indata[:, 0]))))

        try:
            stream = sd.InputStream(
                samplerate=device.sample_rate,
                blocksize=max(256, int(device.sample_rate * 0.04)),
                device=device.id,
                channels=1,
                dtype="int16",
                callback=callback,
            )
            stream.start()
            time.sleep(_PROBE_DURATION_S)
            stream.stop()
            stream.close()
        except Exception:
            logger.debug("Activity probe failed for device [%d] %s", device.id, device.name, exc_info=True)
            return 0.0

        if not peaks:
            return 0.0
        return max(peaks) / 32768.0

    @classmethod
    def list_devices(cls) -> tuple[list[AudioDevice], Optional[int]]:
        """Return filtered microphone devices and the recommended default device id.

        The "most active" probe was deliberately removed — opening and closing
        a real input stream on every candidate device in rapid succession was
        the dominant cause of Windows audio driver state corruption (manifesting
        as paInvalidDevice and unanticipated DirectSound errors on the very
        next real recording). Ranking by host-API priority is sufficient: the
        WASAPI variant of the user's primary mic is what we want anyway.
        """
        devices = cls._dedupe_devices(cls._enumerate_input_devices())
        if not devices:
            return [], None

        recommended = min(
            devices,
            key=lambda dev: (
                _API_PRIORITY.get(dev.host_api.lower(), 99),
                -dev.channels,
                dev.name.lower(),
            ),
        )
        recommended.recommended = True
        cls._cached_recommended_id = recommended.id
        return devices, recommended.id

    @classmethod
    def recommended_device_id(cls) -> Optional[int]:
        devices, recommended_id = cls.list_devices()
        if devices:
            return recommended_id
        return None

    @classmethod
    def describe_device(cls, device_id: Optional[int]) -> Optional[AudioDevice]:
        """Return metadata for a specific input device or the OS default input."""
        if sd is None:
            return None

        try:
            if device_id is None:
                info = sd.query_devices(kind="input")
            else:
                info = sd.query_devices(device_id)
            host_apis = sd.query_hostapis()
        except Exception:
            logger.debug("Failed to describe audio device %s", device_id, exc_info=True)
            return None

        host_api_idx = int(info.get("hostapi", -1))
        return AudioDevice(
            id=int(device_id) if device_id is not None else -1,
            name=str(info.get("name", "Default Input")),
            channels=int(info.get("max_input_channels", 1)),
            sample_rate=float(info.get("default_samplerate", DEFAULT_SAMPLE_RATE)),
            host_api=cls._host_api_name(host_apis, host_api_idx),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, chunk_callback: Callable[[np.ndarray], None]) -> None:
        """Begin streaming audio.  *chunk_callback* is called with each
        numpy chunk (int16, mono) from the audio thread.
        """
        if sd is None:
            raise RuntimeError("sounddevice is not installed")
        if self._recording:
            logger.warning("AudioCapture.start() called while already recording")
            return

        self._chunk_callback = chunk_callback
        with self._lock:
            for chunk in self._session_chunks:
                chunk[:] = 0
            self._session_chunks.clear()
            self._session_samples = 0

        # Build the list of (device_id, use_wasapi) attempts:
        #   1. configured device with WASAPI shared mode
        #   2. configured device with default host-API settings
        #   3. each alternate host-API ID for the same physical mic
        # This handles two real failure modes on Windows:
        #   - paInvalidDevice (-9996) when a recently-closed WASAPI handle has
        #     not been fully released yet (we retry the same ID with a short
        #     sleep between attempts).
        #   - The configured device id being WASAPI-only and currently held
        #     by another process; falling through to DirectSound/MME on the
        #     same physical mic recovers cleanly.
        attempts: list[tuple[int, bool, int]] = [
            (self._device_id, True, self._device_rate),
            (self._device_id, False, self._device_rate),
        ]
        for alt_id, alt_rate in self._alternate_device_ids_with_rate():
            attempts.append((alt_id, False, alt_rate))

        last_error: Optional[Exception] = None
        stream_opened = False
        for attempt_idx in range(3):
            if attempt_idx > 0:
                # 200ms is empirically enough on Windows for WASAPI / DirectSound
                # to release a handle whose previous owner just closed it.
                time.sleep(0.2)
            for dev_id, use_wasapi, dev_rate in attempts:
                blocksize = max(1, int(dev_rate * DEFAULT_CHUNK_MS / 1000))
                kwargs: dict[str, Any] = dict(
                    samplerate=dev_rate,
                    blocksize=blocksize,
                    device=dev_id,
                    channels=1,
                    dtype="int16",
                    callback=self._audio_callback,
                )
                if use_wasapi:
                    try:
                        kwargs["extra_settings"] = sd.WasapiSettings(exclusive=False)
                    except Exception:
                        continue
                try:
                    self._stream = sd.InputStream(**kwargs)
                    self._stream.start()
                except Exception as exc:
                    last_error = exc
                    continue

                # Stream opened successfully — adopt this device's rate.
                if dev_id != self._device_id or dev_rate != self._device_rate:
                    logger.info(
                        "Audio capture: configured device %s unavailable, using device %s instead",
                        self._device_id, dev_id,
                    )
                    self._device_id = dev_id
                    self._update_resample_params(dev_rate)
                self._chunk_samples = blocksize
                stream_opened = True
                logger.info(
                    "Audio capture started (device=%s, host=%s, rate=%d Hz)",
                    dev_id, "WASAPI" if use_wasapi else "default", dev_rate,
                )
                break
            if stream_opened:
                break

        if not stream_opened:
            logger.error("Failed to start audio stream after retries: %s", last_error)
            # Force the next AudioCapture to re-probe in case the device list
            # changed (e.g. mic unplugged mid-session).
            AudioCapture.invalidate_recommendation_cache()
            self._recording = False
            if last_error is not None:
                raise last_error
            raise RuntimeError("Failed to open audio input stream")

        self._recording = True

    def _resample_to_int16(self, raw: np.ndarray) -> np.ndarray:
        """Polyphase-resample an int16 chunk to the target rate.

        ``resample_poly`` runs in float and its FIR ringing can overshoot the
        original full-scale range (a near-0 dBFS input commonly peaks ~1-2 dB
        above ±32767 after filtering). Casting straight to int16 would wrap
        those overshoots into the opposite polarity — a loud click on top of
        the speech that degrades ASR accuracy. Clip to the int16 range before
        casting so overshoot saturates instead of wrapping.
        """
        resampled = resample_poly(
            raw.astype(np.float32), self._resample_up, self._resample_down,
        )
        np.clip(resampled, -32768, 32767, out=resampled)
        return resampled.astype(np.int16)

    def _update_resample_params(self, device_rate: int) -> None:
        self._device_rate = device_rate
        self._needs_resample = device_rate != self._target_rate
        if self._needs_resample:
            g = gcd(self._target_rate, device_rate)
            self._resample_up = self._target_rate // g
            self._resample_down = device_rate // g

    def _alternate_device_ids_with_rate(self) -> list[tuple[int, int]]:
        """Return alternate (device_id, native_rate) tuples for likely-same
        physical mic on different host APIs. Used when the configured device
        cannot be opened (WASAPI handle stuck, exclusive lock, driver state).

        Matching is by name-prefix, not exact normalized name. Windows often
        exposes one physical mic under several near-identical names — e.g.
        "Microphone Array", "Microphone Array 1", "Microphone Array 2",
        "Microphone Array 3" — across MME / DirectSound / WASAPI / WDM-KS.
        Treat all of those as fallback candidates for the same hardware.
        Returned in host-API priority order (WASAPI first, MME last).
        """
        if sd is None or self._device_id is None:
            return []
        try:
            my_info = sd.query_devices(self._device_id)
            my_norm = self._normalize_device_name(str(my_info["name"]))
        except Exception:
            return []

        my_root = my_norm.rstrip(" 0123456789").strip()
        if not my_root:
            return []

        candidates: list[AudioDevice] = []
        seen: set[int] = {self._device_id}
        for dev in self._enumerate_input_devices():
            if dev.id in seen:
                continue
            other_norm = self._normalize_device_name(dev.name)
            other_root = other_norm.rstrip(" 0123456789").strip()
            if my_root == other_root:
                candidates.append(dev)
                seen.add(dev.id)

        candidates.sort(
            key=lambda dev: (
                _API_PRIORITY.get(dev.host_api.lower(), 99),
                -dev.channels,
                dev.name.lower(),
            ),
        )
        return [(dev.id, int(dev.sample_rate)) for dev in candidates]

    def stop(self) -> np.ndarray:
        """Stop streaming and return the full captured audio buffer.

        The internal session buffers are explicitly zeroed after the copy is
        taken (HIPAA compliance).
        """
        if not self._recording:
            logger.warning("AudioCapture.stop() called while not recording")
            return np.array([], dtype=np.int16)

        self._recording = False

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("Error closing audio stream")
            finally:
                self._stream = None

        with self._lock:
            if self._session_chunks:
                audio = np.concatenate(self._session_chunks).copy()
            else:
                audio = np.array([], dtype=np.int16)

            for chunk in self._session_chunks:
                chunk[:] = 0
            self._session_chunks.clear()
            self._session_samples = 0

        logger.info("Audio capture stopped, returned %d samples", len(audio))
        return audio

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Called by sounddevice from the audio thread."""
        if status:
            logger.warning("Audio callback status: %s", status)

        raw = indata[:, 0].copy()  # shape (frames,) int16

        # Resample from device rate to target rate if needed
        if self._needs_resample:
            chunk = self._resample_to_int16(raw)
        else:
            chunk = raw

        with self._lock:
            session_chunk = chunk.copy()
            self._session_chunks.append(session_chunk)
            self._session_samples += len(session_chunk)

        # Invoke user callback outside the lock
        if self._chunk_callback is not None:
            try:
                self._chunk_callback(chunk)
            except Exception:
                logger.exception("Error in chunk callback")
