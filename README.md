<div align="center">

# 🎙️ VoiceFlow

### Private, offline voice-to-text for Windows — **3.6% word error rate, 100% on-device**

Press **`Ctrl + Shift + Space`**, speak, and your words are typed into whatever app you're using —
cleaned up by a **finetuned on-device LLM**. No cloud, no API keys, no audio or text ever leaves your machine.

[![Build](https://github.com/VikramKavuri/VoiceFlow/actions/workflows/release.yml/badge.svg)](https://github.com/VikramKavuri/VoiceFlow/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/VikramKavuri/VoiceFlow?display_name=tag&sort=semver)](https://github.com/VikramKavuri/VoiceFlow/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D6?logo=windows)
![Built with](https://img.shields.io/badge/Tauri%202%20%C2%B7%20Rust%20%C2%B7%20Python%20%C2%B7%20React-informational)

[**⬇ Download**](https://github.com/VikramKavuri/VoiceFlow/releases/latest) ·
[**How it works**](docs/ARCHITECTURE.md) ·
[**Build from source**](CONTRIBUTING.md)

</div>

---

## 📹 Demo

<!-- To add the demo: record yourself pressing Ctrl+Shift+Space and dictating into Notepad
     (e.g. with ScreenToGif), save it as docs/screenshots/demo.gif, then uncomment the line below. -->
<!-- ![VoiceFlow demo](docs/screenshots/demo.gif) -->

> _Demo GIF coming soon — press the hotkey, talk, watch it type._ See
> [`docs/screenshots/`](docs/screenshots/) for how to add one.

---

## Why VoiceFlow

Cloud dictation tools send your voice to someone else's servers — a non-starter for clinicians,
lawyers, and anyone handling sensitive notes. VoiceFlow runs the **entire** speech pipeline on
your own machine: a state-of-the-art ASR model, a language-model rescorer, and a **finetuned LLM
corrector** — yet still types in real time. It's the accuracy of a cloud service with the privacy
of a local app.

## Features

- 🎙️ **Global hotkey** — `Ctrl + Shift + Space` to dictate into any app, anywhere.
- 🔒 **100% offline & private** — zero network calls at runtime; nothing written to disk.
- 🧠 **AI post-processing** — a finetuned local LLM fixes punctuation, casing, homophones,
  word boundaries, and spoken numbers (**~3.6% word error rate** on real speech, down from ~11% raw).
- ⚡ **Live transcription** — partial results stream as you talk.
- 🖥️ **Lives in your tray** — starts on login, always one shortcut away.

## 🛠️ Engineering highlights

> The interesting part isn't the UI — it's making a full speech-to-text + LLM stack run locally,
> in real time, accurately.

- **Polyglot desktop architecture** — a **Rust (Tauri 2)** shell, a **Python** ML sidecar, and a
  **React/TypeScript** UI, wired together over JSON-lines IPC on stdin/stdout.
- **Real-time streaming ASR** — incremental decoding with a *LocalAgreement-2* commit strategy,
  plus a full-session re-decode over **overlapping chunks** to recover words dropped at boundaries.
- **On-device LLM correction with guardrails** — a finetuned **Llama 3.2 3B** (LoRA SFT, merged +
  Q4_K_M) corrects the transcript, wrapped in a guardrail that **rejects hallucinations and
  over-deletion** and falls back to raw ASR. Finetuning cut word error rate from ~11% → **3.6%**.
- **Privacy by construction** — audio and text stay in memory, logs go only to stderr, and the
  HuggingFace/Torch stacks are forced offline at runtime (HIPAA-friendly posture).
- **Production packaging** — models resolve from a writable per-user dir, a first-run downloader
  fetches them once with a progress UI, and the Windows installer is **built in CI** on each tag.

## Download & install

1. Go to the [**Releases**](https://github.com/VikramKavuri/VoiceFlow/releases/latest) page and
   download the latest `VoiceFlow-Setup.exe`.
2. Run it → **Next → Next → Finish**.
3. On first launch, VoiceFlow downloads its models (~2.6 GB, one time). After that it works fully
   offline.
4. Press **`Ctrl + Shift + Space`** in any text field and start talking.

> Windows 10/11 (64-bit). ~3 GB free disk space for models.

## Build from source

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```powershell
pnpm install
./scripts/setup.ps1        # installs deps + downloads models
pnpm tauri dev             # run in development
pnpm tauri build           # produce an installer
```

## Privacy

VoiceFlow is designed to be safe for sensitive dictation:

- Audio and transcripts are kept **in memory only** — never written to disk.
- **No outbound network calls** during transcription (`HF_HUB_OFFLINE=1`).
- The only network use is the **one-time model download** at first run.

## How it works

```
Audio → VAD → Parakeet ASR → post-processing → LM rescorer → finetuned LLM corrector → types into your app
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full pipeline.

## Models & licenses

VoiceFlow downloads three models at setup. They keep their own licenses — see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md):

- **Parakeet TDT 0.6B v3** (ONNX) — speech recognition.
- **Llama 3.2 3B (finetuned, GGUF)** — transcript correction
  ([model card](https://huggingface.co/VikramKavur/voiceflow-corrector-llama-3.2-3b-gguf)).
- **LibriSpeech 3-gram LM** — homophone rescoring.

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). VoiceFlow is MIT-licensed (see
[LICENSE](LICENSE)).
