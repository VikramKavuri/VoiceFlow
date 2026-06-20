"""
VoiceFlow Sidecar - Text post-processing pipeline.

Each stage is independently toggleable via :class:`PostProcessConfig`.
Processing is pure in-memory string manipulation; nothing is written to
disk (HIPAA compliance).
"""

from __future__ import annotations

import csv
import difflib
import io
import logging
import re
from typing import Optional

from config import PostProcessConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Filler-word patterns
# ---------------------------------------------------------------------------

# Standalone fillers (always removed)
_STANDALONE_FILLERS: set[str] = {"um", "uh", "er", "hmm", "hm", "mm"}

# Phrase fillers (removed as complete phrases)
_PHRASE_FILLERS: list[re.Pattern[str]] = [
    re.compile(r"\byou know\b", re.IGNORECASE),
    re.compile(r"\bI mean\b", re.IGNORECASE),
    re.compile(r"\bbasically\b", re.IGNORECASE),
    re.compile(r"\bactually\b", re.IGNORECASE),
]

# Sentence-initial "so" (filler, not conjunction)
_INITIAL_SO: re.Pattern[str] = re.compile(
    r"(?:^|(?<=\.\s))so[,]?\s+", re.IGNORECASE
)

# Words after which "like" is meaningful (verb), not a filler
# Words after which "like" is meaningful (verb), not a filler
_LIKE_KEEP_PREDECESSORS: set[str] = {
    "would", "i", "you", "we", "they", "he", "she", "it", "to",
    "don't", "didn't", "doesn't", "do", "does", "will",
}

# ---------------------------------------------------------------------------
# Number word -> digit mappings
# ---------------------------------------------------------------------------

_ONES: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES: dict[str, int] = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
}
# Stopwords for which phonetic-correction collapsing should never fire
_PHONETIC_SKIP: set[str] = {
    "the", "a", "an", "is", "are", "was", "were",
    "of", "to", "in", "on", "at", "and", "or", "but", "for",
}

_CURRENCY_WORDS: dict[str, str] = {
    "dollars": "$", "dollar": "$",
    "euros": "\u20ac", "euro": "\u20ac",
    "pounds": "\u00a3", "pound": "\u00a3",
    "cents": "\u00a2", "cent": "\u00a2",
}


class PostProcessor:
    """Runs a configurable text-cleaning pipeline on raw transcripts."""

    def __init__(self, config: PostProcessConfig) -> None:
        self._config = config
        self._dictionary: dict[str, str] = {}
        if config.apply_custom_dictionary and config.custom_dictionary_path:
            self.load_dictionary(config.custom_dictionary_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, text: str) -> str:
        """Run the full pipeline and return the cleaned text."""
        if not text or not text.strip():
            return text

        if self._config.remove_fillers:
            text = self._remove_fillers(text)
        elif getattr(self._config, "remove_nonlexical_fillers", True):
            # Safe subset: strip um/uh/er/hmm only (no phrases, no "like").
            text = self._remove_nonlexical_fillers(text)
        if self._config.collapse_false_starts:
            text = self._collapse_false_starts(text)
        # Format numbers BEFORE the duplicate / phonetic stages.  Reconcile
        # chunks overlap by ~1.5 s, so the final word of one chunk and the
        # first word of the next can be the same number-word ("twenty .
        # twenty one" — meaning 20 then 21).  If we deduplicate first, the
        # bare "Twenty" gets merged into the "Twenty one" and we silently
        # drop the 20.  Converting to digits first ("20 21") leaves the
        # subsequent dedup stage with nothing to collapse.
        if self._config.format_numbers:
            text = self._format_numbers(text)
        if self._config.remove_repetitions:
            text = self._remove_repetitions(text)
        if self._config.collapse_phonetic_corrections:
            text = self._collapse_phonetic_corrections(text)
        if self._config.apply_custom_dictionary:
            text = self._apply_dictionary(text)
        if self._config.fix_punctuation:
            text = self._fix_punctuation(text)

        # Final whitespace normalisation
        text = re.sub(r"  +", " ", text).strip()
        return text

    def load_dictionary(self, path: str) -> None:
        """Load a CSV dictionary file (term, replacement) into memory.

        The file is read entirely into memory; the path is not stored
        beyond what is already in the config (no disk handle kept open).
        """
        self._dictionary.clear()
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                content = fh.read()
            reader = csv.reader(io.StringIO(content))
            for row in reader:
                if len(row) >= 2:
                    term, replacement = row[0].strip(), row[1].strip()
                    if term:
                        self._dictionary[term.lower()] = replacement
            logger.info("Loaded %d dictionary entries from %s", len(self._dictionary), path)
        except Exception:
            logger.exception("Failed to load dictionary from %s", path)

    # ------------------------------------------------------------------
    # Pipeline stages (private)
    # ------------------------------------------------------------------

    def _remove_nonlexical_fillers(self, text: str) -> str:
        """Remove ONLY non-lexical fillers (um, uh, er, hmm, hm, mm).

        These never carry meaning, so this is always safe — unlike the broader
        _remove_fillers which also drops phrases and context-dependent words.
        """
        for filler in _STANDALONE_FILLERS:
            text = re.sub(rf"\b{filler}\b[,]?\s*", " ", text, flags=re.IGNORECASE)
        return re.sub(r"  +", " ", text).strip()

    def _remove_fillers(self, text: str) -> str:
        """Remove filler words and phrases (um, uh, er, you know, etc.).

        Context-aware: ``"like"`` is only removed when used as a filler,
        not in phrases such as ``"I like pizza"``.
        """
        # Remove standalone fillers (surrounded by word boundaries)
        text = self._remove_nonlexical_fillers(text)

        # Remove phrase fillers
        for pattern in _PHRASE_FILLERS:
            text = pattern.sub(" ", text)

        # Sentence-initial "so"
        text = _INITIAL_SO.sub("", text)

        # Context-aware "like" — keep when preceded by a verb (e.g. "I like pizza")
        tokens = text.split()
        cleaned_tokens: list[str] = []
        for idx, tok in enumerate(tokens):
            word = tok.lower().rstrip(",.!?;:")
            if word == "like":
                prev = cleaned_tokens[-1].lower().rstrip(",.!?;:") if cleaned_tokens else ""
                if prev in _LIKE_KEEP_PREDECESSORS:
                    cleaned_tokens.append(tok)  # meaningful "like"
                # else: skip filler "like"
            else:
                cleaned_tokens.append(tok)
        text = " ".join(cleaned_tokens)

        return text

    def _collapse_false_starts(self, text: str) -> str:
        """Detect and remove repeated / abandoned phrases.

        Uses a sliding window over tokens to find sequences where the
        speaker started a phrase, abandoned it, then restarted.
        Pattern: "I was going I was going to say" -> "I was going to say"
        """
        tokens = text.split()
        if len(tokens) < 4:
            return text

        result: list[str] = []
        i = 0
        while i < len(tokens):
            # Try window sizes from 4 down to 2
            found_repeat = False
            for win_size in range(min(6, (len(tokens) - i) // 2), 1, -1):
                if i + 2 * win_size > len(tokens):
                    continue
                window_a = tokens[i : i + win_size]
                window_b = tokens[i + win_size : i + 2 * win_size]
                if [w.lower().rstrip(",.!?") for w in window_a] == [
                    w.lower().rstrip(",.!?") for w in window_b
                ]:
                    # Skip the first occurrence (the false start)
                    i += win_size
                    found_repeat = True
                    break
            if not found_repeat:
                result.append(tokens[i])
                i += 1

        return " ".join(result)

    def _remove_repetitions(self, text: str) -> str:
        """Remove immediate word repetitions (disfluencies).

        ``"the the"`` -> ``"the"``
        ``"I I I think"`` -> ``"I think"``

        Numbers are deliberately exempt: a repeated number is usually an
        intentional digit sequence in dictation ("two two two" = the digits
        2 2 2, a phone/card number), not a disfluency. format_numbers is off
        by default, so collapsing them here would silently drop digits. The
        duplicate words created by the 1.5 s reconcile chunk overlap are
        removed separately by ``_dedupe_overlap``, so we don't need to fold
        repeated numbers here to handle seams.
        """
        tokens = text.split()
        if len(tokens) < 2:
            return text

        number_words = set(_ONES) | set(_TENS) | set(_SCALES)

        def _is_number(bare: str) -> bool:
            return bare in number_words or any(ch.isdigit() for ch in bare)

        result: list[str] = []
        for tok in tokens:
            bare = tok.lower().strip(",.;:!?")
            if bare and result:
                prev_bare = result[-1].lower().strip(",.;:!?")
                if bare == prev_bare and not _is_number(bare):
                    continue  # drop the duplicate (keeps the first occurrence)
            result.append(tok)
        return " ".join(result)

    def _collapse_phonetic_corrections(self, text: str) -> str:
        """Collapse immediate phonetic self-corrections.

        When a speaker says a word and immediately says a phonetically similar
        word (e.g. "three tree"), keep the second word (the correction) and
        drop the first.

        Identical pairs are left to ``_remove_repetitions``; this method only
        acts when ``prev_bare != bare`` but the two are highly similar.
        """
        tokens = text.split()
        result: list[str] = []

        for tok in tokens:
            bare = tok.lower().rstrip(",.!?;:")
            if result:
                prev = result[-1]
                prev_bare = prev.lower().rstrip(",.!?;:")
                if (
                    prev_bare != bare
                    and len(prev_bare) >= 3
                    and len(bare) >= 3
                    and prev_bare.isalpha()
                    and bare.isalpha()
                    and prev_bare not in _PHONETIC_SKIP
                    and bare not in _PHONETIC_SKIP
                    and difflib.SequenceMatcher(None, prev_bare, bare).ratio() >= 0.75
                ):
                    # Second word wins — replace last result entry
                    result[-1] = tok
                    continue
            result.append(tok)

        return " ".join(result)

    def _fix_punctuation(self, text: str) -> str:
        """Capitalise sentence starts, ensure terminal punctuation, and
        detect questions.
        """
        if not text:
            return text

        # Split into sentences on existing punctuation
        sentences = re.split(r"(?<=[.!?])\s+", text)
        cleaned: list[str] = []

        question_starters = {
            "who", "what", "when", "where", "why", "how",
            "is", "are", "was", "were", "do", "does", "did",
            "can", "could", "would", "should", "will", "shall",
            "have", "has", "had", "may", "might",
        }

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # Capitalise first letter
            sentence = sentence[0].upper() + sentence[1:] if len(sentence) > 1 else sentence.upper()

            # Add terminal punctuation if missing
            if sentence and sentence[-1] not in ".!?":
                first_word = sentence.split()[0].lower().rstrip(",.") if sentence.split() else ""
                if first_word in question_starters:
                    sentence += "?"
                else:
                    sentence += "."

            cleaned.append(sentence)

        return " ".join(cleaned)

    def _format_numbers(self, text: str) -> str:
        """Convert spelled-out numbers to digits.

        ``"twenty seven"`` -> ``"27"``
        ``"three hundred dollars"`` -> ``"$300"``
        """
        # Pre-expand hyphenated number compounds such as "twenty-four" so that
        # the existing token-level parser can convert them to digits.
        tokens = text.split()
        expanded: list[str] = []
        for tok in tokens:
            if "-" in tok:
                bare = tok.lower().rstrip(",.!?;:")
                trailing = tok[len(tok.rstrip(",.!?;:")):]
                parts = bare.split("-")
                if len(parts) >= 2 and all(
                    p in _ONES or p in _TENS or p in _SCALES for p in parts
                ):
                    for j, p in enumerate(parts):
                        # Re-attach trailing punctuation only to the LAST part
                        # so the number-phrase parser still sees the boundary.
                        if j == len(parts) - 1 and trailing:
                            expanded.append(p + trailing)
                        else:
                            expanded.append(p)
                    continue
            expanded.append(tok)
        tokens = expanded

        result: list[str] = []
        i = 0

        while i < len(tokens):
            # Try to parse a number starting at position i
            num, consumed, currency = self._parse_number_phrase(tokens, i)
            if consumed > 0:
                if currency:
                    result.append(f"{currency}{num}")
                else:
                    result.append(str(num))
                i += consumed
            else:
                result.append(tokens[i])
                i += 1

        return " ".join(result)

    def _parse_number_phrase(
        self, tokens: list[str], start: int
    ) -> tuple[int, int, str]:
        """Try to parse a contiguous number phrase starting at *start*.

        Returns ``(value, tokens_consumed, currency_symbol)``.
        If no number is found, returns ``(0, 0, "")``.

        A state machine prevents greedy summing of what should be separate
        numbers.  ``value_pending`` tracks whether we've consumed a
        value-bearing word (tens/ones) since the last scale reset.
        ``last_kind`` records the most-recent word category so we can detect
        illegal sequences such as TENS→TENS ("twenty thirty") or
        ONES-after-TENS→ONES ("twenty one two").
        """
        i = start
        total = 0
        current = 0
        consumed = 0
        currency = ""

        # State-machine variables
        value_pending: bool = False          # True after any value-bearing word
        last_kind: Optional[str] = None      # None | 'tens' | 'ones' | 'ones_after_tens' | 'scale'

        while i < len(tokens):
            raw = tokens[i]
            stripped = raw.rstrip(",.!?;:")
            trailing_punct = raw[len(stripped):]
            word = stripped.lower()

            matched = True
            if word in _ONES:
                # BREAK if we already have ones (e.g. "one two", "twenty one two")
                if last_kind in ("ones", "ones_after_tens"):
                    break
                current += _ONES[word]
                last_kind = "ones_after_tens" if last_kind == "tens" else "ones"
                value_pending = True
            elif word in _TENS:
                # BREAK if any value-bearing word has already been consumed
                # (e.g. "twenty thirty", "twenty seven thirty")
                if value_pending:
                    break
                current += _TENS[word]
                last_kind = "tens"
                value_pending = True
            elif word in _SCALES:
                scale = _SCALES[word]
                if current == 0:
                    current = 1
                if scale >= 1000:
                    total += current * scale
                    current = 0
                else:
                    current *= scale
                # Scales legitimately allow more digits to follow
                value_pending = False
                last_kind = "scale"
            elif word in _CURRENCY_WORDS and consumed > 0:
                currency = _CURRENCY_WORDS[word]
                consumed = i - start + 1
                i += 1
                break  # currency ends the number phrase
            else:
                matched = False

            if not matched:
                break

            consumed = i - start + 1
            i += 1
            # A number phrase cannot cross a clause / sentence boundary.
            # "ninety, heart rate one" must not become 91 — and "ninety
            # hundred one" should not eat the following clause's number.
            if trailing_punct:
                break

        total += current
        if consumed == 0:
            return 0, 0, ""
        return total, consumed, currency

    def _apply_dictionary(self, text: str) -> str:
        """Apply case corrections from the custom dictionary."""
        if not self._dictionary:
            return text

        for term, replacement in self._dictionary.items():
            # Case-insensitive whole-word replacement
            text = re.sub(
                rf"\b{re.escape(term)}\b",
                replacement,
                text,
                flags=re.IGNORECASE,
            )

        return text
