# Contributing to VoiceFlow

## Prerequisites
- **Node 18+** and **pnpm** (`npm i -g pnpm`)
- **Rust** (stable) + the Tauri prerequisites for Windows
  (see https://tauri.app/start/prerequisites/)
- **Python 3.11 or 3.12** on PATH (3.13 works but `kenlm` falls back to the pure-Python
  `arpa` backend)

## One-time setup
```powershell
pnpm install
./scripts/setup.ps1   # installs frontend + Python deps and downloads ~2.6 GB of models
```

## Run in development
```powershell
pnpm tauri dev
```
In dev mode the Tauri shell runs the Python sidecar directly from `sidecar/main.py`.

## Build an installer
```powershell
./scripts/build-tauri.ps1     # builds the PyInstaller sidecar, then `pnpm tauri build`
```
The installer is written to `src-tauri/target/release/bundle/`.

## Tests
```powershell
pnpm test                                  # frontend (vitest)
cd sidecar && python -m pytest             # sidecar (pytest)
```

## Project layout
- `src/` — React + TypeScript frontend (settings window, overlay, setup screen)
- `src-tauri/` — Rust shell: tray, global hotkey, sidecar lifecycle, first-run setup
- `sidecar/` — Python audio→text pipeline (see `docs/ARCHITECTURE.md`)
- `scripts/` — setup, model download, and build scripts
