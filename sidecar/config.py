"""
VoiceFlow Sidecar - Configuration dataclasses.

All configuration is held in memory only. No values are persisted to disk
by the sidecar itself (HIPAA compliance: zero disk writes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PostProcessConfig:
    """Toggles for each post-processing pipeline stage."""

    # Filler removal would drop meaningful adverbs like "actually", "just",
    # "then" — those carry intent in dictation. Leave to the LLM.
    remove_fillers: bool = False
    # Strip ONLY non-lexical fillers (um, uh, er, hmm) — these never carry
    # meaning, unlike the words remove_fillers also drops (actually/just/like).
    # Safe to keep on; reduces insertions from disfluencies the ASR transcribes.
    remove_nonlexical_fillers: bool = True
    collapse_false_starts: bool = True
    remove_repetitions: bool = True
    # Phonetic-correction collapse is rule-based and tends to over-trigger
    # on speakers with accents. LLM handles this with context.
    collapse_phonetic_corrections: bool = False
    fix_punctuation: bool = True
    # Rule-based number formatter butchers spoken currency / times
    # ("point six zero dollars" → "point 6 $0"). LLM produces correct
    # numbers when given the full context.
    format_numbers: bool = False
    apply_custom_dictionary: bool = False
    custom_dictionary_path: Optional[str] = None
    llm_enabled: bool = True
    # When True, the LLM only corrects low-confidence words (span-scoped) instead
    # of rewriting the whole transcript. Words at/above the threshold are frozen.
    # Default False: scoped mode cannot do multi-word/camelCase fixes and a 3B
    # model just repeats OOV words (measured: 107 flagged, 0 applied). Whole-text
    # mode + the diff guardrail below is the safer validator.
    llm_scoped_correction: bool = False
    llm_confidence_threshold: float = 0.80
    # Programmatic safety net for the whole-text LLM. NEVER trust a 3B model
    # blindly: if its output grows too much (hallucination/insertion) or drops
    # too much content (truncation/paraphrase), reject it and fall back to the
    # raw transcript. Number/currency words are exempted so legitimate spoken
    # number -> digit shrink does not trip the retention check.
    llm_guardrail_enabled: bool = True
    llm_guardrail_max_growth: float = 0.15      # reject if len grows > +15%
    llm_guardrail_min_retention: float = 0.85   # reject if < 85% content words kept
    # Relaxed retention floor used for the finetuned/minimal-prompt corrector. That
    # model legitimately MERGES word-boundary splits ("data base"->"database",
    # "kuber netes"->"Kubernetes"), which drops content-word retention and would
    # trip the strict 0.85 floor — discarding its best fixes. The growth cap still
    # guards against hallucination/insertion. (Measured: strict floor caps in-app
    # WER at 13.0%; relaxed floor recovers the model's native 3.6%.)
    llm_guardrail_min_retention_minimal: float = 0.50
    # Finetuned ASR-correction model (LoRA SFT on ~3.3k correction pairs, merged + Q4_K_M).
    # Original stock model kept at models/llama-3.2-3b-q4_k_m/ — revert this line to switch back.
    llm_model_path: Optional[str] = "models/llama-3.2-3b-finetuned-q4_k_m/llama-3.2-3b-finetuned.Q4_K_M.gguf"
    # The finetuned corrector was SFT'd on a BARE transcript->corrected format with
    # no verbose system prompt. Feeding it the heavy few-shot prompt below pulls it
    # back toward generic instruct behavior and erases the finetuning gains
    # (measured: in-app WER 14.4% with full prompt vs 3.6% with the native prompt).
    # When None, auto-enabled if the model path looks finetuned ("finetuned"/"corrector").
    llm_minimal_prompt: Optional[bool] = None
    llm_context_enabled: bool = True
    llm_max_input_chars: int = 6000
    llm_max_output_tokens: int = 1024
    llm_temperature: float = 0.0
    llm_timeout_sec: int = 60
    itn_enabled: bool = False  # full ITN disabled — times rule over-triggers ("two three"→"2:03")
    # Narrow, safe number pass (spoken years + explicit thousands only). Independent
    # of the full ITN above; on by default because it does not over-trigger.
    itn_numbers_enabled: bool = True
    custom_vocabulary_path: Optional[str] = None
    # Safe name capitalization: fixes ONLY the casing of correctly-spelled name
    # tokens (e.g. "srinivasan" -> "Srinivasan") from a large bundled index,
    # excluding common English words. Never alters letters; O(1) per token.
    name_casing_enabled: bool = True
    name_casing_index_path: Optional[str] = None  # None → bundled name_casing_index.txt
    # Confidence-gated fuzzy name REPLACEMENT: when the ASR is unsure about a
    # name-like word that closely matches a real name, replace it with the
    # closest match ("Gianluk" -> "Gianluca"). Runs early, while per-word
    # confidence still aligns. Safe gates: low confidence + not a common word +
    # not app jargon + high similarity. See name_matcher.py.
    name_match_enabled: bool = True
    name_match_threshold: float = 0.92            # Jaro-Winkler similarity (0..1)
    name_match_confidence_threshold: float = 0.80  # only words below this are candidates
    name_match_capitalized_fallback: bool = False  # risky without confidence; off by default
    # Audio normalizer
    audio_normalizer_enabled: bool = True
    audio_highpass_cutoff_hz: int = 80
    # LM rescorer
    lm_rescorer_enabled: bool = True
    lm_model_path: str = "models/lm/3gram-pruned.arpa"
    # Seam N-best: at chunk overlaps, LM-pick between the two transcriptions of
    # the same audio instead of always keeping the previous chunk's version.
    seam_nbest_enabled: bool = True
    seam_nbest_margin: float = 1.0
    # Full-session reconcile chunking. Parakeet under-decodes a chunk's tail when
    # there is mid-chunk applause/silence; that ~2-3s tail-drop must be smaller
    # than the overlap or whole clauses fall into the seam hole and are lost.
    # Overlap raised from 1.5s -> 4.0s so every clause lands mid-window in some
    # chunk. (Denser grid = more decodes, acceptable for end-of-recording.)
    reconcile_chunk_sec: float = 12.0
    reconcile_overlap_sec: float = 4.0
    # VAD tuning (moved from hardcoded vad.py)
    vad_pre_roll_ms: int = 80
    vad_min_silence_ms: int = 300
    # LLM rolling context
    llm_rolling_context_sentences: int = 3
    # Diagnostic logging (writes one .txt file per session to ./diagnostics/).
    # OFF by default to uphold the zero-disk-writes / HIPAA-conscious guarantee —
    # transcripts must never be persisted in production. Flip on only for local
    # debugging on non-sensitive audio.
    diagnostic_logging_enabled: bool = False
    diagnostic_log_dir: Optional[str] = None  # None → ./diagnostics/ next to sidecar


@dataclass
class AppConfig:
    """Top-level application configuration passed from the Tauri front-end."""

    hotkey: str = "ctrl+shift+space"
    recording_mode: str = "push_to_talk"  # "push_to_talk" | "toggle"
    microphone_device_id: Optional[int] = None
    post_processing: PostProcessConfig = field(default_factory=PostProcessConfig)
    model: str = "istupakov/parakeet-tdt-0.6b-v3-onnx"
    num_threads: int = 4

    def update_from_dict(self, data: dict) -> None:
        """Merge a partial settings dictionary into this config."""
        for key, value in data.items():
            if key == "post_processing" and isinstance(value, dict):
                for pp_key, pp_val in value.items():
                    if hasattr(self.post_processing, pp_key):
                        setattr(self.post_processing, pp_key, pp_val)
            elif key in {"injection_method", "output_mode"}:
                # Legacy delivery-mode keys are intentionally ignored.
                continue
            elif hasattr(self, key):
                setattr(self, key, value)
