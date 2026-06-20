# VoiceFlow

**Private, offline voice-to-text for Windows.** Press **Ctrl + Shift + Space**, speak, and
your words are typed into whatever app you're using — with AI cleanup for punctuation,
homophones, and names. Everything runs locally. No audio or text ever leaves your machine.

> Built for people who dictate sensitive material (healthcare, legal, notes) and need a
> tool that is fast, accurate, and fully offline.

## Features

- 🎙️ **Global hotkey** — Ctrl + Shift + Space to dictate anywhere.
- 🔒 **100% offline & private** — no network calls at runtime; nothing written to disk.
- 🧠 **AI post-processing** — a finetuned local LLM fixes punctuation, casing, homophones,
  word boundaries, and spoken numbers (~3.6% word error rate on real speech).
- ⚡ **Live transcription** — partial results stream as you talk.
- 🖥️ **Lives in your tray** — starts on login, always one shortcut away.

## Download & install

1. Go to the [**Releases**](../../releases/latest) page and download the latest
   `VoiceFlow-Setup.exe`.
2. Run it → **Next → Next → Finish**.
3. On first launch, VoiceFlow downloads its models (~2.6 GB, one time). After that it
   works fully offline.
4. Press **Ctrl + Shift + Space** in any text field and start talking.

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

`Audio → VAD → Parakeet ASR → post-processing → finetuned LLM corrector → types into your app`.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full pipeline.

## Models & licenses

VoiceFlow downloads three models at setup. They keep their own licenses — see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md):

- **Parakeet TDT 0.6B v3** (ONNX) — speech recognition.
- **Llama 3.2 3B (finetuned, GGUF)** — transcript correction.
- **LibriSpeech 3-gram LM** — homophone rescoring.

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). VoiceFlow is MIT-licensed (see
[LICENSE](LICENSE)).
