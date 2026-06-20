"""Confidence-gated, span-scoped correction helpers (pure logic, no model).

The idea: trust the words Parakeet was sure about and only let a corrector
touch the shaky ones. These functions are deliberately model-free so they are
fast and exhaustively unit-testable; the LLM call and pipeline wiring live in
llm_formatter / main and use these primitives.

Flow:
  align_confidences(merged_words, conf_stream)   # confidence per FINAL word
  select_low_confidence(confs, threshold)        # which indices to send out
  apply_scoped_corrections(words, {idx: newword})# substitute ONLY those indices

The scoping guarantee is enforced in apply_scoped_corrections: a correction is
applied only at a flagged index, and rejected if it tries to balloon a word
into many words (a sign the corrector started rewriting). High-confidence words
can never change, regardless of what the corrector returns.
"""

from __future__ import annotations

import re

_STRIP = ".,!?;:\"'()-"
_MAX_REPLACEMENT_WORDS = 3  # reject corrections that try to rewrite, not fix


def _norm(word: str) -> str:
    """Lowercased, punctuation-stripped key for matching words across stages."""
    return word.strip(_STRIP).lower()


def align_confidences(
    merged_words: list[str],
    stream_words: list[str],
    stream_confs: list[float],
) -> list[float]:
    """Map a per-word confidence STREAM onto the FINAL merged word list.

    ``stream_words``/``stream_confs`` are every word Parakeet emitted across all
    chunks (in order) and its confidence. ``merged_words`` is what survived
    overlap-dedup — a subsequence of that stream. A keyed two-pointer walk
    matches by normalized word, so dropped overlap-duplicates don't shift the
    alignment. Merged words with no match within a bounded look-ahead default to
    1.0 ("treat as confident") — the safe choice, since it means we will NOT
    send them out for correction.
    """
    out: list[float] = []
    j = 0
    n = len(stream_words)
    for word in merged_words:
        key = _norm(word)
        found = None
        # Look ahead a bounded distance for the next matching stream word.
        look = j
        limit = min(n, j + 50)
        while look < limit:
            if _norm(stream_words[look]) == key:
                found = stream_confs[look]
                j = look + 1
                break
            look += 1
        out.append(found if found is not None else 1.0)
    return out


def select_low_confidence(confs: list[float], threshold: float) -> list[int]:
    """Indices of words whose confidence is below ``threshold``."""
    return [i for i, c in enumerate(confs) if c < threshold]


def apply_scoped_corrections(
    words: list[str], corrections: dict[int, str]
) -> str:
    """Return text with corrections applied ONLY at the given indices.

    Safety rules (the heart of span-scoping):
      * an index not in ``corrections`` is emitted verbatim — untouchable;
      * a correction that is empty/whitespace is ignored (keep original);
      * a correction expanding to more than ``_MAX_REPLACEMENT_WORDS`` words is
        rejected (the corrector is rewriting, not fixing);
      * surrounding punctuation on the original word is preserved.
    """
    result: list[str] = []
    for i, original in enumerate(words):
        new = corrections.get(i)
        if not new or not new.strip():
            result.append(original)
            continue
        if len(new.split()) > _MAX_REPLACEMENT_WORDS:
            result.append(original)
            continue
        result.append(_preserve_affixes(original, new.strip()))
    return " ".join(result)


def _preserve_affixes(original: str, replacement: str) -> str:
    """Carry leading/trailing punctuation from ``original`` onto ``replacement``.

    e.g. original "their," + replacement "there" -> "there,". Only applied when
    the replacement is a single bare word; multi-word replacements are returned
    as-is.
    """
    if " " in replacement:
        return replacement
    lead = re.match(r"^[^\w]*", original).group(0)
    trail = re.search(r"[^\w]*$", original).group(0)
    return f"{lead}{replacement}{trail}"


def format_marked_transcript(words: list[str], indices: list[int]) -> str:
    """Render the transcript with flagged words tagged [n] for the LLM prompt.

    Returns text like: "the client [1]aproved the revised [2]cod". The numbers
    are 1-based and map back to ``indices`` order (used to parse the reply).
    """
    flset = {idx: pos for pos, idx in enumerate(indices, start=1)}
    out: list[str] = []
    for i, w in enumerate(words):
        if i in flset:
            out.append(f"[{flset[i]}]{w}")
        else:
            out.append(w)
    return " ".join(out)


_REPLY_LINE_RE = re.compile(r"^\s*(\d+)\s*[:=\-]\s*(.+?)\s*$")


def parse_scoped_reply(reply: str, indices: list[int]) -> dict[int, str]:
    """Parse an LLM reply of ``N: word`` lines into ``{word_index: replacement}``.

    ``indices`` is the flagged-word list in the same 1-based order used by
    ``format_marked_transcript``. Lines that don't match or reference unknown
    numbers are ignored.
    """
    corrections: dict[int, str] = {}
    for line in reply.splitlines():
        m = _REPLY_LINE_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= len(indices):
            corrections[indices[n - 1]] = m.group(2).strip()
    return corrections
