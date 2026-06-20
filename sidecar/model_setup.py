"""Download the 3 production models into ``model_paths.models_root()``.

Shared by ``scripts/download_models.py`` (dev) and ``main.py --setup``
(end-user, frozen). All sources are public direct-download URLs so progress
is reportable and no extra deps are required.
"""

from __future__ import annotations

import gzip
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from model_paths import models_root

ProgressCb = Callable[[dict], None]

# Hugging Face serves raw files at /<repo>/resolve/main/<path>.
_PARAKEET = "https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/main"
_CORRECTOR = (
    "https://huggingface.co/VikramKavur/voiceflow-corrector-llama-3.2-3b-gguf/"
    "resolve/main/llama-3.2-3b-finetuned.Q4_K_M.gguf"
)
_LM = "https://www.openslr.org/resources/11/3-gram.pruned.3e-7.arpa.gz"


@dataclass(frozen=True)
class ModelFile:
    name: str          # path relative to models_root(), forward slashes
    url: str
    gunzip: bool = False
    min_bytes: int = 1024   # treat smaller existing files as incomplete
    label: str = ""         # human-friendly group name for progress UI

    def dest(self) -> Path:
        return models_root() / Path(self.name)


MODEL_FILES: list[ModelFile] = [
    ModelFile("parakeet-tdt-0.6b-v3-onnx/encoder-model.int8.onnx",
              f"{_PARAKEET}/encoder-model.int8.onnx", min_bytes=10_000_000, label="Speech model"),
    ModelFile("parakeet-tdt-0.6b-v3-onnx/decoder_joint-model.int8.onnx",
              f"{_PARAKEET}/decoder_joint-model.int8.onnx", min_bytes=1_000_000, label="Speech model"),
    ModelFile("parakeet-tdt-0.6b-v3-onnx/vocab.txt",
              f"{_PARAKEET}/vocab.txt", min_bytes=100, label="Speech model"),
    ModelFile("parakeet-tdt-0.6b-v3-onnx/config.json",
              f"{_PARAKEET}/config.json", min_bytes=10, label="Speech model"),
    ModelFile("llama-3.2-3b-finetuned-q4_k_m/llama-3.2-3b-finetuned.Q4_K_M.gguf",
              _CORRECTOR, min_bytes=1_000_000_000, label="Corrector model"),
    ModelFile("lm/3gram-pruned.arpa",
              _LM, gunzip=True, min_bytes=1_000_000, label="Language model"),
]


def missing_models() -> list[ModelFile]:
    out = []
    for f in MODEL_FILES:
        d = f.dest()
        if not d.exists() or d.stat().st_size < f.min_bytes:
            out.append(f)
    return out


def _emit(progress: Optional[ProgressCb], **kw) -> None:
    if progress:
        progress(dict(kw))


def _download_one(f: ModelFile, progress: Optional[ProgressCb]) -> None:
    dest = f.dest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    _emit(progress, event="model_start", name=f.name, label=f.label)

    def hook(block_num: int, block_size: int, total_size: int) -> None:
        done = block_num * block_size
        pct = (min(100.0, done * 100 / total_size) if total_size > 0 else 0.0)
        _emit(progress, event="model_progress", name=f.name, label=f.label,
              downloaded_mb=round(done / 1_000_000, 1),
              total_mb=round(total_size / 1_000_000, 1), pct=round(pct, 1))

    if f.gunzip:
        gz_tmp = dest.with_suffix(dest.suffix + ".gz.part")
        urllib.request.urlretrieve(f.url, gz_tmp, hook)
        with gzip.open(gz_tmp, "rb") as src, tmp.open("wb") as out:
            shutil.copyfileobj(src, out)
        gz_tmp.unlink(missing_ok=True)
    else:
        urllib.request.urlretrieve(f.url, tmp, hook)

    tmp.replace(dest)
    _emit(progress, event="model_done", name=f.name, label=f.label)


def download_all(progress: Optional[ProgressCb] = None) -> None:
    todo = missing_models()
    _emit(progress, event="setup_start", total=len(todo))
    for i, f in enumerate(todo, 1):
        _emit(progress, event="setup_step", index=i, total=len(todo), name=f.name, label=f.label)
        _download_one(f, progress)
    _emit(progress, event="setup_done")
