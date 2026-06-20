"""Custom vocabulary correction for names, acronyms, and jargon."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path
import re
from typing import Optional


TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class VocabularyTerm:
    text: str
    words: tuple[str, ...]


class VocabularyCorrector:
    """Fuzzy post-ASR correction for user-provided terms.

    The current onnx_asr adapter does not expose a hotword-biasing argument, so
    this acts as a conservative post-ASR fallback. Terms come from a text file,
    one term per line. CSV-style `wrong,right` lines are also accepted.
    """

    def __init__(self, path: str | None = None) -> None:
        self._terms: list[VocabularyTerm] = []
        # Always load the bundled default vocabulary first so proper nouns
        # like "VoiceFlow" keep their canonical casing even when the user
        # has not configured a custom vocabulary file.
        default = Path(__file__).resolve().parent / "default_vocabulary.txt"
        if default.exists():
            self.load(str(default))
        if path:
            # Custom file is appended on top of defaults.
            existing = list(self._terms)
            self.load(path)
            # Preserve uniqueness by canonical text (custom wins).
            seen = {t.text.lower() for t in self._terms}
            for term in existing:
                if term.text.lower() not in seen:
                    self._terms.append(term)
            self._terms = sorted(self._terms, key=lambda t: len(t.words), reverse=True)

    def load(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.exists():
            self._terms = []
            return

        terms: list[VocabularyTerm] = []
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            canonical = raw.split(",", 1)[-1].strip()
            words = tuple(_word_tokens(canonical))
            if words:
                terms.append(VocabularyTerm(text=canonical, words=words))
        self._terms = sorted(terms, key=lambda term: len(term.words), reverse=True)

    def correct(self, text: str) -> str:
        """Replace vocab terms in *text* while preserving all surrounding
        whitespace and punctuation exactly as it was.

        Operates by finding word spans (\\w+) in the input, matching consecutive
        words against the vocabulary, and substituting only the matched
        character ranges. Anything between/around the matched words (periods
        inside ``Marcus.lee``, spaces, commas, ``@``, ``/``) is left intact.
        """
        if not text or not self._terms:
            return text

        word_matches = list(re.finditer(r"\w+", text, re.UNICODE))
        if not word_matches:
            return text

        words = [m.group(0) for m in word_matches]
        norm = [_normalize(w) for w in words]
        spans = [(m.start(), m.end()) for m in word_matches]

        # Scan greedily for term matches (longest terms are sorted first).
        replacements: list[tuple[int, int, str]] = []  # (char_start, char_end, text)
        i = 0
        while i < len(words):
            consumed = 0
            replacement: Optional[str] = None
            for term in self._terms:
                size = len(term.words)
                if size == 0 or i + size > len(words):
                    continue
                candidate = tuple(norm[i : i + size])
                if _is_match(candidate, term.words):
                    replacement = term.text
                    consumed = size
                    break
            if replacement and consumed:
                char_start = spans[i][0]
                char_end = spans[i + consumed - 1][1]
                # Don't substitute inside emails or URLs: skip when the
                # context character on either side is '@', '/', ':' or '.',
                # which means the word is part of foo@bar.com / a/b / a:b /
                # foo.bar style identifiers we must not touch.
                left = text[char_start - 1] if char_start > 0 else " "
                right = text[char_end] if char_end < len(text) else " "
                if left in "@/:." or right in "@/:":
                    i += 1
                    continue
                replacements.append((char_start, char_end, replacement))
                i += consumed
            else:
                i += 1

        if not replacements:
            return text

        # Apply right-to-left so earlier spans stay valid.
        out = text
        for char_start, char_end, replacement in reversed(replacements):
            out = out[:char_start] + replacement + out[char_end:]
        return out


def _word_tokens(text: str) -> list[str]:
    return [_normalize(token) for token in re.findall(r"\w+", text) if _normalize(token)]


def _normalize(token: str) -> str:
    return token.lower().replace("_", "").replace("-", "")


def _is_match(candidate: tuple[str, ...], canonical: tuple[str, ...]) -> bool:
    if candidate == canonical:
        return True
    cand_text = " ".join(candidate)
    canon_text = " ".join(canonical)
    if len(canon_text) < 5:
        return False
    return difflib.SequenceMatcher(None, cand_text, canon_text).ratio() >= 0.88


def _join_tokens(tokens: list[str]) -> str:
    text = ""
    for token in tokens:
        if not text:
            text = token
        elif re.match(r"[^\w\s]", token):
            text += token
        else:
            text += " " + token
    return text
