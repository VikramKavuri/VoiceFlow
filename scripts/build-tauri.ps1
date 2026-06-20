# VoiceFlow Full Build Script
# Builds the complete Tauri application including the Python sidecar

param(
    [switch]$Debug,
    [switch]$SkipSidecar
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Join-Path $ScriptDir ".."

Write-Host "=== VoiceFlow Full Build ===" -ForegroundColor Cyan

# Step 1: Build the Python sidecar
if (-not $SkipSidecar) {
    Write-Host "`n--- Building Python sidecar ---" -ForegroundColor Yellow
    & "$ScriptDir\build-sidecar.ps1"
}

# Step 2: Install frontend dependencies
Write-Host "`n--- Installing frontend dependencies ---" -ForegroundColor Yellow
Push-Location $ProjectDir
pnpm install

# Step 3: Build the Tauri app
Write-Host "`n--- Building Tauri application ---" -ForegroundColor Yellow
if ($Debug) {
    pnpm tauri dev
} else {
    pnpm tauri build
}
Pop-Location

Write-Host "`n=== Full build complete ===" -ForegroundColor Green
