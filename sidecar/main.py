"""
VoiceFlow Sidecar - Entry point.

Wires together the full pipeline:
  AudioCapture -> VAD -> ASREngine -> PostProcessor -> TextInjector

Communicates with the Tauri front-end via JSON-lines IPC on
stdin / stdout.  All logging goes to stderr.

HIPAA compliance guarantees:
  - Zero disk writes (audio, text, logs stay in memory only)
  - Explicit buffer zeroing on stop / shutdown
  - No outbound network calls
"""

from __future__ import annotations

import os
import logging
import math
import queue
import sys
import threading
import time
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any

# Block all outbound network calls from HuggingFace and PyTorch libraries.
# Models must be pre-cached; this ensures fully offline / HIPAA-safe operation.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TORCH_HOME", os.environ.get("TORCH_HOME", ""))  # keep existing if set

import numpy as np

from asr_engine import ASREngine, TranscriptionResult
from audio_capture import AudioCapture
from audio_normalizer import AudioNormalizer
from config import AppConfig
from confidence_gate import align_confidences
from context_capture import ActiveTextContext, capture_active_text_context
from diagnostic_logger import diag
from itn import InverseTextNormalizer
from ipc_bridge import IPCBridge
from llm_formatter import LLMFormatter
from lm_rescorer import LMRescorer
from post_processor import PostProcessor
from seam_nbest import seam_merge
from text_injector import FinalDeliveryResult, TextInjector
from vad import VoiceActivityDetector
from vocabulary import VocabularyCorrector
from name_casing import NameCasingCorrector
from name_matcher import NameMatcher

# ---------------------------------------------------------------------------
# Logging: stderr only (HIPAA - no disk writes)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("voiceflow.main")

# Sentinel object pushed into the queue to signal the worker to stop.
_STOP_SENTINEL = object()

# ---------------------------------------------------------------------------
# LocalAgreement-2 sample-drop clamps.
#
# When the agreement step commits N words from a partial decode, we drop a
# proportional prefix from the unconfirmed audio buffer:
#     samples_to_drop = unconfirmed_samples * (agreed / total_words)
# That formula assumes a uniform speaking rate across the buffer. In practice
# the trailing edge often holds silence/hesitation, so the front of the buffer
# is denser in words; the proportional estimate then UNDER-drops, leaving
# already-committed audio in the buffer. LA-2 then re-decodes that audio and
# can either thrash (oscillating commits) or emit duplicate commits.
#
# We clamp the drop range to bound that failure mode:
#   - LOWER bound: ADAPTIVE. We track an EMA of observed samples-per-word from
#     each commit's proportional estimate, and use 70% of that as the dynamic
#     floor. The constant below is the *absolute hard floor* — ~75ms covers
#     the shortest plausible English token (digits, monosyllables). A static
#     ~200ms floor over-drops fast speech (e.g. counting digits at ~150ms each)
#     and produces torn fragments that the ASR hallucinates on.
#   - UPPER bound: always keep ~100ms of unconfirmed tail so the next partial
#     decode has something fresh to compare against (prevents over-drop into
#     the next speech segment).
# ---------------------------------------------------------------------------
_LA_MIN_SAMPLES_PER_WORD = 1200   # ~75ms @ 16kHz absolute hard floor per word
_LA_MIN_TAIL_SAMPLES = 1600       # ~100ms @ 16kHz, always retained as tail

# ---------------------------------------------------------------------------
# Parakeet silence-hallucination filter
# ---------------------------------------------------------------------------
# Parakeet (and all encoder-decoder ASR) produces phantom single-token
# interjections on near-silent audio: "Yeah", "Mm", "Okay", "Uh", "Hmm",
# "Mm-hmm". These pollute the live preview, the VAD-commit stream, and the
# rolling-context block we feed to the LLM. A simple two-part guard works
# well in practice: (1) the decoded text is one of a known stop-words set,
# AND (2) the audio peak is below a confidence threshold.
_HALLUCINATION_TOKENS: frozenset[str] = frozenset({
    "yeah", "yeah.", "ok", "okay", "okay.",
    "mm", "mm.", "mm-hmm", "mm-hmm.", "hmm", "hmm.",
    "uh", "uh.", "uhh", "umm", "um", "um.",
    "ah", "ah.", "oh", "oh.",
})
# Peak amplitude (int16) below which a one-token decode is treated as a
# hallucination. ~ -28 dBFS — quieter than typical desk-microphone speech.
_HALLUCINATION_PEAK_INT16: int = 1300


def _is_likely_hallucination(text: str, audio: np.ndarray) -> bool:
    """Return True if *text* is a single short interjection from quiet audio."""
    if not text:
        return False
    stripped = text.strip().lower()
    if not stripped:
        return False
    # Single token (≤ 2 syllables, no spaces means one word)
    tokens = stripped.split()
    if len(tokens) > 1:
        return False
    if tokens[0] not in _HALLUCINATION_TOKENS:
        return False
    if audio is None or audio.size == 0:
        return True
    return int(np.max(np.abs(audio))) < _HALLUCINATION_PEAK_INT16


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

class Pipeline:
    """Manages the lifecycle of all pipeline components."""

    def __init__(self) -> None:
        self.config = AppConfig()
        self.audio = AudioCapture(
            device_id=self.config.microphone_device_id,
        )
        self.vad = VoiceActivityDetector(
            min_silence_ms=getattr(self.config.post_processing, "vad_min_silence_ms", 300),
            pre_roll_ms=getattr(self.config.post_processing, "vad_pre_roll_ms", 80),
        )
        self.asr = ASREngine(
            model_name=self.config.model,
            num_threads=self.config.num_threads,
        )
        self.post = PostProcessor(self.config.post_processing)
        self.itn = InverseTextNormalizer()
        self.vocab = VocabularyCorrector(getattr(self.config.post_processing, "custom_vocabulary_path", None))
        self.name_casing = NameCasingCorrector(getattr(self.config.post_processing, "name_casing_index_path", None))
        self.name_matcher = self._build_name_matcher()
        self.llm_formatter = LLMFormatter(self.config.post_processing)
        self.audio_normalizer = AudioNormalizer(self.config.post_processing)
        self.lm_rescorer = LMRescorer(self.config.post_processing)
        self.injector = TextInjector()
        diag.configure(
            enabled=getattr(self.config.post_processing, "diagnostic_logging_enabled", False),
            output_dir=getattr(self.config.post_processing, "diagnostic_log_dir", None),
        )

        # Transcript state for the current recording session
        self._session_raw_parts: list[str] = []
        self._session_text_parts: list[str] = []
        self._session_transcript: str = ""
        self._live_partial_text: str = ""
        self._live_partial_raw_text: str = ""
        # LocalAgreement-2 streaming state
        # Rolling unconfirmed audio buffer (concatenated int16 samples) and the
        # previous partial transcript's word list. Words common (positionally)
        # between two consecutive partials are committed; the matching audio
        # prefix is dropped from the unconfirmed buffer.
        self._unconfirmed_audio: list[np.ndarray] = []
        self._unconfirmed_samples: int = 0
        self._samples_since_last_la: int = 0
        self._last_partial_words: list[str] = []
        self._la_cadence_sec: float = 0.3
        self._la_cadence_samples: int = int(16_000 * self._la_cadence_sec)
        self._la_max_buffer_sec: float = 30.0
        self._la_max_buffer_samples: int = int(16_000 * self._la_max_buffer_sec)
        # Adaptive samples-per-word tracker for the drop-floor (see constants
        # block above). Bootstrap at ~150ms/word — fast-speech tolerant so the
        # first few commits don't over-drop. EMA-updated from each commit.
        self._la_samples_per_word_ema: float = 2400.0
        self._la_ema_alpha: float = 0.3
        self._session_speech_detected: bool = False
        self._authoritative_commit_failed: bool = False
        self._active_text_context: ActiveTextContext | None = None
        # Per-word confidences for the final reconciled text (confidence-gated
        # scoped LLM correction). None when reconcile did not run (fallback path).
        self._last_word_confidences: list[float] | None = None

        # Silence tracking for auto-stop (in samples at 16 kHz)
        self._silence_since_last_speech: int = 0
        self._auto_stop_silence_sec: float = 0.0  # 0 disables silence-based auto-stop
        self._auto_stop_pending: bool = False

        # Worker thread plumbing
        self._chunk_queue: queue.Queue[Any] = queue.Queue(maxsize=500)
        self._worker_thread: threading.Thread | None = None

        # Deferred settings-rebuild state. A settings update that arrives while
        # the worker thread is consuming chunks must not hot-swap the VAD /
        # normalizer / ASR out from under it (see update_settings). We mark a
        # rebuild pending and apply it at the next start_recording instead.
        self._pending_rebuild: bool = False
        self._pending_rebuild_asr: bool = False

    def _current_session_text(self) -> str:
        return " ".join(self._session_text_parts).strip()

    def _current_raw_session_text(self) -> str:
        return " ".join(self._session_raw_parts).strip()

    def _current_best_transcript(self, current_segment: str | None = None) -> str:
        pieces = list(self._session_text_parts)
        active = current_segment if current_segment is not None else self._live_partial_text
        active = active.strip()
        if active:
            pieces.append(active)
        return " ".join(part for part in pieces if part).strip()

    @staticmethod
    def _delivery_payload(text: str, result: FinalDeliveryResult) -> dict[str, Any]:
        return {
            "text": text,
            "delivery_status": result.status,
            "copied_to_clipboard": result.copied_to_clipboard,
            "pasted_to_target": result.pasted_to_target,
            "manual_paste_required": result.manual_paste_required,
            "failure_reason": result.failure_reason,
        }

    @staticmethod
    def _stable_partial_prefix(partial: str, keep_tail_words: int = 3) -> str:
        text = " ".join(partial.split()).strip()
        if not text:
            return ""
        words = text.split()
        if len(words) <= keep_tail_words:
            return ""
        return " ".join(words[:-keep_tail_words]).strip()

    def _commit_live_partial(self, partial: str) -> None:
        stable = self._stable_partial_prefix(partial)
        clean_partial = " ".join(partial.split()).strip()
        self._live_partial_raw_text = clean_partial
        self._live_partial_text = stable or clean_partial

    def _commit_segment(
        self,
        raw_text: str,
        processed: str,
        *,
        confidence: float | None = None,
        latency_ms: float | None = None,
        audio_duration_ms: float | None = None,
    ) -> None:
        """Commit a finalized segment to the in-app preview transcript."""
        raw_segment = " ".join(raw_text.split()).strip()
        preview_text = processed.strip() or raw_segment
        if not preview_text:
            return

        if raw_segment:
            self._session_raw_parts.append(raw_segment)
        self._session_text_parts.append(preview_text)
        self._session_transcript = self._current_session_text()
        self._live_partial_text = ""
        self._live_partial_raw_text = ""

        IPCBridge.send_event(
            "final_transcript",
            text=self._session_transcript,
            segment_text=preview_text,
            confidence=confidence,
            latency_ms=latency_ms,
            audio_duration_ms=audio_duration_ms,
        )

    @staticmethod
    def _zero_audio_chunks(chunks: list[np.ndarray]) -> None:
        for chunk in chunks:
            if len(chunk) > 0:
                chunk[:] = 0

    # ------------------------------------------------------------------
    # LocalAgreement-2 streaming buffer
    # ------------------------------------------------------------------

    def _clear_unconfirmed_audio(self) -> None:
        """Zero and drop the rolling unconfirmed audio buffer (HIPAA)."""
        self._zero_audio_chunks(self._unconfirmed_audio)
        self._unconfirmed_audio.clear()
        self._unconfirmed_samples = 0
        self._samples_since_last_la = 0
        self._last_partial_words = []

    def _append_unconfirmed_audio(self, chunk: np.ndarray) -> None:
        if len(chunk) == 0:
            return
        self._unconfirmed_audio.append(chunk.copy())
        self._unconfirmed_samples += len(chunk)
        self._samples_since_last_la += len(chunk)

    def _drop_unconfirmed_prefix(self, samples_to_drop: int) -> None:
        """Drop *samples_to_drop* leading samples from the unconfirmed buffer."""
        if samples_to_drop <= 0 or not self._unconfirmed_audio:
            return
        if samples_to_drop >= self._unconfirmed_samples:
            self._clear_unconfirmed_audio()
            return

        combined = np.concatenate(self._unconfirmed_audio)
        remainder = combined[samples_to_drop:].copy()
        # Zero originals for HIPAA hygiene before dropping references.
        self._zero_audio_chunks(self._unconfirmed_audio)
        self._unconfirmed_audio.clear()
        if remainder.size > 0:
            self._unconfirmed_audio.append(remainder)
            self._unconfirmed_samples = len(remainder)
        else:
            self._unconfirmed_samples = 0

    @staticmethod
    def _common_prefix_length(prev_words: list[str], curr_words: list[str]) -> int:
        """Return the length of the positional common word prefix."""
        n = min(len(prev_words), len(curr_words))
        i = 0
        while i < n and prev_words[i] == curr_words[i]:
            i += 1
        return i

    def _emit_partial_event(self) -> None:
        """Send a partial_transcript event with ONLY the unconfirmed tail.

        The frontend renders committed segments (from `final_transcript`) and
        the live partial preview (from `partial_transcript`) in two SEPARATE
        panels — they are not concatenated. Emitting committed+tail here would
        double-render the committed prefix in both panels. The tail-only
        payload matches what the React consumer in `useTauriEvents.ts`
        expects: `setPartialTranscript(event.payload.text)` should receive
        only the still-unconfirmed words.

        `_live_partial_text` / `_live_partial_raw_text` mirror the same tail
        so there is a single source of truth for "the live preview text".
        """
        unconfirmed_tail = " ".join(self._last_partial_words).strip()
        self._live_partial_raw_text = unconfirmed_tail
        self._live_partial_text = unconfirmed_tail
        if unconfirmed_tail:
            IPCBridge.send_event("partial_transcript", text=unconfirmed_tail)

    def _maybe_run_local_agreement(self) -> None:
        """Run a fresh partial decode and commit any agreed prefix.

        Called periodically (every ~_la_cadence_sec of new audio). Implements
        LocalAgreement-2: a word is committed only after it appears in two
        consecutive partial decodes at the same position.
        """
        if self._unconfirmed_samples <= 1600:
            return
        if self._samples_since_last_la < self._la_cadence_samples:
            return
        self._samples_since_last_la = 0

        try:
            combined = np.concatenate(self._unconfirmed_audio)
            audio_s = len(combined) / 16000.0
            normalized = self.audio_normalizer.normalize_loudness(combined)
            result = self.asr.transcribe_segment(normalized)
        except Exception:
            self._authoritative_commit_failed = True
            logger.exception("Error in LocalAgreement-2 partial decode")
            return

        new_words = result.text.split()
        diag.chunk("LA-2", "partial", result.text, audio_s=round(audio_s, 2),
                   latency_ms=int(result.latency_ms or 0))
        if not new_words:
            # No speech recognised yet; keep accumulating.
            return
        # Drop single-token interjections that come from near-silent audio
        # (Parakeet hallucinations: "Yeah", "Mm", "Okay", "Uh", ...).
        if _is_likely_hallucination(result.text, combined):
            diag.event("LA-2", "dropped hallucination",
                       text=result.text,
                       peak=int(np.max(np.abs(combined))) if combined.size else 0)
            return

        prev_words = self._last_partial_words
        agreed = self._common_prefix_length(prev_words, new_words)
        diag.event("LA-2", f"agreement: {agreed} of {len(new_words)} words match previous partial",
                   prev_tail=" ".join(prev_words[-6:]) if prev_words else "",
                   new_head=" ".join(new_words[:6]))

        if agreed > 0:
            confirmed_text = " ".join(new_words[:agreed]).strip()
            if confirmed_text:
                rescored = self.lm_rescorer.rescore(confirmed_text)
                if rescored != confirmed_text:
                    diag.diff("LA-2/lm-rescore", confirmed_text, rescored)
                confirmed_text = rescored
                # Skip rule-based post-processing on short streaming fragments
                # (< 5 words). It adds spurious end-of-sentence periods to
                # phrases that are mid-utterance ("Please" → "Please.").
                # Full post-processing still runs on the final reconciled text.
                if len(confirmed_text.split()) >= 5:
                    processed = self.post.process(confirmed_text) or confirmed_text
                    if processed != confirmed_text:
                        diag.diff("LA-2/post-proc", confirmed_text, processed)
                else:
                    processed = confirmed_text
                # Estimate the audio duration corresponding to the confirmed
                # words. Proportional split assumes a uniform speaking rate;
                # clamp to a per-word floor (under-drop -> thrash) and a
                # tail-preserving ceiling (over-drop -> miss next word).
                total_words = max(len(new_words), 1)
                ratio = agreed / total_words
                proportional_drop = int(self._unconfirmed_samples * ratio)

                # Update EMA from the proportional estimate BEFORE clamping —
                # we want to learn the true rate, not the floor we enforce.
                # Sanity-bound observations to [50ms, 1s] per word to filter
                # outliers (e.g. silence-padded buffers).
                obs_per_word = proportional_drop / agreed
                if 800 <= obs_per_word <= 16000:
                    self._la_samples_per_word_ema = (
                        (1.0 - self._la_ema_alpha) * self._la_samples_per_word_ema
                        + self._la_ema_alpha * obs_per_word
                    )

                # Adaptive lower floor: 70% of learned rate, never below the
                # absolute hard floor. Gives slack for pace variation while
                # still preventing under-drop thrash.
                adaptive_min_per_word = max(
                    float(_LA_MIN_SAMPLES_PER_WORD),
                    self._la_samples_per_word_ema * 0.7,
                )
                samples_to_drop = max(
                    proportional_drop,
                    int(agreed * adaptive_min_per_word),
                )
                samples_to_drop = min(
                    samples_to_drop,
                    max(0, self._unconfirmed_samples - _LA_MIN_TAIL_SAMPLES),
                )
                self._commit_segment(
                    confirmed_text,
                    processed,
                    confidence=result.confidence,
                    latency_ms=result.latency_ms,
                    audio_duration_ms=samples_to_drop / 16.0,  # ms
                )
                self._drop_unconfirmed_prefix(samples_to_drop)

                # The leftover unconfirmed words are everything past the agreed
                # prefix in the latest partial. Keep them as the new baseline.
                self._last_partial_words = new_words[agreed:]
            else:
                self._last_partial_words = new_words
        else:
            self._last_partial_words = new_words

        # Hard-flush guard: if the unconfirmed buffer has grown beyond cap,
        # force-commit the latest partial as confirmed and reset.
        if self._unconfirmed_samples >= self._la_max_buffer_samples:
            logger.warning(
                "DIAG LocalAgreement: hard-flush at %.1fs of unconfirmed audio",
                self._unconfirmed_samples / 16_000.0,
            )
            self._force_commit_unconfirmed()
            return

        self._emit_partial_event()

    def _force_commit_unconfirmed(self) -> None:
        """Force a commit of all unconfirmed audio (e.g. on VAD silence boundary).

        Decodes the entire unconfirmed buffer, commits the resulting text as
        a finalised segment, and resets the LA-2 state. This is the cleanest
        commit point because it lines up with a real silence boundary.
        """
        if self._unconfirmed_samples <= 1600:
            self._clear_unconfirmed_audio()
            return

        try:
            combined = np.concatenate(self._unconfirmed_audio)
            audio_s = len(combined) / 16000.0
            normalized = self.audio_normalizer.normalize_loudness(combined)
            result = self.asr.transcribe_segment(normalized)
            raw_text = result.text.strip()
            diag.chunk("VAD-commit", "force-commit", raw_text,
                       audio_s=round(audio_s, 2),
                       latency_ms=int(result.latency_ms or 0))
            if raw_text and _is_likely_hallucination(raw_text, combined):
                diag.event("VAD-commit", "dropped hallucination",
                           text=raw_text,
                           peak=int(np.max(np.abs(combined))) if combined.size else 0)
                raw_text = ""
            if raw_text:
                processed = self.post.process(raw_text) or raw_text
                if processed != raw_text:
                    diag.diff("VAD-commit/post-proc", raw_text, processed)
                self._commit_segment(
                    raw_text,
                    processed,
                    confidence=result.confidence,
                    latency_ms=result.latency_ms,
                    audio_duration_ms=result.audio_duration_ms,
                )
        except Exception:
            self._authoritative_commit_failed = True
            logger.exception("Error in force-commit decode")
        finally:
            self._clear_unconfirmed_audio()
            try:
                self.asr.reset_stream()
            except Exception:
                logger.debug("ASR reset_stream failed during force-commit", exc_info=True)

    def _build_final_raw_text(self, audio: np.ndarray) -> str:
        """Return the best raw transcript for final delivery.

        The live/session accumulators are still useful for overlay UX during
        recording, but the final pasted text should prefer a full-session
        reconciliation pass so we do not depend on interim chunk boundaries
        or worker timing for correctness.
        """
        reconciled_text = self._reconcile_full_session(audio)
        if reconciled_text:
            return reconciled_text

        if not self._authoritative_commit_failed:
            return self._current_raw_session_text()

        return ""

    def _build_name_matcher(self) -> NameMatcher:
        pp = self.config.post_processing
        return NameMatcher(
            threshold=getattr(pp, "name_match_threshold", 0.92),
            confidence_threshold=getattr(pp, "name_match_confidence_threshold", 0.80),
            capitalized_fallback=getattr(pp, "name_match_capitalized_fallback", False),
        )

    def _format_final_text(self, raw_text: str) -> str:
        """Run LM rescoring, deterministic cleanup, ITN/vocabulary correction, then optional LLM."""
        if not raw_text or not raw_text.strip():
            return ""

        diag.section("LAYER 3: FINAL PIPELINE (scoped LLM → LM rescore → post → ITN → vocab)")

        # --- Confidence-gated scoped LLM correction FIRST ---
        # Runs while per-word confidences still align to the reconciled words
        # (the deterministic stages below change word counts). Scoped mode only
        # edits low-confidence words and freezes the rest; the legacy whole-text
        # rewrite remains available behind a config flag.
        n = int(getattr(self.config.post_processing, "llm_rolling_context_sentences", 3))
        rolling = list(self._session_text_parts[-n:]) if n > 0 else None
        vocab_terms = [t.text for t in self.vocab._terms] if self.vocab._terms else None
        use_scoped = getattr(self.config.post_processing, "llm_scoped_correction", True)
        confs = self._last_word_confidences

        # --- Confidence-gated fuzzy NAME replacement (before anything reshapes
        # the words, so per-word confidence still aligns 1:1 with raw_text). ---
        if getattr(self.config.post_processing, "name_match_enabled", True):
            after_names = self.name_matcher.correct(raw_text, confidences=confs, vocab_terms=vocab_terms)
            diag.diff("name-matcher", raw_text, after_names,
                      label=f"({self.name_matcher.size} names, confs={'yes' if confs else 'no'})")
            raw_text = after_names
        diag.event("llm", "scoped" if use_scoped else "whole-text",
                   input_chars=len(raw_text),
                   word_confs=len(confs) if confs else 0,
                   rolling_ctx_sents=len(rolling) if rolling else 0,
                   vocab_terms=len(vocab_terms) if vocab_terms else 0,
                   llm_enabled=getattr(self.config.post_processing, "llm_enabled", False))
        t0 = time.perf_counter()
        if use_scoped:
            after_llm = self.llm_formatter.format_scoped(
                raw_text, confs, self._active_text_context,
                session_context=rolling, vocab_terms=vocab_terms,
            )
        else:
            after_llm = self.llm_formatter.format_text(
                raw_text, self._active_text_context,
                session_context=rolling, vocab_terms=vocab_terms,
            )
        diag.diff("llm", raw_text, after_llm, label=f"({time.perf_counter() - t0:.1f}s)")
        raw_text = after_llm

        rescored = self.lm_rescorer.rescore(raw_text)
        diag.diff("lm-rescorer", after_llm, rescored,
                  label=f"(backend={self.lm_rescorer._backend})")
        raw_text = rescored

        processed = self.post.process(raw_text).strip() or raw_text.strip()
        diag.diff("post-processor", raw_text, processed)
        formatted = processed

        itn_on = getattr(self.config.post_processing, "itn_enabled", True)
        if itn_on:
            after_itn = self.itn.normalize(formatted)
            diag.diff("itn", formatted, after_itn)
            formatted = after_itn
        else:
            diag.event("itn", "skipped (itn_enabled=False)")

        # Narrow, safe number pass (years + explicit thousands), independent of
        # the full ITN above.
        if getattr(self.config.post_processing, "itn_numbers_enabled", True):
            after_num = self.itn.normalize_numbers(formatted)
            diag.diff("itn-numbers", formatted, after_num)
            formatted = after_num

        after_vocab = self.vocab.correct(formatted)
        diag.diff("vocab-corrector", formatted, after_vocab,
                  label=f"({len(self.vocab._terms)} terms loaded)")
        formatted = after_vocab

        if getattr(self.config.post_processing, "name_casing_enabled", True):
            after_names = self.name_casing.correct(formatted)
            diag.diff("name-casing", formatted, after_names,
                      label=f"({self.name_casing.size} names indexed)")
            formatted = after_names

        diag.note("FINAL", f"({len(formatted)} chars) — to be injected:\n{formatted}")
        return formatted.strip()

    @staticmethod
    def _dedupe_overlap(prev_part: str, next_part: str, *, k: int = 60, min_match: int = 2) -> str:
        """Return *next_part* with a leading region duplicated against the
        tail of *prev_part* removed.

        Two-stage match:
          1. Word-level (case-insensitive): longest common block on the
             tail/head windows. Accepted when ≥ min_match words and the
             head match begins within the first ~5 words.
          2. Char-level fallback: when chunk boundaries cut mid-word or
             Parakeet hallucinates near the seam, the word lists won't
             share enough exact tokens. Match the last ~80 chars of
             *prev_part* against the first ~80 chars of *next_part*
             (lowercased) and trim the overlap.

        Both stages are conservative: if no convincing overlap exists,
        *next_part* is returned unchanged.
        """
        next_words = next_part.split()
        prev_words = prev_part.split()
        if not next_words or not prev_words:
            return next_part

        # ---- Stage 1: word-level, case-insensitive ----
        # Normalize by removing ALL non-alphanumeric chars (not just stripping
        # the word ends) so acoustically identical words that differ only in
        # punctuation across a seam still match — e.g. "p.m." vs "PM". The old
        # end-strip left internal punctuation ("p.m." -> "p.m"), which broke the
        # overlap match mid-block and duplicated the remainder
        # ("...two forty five p.m. The budget. PM The budget changed...").
        def _norm(w: str) -> str:
            n = "".join(ch for ch in w.lower() if ch.isalnum())
            return n or w.lower()  # keep punctuation-only tokens comparable
        tail_lower = [_norm(w) for w in prev_words[-k:]]
        head_lower = [_norm(w) for w in next_words[:k]]
        if tail_lower and head_lower:
            matcher = SequenceMatcher(a=tail_lower, b=head_lower, autojunk=False)
            match = matcher.find_longest_match(0, len(tail_lower), 0, len(head_lower))
            if match.size >= min_match and match.b <= 5:
                drop_until = match.b + match.size
                return " ".join(next_words[drop_until:]).strip()

        # ---- Stage 2: char-level fallback for mid-word seams ----
        tail_chars = prev_part[-80:].lower()
        head_chars = next_part[:80].lower()
        if tail_chars and head_chars:
            c_matcher = SequenceMatcher(a=tail_chars, b=head_chars, autojunk=False)
            c_match = c_matcher.find_longest_match(0, len(tail_chars), 0, len(head_chars))
            # Require a substantial char run that begins near the seam start
            if c_match.size >= 12 and c_match.b <= 10:
                trimmed = next_part[c_match.b + c_match.size:]
                # Snap to nearest word boundary if we trimmed mid-word
                space = trimmed.find(" ")
                if space != -1 and space < 20:
                    trimmed = trimmed[space + 1:]
                return trimmed.strip()

        return next_part

    # ------------------------------------------------------------------
    # Pre-load models (called once, before any recording)
    # ------------------------------------------------------------------

    def warmup(self) -> None:
        """Pre-load VAD and ASR models so the first recording is fast."""
        logger.info("Warming up models...")
        try:
            self.vad.warmup()
        except Exception:
            logger.exception("VAD warmup failed")
        try:
            self.asr.warmup()
        except Exception:
            logger.exception("ASR warmup failed")
        logger.info("Model warmup complete")

    # ------------------------------------------------------------------
    # Audio chunk callback — runs on the sounddevice audio thread.
    # MUST be fast (< 1 ms).  Only enqueues the chunk.
    # ------------------------------------------------------------------

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        """Enqueue the audio chunk for the worker thread to process."""
        try:
            self._chunk_queue.put_nowait(chunk)
        except queue.Full:
            # Drop oldest to make room (back-pressure)
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._chunk_queue.put_nowait(chunk)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Worker thread — runs VAD, streaming ASR, and live injection
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Dequeue audio chunks and run the heavy ML pipeline."""
        logger.info("Worker thread started")
        while True:
            try:
                item = self._chunk_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is _STOP_SENTINEL:
                logger.info("Worker thread received stop sentinel")
                break

            chunk: np.ndarray = item
            try:
                self._process_chunk(chunk)
            except Exception:
                logger.exception("Error in worker chunk processing")
                IPCBridge.send_event("error", message="Audio processing error")

        logger.info("Worker thread exiting")

    def _process_chunk(self, chunk: np.ndarray) -> None:
        """Process a single audio chunk (runs on the worker thread)."""
        chunk = self.audio_normalizer.apply_highpass(chunk)
        vad_result = self.vad.process_chunk(chunk)
        self._append_unconfirmed_audio(chunk)

        if vad_result.is_speech:
            self._silence_since_last_speech = 0
            self._session_speech_detected = True
            IPCBridge.send_event("vad_speech_detected", probability=vad_result.probability)
        else:
            self._silence_since_last_speech += len(chunk)

        # VAD silence boundary -> force-commit the entire unconfirmed buffer.
        # This is the cleanest commit point: a real silence boundary means we
        # do not need word-agreement to be confident about word boundaries.
        if vad_result.segment_complete and vad_result.speech_audio is not None:
            self._force_commit_unconfirmed()
        else:
            # Run LocalAgreement-2 on the rolling unconfirmed buffer at the
            # configured cadence (~1s of new audio).
            self._maybe_run_local_agreement()

        # Auto-stop after prolonged silence.  Dispatch to a separate
        # thread because _do_stop_recording() calls audio.stop() which
        # cannot be called from the worker thread while it is still
        # consuming chunks (would deadlock on queue drain).
        auto_stop_samples = int(16_000 * self._auto_stop_silence_sec)
        if (
            self._auto_stop_silence_sec > 0
            and self._silence_since_last_speech >= auto_stop_samples
            and self._session_speech_detected
            and not self._auto_stop_pending
        ):
            logger.info(
                "Auto-stop triggered after %.1fs of silence",
                self._auto_stop_silence_sec,
            )
            self._auto_stop_pending = True
            IPCBridge.send_event("auto_stop_triggered")
            threading.Thread(
                target=self._do_stop_recording,
                name="auto-stop",
                daemon=True,
            ).start()

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def start_recording(self) -> None:
        """Start the audio capture + VAD + ASR pipeline."""
        if self.audio.is_recording:
            IPCBridge.send_event("error", message="Already recording")
            return

        # Apply any settings rebuild that was deferred because it arrived
        # mid-recording. Safe now: nothing is consuming the pipeline.
        if self._pending_rebuild:
            logger.info("Applying deferred settings rebuild (asr=%s)", self._pending_rebuild_asr)
            self._rebuild_runtime_components(rebuild_asr=self._pending_rebuild_asr)
            self._pending_rebuild = False
            self._pending_rebuild_asr = False

        self._session_raw_parts.clear()
        self._session_text_parts.clear()
        self._session_transcript = ""
        self._live_partial_text = ""
        self._live_partial_raw_text = ""
        self._clear_unconfirmed_audio()
        self._silence_since_last_speech = 0
        self._auto_stop_pending = False
        self._session_speech_detected = False
        self._authoritative_commit_failed = False
        self._active_text_context = None
        self.vad.reset()
        self.asr.reset_stream()
        self.audio_normalizer.reset()
        target_info = self.injector.capture_target()
        self._active_text_context = capture_active_text_context(target_info)
        if self._active_text_context.compact():
            logger.info("Captured formatter context: %s", self._active_text_context.compact(300))
        logger.info("Recording session started with final paste delivery enabled")

        # ---- Open a fresh diagnostic file for this recording ----
        diag.start_session()
        diag.section("LAYER 0: SESSION CONTEXT")
        diag.event("session", "recording started",
                   mic_device_id=self.config.microphone_device_id,
                   model=self.config.model,
                   la_cadence_s=self._la_cadence_sec,
                   vad_min_silence_ms=getattr(self.config.post_processing, "vad_min_silence_ms", 300),
                   vad_pre_roll_ms=getattr(self.config.post_processing, "vad_pre_roll_ms", 80),
                   itn_enabled=getattr(self.config.post_processing, "itn_enabled", True),
                   llm_enabled=getattr(self.config.post_processing, "llm_enabled", False))
        if self._active_text_context.compact():
            diag.note("session", f"UIAutomation context:\n{self._active_text_context.compact(800)}")

        # Drain any stale chunks from the queue
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                break

        # Recreate the capture object at session start so deferred microphone
        # changes apply cleanly to the next recording.
        self.audio = AudioCapture(device_id=self.config.microphone_device_id)
        active_mic = AudioCapture.describe_device(self.audio._device_id)

        # Start the worker thread BEFORE audio capture so it is ready
        # to consume chunks immediately.
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="pipeline-worker",
            daemon=True,
        )
        self._worker_thread.start()

        try:
            self.audio.start(self._on_audio_chunk)
            if active_mic is not None:
                IPCBridge.send_event(
                    "active_microphone",
                    id=active_mic.id,
                    name=active_mic.name,
                    host_api=active_mic.host_api,
                    from_default=self.config.microphone_device_id is None,
                )
            IPCBridge.send_event("recording_started")
            logger.info("Recording started")
        except Exception as exc:
            # Stop the worker if audio failed
            self._chunk_queue.put(_STOP_SENTINEL)
            logger.exception("Failed to start recording")
            IPCBridge.send_event("error", message=f"Failed to start recording: {exc}")

    def stop_recording(self) -> None:
        """Stop capture, finalize transcription, post-process, and inject."""
        if not self.audio.is_recording:
            IPCBridge.send_event("error", message="Not recording")
            return
        self._do_stop_recording()

    def _do_stop_recording(self) -> None:
        """Internal stop logic shared between manual stop and auto-stop.

        Segments that were already finalized during recording are still kept
        for live UX, but the *final clipboard text* is derived from the full
        captured session audio. This avoids losing earlier speech when VAD
        segmentation is imperfect and only the trailing utterance is flushed
        at stop time.
        """
        # 1. Stop audio capture first (returns full session audio)
        remaining_audio = self.audio.stop()
        logger.info("Recording stopped, %d samples remaining", len(remaining_audio))
        diag.event("session", "recording stopped",
                   total_samples=len(remaining_audio),
                   total_s=round(len(remaining_audio) / 16000.0, 2))

        # 2. Stop the worker thread (drain queue first)
        self._chunk_queue.put(_STOP_SENTINEL)
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
            self._worker_thread = None

        # 3. Flush any trailing speech from the VAD so the detector state can
        #    be cleared safely. Authoritative transcript assembly is handled by
        #    the full-session reconciliation pass on the captured audio.
        trailing_speech = self.vad.flush()
        # Force-commit any unconfirmed audio so the live preview transcript
        # reflects every word that was captured. The full-session reconcile
        # pass below remains the authoritative source for final delivery.
        self._force_commit_unconfirmed()

        # 4. Reset state
        self.vad.reset()
        self.asr.reset_stream()

        # 5. Build one raw transcript for the whole recording and run
        #    post-processing once at the very end.
        raw_full_text = self._build_final_raw_text(remaining_audio)

        full_text = ""
        if raw_full_text:
            full_text = self._format_final_text(raw_full_text)
            if not full_text:
                full_text = raw_full_text.strip()

        self._session_transcript = full_text

        logger.info(
            "Final text (%d raw parts, %d preview parts, %d chars): %s",
            len(self._session_raw_parts),
            len(self._session_text_parts),
            len(full_text),
            full_text[:200] if full_text else "(empty)",
        )

        # Low-confidence transcription warning: if audio was long but the
        # transcript is suspiciously short, the user likely wasn't heard well.
        # The warning is also attached to the `final_delivery` payload below
        # because the Rust shell currently has no match arm forwarding the
        # standalone `transcription_warning` event to the frontend.
        audio_seconds = len(remaining_audio) / 16_000.0
        transcript_chars = len(full_text)
        warning_payload: dict[str, Any] | None = None
        if audio_seconds > 4.0:
            ratio = transcript_chars / audio_seconds if audio_seconds > 0 else 0.0
            if ratio < 6.0:
                warning_payload = {
                    "code": "low_confidence",
                    "audio_seconds": round(audio_seconds, 2),
                    "transcript_chars": transcript_chars,
                    "ratio": round(ratio, 2),
                    "message": "I am sorry, I couldn't hear you well. Can you please repeat the speech completely?",
                }
                IPCBridge.send_event("transcription_warning", **warning_payload)
                logger.info(
                    "Low-confidence transcription: %.2fs audio -> %d chars (ratio %.2f)",
                    audio_seconds, transcript_chars, ratio,
                )

        delivery = FinalDeliveryResult(
            copied_to_clipboard=False,
            pasted_to_target=False,
            manual_paste_required=False,
            status="copy_failed",
            failure_reason="empty_text",
        )
        if full_text:
            try:
                delivery = self.injector.deliver_final_text(full_text)
                logger.info(
                    "Final delivery: status=%s copied=%s pasted=%s",
                    delivery.status,
                    delivery.copied_to_clipboard,
                    delivery.pasted_to_target,
                )
                diag.section("LAYER 4: TEXT INJECTION")
                diag.event("text-injector", "delivery",
                           status=delivery.status,
                           copied=delivery.copied_to_clipboard,
                           pasted=delivery.pasted_to_target,
                           manual=delivery.manual_paste_required,
                           chars=len(full_text))
            except Exception:
                logger.exception("Final delivery failed")
                delivery = FinalDeliveryResult(
                    copied_to_clipboard=False,
                    pasted_to_target=False,
                    manual_paste_required=False,
                    status="copy_failed",
                    failure_reason="delivery_failed",
                )
        else:
            logger.warning("No text to copy to clipboard")

        final_payload = self._delivery_payload(full_text, delivery)
        if warning_payload is not None:
            final_payload["transcription_warning"] = warning_payload
        IPCBridge.send_event("final_delivery", **final_payload)

        # 6. Explicit zeroing of audio buffers (HIPAA)
        if len(remaining_audio) > 0:
            remaining_audio[:] = 0
        if trailing_speech is not None and len(trailing_speech) > 0:
            trailing_speech[:] = 0

        self._session_raw_parts.clear()
        self._session_text_parts.clear()
        self._session_transcript = ""
        self._live_partial_text = ""
        self._live_partial_raw_text = ""
        self._clear_unconfirmed_audio()
        self._session_speech_detected = False
        self._authoritative_commit_failed = False
        self._active_text_context = None
        self.injector.clear_target()

        # Close the diagnostic log file (one per recording session)
        diag.end_session()

        # NOTE: We do NOT send "recording_stopped" here.  The Rust frontend
        # already emits "recording-stopped" on both manual stop and auto-stop
        # (via the auto_stop_triggered handler).  Sending it here — *after*
        # "text_injected" — would cause the frontend to transition from
        # "idle" back to "processing" and get permanently stuck.

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Apply partial settings update from the front-end."""
        # Normalize microphone field from Tauri ("microphone") to sidecar ("microphone_device_id")
        if "microphone" in settings:
            mic_val = settings.pop("microphone")
            if mic_val in (None, "default", ""):
                settings["microphone_device_id"] = None
            else:
                try:
                    settings["microphone_device_id"] = int(mic_val)
                except (ValueError, TypeError):
                    settings["microphone_device_id"] = None

        old_device_id = self.config.microphone_device_id
        self.config.update_from_dict(settings)

        rebuild_asr = "model" in settings or "num_threads" in settings

        # Rebuilding the config-derived components hot-swaps live pipeline
        # objects. Doing that mid-recording is unsafe: the worker thread is
        # calling self.vad.process_chunk / self.audio_normalizer right now, so
        # a swap throws away the VAD's in-flight speech buffer and high-pass
        # state, and an ASR rebuild forces a multi-second model reload in the
        # middle of an utterance. Defer to the next recording instead.
        if self.audio.is_recording:
            self._pending_rebuild = True
            self._pending_rebuild_asr = self._pending_rebuild_asr or rebuild_asr
            logger.info(
                "Settings update received mid-recording; deferring pipeline "
                "component rebuild until the next recording (asr=%s)",
                self._pending_rebuild_asr,
            )
        else:
            self._rebuild_runtime_components(rebuild_asr=rebuild_asr)

        diag.configure(
            enabled=getattr(self.config.post_processing, "diagnostic_logging_enabled", False),
            output_dir=getattr(self.config.post_processing, "diagnostic_log_dir", None),
        )

        # Rebuild AudioCapture if the device changed. Avoid hot-swapping while
        # recording because that would splice or drop the current session;
        # start_recording always rebuilds AudioCapture from config, so the
        # change still lands on the next recording.
        if self.config.microphone_device_id != old_device_id:
            if self.audio.is_recording:
                logger.info(
                    "Microphone change deferred until next recording (requested device_id=%s)",
                    self.config.microphone_device_id,
                )
            else:
                self.audio = AudioCapture(device_id=self.config.microphone_device_id)
                logger.info("AudioCapture rebuilt with device_id=%s", self.config.microphone_device_id)

        IPCBridge.send_event("settings_updated", settings=asdict(self.config))
        logger.info("Settings updated")

    def _rebuild_runtime_components(self, *, rebuild_asr: bool) -> None:
        """(Re)build the config-derived pipeline components.

        Called from update_settings when idle, or deferred to start_recording
        when a settings update arrives mid-recording. Rebuilding the ASR engine
        drops the warmed model, so it is gated behind *rebuild_asr* (only when
        the model or thread count actually changed).
        """
        self.post = PostProcessor(self.config.post_processing)
        self.vocab = VocabularyCorrector(
            getattr(self.config.post_processing, "custom_vocabulary_path", None)
        )
        self.name_casing = NameCasingCorrector(
            getattr(self.config.post_processing, "name_casing_index_path", None)
        )
        self.name_matcher = self._build_name_matcher()
        self.llm_formatter = LLMFormatter(self.config.post_processing)
        self.audio_normalizer = AudioNormalizer(self.config.post_processing)
        self.lm_rescorer = LMRescorer(self.config.post_processing)
        self.vad = VoiceActivityDetector(
            min_silence_ms=getattr(self.config.post_processing, "vad_min_silence_ms", 300),
            pre_roll_ms=getattr(self.config.post_processing, "vad_pre_roll_ms", 80),
        )
        if rebuild_asr:
            self.asr = ASREngine(
                model_name=self.config.model,
                num_threads=self.config.num_threads,
            )

    def list_devices(self) -> None:
        """Return the list of available microphone devices."""
        devices, recommended_id = AudioCapture.list_devices()
        device_list = [
            {
                "id": d.id,
                "name": d.name,
                "channels": d.channels,
                "sample_rate": d.sample_rate,
                "host_api": d.host_api,
                "activity_score": d.activity_score,
                "recommended": d.recommended,
            }
            for d in devices
        ]
        IPCBridge.send_event(
            "device_list",
            devices=device_list,
            recommended_device_id=recommended_id,
        )

    def shutdown(self) -> None:
        """Clean shutdown with buffer zeroing."""
        logger.info("Shutdown requested")

        if self.audio.is_recording:
            audio = self.audio.stop()
            if len(audio) > 0:
                audio[:] = 0

        # Stop worker if running
        self._chunk_queue.put(_STOP_SENTINEL)
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        self.vad.reset()
        self.asr.reset_stream()
        self._session_raw_parts.clear()
        self._session_text_parts.clear()
        self._session_transcript = ""
        self._live_partial_text = ""
        self._live_partial_raw_text = ""
        self._clear_unconfirmed_audio()
        self._session_speech_detected = False
        self._authoritative_commit_failed = False
        self._active_text_context = None
        self.injector.clear_target()

        IPCBridge.send_event("shutdown_complete")
        raise SystemExit(0)

    @staticmethod
    def _audio_amplitude_stats(audio: np.ndarray) -> tuple[int, float, float, float, float]:
        """Return (peak, rms, peak_dbfs, rms_dbfs, silent_fraction) for int16 audio."""
        if len(audio) == 0:
            return 0, 0.0, -120.0, -120.0, 1.0
        abs_audio = np.abs(audio).astype(np.float64)
        peak = int(np.max(abs_audio))
        rms = float(np.sqrt(np.mean(abs_audio * abs_audio)))
        peak_dbfs = 20.0 * np.log10(max(peak, 1) / 32768.0)
        rms_dbfs = 20.0 * np.log10(max(rms, 1.0) / 32768.0)
        silent_fraction = float(np.mean(abs_audio < 200.0))
        return peak, rms, peak_dbfs, rms_dbfs, silent_fraction

    def _log_session_audio_stats(self, audio: np.ndarray) -> None:
        if len(audio) == 0:
            logger.info("DIAG: empty session audio")
            return
        peak, rms, peak_dbfs, rms_dbfs, silent = self._audio_amplitude_stats(audio)
        duration_s = len(audio) / 16_000.0
        logger.info(
            "DIAG session-audio: dur=%.2fs samples=%d peak=%d (%.1f dBFS) "
            "rms=%.0f (%.1f dBFS) silent_frac=%.2f",
            duration_s, len(audio), peak, peak_dbfs, rms, rms_dbfs, silent,
        )

    @staticmethod
    def _boost_audio_if_quiet(audio: np.ndarray) -> tuple[np.ndarray, float]:
        """If the chunk peak is below ~-12 dBFS, scale it up toward -6 dBFS.

        Returns ``(possibly_boosted_audio, gain_applied)``. Parakeet's
        recognition rate falls off sharply on quiet audio; a software gain
        boost recovers most of the lost accuracy with negligible cost. We
        cap the boost at 4x to avoid amplifying background noise to the
        point where the model hallucinates on it.
        """
        if len(audio) == 0:
            return audio, 1.0
        peak = int(np.max(np.abs(audio)))
        if peak == 0:
            return audio, 1.0
        threshold_peak = 8200   # ~ -12 dBFS
        target_peak = 16384     # ~ -6 dBFS  (leaves 6 dB headroom)
        if peak >= threshold_peak:
            return audio, 1.0
        gain = min(target_peak / peak, 4.0)
        boosted = audio.astype(np.int32) * gain
        np.clip(boosted, -32768, 32767, out=boosted)
        return boosted.astype(np.int16), float(gain)

    @staticmethod
    def _trim_trailing_silence(chunk: np.ndarray) -> np.ndarray:
        """Trim consecutive silent 100 ms windows from the tail of *chunk*.

        A window is considered silent when its peak amplitude is below 200
        (int16 scale).  The chunk is never trimmed shorter than 1.0 s
        (16 000 samples).
        """
        window_samples = 1600      # 100 ms at 16 kHz
        min_samples = 16_000       # 1.0 s minimum
        end = len(chunk)
        while end - window_samples >= min_samples:
            window = chunk[end - window_samples:end]
            if np.max(np.abs(window)) < 200:
                end -= window_samples
            else:
                break
        return chunk[:end]

    def _reconcile_full_session(self, audio: np.ndarray) -> str:
        """Chunk long recordings into smaller decodes and return raw text.

        Chunks overlap by OVERLAP_SECONDS so that words straddling a hard
        boundary are not dropped or hallucinated.  Each chunk tail is trimmed
        of trailing silence before decoding to reduce Parakeet hallucinations.
        """
        if len(audio) <= 1600:
            return ""

        self._log_session_audio_stats(audio)

        # Apply the same rumble/DC high-pass the live preview path gets, but in
        # one zero-phase pass over the whole buffer. The raw session audio
        # otherwise reaches Parakeet completely unfiltered (see audio_normalizer
        # .filter_session). Done once here so every chunk below is already
        # cleaned before trimming / boosting / loudness-normalising.
        audio = self.audio_normalizer.filter_session(audio)

        diag.section("LAYER 1: FULL-SESSION RECONCILIATION (per chunk Parakeet decode)")
        diag.event("reconcile", "session audio",
                   total_s=round(len(audio) / 16000.0, 2),
                   samples=len(audio))

        chunk_seconds = float(getattr(self.config.post_processing, "reconcile_chunk_sec", 12.0))
        overlap_seconds = float(getattr(self.config.post_processing, "reconcile_overlap_sec", 4.0))
        chunk_samples = int(16_000 * chunk_seconds)
        stride_samples = int(16_000 * max(0.5, chunk_seconds - overlap_seconds))
        parts: list[str] = []
        # Per-word confidence stream across all appended chunks (for the
        # confidence-gated scoped LLM stage). Reset each reconcile.
        stream_words: list[str] = []
        stream_confs: list[float] = []
        self._last_word_confidences = None

        idx = 0
        start = 0
        while start < len(audio):
            end = min(len(audio), start + chunk_samples)
            chunk = audio[start:end]
            if len(chunk) <= 1600:
                if end >= len(audio):
                    break
                start += stride_samples
                idx += 1
                continue

            # Fix 3: trim trailing silence before decoding
            trimmed = self._trim_trailing_silence(chunk)
            trim_samples = len(chunk) - len(trimmed)
            if trim_samples > 0:
                logger.info(
                    "DIAG trim-tail: chunk %d trimmed %.2fs of trailing silence",
                    idx + 1, trim_samples / 16_000.0,
                )

            chunk_dur = len(trimmed) / 16_000.0
            peak, _rms, peak_dbfs, rms_dbfs, silent = self._audio_amplitude_stats(trimmed)
            # Loudness-normalise first, THEN boost any still-quiet chunk.
            # normalize_loudness targets -23 LUFS and caps gain at +6 dB, so
            # genuinely quiet speech stays under-amplified and Parakeet's
            # accuracy drops. Running the boost AFTER normalisation lifts the
            # peak toward -6 dBFS (up to 4x) with nothing downstream to undo it
            # — boosting BEFORE would just get pulled back down by the -23 LUFS
            # target. On healthy-level audio the boost is a no-op.
            normalized = self.audio_normalizer.normalize_loudness(trimmed)
            normalized, boost_gain = self._boost_audio_if_quiet(normalized)
            if boost_gain != 1.0:
                logger.info("DIAG boost: chunk %d gained %.2fx (quiet audio)", idx + 1, boost_gain)
            norm_peak = int(np.max(np.abs(normalized))) if normalized.size else 0
            try:
                _t0 = time.perf_counter()
                # Prefer the confidence-bearing path; fall back to plain decode
                # for engines/stubs that don't implement it (keeps tests and any
                # alternate backend working — they just get no word confidence).
                if hasattr(self.asr, "transcribe_segment_with_words"):
                    raw_text, chunk_words = self.asr.transcribe_segment_with_words(normalized)
                else:
                    raw_text = self.asr.transcribe_segment(normalized).text
                    chunk_words = []
                chunk_latency_ms = (time.perf_counter() - _t0) * 1000.0
                result = TranscriptionResult(
                    text=raw_text, confidence=None,
                    latency_ms=round(chunk_latency_ms, 1),
                    audio_duration_ms=round(chunk_dur * 1000.0, 1),
                )
                raw_text = raw_text.strip()
                word_count = len(raw_text.split()) if raw_text else 0
                logger.info(
                    "DIAG reconcile-chunk %d: dur=%.1fs peak=%d (%.1f dBFS) "
                    "rms_dbfs=%.1f silent_frac=%.2f -> words=%d text=%r",
                    idx + 1, chunk_dur, peak, peak_dbfs,
                    rms_dbfs, silent, word_count, raw_text[:120],
                )
                diag.chunk("reconcile", "chunk", raw_text,
                           offset_s=round(start / 16000.0, 2),
                           dur=round(chunk_dur, 2),
                           peak_in=peak, peak_dbfs=round(peak_dbfs, 1),
                           rms_dbfs=round(rms_dbfs, 1),
                           silent_frac=round(silent, 2),
                           peak_post_norm=norm_peak,
                           words=word_count,
                           latency_ms=int(result.latency_ms or 0))
                if raw_text:
                    parts.append(raw_text)
                    for wc in chunk_words:
                        stream_words.append(wc.word)
                        stream_confs.append(wc.confidence)
            except Exception:
                logger.exception("Error transcribing chunk %d", idx + 1)

            if end >= len(audio):
                break
            start += stride_samples
            idx += 1

        if not parts:
            return ""

        diag.section("LAYER 2: CHUNK DEDUP / MERGE (overlap removal)")

        # Fold parts together with LCS-based overlap deduplication so that
        # words appearing in both the tail of part N and the head of part N+1
        # (an artifact of the 1.5 s overlap between chunk windows) are not
        # duplicated in the final transcript.
        merged = parts[0].strip()
        diag.event("dedup", "chunk #1 (no prior)", text=merged[:80])
        # Seam N-best: when enabled and an LM is available, let the LM pick
        # between the two chunks' transcriptions of the overlapped audio.
        use_seam = getattr(self.config.post_processing, "seam_nbest_enabled", True)
        seam_margin = float(getattr(self.config.post_processing, "seam_nbest_margin", 1.0))
        seam_score_fn = self.lm_rescorer.score_text if use_seam else None
        for i, nxt in enumerate(parts[1:], start=2):
            deduped = self._dedupe_overlap(merged, nxt)
            if not deduped:
                diag.event("dedup", f"chunk #{i} fully consumed by overlap (dropped)",
                           original=nxt[:60])
                continue
            if deduped != nxt:
                diag.event("dedup", f"chunk #{i} trimmed",
                           prev_tail=merged[-60:],
                           next_head_in=nxt[:60],
                           next_head_out=deduped[:60])
            else:
                diag.event("dedup", f"chunk #{i} appended unchanged",
                           text=deduped[:60])
            base_merged = (merged + " " + deduped).strip() if merged else deduped
            if seam_score_fn is not None:
                merged = seam_merge(merged, nxt, self._dedupe_overlap, seam_score_fn,
                                    margin=seam_margin, trimmed_next=deduped)
                if merged != base_merged:
                    diag.event("seam-nbest",
                               f"chunk #{i}: LM preferred next chunk's seam words",
                               tail=merged[-80:])
            else:
                merged = base_merged
        diag.note("dedup", f"final merged text ({len(merged)} chars):\n{merged}")

        # Align the per-word confidence stream onto the final merged words so the
        # scoped LLM stage knows which words to trust. Keyed match tolerates the
        # overlap-duplicates dedup removed above.
        merged = merged.strip()
        if stream_words:
            self._last_word_confidences = align_confidences(
                merged.split(), stream_words, stream_confs
            )
        return merged


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def _make_handler(pipeline: Pipeline):
    """Create the IPC command handler closure."""

    def handler(cmd: dict[str, Any]) -> None:
        command = cmd.get("command", cmd.get("cmd", ""))
        logger.info("Received command: %s", command)

        match command:
            case "start_recording":
                pipeline.start_recording()
            case "stop_recording":
                pipeline.stop_recording()
            case "update_settings":
                pipeline.update_settings(cmd.get("settings", {}))
            case "list_devices" | "list_microphones":
                pipeline.list_devices()
            case "shutdown":
                pipeline.shutdown()
            case _:
                IPCBridge.send_event("error", message=f"Unknown command: {command}")

    return handler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_setup() -> int:
    """Download missing models, streaming one JSON event per line to stdout.

    Must run ONLINE — explicitly clears the offline guards set at import time.
    """
    import os as _os
    _os.environ["HF_HUB_OFFLINE"] = "0"
    _os.environ["TRANSFORMERS_OFFLINE"] = "0"
    import json as _json
    import model_setup

    def progress(ev: dict) -> None:
        sys.stdout.write(_json.dumps(ev) + "\n")
        sys.stdout.flush()

    try:
        model_setup.download_all(progress)
        return 0
    except Exception as exc:  # surface a structured failure to the UI
        sys.stdout.write(_json.dumps({"event": "setup_error", "message": str(exc)}) + "\n")
        sys.stdout.flush()
        return 1


def main() -> None:
    """Sidecar entry point."""
    if "--setup" in sys.argv:
        sys.exit(_run_setup())

    logger.info("VoiceFlow sidecar starting")

    try:
        pipeline = Pipeline()

        # Pre-load models so the first recording is instant
        pipeline.warmup()

        handler = _make_handler(pipeline)
        IPCBridge.send_event("ready")
        IPCBridge.run(handler)
    except SystemExit:
        pass
    except Exception:
        logger.exception("Fatal error in sidecar")
        IPCBridge.send_event("error", message="Fatal sidecar error")
        sys.exit(1)
    finally:
        logger.info("VoiceFlow sidecar exiting")


if __name__ == "__main__":
    main()
