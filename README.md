<div align="center">

# 🎙️ VoiceFlow

### Private, offline - HIPAA Compliant voice-to-text for Windows — **3.6% word error rate, 100% on-device**

Press **`Ctrl + Shift + Space`**, speak, and your words are typed into whatever app you're using —
cleaned up by a **finetuned on-device LLM**. No cloud, no API keys, no audio or text ever leaves your machine.

[![Build](https://github.com/VikramKavuri/VoiceFlow/actions/workflows/release.yml/badge.svg)](https://github.com/VikramKavuri/VoiceFlow/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/VikramKavuri/VoiceFlow?display_name=tag&sort=semver)](https://github.com/VikramKavuri/VoiceFlow/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D6?logo=windows)
![Built with](https://img.shields.io/badge/Tauri%202%20%C2%B7%20Rust%20%C2%B7%20Python%20%C2%B7%20React-informational)

[**⬇ Download**](https://github.com/VikramKavuri/VoiceFlow/releases/latest) ·
[**How it works**](#-what-happens-when-you-speak) ·
[**Accuracy story**](#-how-the-accuracy-was-achieved) ·
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

## Contents

- [What VoiceFlow aims to do](#what-voiceflow-aims-to-do)
- [Privacy & HIPAA-conscious design](#-privacy--hipaa-conscious-design)
- [What happens when you speak](#-what-happens-when-you-speak)
- [Which model runs, and when](#-which-model-runs-and-when)
- [How the accuracy was achieved](#-how-the-accuracy-was-achieved)
- [Engineering highlights](#️-engineering-highlights)
- [Download & install](#download--install)
- [Build from source](#build-from-source)
- [Models & licenses](#models--licenses)

---

## What VoiceFlow aims to do

Cloud dictation tools send your voice to someone else's servers — a non-starter for clinicians,
lawyers, and anyone handling sensitive notes. VoiceFlow sets out to prove you can have **all three**
at once:

1. **Privacy** — the *entire* speech pipeline runs on your machine; nothing is transmitted or stored.
2. **Accuracy** — state-of-the-art ASR plus a **finetuned LLM corrector** (≈3.6% word error rate).
3. **Frictionless UX** — one global hotkey, types into any app, lives in your tray, starts on login.

The rest of this README explains, concretely, how each of those is achieved — and is honest about
what was hard and what changed along the way.

---

## 🔒 Privacy & HIPAA-conscious design

VoiceFlow is built for dictating **PHI-adjacent** material (clinical notes, legal matters). The
design goal is simple: **PHI is never transmitted and never written to disk.** Concretely:

| Safeguard | How it's enforced | Where |
|---|---|---|
| **No data leaves the machine** | The whole pipeline (ASR, LM, LLM) runs locally. No telemetry, no API calls. | entire app |
| **No network at runtime** | HuggingFace/Torch stacks are forced offline: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. | `sidecar/main.py` |
| **Nothing written to disk** | Audio and transcripts live **in memory only**; logs go to **stderr**, never files. | sidecar pipeline |
| **Diagnostic logging OFF by default** | The optional per-session debug log is **disabled** so transcripts are never persisted. | `sidecar/config.py` |
| **Buffers zeroed** | Raw audio buffers are explicitly zeroed on stop/shutdown, not just dropped. | sidecar pipeline |
| **Network used once, transparently** | The *only* network access is the one-time model download at first run — never during transcription. | `sidecar/model_setup.py` |

**An honest note on "HIPAA compliant."** HIPAA compliance is ultimately a property of your
organization's policies, Business Associate Agreements, access controls, and physical environment —
**not of any single piece of software.** What VoiceFlow gives you is a tool that, *by construction*,
does not transmit or store PHI, removing the largest technical risks. Operating it compliantly
(device security, who has access, etc.) is still on the deployer. We'd rather state that plainly
than slap on a badge we can't back.

---

## 🗣️ What happens when you speak

There are **two parallel paths** off a single microphone stream — a throwaway **live-preview** path
(so you see words instantly) and an authoritative path that produces the **delivered** text.

```
                                  ┌─────────────── LIVE PREVIEW (throwaway) ───────────────┐
  Mic ─► capture (resample 16kHz, ├─► high-pass ─► Silero VAD ─► Parakeet partial decode ─► LocalAgreement-2 ─► overlay
        clip overflow) ──────────┤                                                          (~every 0.5s)
        │  writes to ▼            └─────────────────────────────────────────────────────────────────────────────────┘
        │   Session buffer  ◄── the single source of truth for the final text
        │        │
        │  on stop ▼
        └─► reconcile_full_session: slice into 12s windows w/ 4s overlap
                 ► per window: loudness-normalize ─► Parakeet decode ─► stitch (seam de-dup + LM n-best)
                       ► CORRECTION CHAIN (below) ─► inject into the focused app (clipboard + paste)
```

**Why two paths?** Streaming a partial decode every half-second feels instant, but partial decodes
are unreliable at the edges. So the preview is *discarded*; the text you actually get is re-decoded
from the full-session buffer at the end, where the model has complete context.

**The correction chain** (run on the stitched transcript, in this exact order — see
[`sidecar/main.py` `_format_final_text`](sidecar/main.py)):

1. **Confidence-gated name repair** — low-confidence, name-like words are swapped for the closest
   real name (`"Gianluk"` → `"Gianluca"`). Runs *first*, while per-word ASR confidences still line
   up 1:1 with the words.
2. **Finetuned LLM correction** — the whole transcript is rewritten by a finetuned Llama 3.2 3B to
   fix homophones, word boundaries, punctuation, casing, and spoken numbers — wrapped in a
   **guardrail** that rejects the output (and falls back to raw ASR) if it hallucinates or deletes
   too much.
3. **LM rescoring** — a KenLM 3-gram resolves remaining homophones/confusables (their/there).
4. **Deterministic cleanup** — non-lexical fillers (um/uh), false starts, and repetitions removed;
   punctuation fixed.
5. **Safe number normalization** — spoken years and explicit thousands → digits (kept narrow to
   avoid mangling currency/times).
6. **Vocabulary + name casing** — custom terms and proper-noun capitalization.

---

## 🧩 Which model runs, and when

| Stage | Model / engine | Quant / size | When it runs |
|---|---|---|---|
| Speech/silence detection | **Silero VAD** (Torch) | small | continuously, both paths |
| Speech recognition (ASR) | **Parakeet TDT 0.6B v3** (ONNX) | int8, ~640 MB | live partials **and** final re-decode |
| Homophone rescoring | **KenLM 3-gram** (pure-Python `arpa` fallback) | ~38 MB | final chain (+ confirmed preview text) |
| Transcript correction | **Finetuned Llama 3.2 3B** (llama.cpp) | Q4_K_M, ~1.9 GB | **once**, at end of recording, on the full transcript |

The heavy LLM pass deliberately runs **once at the end** (not per word), so live typing stays
responsive while the delivered text still gets full-context correction.

---

## 📈 How the accuracy was achieved

The headline number (**3.6% WER**) was *not* the first result — it came from several iterations, and
some of them failed instructively. The story, with real measurements (the rationale for each is
documented inline in [`sidecar/config.py`](sidecar/config.py)):

**Baseline — raw ASR (~11% WER).** Parakeet alone is strong but makes the predictable mistakes:
homophones, word-boundary splits (`"data base"`), missing punctuation, and silence hallucinations.

**Iteration 1 — the corrector that secretly did nothing.** The original design added a context-aware
LLM corrector and an LM rescorer. An audit ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and the
project's accuracy map) found that on CPU **both were effectively inert**: the LLM call timed out
(>600 s) and *silently* fell back to raw text, and KenLM wasn't even installed. Lesson: a fallback
that's invisible is worse than no fallback. → made the corrector run **in-process** (llama.cpp with a
trimmed prompt instead of reloading a CLI per call) and made failures visible.

**Iteration 2 — finetuning, and a counter-intuitive regression.** A finetuned Llama 3.2 3B (LoRA SFT
on ~3.3k transcript→correction pairs, merged + Q4_K_M) *should* have helped — but with the original
**verbose few-shot prompt it scored 14.4% WER, worse than raw.** The heavy prompt pulled the
finetuned model back toward generic instruct behavior and erased the finetuning. Swapping the `.gguf`
alone was not enough.

**Iteration 3 — match the prompt to the training, relax the guardrail.** Two changes unlocked the
model:
- **Minimal prompt.** The model was finetuned on a bare `transcript → corrected` format, so it's fed
  a minimal prompt. *Measured: 14.4% (full prompt) → **3.6%** (native prompt).*
- **Relaxed the guardrail's retention floor (0.85 → 0.50).** The strict floor was *rejecting the
  model's best fixes* — legitimately merging `"data base"` → `"database"` drops content-word
  retention and tripped the guard. *Measured: strict floor caps at 13.0%; relaxed floor recovers
  **3.6%**.* The growth cap still guards against hallucination.

**Supporting layers.** On top of the corrector: confidence-gated name repair, KenLM homophone
rescoring, seam-aware de-dup at chunk boundaries, and audio fixes (int16-overflow clipping,
loudness/high-pass on the *final* decode path, not just the preview).

> **Honest scope:** 3.6% is the word error rate on the project's internal correction-evaluation
> harness, not a universal benchmark — treat it as "how much the corrector improves *this* pipeline's
> output," and reproduce it with the scripts under `scripts/`.

---

## 🛠️ Engineering highlights

- **Polyglot desktop architecture** — a **Rust (Tauri 2)** shell, a **Python** ML sidecar, and a
  **React/TypeScript** UI, wired together over JSON-lines IPC on stdin/stdout.
- **Real-time streaming ASR** — incremental decoding with a *LocalAgreement-2* commit strategy, plus
  a full-session re-decode over **overlapping 12 s windows** to recover words dropped at boundaries.
- **On-device LLM with guardrails** — finetuned Llama 3.2 3B correction behind a diff guardrail that
  rejects hallucination/over-deletion; **11% → 3.6% WER** (see above).
- **Privacy by construction** — in-memory only, stderr-only logging, runtime offline-enforced.
- **Production packaging** — models resolve from a writable per-user dir, a first-run downloader
  fetches them once with a progress UI, and the Windows installer is **built in CI** on each tag.

---

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

Full pipeline details live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
