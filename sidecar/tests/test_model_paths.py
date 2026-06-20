import importlib
import sys
from pathlib import Path


def _fresh(monkeypatch, *, frozen, localappdata=None, override=None):
    monkeypatch.delenv("VOICEFLOW_MODELS_DIR", raising=False)
    if override is not None:
        monkeypatch.setenv("VOICEFLOW_MODELS_DIR", override)
    if localappdata is not None:
        monkeypatch.setenv("LOCALAPPDATA", localappdata)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    sys.modules.pop("model_paths", None)
    return importlib.import_module("model_paths")


def test_dev_mode_uses_sidecar_models_dir(monkeypatch):
    mp = _fresh(monkeypatch, frozen=False)
    assert mp.models_root().name == "models"
    assert mp.models_root().parent.name == "sidecar"
    assert mp.runtime_root() == mp.models_root().parent


def test_frozen_mode_uses_localappdata(monkeypatch, tmp_path):
    mp = _fresh(monkeypatch, frozen=True, localappdata=str(tmp_path))
    assert mp.models_root() == tmp_path / "VoiceFlow" / "models"


def test_override_wins(monkeypatch, tmp_path):
    mp = _fresh(monkeypatch, frozen=True, override=str(tmp_path / "custom"))
    assert mp.models_root() == (tmp_path / "custom").resolve()
