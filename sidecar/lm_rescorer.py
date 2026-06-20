"""KenLM n-gram language model rescorer for homophone/confusable resolution."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from model_paths import runtime_root

if TYPE_CHECKING:
    from config import PostProcessConfig

logger = logging.getLogger(__name__)

# Minimum log-probability margin the best candidate must beat the original
# sentence by before we substitute. 1.0 = roughly 10× more likely under the
# LM. Smaller margins produce too many false flips on short fragments.
_MIN_SUBSTITUTION_MARGIN: float = 1.0

# ---------------------------------------------------------------------------
# Confusables dictionary — embedded; never fetched at runtime (HIPAA safe)
# ---------------------------------------------------------------------------

CONFUSABLES: dict[str, list[str]] = {
    "their": ["their", "there", "they're"],
    "there": ["their", "there", "they're"],
    "they're": ["their", "there", "they're"],
    # to/too/two intentionally NOT auto-rescored: the digit meaning of "two"
    # is too common in dictation (numbers, counts, dollar amounts) and the
    # LM consistently mis-scores "to thousand" > "two thousand" on short
    # fragments. Left to the LLM with full sentence context.
    "write": ["write", "right", "rite"],
    "right": ["write", "right", "rite"],
    "rite": ["write", "right", "rite"],
    "hear": ["hear", "here"],
    "here": ["hear", "here"],
    "new": ["new", "knew"],
    "knew": ["new", "knew"],
    "its": ["its", "it's"],
    "it's": ["its", "it's"],
    "your": ["your", "you're"],
    "you're": ["your", "you're"],
    "affect": ["affect", "effect"],
    "effect": ["affect", "effect"],
    "accept": ["accept", "except"],
    "except": ["accept", "except"],
    "then": ["then", "than"],
    "than": ["then", "than"],
    "passed": ["passed", "past"],
    "past": ["passed", "past"],
    "week": ["week", "weak"],
    "weak": ["week", "weak"],
    "whether": ["whether", "weather"],
    "weather": ["whether", "weather"],
    "forth": ["forth", "fourth"],
    "fourth": ["forth", "fourth"],
    "pray": ["pray", "prey"],
    "prey": ["pray", "prey"],
    "bare": ["bare", "bear"],
    "bear": ["bare", "bear"],
    "buy": ["buy", "by", "bye"],
    "by": ["buy", "by", "bye"],
    "bye": ["buy", "by", "bye"],
    "fair": ["fair", "fare"],
    "fare": ["fair", "fare"],
    "hair": ["hair", "hare"],
    "hare": ["hair", "hare"],
    "hole": ["hole", "whole"],
    "whole": ["hole", "whole"],
    "hour": ["hour", "our"],
    "our": ["hour", "our"],
    "knew": ["new", "knew"],
    "know": ["know", "no"],
    "no": ["know", "no"],
    "mail": ["mail", "male"],
    "male": ["mail", "male"],
    "meat": ["meat", "meet"],
    "meet": ["meat", "meet"],
    "morning": ["morning", "mourning"],
    "mourning": ["morning", "mourning"],
    "none": ["none", "nun"],
    "nun": ["none", "nun"],
    "pail": ["pail", "pale"],
    "pale": ["pail", "pale"],
    "pair": ["pair", "pear"],
    "pear": ["pair", "pear"],
    "peace": ["peace", "piece"],
    "piece": ["peace", "piece"],
    "plain": ["plain", "plane"],
    "plane": ["plain", "plane"],
    "principal": ["principal", "principle"],
    "principle": ["principal", "principle"],
    "rain": ["rain", "reign", "rein"],
    "reign": ["rain", "reign", "rein"],
    "rein": ["rain", "reign", "rein"],
    "raise": ["raise", "raze"],
    "raze": ["raise", "raze"],
    "read": ["read", "reed"],
    "reed": ["read", "reed"],
    "real": ["real", "reel"],
    "reel": ["real", "reel"],
    "road": ["road", "rowed"],
    "rowed": ["road", "rowed"],
    "role": ["role", "roll"],
    "roll": ["role", "roll"],
    "sail": ["sail", "sale"],
    "sale": ["sail", "sale"],
    "scene": ["scene", "seen"],
    "seen": ["scene", "seen"],
    "sea": ["sea", "see"],
    "see": ["sea", "see"],
    "seam": ["seam", "seem"],
    "seem": ["seam", "seem"],
    "sight": ["sight", "site", "cite"],
    "site": ["sight", "site", "cite"],
    "cite": ["sight", "site", "cite"],
    "sole": ["sole", "soul"],
    "soul": ["sole", "soul"],
    "some": ["some", "sum"],
    "sum": ["some", "sum"],
    "son": ["son", "sun"],
    "sun": ["son", "sun"],
    "stationary": ["stationary", "stationery"],
    "stationery": ["stationary", "stationery"],
    "steal": ["steal", "steel"],
    "steel": ["steal", "steel"],
    "tail": ["tail", "tale"],
    "tale": ["tail", "tale"],
    "way": ["way", "weigh"],
    "weigh": ["way", "weigh"],
    "which": ["which", "witch"],
    "witch": ["which", "witch"],
    "who's": ["who's", "whose"],
    "whose": ["who's", "whose"],
    "wood": ["wood", "would"],
    "would": ["wood", "would"],
}


class LMRescorer:
    """Resolves homophones/confusables using a KenLM n-gram language model.

    Backend priority:
      1. kenlm C-extension (fast, ~3 ms/call)
      2. arpa pure-Python fallback (~20 ms/call)
      3. Disabled passthrough if neither available or model absent
    """

    def __init__(self, config: "PostProcessConfig | None" = None) -> None:
        self._config = config
        self._model = None
        self._backend: str = "none"
        self._load_attempted = False
        self._enabled: bool = getattr(config, "lm_rescorer_enabled", True) if config else True
        self._model_path_raw: str = (
            getattr(config, "lm_model_path", "models/lm/3gram-pruned.arpa") if config
            else "models/lm/3gram-pruned.arpa"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_text(self, text: str) -> "float | None":
        """Public LM log-probability of ``text`` (higher = more likely English).

        Returns ``None`` when no model is available, so callers (e.g. seam
        N-best) can cleanly skip LM-based decisions. Used to pick between two
        competing transcriptions of the same audio.
        """
        if not self._enabled or not text or not text.strip():
            return None
        model = self._ensure_model()
        if model is None:
            return None
        try:
            return self._score(text, model)
        except Exception:
            return None

    def rescore(self, text: str) -> str:
        """Return text with confusable words resolved using LM context."""
        if not self._enabled or not text or not text.strip():
            return text

        model = self._ensure_model()
        if model is None:
            return text

        try:
            return self._resolve_confusables(text, model)
        except Exception:
            logger.debug("LM rescoring failed; returning original text", exc_info=True)
            return text

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        if self._load_attempted:
            return None
        self._load_attempted = True

        model_path = self._resolve_model_path(self._model_path_raw)
        if not model_path.exists():
            logger.info("LM model not found at %s; rescoring disabled", model_path)
            return None

        # Try kenlm first
        try:
            import kenlm  # type: ignore

            self._model = kenlm.Model(str(model_path))
            self._backend = "kenlm"
            logger.info("LMRescorer: kenlm model loaded from %s", model_path)
            return self._model
        except ImportError:
            logger.debug("kenlm not available; trying arpa fallback")
        except Exception:
            logger.debug("kenlm model load failed; trying arpa fallback", exc_info=True)

        # Try arpa fallback — only safe for small pruned models (< 200 MB)
        _ARPA_MAX_BYTES = 200 * 1024 * 1024  # 200 MB hard ceiling
        try:
            file_size = model_path.stat().st_size
            if file_size > _ARPA_MAX_BYTES:
                logger.warning(
                    "LMRescorer: ARPA file is %.0f MB — too large for pure-Python arpa backend "
                    "(limit %d MB). Use a pruned model. Rescoring disabled.",
                    file_size / 1_048_576, _ARPA_MAX_BYTES // 1_048_576,
                )
                return None
        except Exception:
            pass

        try:
            import arpa  # type: ignore

            models = arpa.loadf(str(model_path))
            self._model = models[0]
            self._backend = "arpa"
            logger.info("LMRescorer: arpa model loaded from %s", model_path)
            return self._model
        except ImportError:
            logger.info("Neither kenlm nor arpa available; LM rescoring disabled")
        except Exception:
            logger.info("arpa model load failed; LM rescoring disabled", exc_info=True)

        return None

    def _score(self, sentence: str, model) -> float:
        """Return log-probability of sentence under the loaded model."""
        try:
            if self._backend == "kenlm":
                return model.score(sentence, bos=True, eos=True)
            if self._backend == "arpa":
                # Use the library's full-sentence scorer: it applies proper
                # n-gram backoff for unseen contexts (adding <s>/</s> bounds).
                # The previous manual per-word log_p loop did NOT back off —
                # an unseen trigram fell straight to a unigram or contributed
                # zero — which collapsed the score gap between homophones the
                # model can actually distinguish (e.g. "over there" vs "over
                # their"), so almost nothing got corrected. arpa expects upper.
                try:
                    return model.log_s(sentence.upper())
                except Exception:
                    # Robust fallback for tokens log_s can't handle: rough
                    # per-word unigram sum so scoring degrades instead of
                    # raising. Still better than silently returning -inf.
                    total = 0.0
                    for word in sentence.upper().split():
                        try:
                            total += model.log_p((word,))
                        except Exception:
                            pass
                    return total
        except Exception:
            pass
        return float("-inf")

    def _resolve_confusables(self, text: str, model) -> str:
        """Substitute confusable words only when the LM is *decisively* sure.

        We require the best candidate to beat the original by at least
        ``_MIN_SUBSTITUTION_MARGIN`` log-prob points. Without this margin the
        LM frequently misfires on short fragments (e.g. "Two thousand" gets
        rescored to "To thousand" because "to" is a higher-frequency unigram).
        """
        words = text.split()
        result = list(words)

        for i, word in enumerate(words):
            lower = word.lower()
            # Preserve leading/trailing punctuation around the word
            stripped = lower.strip(".,!?;:\"'()-")
            if stripped not in CONFUSABLES:
                continue

            candidates = CONFUSABLES[stripped]
            if len(candidates) <= 1:
                continue

            # Preserve original casing hint from the word
            is_capitalized = word[0].isupper() if word else False

            # Score the ORIGINAL first to establish a baseline
            original_sentence = " ".join(result)
            baseline = self._score(original_sentence, model)

            best_score = baseline
            best_candidate = word  # default: keep original

            for candidate in candidates:
                if candidate.lower() == stripped:
                    continue  # already scored as baseline
                test_words = list(result)
                test_word = candidate.capitalize() if is_capitalized else candidate
                test_words[i] = test_word
                sentence = " ".join(test_words)
                score = self._score(sentence, model)
                if score > best_score:
                    best_score = score
                    best_candidate = test_word

            # Only substitute when the LM is decisively sure
            if best_candidate != word and (best_score - baseline) >= _MIN_SUBSTITUTION_MARGIN:
                result[i] = best_candidate

        return " ".join(result)

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

    @staticmethod
    def _runtime_root() -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return Path(__file__).resolve().parent
