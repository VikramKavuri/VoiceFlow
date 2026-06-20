"""Per-session diagnostic logger.

Writes a single text file per recording session showing what each pipeline
layer received and produced. Intended for manual debugging; OFF by default
because it writes to disk (HIPAA-relevant). Opt in via config.

File format is plain text with clear `===` section dividers and `[HH:MM:SS]
[layer]` event prefixes so the file opens cleanly in Notepad.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime
from difflib import unified_diff
from pathlib import Path
from typing import Optional, TextIO

logger = logging.getLogger(__name__)

_BAR = "=" * 72
_THIN = "-" * 72


class SessionDiagnosticLogger:
    """One instance writes one file per recording session.

    All public methods are no-ops while inactive (no `start_session()` call
    yet, or `enabled=False`), so wiring code can call them unconditionally.
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._output_dir: Path = self._default_dir()
        self._file: Optional[TextIO] = None
        self._lock = threading.Lock()
        self._session_start: Optional[datetime] = None
        self._current_path: Optional[Path] = None
        self._stage_counters: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def configure(self, enabled: bool, output_dir: Optional[str] = None) -> None:
        self._enabled = bool(enabled)
        if output_dir:
            self._output_dir = Path(output_dir).expanduser()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def output_path(self) -> Optional[Path]:
        return self._current_path

    def start_session(self, label: str = "") -> Optional[Path]:
        """Open a new diagnostic file. Returns its path, or None if disabled."""
        if not self._enabled:
            return None
        with self._lock:
            self._end_locked()
            try:
                self._output_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now()
                stamp = ts.strftime("%Y-%m-%d_%H%M%S")
                suffix = f"_{label}" if label else ""
                path = self._output_dir / f"session_{stamp}{suffix}.txt"
                self._file = path.open("w", encoding="utf-8")
                self._session_start = ts
                self._current_path = path
                self._stage_counters.clear()
                self._write_unlocked(_BAR)
                self._write_unlocked(f"# VoiceFlow Session Diagnostic Log")
                self._write_unlocked(f"# Started: {ts.isoformat(timespec='seconds')}")
                self._write_unlocked(f"# Path:    {path}")
                self._write_unlocked(_BAR)
                self._file.flush()
                logger.info("Diagnostic log started: %s", path)
                return path
            except Exception:
                logger.exception("Diagnostic log start failed")
                self._file = None
                self._current_path = None
                return None

    def end_session(self) -> None:
        with self._lock:
            self._end_locked()

    def _end_locked(self) -> None:
        if self._file is None:
            return
        try:
            ended = datetime.now()
            dur = (ended - self._session_start).total_seconds() if self._session_start else 0.0
            self._write_unlocked("")
            self._write_unlocked(_BAR)
            self._write_unlocked(f"# Session ended: {ended.isoformat(timespec='seconds')}  ({dur:.1f}s)")
            self._write_unlocked(_BAR)
            self._file.flush()
            self._file.close()
        except Exception:
            logger.exception("Diagnostic log end failed")
        finally:
            self._file = None
            self._current_path = None
            self._session_start = None

    # ------------------------------------------------------------------
    # Writers (all safe to call when disabled or before start_session)
    # ------------------------------------------------------------------

    def section(self, title: str) -> None:
        if not self._enabled or self._file is None:
            return
        with self._lock:
            self._write_unlocked("")
            self._write_unlocked(_BAR)
            self._write_unlocked(f"# {title}")
            self._write_unlocked(_BAR)
            self._file.flush()

    def subsection(self, title: str) -> None:
        if not self._enabled or self._file is None:
            return
        with self._lock:
            self._write_unlocked("")
            self._write_unlocked(_THIN)
            self._write_unlocked(f"# {title}")
            self._write_unlocked(_THIN)
            self._file.flush()

    def event(self, layer: str, message: str, **kv: object) -> None:
        if not self._enabled or self._file is None:
            return
        with self._lock:
            ts = self._timestamp()
            kv_str = self._fmt_kv(kv)
            line = f"[{ts}] [{layer}] {message}"
            if kv_str:
                line += f"  | {kv_str}"
            self._write_unlocked(line)
            self._file.flush()

    def chunk(
        self,
        layer: str,
        stage: str,
        text: str,
        **kv: object,
    ) -> None:
        """Log a chunk with verbatim text. ``stage`` becomes the chunk index."""
        if not self._enabled or self._file is None:
            return
        idx = self._stage_counters.get(stage, 0) + 1
        self._stage_counters[stage] = idx
        with self._lock:
            ts = self._timestamp()
            kv_str = self._fmt_kv(kv)
            header = f"[{ts}] [{layer}] {stage} #{idx}"
            if kv_str:
                header += f"  | {kv_str}"
            self._write_unlocked("")
            self._write_unlocked(header)
            self._write_unlocked(f"  text: {text!r}")
            self._file.flush()

    def diff(self, layer: str, before: str, after: str, label: str = "") -> None:
        """Log a before/after text transformation."""
        if not self._enabled or self._file is None:
            return
        with self._lock:
            ts = self._timestamp()
            tag = f"[{ts}] [{layer}]"
            if label:
                tag += f" {label}"
            self._write_unlocked("")
            if before == after:
                self._write_unlocked(f"{tag}  (no change, {len(before)} chars)")
                self._write_unlocked(f"  text  : {before!r}")
            else:
                self._write_unlocked(f"{tag}  CHANGED  ({len(before)}→{len(after)} chars)")
                self._write_unlocked(f"  before: {before!r}")
                self._write_unlocked(f"  after : {after!r}")
                if len(before) + len(after) < 600:
                    diff_lines = list(
                        unified_diff(
                            before.splitlines() or [""],
                            after.splitlines() or [""],
                            n=0, lineterm="",
                        )
                    )
                    # Drop the first 2 header lines from unified_diff
                    rest = [l for l in diff_lines[2:] if l]
                    if rest:
                        self._write_unlocked("  diff  :")
                        for line in rest:
                            self._write_unlocked(f"    {line}")
            self._file.flush()

    def note(self, layer: str, text: str) -> None:
        """Free-form multiline note under a layer (useful for prompts, etc)."""
        if not self._enabled or self._file is None:
            return
        with self._lock:
            ts = self._timestamp()
            self._write_unlocked("")
            self._write_unlocked(f"[{ts}] [{layer}] note")
            for line in text.splitlines() or [text]:
                self._write_unlocked(f"  | {line}")
            self._file.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _timestamp(self) -> str:
        now = datetime.now()
        elapsed = ""
        if self._session_start is not None:
            secs = (now - self._session_start).total_seconds()
            elapsed = f" +{secs:6.2f}s"
        return now.strftime("%H:%M:%S.%f")[:-3] + elapsed

    @staticmethod
    def _fmt_kv(kv: dict) -> str:
        parts = []
        for k, v in kv.items():
            if isinstance(v, float):
                parts.append(f"{k}={v:.3f}")
            else:
                parts.append(f"{k}={v}")
        return " ".join(parts)

    def _write_unlocked(self, line: str) -> None:
        if self._file is None:
            return
        try:
            self._file.write(line + "\n")
        except Exception:
            logger.debug("Diagnostic write failed", exc_info=True)

    @staticmethod
    def _default_dir() -> Path:
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        else:
            base = Path(__file__).resolve().parent.parent  # voiceflow/
        return base / "diagnostics"


# Module-level singleton — import this everywhere
diag = SessionDiagnosticLogger()
