# Architecture

VoiceFlow is a Tauri v2 desktop app with three layers:

1. **Rust shell (`src-tauri/`)** — owns the system tray, the global Ctrl+Shift+Space
   shortcut, the settings/overlay windows, first-run model setup, and the lifecycle of the
   Python sidecar. Talks to the sidecar over JSON-lines on stdin/stdout.
2. **React frontend (`src/`)** — the settings window, the recording overlay, and the
   one-time setup screen. Communicates with Rust via Tauri commands/events.
3. **Python sidecar (`sidecar/`)** — the speech pipeline.

## Pipeline
```
AudioCapture → VAD (silero) → ASR (Parakeet TDT v3, ONNX int8)
  → PostProcessor (normalization, ITN, vocabulary, name casing/matching)
  → LM rescorer (KenLM 3-gram, homophone fixes)
  → LLM corrector (finetuned Llama 3.2 3B, GGUF, via llama.cpp)
  → TextInjector (types into the focused app)
```

- **Streaming:** partial transcripts stream every ~0.5 s; a durable window commits
  authoritative text; on stop, the full session is re-decoded in overlapping chunks for the
  final result.
- **Models** live in `%LOCALAPPDATA%\VoiceFlow\models` (installed) or `sidecar/models`
  (dev), resolved by `sidecar/model_paths.py`. They are downloaded once by
  `sidecar/model_setup.py`.
- **Privacy:** audio/text stay in memory; logs go to stderr; `HF_HUB_OFFLINE=1` blocks
  network during transcription. The only network use is the one-time model download.
