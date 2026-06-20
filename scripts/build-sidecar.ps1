# VoiceFlow Sidecar Build Script
# Freezes the Python sidecar with PyInstaller for distribution

param(
    [switch]$Clean,
    [string]$OutputDir = "..\src-tauri\sidecar"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SidecarDir = Join-Path $ScriptDir "..\sidecar"

Write-Host "=== VoiceFlow Sidecar Build ===" -ForegroundColor Cyan

# Clean previous build
if ($Clean) {
    Write-Host "Cleaning previous build..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "$SidecarDir\dist" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "$SidecarDir\build" -ErrorAction SilentlyContinue
}

# Create virtual environment if it doesn't exist
$VenvDir = Join-Path $SidecarDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv $VenvDir
}

# Activate venv and install dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
& "$VenvDir\Scripts\pip.exe" install -r "$SidecarDir\requirements.txt" --quiet
& "$VenvDir\Scripts\pip.exe" install pyinstaller --quiet

# Run PyInstaller via the .spec file. The spec emits a single-file exe named
# for the target triple (voiceflow-sidecar-x86_64-pc-windows-msvc.exe), which is
# exactly the name Tauri's `externalBin` ("sidecar/voiceflow-sidecar") expects,
# and bundles the small text data files the sidecar needs at runtime.
Write-Host "Building sidecar with PyInstaller..." -ForegroundColor Yellow
Push-Location $SidecarDir
& "$VenvDir\Scripts\pyinstaller.exe" `
    --distpath "$OutputDir" `
    --noconfirm `
    --clean `
    "voiceflow-sidecar-x86_64-pc-windows-msvc.spec"
Pop-Location

Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "Output: $OutputDir\voiceflow-sidecar-x86_64-pc-windows-msvc.exe" -ForegroundColor Green
