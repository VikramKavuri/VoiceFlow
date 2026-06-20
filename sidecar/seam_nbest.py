"""Seam N-best: LM-pick between two transcriptions of the overlapped audio.

Reconcile decodes 12 s chunks that overlap by 1.5 s, so the words at a chunk
seam are transcribed twice. The structural dedup keeps the PREVIOUS chunk's
version and discards the next chunk's competing words. When the two chunks
*disagree* at the seam, that discarded version is a real second hypothesis for
the same audio — the only genuine N-best available on a greedy decoder.

``seam_merge`` arbitrates that one spot with a language model: it scores the
two candidate seam reconstructions and swaps in the next chunk's words only
when the LM prefers them by a margin. Everything is injected (dedupe + score
functions) so this stays pure and unit-testable, and it can NEVER invent words
— it only chooses between two transcriptions the model already produced.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable, Optional

_STRIP = ".,!?;:'\""


def _norm(word: str) -> str:
    return word.lower().strip(_STRIP)


def seam_merge(
    prev: str,
    nxt: str,
    dedupe_fn: Callable[[str, str], str],
    score_fn: Optional[Callable[[str], Optional[float]]],
    *,
    margin: float = 1.0,
    k: int = 60,
    min_match: int = 2,
    context: int = 4,
    trimmed_next: Optional[str] = None,
) -> str:
    """Return ``prev`` and ``nxt`` merged, resolving the seam by LM preference.

    Falls back to the plain dedup-and-append result whenever there is no clean
    anchor, no actual disagreement, or no LM — so behaviour is unchanged except
    at genuine, confidently-better seams.
    """
    if trimmed_next is None:
        trimmed_next = dedupe_fn(prev, nxt)
    default = (prev + " " + trimmed_next).strip() if prev else trimmed_next.strip()

    if score_fn is None:
        return default

    prev_words = prev.split()
    next_words = nxt.split()
    if not prev_words or not next_words:
        return default

    # Stage-1 word anchor (same matching the structural dedup uses).
    tail = [_norm(w) for w in prev_words[-k:]]
    head = [_norm(w) for w in next_words[:k]]
    m = SequenceMatcher(a=tail, b=head, autojunk=False).find_longest_match(
        0, len(tail), 0, len(head)
    )
    # Need an anchor AND some pre-anchor words in next to arbitrate (m.b > 0).
    if m.size < min_match or m.b == 0 or m.b > 5:
        return default

    a_start = len(prev_words) - len(tail) + m.a
    pre = m.b
    if a_start - pre < 0:
        return default

    prev_version = prev_words[a_start - pre:a_start]   # prev's take on the seam
    next_version = next_words[:pre]                     # next's competing take
    if [_norm(w) for w in prev_version] == [_norm(w) for w in next_version]:
        return default  # identical -> nothing to choose

    # Score both reconstructions in a small shared context window.
    left = prev_words[max(0, a_start - pre - context):a_start - pre]
    after = next_words[m.b:m.b + context]
    cand_prev = " ".join(left + prev_version + after)
    cand_next = " ".join(left + next_version + after)

    s_prev = score_fn(cand_prev)
    s_next = score_fn(cand_next)
    if s_prev is None or s_next is None:
        return default

    if s_next > s_prev + margin:
        new_prev = prev_words[:a_start - pre] + next_version + prev_words[a_start:]
        return (" ".join(new_prev) + " " + trimmed_next).strip()
    return default
