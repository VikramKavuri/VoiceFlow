"""Optional local LLM transcript formatter backed by llama-cpp-python."""

from __future__ import annotations

import logging
import re
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional, TYPE_CHECKING

from model_paths import runtime_root

from context_capture import ActiveTextContext
from confidence_gate import (
    select_low_confidence,
    format_marked_transcript,
    parse_scoped_reply,
    apply_scoped_corrections,
)
from diagnostic_logger import diag

if TYPE_CHECKING:
    from config import PostProcessConfig

logger = logging.getLogger(__name__)

# Strips Qwen3-style <think>...</think> reasoning blocks (including any
# stray openings/closings) plus a couple of common prefixes the model
# sometimes emits despite being told not to. Multiline DOTALL.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_STRAY_TAG_RE = re.compile(r"</?think>\s*", re.IGNORECASE)
_PREFIX_RES = [
    re.compile(r"^\s*(?:here(?:'s| is)?\s+(?:the\s+)?(?:corrected|cleaned)\s*(?:transcript)?\s*[:\-]\s*)", re.IGNORECASE),
    re.compile(r"^\s*(?:corrected\s+transcript\s*[:\-]\s*)", re.IGNORECASE),
    re.compile(r"^\s*output\s*[:\-]\s*", re.IGNORECASE),
]


# Words allowed to disappear from the output without counting as content loss:
# spoken numbers (the LLM/ITN legitimately turns them into digits) and the
# currency/percent words that go with them.
_GUARDRAIL_EXEMPT: frozenset[str] = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion", "point", "dollars", "dollar", "cents",
    "cent", "percent", "oh",
})

_WORD_RE = re.compile(r"[a-z0-9']+")


def _strip_llm_artifacts(text: str) -> str:
    """Remove <think> blocks and common LLM preamble phrases."""
    if not text:
        return text
    # Drop full <think>...</think> blocks first
    text = _THINK_BLOCK_RE.sub("", text)
    # Drop any orphaned <think>/</think> tags left behind
    text = _STRAY_TAG_RE.sub("", text)
    # Strip preamble phrases the model sometimes prepends
    for pat in _PREFIX_RES:
        text = pat.sub("", text, count=1)
    return text.strip()


class LLMFormatter:
    """Small local LLM formatter with a strict no-rewrite prompt.

    The stage is optional. Missing dependency/model path disables it without
    affecting the ASR pipeline.
    """

    def __init__(self, config: "PostProcessConfig | Any") -> None:
        self._config = config
        self._llm = None
        self._load_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(
            getattr(self._config, "llm_enabled", False)
            and getattr(self._config, "llm_model_path", None)
        )

    def format_text(
        self,
        text: str,
        context: ActiveTextContext | None = None,
        session_context: list[str] | None = None,
        vocab_terms: list[str] | None = None,
    ) -> str:
        if not text or not text.strip() or not self.enabled:
            return text

        llm = self._ensure_model()
        if llm is None:
            return self._format_with_llama_cli(text, context, session_context, vocab_terms)

        prompt = self._build_prompt(text, context, session_context, vocab_terms)
        diag.note("llm-formatter", f"prompt ({len(prompt)} chars):\n{prompt}")
        try:
            response = llm(
                prompt,
                max_tokens=getattr(self._config, "llm_max_output_tokens", 2048),
                temperature=getattr(self._config, "llm_temperature", 0.0),
                stop=["<|im_end|>", "</s>"],
            )
            raw = self._extract_response_text(response)
            diag.note("llm-formatter", f"raw response ({len(raw)} chars):\n{raw}")
            cleaned = _strip_llm_artifacts(raw)
            if cleaned != raw.strip():
                diag.event("llm-formatter", "stripped artifacts (think/preamble)",
                           raw_chars=len(raw), clean_chars=len(cleaned))
            passed, reason = self._passes_guardrail(text, cleaned)
            if not passed:
                diag.event("llm-formatter", f"guardrail REJECTED rewrite ({reason}); using raw text")
                return text
            return cleaned or text
        except Exception:
            logger.exception("LLM formatting failed; falling back to rule-based text")
            diag.event("llm-formatter", "EXCEPTION — falling back to rule-based text")
            return text

    def format_scoped(
        self,
        text: str,
        word_confidences: list[float] | None,
        context: ActiveTextContext | None = None,
        session_context: list[str] | None = None,
        vocab_terms: list[str] | None = None,
    ) -> str:
        """Confidence-gated, span-scoped correction.

        Only words below the confidence threshold are sent to the LLM, and the
        reply is applied to ONLY those word positions (enforced in code via
        apply_scoped_corrections). High-confidence words are frozen, so the LLM
        cannot rewrite the parts the model was already sure about — the failure
        mode that made the whole-text rewrite hurt WER. If confidences are
        unavailable/misaligned, or nothing is low-confidence, the text is
        returned unchanged with no LLM call (a latency win on clean speech).
        """
        if not text or not text.strip() or not self.enabled:
            return text

        words = text.split()
        if not word_confidences or len(word_confidences) != len(words):
            diag.event("llm-scoped", "skipped (no aligned confidence)",
                       words=len(words),
                       confs=len(word_confidences) if word_confidences else 0)
            return text

        threshold = float(getattr(self._config, "llm_confidence_threshold", 0.80))
        flagged = select_low_confidence(word_confidences, threshold)
        if not flagged:
            diag.event("llm-scoped", "skipped (all words confident)",
                       words=len(words), threshold=threshold)
            return text

        marked = format_marked_transcript(words, flagged)
        prompt = self._build_scoped_prompt(marked, len(flagged), context,
                                           session_context, vocab_terms)
        # Reply is one short "N: word" line per flagged word; budget ~8 tokens
        # each (+headroom) so a long flagged list isn't truncated, capped to keep
        # latency bounded.
        max_tokens = min(1024, 64 + len(flagged) * 8)
        diag.event("llm-scoped", "correcting low-confidence words",
                   flagged=len(flagged), words=len(words),
                   threshold=threshold, max_tokens=max_tokens)
        raw = self._complete_prompt(prompt, max_tokens=max_tokens)
        if not raw:
            diag.event("llm-scoped", "no LLM output; text unchanged")
            return text

        corrections = parse_scoped_reply(_strip_llm_artifacts(raw), flagged)
        out = apply_scoped_corrections(words, corrections)
        diag.diff("llm-scoped", text, out, label=f"({len(corrections)} applied)")
        return out or text

    def _passes_guardrail(self, original: str, corrected: str) -> tuple[bool, str]:
        """Programmatic safety net: should we trust the LLM's rewrite?

        Returns (passed, reason). A 3B model is never trusted blindly:
          * empty output                      -> reject
          * grew more than max_growth         -> reject (hallucination/insertion)
          * kept < min_retention of the input's distinct content words
            (number/currency words exempted)  -> reject (truncation/paraphrase)

        When it returns False the caller falls back to the raw transcript.
        """
        if not getattr(self._config, "llm_guardrail_enabled", True):
            return True, "guardrail disabled"
        corrected = corrected or ""
        if not corrected.strip():
            return False, "empty output"

        orig_len = max(1, len(original))
        max_growth = float(getattr(self._config, "llm_guardrail_max_growth", 0.15))
        if len(corrected) > orig_len * (1.0 + max_growth):
            grew = (len(corrected) - orig_len) / orig_len
            return False, f"grew {grew:+.0%} (> {max_growth:+.0%})"

        if self._use_minimal_prompt():
            # Reliable finetuned corrector: allow legitimate word-boundary merges
            # (which lower retention) while the growth cap above still guards
            # against hallucination/insertion.
            min_ret = float(getattr(self._config, "llm_guardrail_min_retention_minimal", 0.50))
        else:
            min_ret = float(getattr(self._config, "llm_guardrail_min_retention", 0.85))
        orig_content = {
            w for w in _WORD_RE.findall(original.lower())
            if len(w) >= 4 and w not in _GUARDRAIL_EXEMPT
        }
        if orig_content:
            out_words = set(_WORD_RE.findall(corrected.lower()))
            kept = sum(1 for w in orig_content if w in out_words)
            ratio = kept / len(orig_content)
            if ratio < min_ret:
                return False, f"retention {ratio:.0%} (< {min_ret:.0%})"
        return True, "ok"

    def _complete_prompt(self, prompt: str, max_tokens: int = 256) -> Optional[str]:
        """Run a raw prompt through the LLM (in-process, else llama-cli)."""
        llm = self._ensure_model()
        if llm is not None:
            try:
                response = llm(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=getattr(self._config, "llm_temperature", 0.0),
                    stop=["<|im_end|>", "</s>", "<|eot_id|>"],
                )
                return self._extract_response_text(response)
            except Exception:
                logger.exception("scoped in-process LLM call failed")
                return None

        llama_cli = self._find_llama_cli()
        if not llama_cli:
            return None
        model_path = self._resolve_model_path(str(getattr(self._config, "llm_model_path", "") or ""))
        if not model_path.exists():
            return None
        command = [
            llama_cli, "-m", str(model_path), "-p", prompt,
            "-n", str(max_tokens),
            "--temp", str(getattr(self._config, "llm_temperature", 0.0)),
            "--no-display-prompt",
        ]
        try:
            completed = subprocess.run(
                command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                timeout=getattr(self._config, "llm_timeout_sec", 60),
            )
            if completed.returncode != 0:
                logger.warning("scoped llama-cli failed: %s", completed.stderr[-500:])
                return None
            return completed.stdout
        except Exception:
            logger.exception("scoped llama-cli call failed")
            return None

    def _build_scoped_prompt(
        self,
        marked_transcript: str,
        n_flagged: int,
        context: ActiveTextContext | None,
        session_context: list[str] | None,
        vocab_terms: list[str] | None,
    ) -> str:
        system = (
            "You fix individual misheard words in a speech-to-text transcript. "
            "Only the words tagged [n] may be wrong. For EACH tagged word, decide "
            "the correct single word from context (homophones, proper nouns, "
            "obvious mis-hearings). Use the document/vocabulary only to pick the "
            "right spelling of a word the speaker clearly said — never to swap in "
            "an unrelated term.\n\n"
            "OUTPUT: one line per tag, exactly 'N: word' (the corrected single "
            "word). If a tagged word is already correct, repeat it. Output ONLY "
            "these lines — no other text, no <think>."
        )

        user_parts: list[str] = []
        if vocab_terms:
            user_parts.append("[VOCABULARY]\n" + ", ".join(vocab_terms[:50]))
        if getattr(self._config, "llm_context_enabled", True) and context is not None:
            ctx = context.compact()
            if ctx:
                user_parts.append("[DOCUMENT CONTEXT]\n" + ctx)
        if session_context:
            joined = " ".join(session_context[-3:])[:400]
            if joined:
                user_parts.append("[RECENT TRANSCRIPT]\n" + joined)
        user_parts.append(
            f"[TRANSCRIPT — fix the {n_flagged} tagged word(s)]\n{marked_transcript}"
        )

        model_path_str = str(getattr(self._config, "llm_model_path", "") or "").lower()
        is_llama3 = "llama-3" in model_path_str or "llama3" in model_path_str
        is_qwen3 = "qwen3" in model_path_str
        if is_qwen3:
            user_parts.append("/no_think")
        user = "\n\n".join(user_parts)

        if is_llama3:
            return (
                "<|start_header_id|>system<|end_header_id|>\n\n"
                f"{system}<|eot_id|>"
                "<|start_header_id|>user<|end_header_id|>\n\n"
                f"{user}<|eot_id|>"
                "<|start_header_id|>assistant<|end_header_id|>\n\n"
            )
        suffix = "<think>\n\n</think>\n\n" if is_qwen3 else ""
        return (
            "<|im_start|>system\n"
            f"{system}<|im_end|>\n"
            "<|im_start|>user\n"
            f"{user}<|im_end|>\n"
            "<|im_start|>assistant\n"
            f"{suffix}"
        )

    def _ensure_model(self):
        if self._llm is not None:
            return self._llm
        if self._load_error:
            return None

        model_path = self._resolve_model_path(str(getattr(self._config, "llm_model_path", "") or ""))
        if not model_path.exists():
            self._load_error = f"LLM model not found: {model_path}"
            logger.warning(self._load_error)
            return None

        try:
            from llama_cpp import Llama  # type: ignore
        except Exception:
            self._load_error = "llama-cpp-python is not installed"
            if self._find_llama_cli():
                logger.info("%s; using llama-cli fallback", self._load_error)
            else:
                logger.warning("%s; local LLM formatting disabled", self._load_error)
            return None

        try:
            self._llm = Llama(
                model_path=str(model_path),
                n_ctx=4096,
                n_threads=4,
                verbose=False,
            )
            logger.info("Loaded local LLM formatter model: %s", model_path)
        except Exception as exc:
            self._load_error = str(exc)
            logger.exception("Failed to load local LLM formatter")
            return None
        return self._llm

    def _format_with_llama_cli(
        self,
        text: str,
        context: ActiveTextContext | None,
        session_context: list[str] | None = None,
        vocab_terms: list[str] | None = None,
    ) -> str:
        llama_cli = self._find_llama_cli()
        if not llama_cli:
            return text

        model_path = self._resolve_model_path(str(getattr(self._config, "llm_model_path", "") or ""))
        if not model_path.exists():
            return text

        prompt = self._build_prompt(text, context, session_context, vocab_terms)
        command = [
            llama_cli,
            "-m",
            str(model_path),
            "-p",
            prompt,
            "-n",
            str(getattr(self._config, "llm_max_output_tokens", 1024)),
            "--temp",
            str(getattr(self._config, "llm_temperature", 0.0)),
            "--no-display-prompt",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=getattr(self._config, "llm_timeout_sec", 60),
            )
            if completed.returncode != 0:
                logger.warning("llama-cli formatter failed: %s", completed.stderr[-1000:])
                return text
            cleaned = _strip_llm_artifacts(completed.stdout)
            passed, reason = self._passes_guardrail(text, cleaned)
            if not passed:
                logger.warning("llama-cli guardrail rejected rewrite (%s); using raw", reason)
                return text
            return cleaned or text
        except Exception:
            logger.exception("llama-cli formatting failed; falling back to rule-based text")
            return text

    @staticmethod
    def _find_llama_cli() -> str | None:
        found = shutil.which("llama-cli")
        if found:
            return found

        local_appdata = Path.home() / "AppData" / "Local"
        winget_root = local_appdata / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            matches = list(winget_root.rglob("llama-cli.exe"))
            if matches:
                return str(matches[0])
        return None

    @staticmethod
    def _runtime_root() -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return Path(__file__).resolve().parent

    @classmethod
    def _resolve_model_path(cls, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path

        candidates = [
            Path.cwd() / path,
            runtime_root() / path,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return candidates[-1].resolve()

    # Short instruction matching the finetuned corrector's training distribution.
    _MINIMAL_SYSTEM = (
        "Correct the speech-to-text transcript. Fix homophones, word boundaries, "
        "punctuation, casing, and spoken numbers. Output only the corrected transcript."
    )

    def _use_minimal_prompt(self) -> bool:
        flag = getattr(self._config, "llm_minimal_prompt", None)
        if flag is not None:
            return bool(flag)
        # Auto-detect a finetuned corrector from the model path.
        path = str(getattr(self._config, "llm_model_path", "") or "").lower()
        return "finetuned" in path or "corrector" in path

    def _build_minimal_prompt(self, trimmed: str) -> str:
        model_path_str = str(getattr(self._config, "llm_model_path", "") or "").lower()
        is_qwen3 = "qwen3" in model_path_str
        is_llama3 = "llama-3" in model_path_str or "llama3" in model_path_str or not is_qwen3
        if is_llama3:
            return (
                "<|start_header_id|>system<|end_header_id|>\n\n"
                f"{self._MINIMAL_SYSTEM}<|eot_id|>"
                "<|start_header_id|>user<|end_header_id|>\n\n"
                f"{trimmed}<|eot_id|>"
                "<|start_header_id|>assistant<|end_header_id|>\n\n"
            )
        suffix = "<think>\n\n</think>\n\n" if is_qwen3 else ""
        user = f"{trimmed}\n\n/no_think" if is_qwen3 else trimmed
        return (
            "<|im_start|>system\n"
            f"{self._MINIMAL_SYSTEM}<|im_end|>\n"
            "<|im_start|>user\n"
            f"{user}<|im_end|>\n"
            "<|im_start|>assistant\n"
            f"{suffix}"
        )

    def _build_prompt(
        self,
        text: str,
        context: ActiveTextContext | None,
        session_context: list[str] | None = None,
        vocab_terms: list[str] | None = None,
    ) -> str:
        trimmed = text[: getattr(self._config, "llm_max_input_chars", 6000)]

        # Minimal/native prompt path for the finetuned corrector. It was SFT'd on
        # bare transcript->corrected pairs; the verbose few-shot prompt below
        # suppresses its finetuning. Send just a short instruction + the
        # transcript in the model's chat template (measured: 14.4% -> 3.6% WER).
        if self._use_minimal_prompt():
            return self._build_minimal_prompt(trimmed)

        system = (
            "You clean up speech-to-text transcripts. Fix obvious ASR errors. "
            "Keep the speaker's wording and meaning intact.\n\n"
            "WHAT TO FIX (be confident and decisive):\n"
            "• Homophones using context: their/there, to/too/two, week/weak, launch/lawn.\n"
            "• Proper noun casing: VoiceFlow, Sarah, GitHub, Q3.\n"
            "• Punctuation and sentence boundaries.\n"
            "• Convert spoken numbers/dates/times/currency/emails into written form.\n"
            "• Remove obvious ASR duplications at chunk seams (e.g. \"X. X also...\" → \"X also...\").\n"
            "• Fix obvious single-word substitutions: 'they won' → 'they want', "
            "'appeared to the case' (mid-sentence garbage) → delete it.\n\n"
            "WHAT TO LEAVE ALONE (hard rules):\n"
            "• Word choice and word order — never paraphrase or 'improve' grammar "
            "  when the meaning is already clear.\n"
            "• NEVER summarize, compress, shorten, or truncate. Output every clause.\n"
            "• NEVER add new words, content, commentary, intros, or closing remarks.\n"
            "• If you cannot confidently identify a misheard word, LEAVE IT EXACTLY "
            "  AS-IS. Never insert your guess next to the original (writing both is "
            "  WORSE than leaving it). Only replace a word in place — never duplicate.\n"
            "• Keep the speaker's tone, dialect and voice.\n"
            "• Don't drop filler that carries meaning (\"actually\", \"just\", \"then\", \"also\").\n"
            "• Don't replace 'I was going to' with 'I'm going to'.\n"
            "• Do NOT replace project / product / company names that the speaker used "
            "  with different names from the vocabulary or context blocks. If the "
            "  speaker said 'project Atlas', keep 'Atlas' — even if the vocabulary "
            "  contains other project names like 'VoiceFlow'. Same for domain names "
            "  (example.com, github.com — keep what the speaker said).\n\n"
            "EXAMPLES (input → output):\n"
            "1. 'meeting at three thirty PM on April twenty second'\n"
            "   → 'meeting at 3:30 PM on April 22nd'\n"
            "2. 'invoice for twelve fifty point seven five dollars'\n"
            "   → 'invoice for $1,250.75'\n"
            "3. 'send it to john at example dot com'\n"
            "   → 'send it to john@example.com'\n"
            "4. 'the Q three numbers for twenty twenty four'\n"
            "   → 'the Q3 numbers for 2024'\n"
            "5. 'the client appeared to the case. Approved the revised invoice'\n"
            "   → 'the client approved the revised invoice'\n"
            "6. 'delay the lawn but actually I think we can move forward'\n"
            "   → 'delay the launch, but actually I think we can move forward'\n"
            "7. 'add Maya Patel, Chris Wong and Alina Rodrigue to the follow up list'\n"
            "   → 'add Maya Patel, Chris Wong and Alina Rodrigue to the follow-up list'\n"
            "   (keep names as transcribed — don't guess at name spellings)\n\n"
            "OUTPUT FORMAT:\n"
            "Return ONLY the cleaned transcript. No <think>, no reasoning, no preamble, "
            "no quotes, no markdown, no explanation. First character of your response = "
            "first character of the cleaned transcript."
        )

        user_parts: list[str] = []

        # Known vocabulary terms
        if vocab_terms:
            terms_text = ", ".join(vocab_terms[:50])  # cap to avoid token overflow
            user_parts.append(
                "[CASING REFERENCE — these terms exist in the user's vocabulary. "
                "ONLY apply these spellings/casing when the transcript already "
                "contains the same word phonetically. NEVER substitute one of "
                "these terms IN PLACE OF a different word that was spoken.]\n"
                f"{terms_text}"
            )

        # UIAutomation document context
        ui_context_text = ""
        if getattr(self._config, "llm_context_enabled", True) and context is not None:
            ui_context_text = context.compact()
        if ui_context_text:
            user_parts.append(f"[DOCUMENT CONTEXT — text at insertion point in target app]\n{ui_context_text}")

        # Rolling session transcript (last N committed segments)
        if session_context:
            n = getattr(self._config, "llm_rolling_context_sentences", 3)
            recent = session_context[-n:] if n > 0 else []
            if recent:
                joined = " ".join(recent)[:400]
                user_parts.append(f"[RECENT TRANSCRIPT — last {len(recent)} sentence(s) already committed this session]\n{joined}")

        user_parts.append(f"[TRANSCRIPT TO CORRECT]\n{trimmed}")

        # Detect model family from path to pick the right chat template.
        # Qwen uses ChatML (<|im_start|>...), Llama-3.x uses its own header tokens.
        model_path_str = str(getattr(self._config, "llm_model_path", "") or "").lower()
        is_llama3 = "llama-3" in model_path_str or "llama3" in model_path_str
        is_qwen3 = "qwen3" in model_path_str

        if is_qwen3:
            # Qwen3 reasoning mode suppression
            user_parts.append("/no_think")

        user = "\n\n".join(user_parts)

        if is_llama3:
            # Llama-3.x chat template — llama-cpp-python prepends BOS token
            # automatically, so we omit <|begin_of_text|> here to avoid the
            # "duplicate leading <|begin_of_text|>" warning.
            return (
                "<|start_header_id|>system<|end_header_id|>\n\n"
                f"{system}<|eot_id|>"
                "<|start_header_id|>user<|end_header_id|>\n\n"
                f"{user}<|eot_id|>"
                "<|start_header_id|>assistant<|end_header_id|>\n\n"
            )

        # ChatML (Qwen, default)
        suffix = "<think>\n\n</think>\n\n" if is_qwen3 else ""
        return (
            "<|im_start|>system\n"
            f"{system}<|im_end|>\n"
            "<|im_start|>user\n"
            f"{user}<|im_end|>\n"
            "<|im_start|>assistant\n"
            f"{suffix}"
        )

    @staticmethod
    def _extract_response_text(response: object) -> str:
        if isinstance(response, dict):
            choices = response.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    return str(first.get("text") or "")
        return str(response)
