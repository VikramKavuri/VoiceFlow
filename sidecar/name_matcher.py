"""Confidence-gated fuzzy name replacement.

When the ASR produces a LOW-CONFIDENCE, name-like word that is close to a real
name in the bundled list, replace it with the closest match
("O'Corner" -> "O'Connor", "Gianluk" -> "Gianluca"). This is the part that fixes
*misheard* names — unlike NameCasingCorrector, which only fixes capitalization.

It is deliberately hard to misfire. A word is replaced only if ALL hold:
  * it is name-like: >= MIN_LEN letters, alphabetic (apostrophes allowed);
  * it is NOT a common English word (common_words.txt) — so ordinary words are
    never turned into names, even when low-confidence;
  * it is NOT in the app's known vocabulary/jargon (protects Kubernetes, etc.);
  * the ASR was unsure about it: confidence < threshold, OR (when no confidence
    is available) the token is capitalized mid-sentence (a name signal);
  * the closest name scores >= the similarity threshold (Jaro-Winkler).

Matching is bucketed by first letter and uses rapidfuzz (C++), so scanning a
90k-name list costs well under a millisecond per candidate.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Optional

from rapidfuzz import process
from rapidfuzz.distance import JaroWinkler

MIN_LEN = 4
_CORE_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def _norm(s: str) -> str:
    """Lowercase, strip everything but a-z (apostrophes/hyphens removed) for
    phonetic-ish comparison; 'O'Connor' and 'oconnor' compare equal-ish."""
    return re.sub(r"[^a-z]", "", s.lower())


class NameMatcher:
    def __init__(
        self,
        index_path: str | None = None,
        common_words_path: str | None = None,
        threshold: float = 0.92,
        confidence_threshold: float = 0.80,
        capitalized_fallback: bool = False,
    ) -> None:
        self._threshold = float(threshold)
        self._conf_threshold = float(confidence_threshold)
        # When True AND no per-word confidence is available, fall back to
        # treating capitalized mid-sentence tokens as candidates. Off by default
        # because without confidence it can corrupt capitalized jargon
        # ("PostgreSQL" -> "Postres"); the live app provides confidences.
        self._cap_fallback = bool(capitalized_fallback)
        self._common: set[str] = set()
        # bucket: first-letter -> (list[norm_key], list[canonical])
        self._buckets: dict[str, tuple[list[str], list[str]]] = {}

        here = Path(__file__).resolve().parent
        idx_p = Path(index_path).expanduser() if index_path else here / "name_casing_index.txt"
        cw_p = Path(common_words_path).expanduser() if common_words_path else here / "common_words.txt"
        if idx_p.exists():
            self._load_index(idx_p)
        if cw_p.exists():
            self._common = {
                w.strip().lower() for w in cw_p.read_text(encoding="utf-8-sig").splitlines() if w.strip()
            }

    def _load_index(self, path: Path) -> None:
        raw: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "\t" not in line:
                continue
            key, canon = line.split("\t", 1)
            canon = canon.strip()
            nk = _norm(key)
            if len(nk) >= MIN_LEN and nk not in raw:
                raw[nk] = canon
        buckets: dict[str, tuple[list[str], list[str]]] = defaultdict(lambda: ([], []))
        for nk, canon in raw.items():
            keys, canons = buckets[nk[0]]
            keys.append(nk)
            canons.append(canon)
        self._buckets = dict(buckets)

    @property
    def size(self) -> int:
        return sum(len(k) for k, _ in self._buckets.values())

    def _best_match(self, norm_word: str) -> tuple[str, float] | None:
        bucket = self._buckets.get(norm_word[0])
        if not bucket:
            return None
        keys, canons = bucket
        matches = process.extract(
            norm_word, keys,
            scorer=JaroWinkler.normalized_similarity,
            score_cutoff=self._threshold,
            limit=10,
        )
        if not matches:
            return None
        # Prefer the candidate closest in LENGTH to the heard word, then by
        # score. This avoids picking a shorter near-name ("Srinivason" ->
        # "Srinivas") or truncating an absent name ("Brontwel" -> "Bronte").
        tol = 1 if len(norm_word) < 9 else 2
        best = min(
            matches,
            key=lambda m: (abs(len(m[0]) - len(norm_word)), -m[1]),
        )
        choice, score, idx = best
        if abs(len(choice) - len(norm_word)) > tol:
            return None
        return canons[idx], score

    def correct(
        self,
        text: str,
        confidences: list[float] | None = None,
        vocab_terms: Optional[list[str]] = None,
    ) -> str:
        if not text or not text.strip() or not self._buckets:
            return text

        words = text.split()
        confs = confidences if (confidences and len(confidences) == len(words)) else None
        protected = {t.lower() for t in (vocab_terms or [])}

        out: list[str] = []
        for i, tok in enumerate(words):
            m = _CORE_RE.search(tok)
            if not m:
                out.append(tok); continue
            core = m.group(0)
            nk = _norm(core)
            # --- eligibility gates ---
            eligible = (
                len(nk) >= MIN_LEN
                and nk not in self._common               # not a common English word
                and core.lower() not in protected         # not app jargon
                and nk not in {_norm(p) for p in protected}
            )
            if eligible:
                if confs is not None:
                    eligible = confs[i] < self._conf_threshold
                elif self._cap_fallback:
                    # No confidence available: only act on capitalized tokens
                    # that aren't sentence-initial (a name signal in raw ASR).
                    eligible = core[:1].isupper() and i > 0
                else:
                    eligible = False
            if not eligible:
                out.append(tok); continue

            best = self._best_match(nk)
            if best and _norm(best[0]) != nk:
                canon = best[0]
                s, e = m.span()
                out.append(tok[:s] + canon + tok[e:])
            else:
                out.append(tok)
        return " ".join(out)
