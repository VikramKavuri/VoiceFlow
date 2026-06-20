# Third-Party Licenses

VoiceFlow's own source is MIT-licensed (see [LICENSE](LICENSE)). The models it
downloads and the major libraries it builds on carry their own licenses:

## Models
- **Parakeet TDT 0.6B v3 (ONNX)** — repackaged from NVIDIA NeMo Parakeet. Subject to the
  upstream model license; see https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx.
- **Llama 3.2 3B (finetuned, GGUF)** — derived from Meta's Llama 3.2, governed by the
  **Llama 3.2 Community License** (https://www.llama.com/llama3_2/license/). The finetune is
  distributed at https://huggingface.co/VikramKavur/voiceflow-corrector-llama-3.2-3b-gguf.
- **LibriSpeech 3-gram LM** — from OpenSLR (https://www.openslr.org/11/), CC BY 4.0.

## Key libraries
- Tauri (MIT/Apache-2.0), React (MIT), onnxruntime (MIT), onnx-asr (MIT),
  silero-vad (MIT), llama-cpp-python (MIT), KenLM/arpa (LGPL/MIT).

Each library retains its own license; this list is informational, not exhaustive.
