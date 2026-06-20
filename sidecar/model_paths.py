"""Resolve where VoiceFlow's model files live.

Dev (running from source): models sit next to the sidecar package
(`sidecar/models/`). Installed (PyInstaller `frozen`): models live in a
writable per-user dir (`%LOCALAPPDATA%\\VoiceFlow\\models`) so the first-run
downloader can write them and they persist across launches.

`VOICEFLOW_MODELS_DIR` overrides everything (used by tests / CI / power users).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "VoiceFlow"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def models_root() -> Path:
    """Absolute path to the directory that contains the model subfolders."""
    override = os.environ.get("VOICEFLOW_MODELS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if _is_frozen():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(base) / APP_NAME / "models").resolve()
    return (Path(__file__).resolve().parent / "models").resolve()


def runtime_root() -> Path:
    """Parent of `models_root()` — base for config paths that include a
    leading ``models/`` (e.g. ``models/lm/3gram-pruned.arpa``)."""
    return models_root().parent
