import importlib
import sys

import pytest

onnxruntime = pytest.importorskip("onnxruntime", reason="onnxruntime not installed")


def test_asr_dir_follows_models_root(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEFLOW_MODELS_DIR", str(tmp_path))
    monkeypatch.delenv("VOICEFLOW_ASR_MODEL_DIR", raising=False)
    for m in ("model_paths", "asr_engine"):
        sys.modules.pop(m, None)
    asr_engine = importlib.import_module("asr_engine")
    eng = asr_engine.ASREngine(model_name="istupakov/parakeet-tdt-0.6b-v3-onnx")
    assert eng._model_dir == tmp_path.resolve() / "parakeet-tdt-0.6b-v3-onnx"
