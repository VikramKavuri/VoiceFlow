"""Safe, fast name capitalization from a large bundled name index.

Unlike the fuzzy ``VocabularyCorrector`` (which scans every term for every word
and can rewrite ordinary words into similar-looking names), this corrector does
ONE thing and does it safely:

  * It only fixes the CASING of a token the ASR already spelled correctly
    (e.g. "srinivasan" -> "Srinivasan"). It never changes letters, so it cannot
    turn a real word into a different name.
  * Common English words are excluded from the index at build time
    (see scripts/correction_eval/generate_name_index.py), so it will not
    capitalize "will", "grace", "case", "may", etc.
  * Lookups are O(1) dict hits, so a 90k-name index adds negligible latency.

This is the deliberate, conservative half of name handling. It does NOT fix
ASR mishearings ("O'Corner" -> "O'Connor"); that needs decode-time hotword
biasing (the ASR adapter does not expose it) or a small user vocabulary.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

# A token is a run of letters optionally containing apostrophes (O'Connor).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*")


class NameCasingCorrector:
    def __init__(self, index_path: str | None = None) -> None:
        self._index: dict[str, str] = {}
        path = (
            Path(index_path).expanduser()
            if index_path
            else Path(__file__).resolve().parent / "name_casing_index.txt"
        )
        if path.exists():
            self.load(str(path))

    def load(self, path: str) -> None:
        idx: dict[str, str] = {}
        p = Path(path).expanduser()
        if not p.exists():
            self._index = {}
            return
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            if "\t" not in line:
                continue
            key, canon = line.split("\t", 1)
            key = key.strip().lower()
            canon = canon.strip()
            # Invariant: the stored canonical must be a pure recasing of the key,
            # so applying it can never alter letters.
            if key and canon and key == canon.lower():
                idx[key] = canon
        self._index = idx

    @property
    def size(self) -> int:
        return len(self._index)

    def correct(self, text: str) -> str:
        if not text or not self._index:
            return text

        def repl(m: re.Match[str]) -> str:
            w = m.group(0)
            # Only act on fully lowercase tokens — never override a casing the
            # ASR/LLM already chose (sentence starts, acronyms, deliberate caps).
            if not w.islower():
                return w
            canon = self._index.get(w)
            if canon is None or canon == w or canon.lower() != w:
                return w
            return canon

        return _TOKEN_RE.sub(repl, text)
