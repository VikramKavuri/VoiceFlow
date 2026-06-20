import importlib
import sys


def _mod(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEFLOW_MODELS_DIR", str(tmp_path))
    for m in ("model_paths", "model_setup"):
        sys.modules.pop(m, None)
    return importlib.import_module("model_setup")


def test_specs_cover_three_models(monkeypatch, tmp_path):
    ms = _mod(monkeypatch, tmp_path)
    names = {f.name for f in ms.MODEL_FILES}
    assert "parakeet-tdt-0.6b-v3-onnx/encoder-model.int8.onnx" in names
    assert any("llama-3.2-3b-finetuned" in n for n in names)
    assert any("3gram-pruned.arpa" in n for n in names)


def test_missing_models_when_nothing_downloaded(monkeypatch, tmp_path):
    ms = _mod(monkeypatch, tmp_path)
    assert len(ms.missing_models()) == len(ms.MODEL_FILES)


def test_missing_models_skips_present_files(monkeypatch, tmp_path):
    ms = _mod(monkeypatch, tmp_path)
    spec = ms.MODEL_FILES[0]
    dest = ms.models_root() / spec.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * (spec.min_bytes + 1))
    assert spec not in ms.missing_models()
