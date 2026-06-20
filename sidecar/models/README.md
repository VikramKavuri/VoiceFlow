# Models

Model files are **not** committed. They are downloaded automatically:

- **End users:** on first launch, into `%LOCALAPPDATA%\VoiceFlow\models`.
- **Developers:** run `python scripts/download_models.py` (or `scripts/setup.ps1`) to
  populate this folder.

Downloaded models:
| Folder | Model | Source |
|---|---|---|
| `parakeet-tdt-0.6b-v3-onnx/` | Parakeet TDT 0.6B v3 (int8 ONNX) | Hugging Face `istupakov/...` |
| `llama-3.2-3b-finetuned-q4_k_m/` | Finetuned corrector (Q4_K_M GGUF) | Hugging Face `VikramKavur/...` |
| `lm/3gram-pruned.arpa` | LibriSpeech pruned 3-gram | OpenSLR |
