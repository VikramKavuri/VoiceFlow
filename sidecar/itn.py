"""Lightweight inverse text normalization for common dictation patterns."""

from __future__ import annotations

import re


_MONTHS = {
    "january": "January",
    "february": "February",
    "march": "March",
    "april": "April",
    "may": "May",
    "june": "June",
    "july": "July",
    "august": "August",
    "september": "September",
    "october": "October",
    "november": "November",
    "december": "December",
}

_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty first": 21,
    "twenty second": 22,
    "twenty third": 23,
    "twenty fourth": 24,
    "twenty fifth": 25,
    "twenty sixth": 26,
    "twenty seventh": 27,
    "twenty eighth": 28,
    "twenty ninth": 29,
    "thirtieth": 30,
    "thirty first": 31,
}

_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}

_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# Century-leading words that plausibly start a spoken year (1700-2099).
_YEAR_CENTURY = {"seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}

# Number words that, when they immediately precede "two thousand", mean the
# "two" is part of a larger magnitude ("thirty-two thousand", "twenty two
# thousand") rather than the standalone 2000+N the safe rule converts.
_COMPOUND_GUARD = set(_ONES) | set(_TENS) | {"hundred", "thousand", "million", "billion"}


class InverseTextNormalizer:
    """Small deterministic ITN pass for values users visibly care about."""

    def normalize(self, text: str) -> str:
        if not text:
            return text
        text = self._normalize_emails(text)
        text = self._normalize_urls(text)
        text = self._normalize_times(text)
        text = self._normalize_dates(text)
        text = self._normalize_currency(text)
        return re.sub(r" {2,}", " ", text).strip()

    def normalize_numbers(self, text: str) -> str:
        """Conservative number→digit pass: spoken YEARS and explicit thousands.

        This is the safe subset of ITN — it deliberately avoids the times/dates/
        currency rules that over-trigger (e.g. "two three" → "2:03"). It only
        fires on patterns that are unambiguously large numbers:

          * "two thousand [and] N"            -> 2000+N   ("two thousand five" -> 2005)
          * CENTURY + tens [ones]             -> year     ("twenty sixty five" -> 2065)
          * CENTURY + teen                    -> year     ("twenty thirteen"   -> 2013)
          * CENTURY + "hundred"               -> year     ("nineteen hundred"  -> 1900)
          * CENTURY + ("oh"|"o") + ones       -> year     ("nineteen oh five"  -> 1905)

        Bare cardinals like "twenty five" are left untouched (the second word
        must be a TENS/teen/hundred), so this cannot turn 25 into 2005.
        """
        if not text:
            return text
        # Order matters: times/percent consume their marker words first, then
        # the year/thousand passes, then bare digit-runs collapse last (so a
        # multi-word year like "nineteen eighty four" is already a single token
        # and is never mistaken for a digit sequence).
        text = self._normalize_times_marked(text)
        text = self._normalize_percent(text)
        text = self._normalize_two_thousand(text)
        text = self._normalize_years(text)
        text = self._normalize_digit_sequences(text)
        return re.sub(r" {2,}", " ", text).strip()

    # ------------------------------------------------------------------
    # Safe extensions (part of the conservative number pass)
    # ------------------------------------------------------------------

    def _normalize_times_marked(self, text: str) -> str:
        """Spoken clock times -> "H:MM AM/PM", ONLY when an am/pm marker is present.

        Requiring the marker is what makes this safe: bare pairs like "two three"
        or "meet at three thirty" (no marker) are left untouched, avoiding the
        over-triggering that kept the full time rule disabled.
        """
        hours = sorted((w for w, v in _ONES.items() if 1 <= v <= 12), key=len, reverse=True)
        ones19 = sorted((w for w, v in _ONES.items() if 1 <= v <= 9), key=len, reverse=True)
        teens = sorted((w for w, v in _ONES.items() if 10 <= v <= 19), key=len, reverse=True)
        mtens = ["twenty", "thirty", "forty", "fifty"]

        minute_pat = (
            rf"(?:(?P<oh>oh|o)\s+(?P<ohn>{'|'.join(ones19)})"
            rf"|(?P<teen>{'|'.join(teens)})"
            rf"|(?P<tens>{'|'.join(mtens)})(?:\s+(?P<tones>{'|'.join(ones19)}))?)"
        )
        pattern = (
            rf"\b(?P<hour>{'|'.join(hours)})"
            rf"(?:\s+{minute_pat})?"
            rf"\s+(?P<mer>a\.?m\.?|p\.?m\.?)(?!\w)"
        )

        def repl(m: re.Match[str]) -> str:
            hour = _ONES[m.group("hour").lower()]
            mer = "AM" if m.group("mer").lower().startswith("a") else "PM"
            if m.group("oh") is not None:
                minute = _ONES[m.group("ohn").lower()]
            elif m.group("teen"):
                minute = _ONES[m.group("teen").lower()]
            elif m.group("tens"):
                minute = _TENS[m.group("tens").lower()]
                if m.group("tones"):
                    minute += _ONES[m.group("tones").lower()]
            else:
                return f"{hour} {mer}"
            return f"{hour}:{minute:02d} {mer}"

        return re.sub(pattern, repl, text, flags=re.I)

    def _normalize_percent(self, text: str) -> str:
        """"seven point five percent" -> "7.5%"; "seventy five percent" -> "75%".

        Only fires when a parseable number phrase immediately precedes the word
        "percent"; a bare "percent" with no number is left alone.
        """
        tokens = text.split()
        out: list[str] = []
        for tok in tokens:
            bare = tok.lower().strip(".,!?;:")
            if bare == "percent":
                num = self._pop_percent_number(out)
                if num is not None:
                    trailing = tok[len(tok.rstrip(".,!?;:")):]
                    out.append(f"{num}%{trailing}")
                    continue
            out.append(tok)
        return " ".join(out)

    def _pop_percent_number(self, out: list[str]) -> str | None:
        """Pop the trailing number phrase from *out*, return it formatted, or None.

        Handles an integer (up to hundreds) with an optional "point D D" decimal
        tail. Mutates *out* in place only when a valid number is found.
        """
        number_parts = set(_ONES) | set(_TENS) | {"hundred", "point"}
        # Collect the maximal trailing run of number-component words.
        run_len = 0
        for tok in reversed(out):
            if tok.lower().strip(".,!?;:") in number_parts:
                run_len += 1
            else:
                break
        if run_len == 0:
            return None
        run = [t.lower().strip(".,!?;:") for t in out[len(out) - run_len:]]

        if "point" in run:
            idx = run.index("point")
            int_words, dec_words = run[:idx], run[idx + 1:]
            int_val = self._int_from_words(int_words) if int_words else 0
            digits = [str(_ONES[w]) for w in dec_words if w in _ONES and _ONES[w] <= 9]
            if int_val is None or not digits:
                return None
            value = f"{int_val}.{''.join(digits)}"
        else:
            int_val = self._int_from_words(run)
            if int_val is None:
                return None
            value = str(int_val)

        del out[len(out) - run_len:]
        return value

    @staticmethod
    def _int_from_words(words: list[str]) -> int | None:
        """Parse a small cardinal (ones/teens/tens/hundreds), no thousands."""
        total = current = 0
        seen = False
        for w in words:
            if w in _ONES:
                current += _ONES[w]
            elif w in _TENS:
                current += _TENS[w]
            elif w == "hundred":
                current = (current or 1) * 100
            else:
                return None
            seen = True
        return total + current if seen else None

    def _normalize_digit_sequences(self, text: str) -> str:
        """Collapse runs of >=4 single-digit words into a digit string.

        Phone numbers, passcodes and PINs are dictated digit-by-digit
        ("four one five ...") and should render as "415...". "oh"/"o" count as 0
        inside a run. The >=4 threshold keeps short cardinals ("one two three")
        and stray "oh"s untouched.
        """
        tokens = text.split()
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            digits: list[str] = []
            j = i
            while j < n:
                bare = tokens[j].lower().strip(".,!?;:")
                val = self._single_digit(bare)
                if val is None:
                    break
                digits.append(str(val))
                # A token carrying trailing punctuation ends the run.
                if tokens[j] != tokens[j].rstrip(".,!?;:"):
                    j += 1
                    break
                j += 1
            if len(digits) >= 4:
                last = tokens[j - 1]
                trailing = last[len(last.rstrip(".,!?;:")):]
                out.append("".join(digits) + trailing)
                i = j
            else:
                out.append(tokens[i])
                i += 1
        return " ".join(out)

    @staticmethod
    def _single_digit(word: str) -> int | None:
        if word in ("oh", "o"):
            return 0
        v = _ONES.get(word)
        return v if v is not None and 0 <= v <= 9 else None

    def _normalize_two_thousand(self, text: str) -> str:
        ones = "|".join(_ONES)          # includes teens (ten..nineteen)
        tens = "|".join(_TENS)

        def repl(m: re.Match[str]) -> str:
            s = m.string
            # Left guard: "thirty-two thousand" / "twenty two thousand" — the
            # leading "two" belongs to a bigger magnitude (32k), not 2000.
            prev = re.search(r"([A-Za-z]+)[\s-]*$", s[: m.start()])
            if prev and prev.group(1).lower() in _COMPOUND_GUARD:
                return m.group(0)
            # Right guard: "two thousand two hundred" — "X thousand Y hundred" is
            # a compound this simple rule can't compute; leave it as words.
            if re.match(r"\s+hundred\b", s[m.end():], re.I):
                return m.group(0)
            tail = (m.group("tail") or "").strip().lower()
            n = self._number(tail) if tail else 0
            if n is None:
                return m.group(0)
            return str(2000 + n)

        pattern = rf"\btwo\s+thousand(?:\s+and)?(?P<tail>(?:\s+(?:{tens}))?(?:\s+(?:{ones}))?)\b"
        return re.sub(pattern, repl, text, flags=re.I)

    def _normalize_years(self, text: str) -> str:
        century = "|".join(_YEAR_CENTURY)
        tens = "|".join(_TENS)
        ones = "|".join(k for k in _ONES if _ONES[k] >= 1 and _ONES[k] <= 9)  # one..nine
        teens = "|".join(k for k in _ONES if 10 <= _ONES[k] <= 19)            # ten..nineteen

        def repl(m: re.Match[str]) -> str:
            c = _YEAR_CENTURY[m.group("c").lower()]
            base = c * 100
            if m.group("hundred"):
                return str(base)
            if m.group("oh") is not None:
                return str(base + _ONES[m.group("ohones").lower()])
            if m.group("teen"):
                return str(base + _ONES[m.group("teen").lower()])
            tval = _TENS[m.group("tens").lower()]
            oval = _ONES[m.group("ones").lower()] if m.group("ones") else 0
            return str(base + tval + oval)

        pattern = (
            rf"\b(?P<c>{century})\s+(?:"
            rf"(?P<hundred>hundred)"
            rf"|(?P<oh>oh|o)\s+(?P<ohones>{ones})"
            rf"|(?P<teen>{teens})"
            rf"|(?P<tens>{tens})(?:\s+(?P<ones>{ones}))?"
            rf")\b"
        )
        return re.sub(pattern, repl, text, flags=re.I)

    def _normalize_urls(self, text: str) -> str:
        text = re.sub(r"\b([a-z0-9-]+)\s+dot\s+(com|org|net|io|ai|dev)\b", r"\1.\2", text, flags=re.I)
        text = re.sub(r"\s+slash\s+", "/", text, flags=re.I)
        text = re.sub(r"\s+dash\s+", "-", text, flags=re.I)
        return text

    def _normalize_emails(self, text: str) -> str:
        pattern = re.compile(
            r"\b([a-z0-9._%+-]+)\s+at\s+([a-z0-9.-]+)\s+dot\s+([a-z]{2,})\b",
            re.I,
        )
        return pattern.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)

    def _normalize_times(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            hour = self._number(match.group("hour"))
            minute = self._number(match.group("minute"))
            suffix = match.group("suffix")
            if hour is None or minute is None:
                return match.group(0)
            marker = f" {suffix.upper().replace('.', '')}" if suffix else ""
            return f"{hour}:{minute:02d}{marker}"

        number_words = "|".join(list(_ONES) + list(_TENS))
        return re.sub(
            rf"\b(?P<hour>{number_words})\s+(?P<minute>{number_words})(?:\s+(?P<suffix>a\.?m\.?|p\.?m\.?))?\b",
            repl,
            text,
            flags=re.I,
        )

    def _normalize_dates(self, text: str) -> str:
        ordinal_words = sorted(_ORDINALS, key=len, reverse=True)
        pattern = re.compile(
            rf"\b({'|'.join(_MONTHS)})\s+({'|'.join(map(re.escape, ordinal_words))})\b",
            re.I,
        )
        return pattern.sub(lambda m: f"{_MONTHS[m.group(1).lower()]} {_ORDINALS[m.group(2).lower()]}", text)

    def _normalize_currency(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            dollars = self._number(match.group("dollars"))
            cents = self._number(match.group("cents") or "")
            if dollars is None:
                return match.group(0)
            if cents is None:
                return f"${dollars}"
            return f"${dollars}.{cents:02d}"

        number_words = "|".join(list(_ONES) + list(_TENS))
        return re.sub(
            rf"\b(?P<dollars>{number_words})(?:\s+dollars?)?(?:\s+and\s+(?P<cents>{number_words})\s+cents?)\b",
            repl,
            text,
            flags=re.I,
        )

    def _number(self, words: str) -> int | None:
        parts = words.lower().split()
        if not parts:
            return None
        if len(parts) == 1:
            return _ONES.get(parts[0], _TENS.get(parts[0]))
        if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES:
            return _TENS[parts[0]] + _ONES[parts[1]]
        return None
