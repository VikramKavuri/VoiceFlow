# VoiceFlow dev setup: frontend deps + Python sidecar deps + models.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host "=== VoiceFlow setup ===" -ForegroundColor Cyan

Write-Host "`n[1/3] Installing frontend dependencies (pnpm)..." -ForegroundColor Yellow
Push-Location $Root
pnpm install
Pop-Location

Write-Host "`n[2/3] Installing Python sidecar dependencies..." -ForegroundColor Yellow
python -m pip install -r (Join-Path $Root "sidecar\requirements.txt")

Write-Host "`n[3/3] Downloading models (~2.6 GB, one time)..." -ForegroundColor Yellow
python (Join-Path $Root "scripts\download_models.py")

Write-Host "`n=== Done. Run 'pnpm tauri dev' to start. ===" -ForegroundColor Green
