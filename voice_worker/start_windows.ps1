# Launch Jeff Bot Desktop GPU Voice Worker on port 8100
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Run .\setup_windows.ps1 first!" -ForegroundColor Red
    exit 1
}

if (-not $env:VOICE_WORKER_HOST) { $env:VOICE_WORKER_HOST = "0.0.0.0" }
if (-not $env:VOICE_WORKER_PORT) { $env:VOICE_WORKER_PORT = "8100" }
if (-not $env:DEFAULT_VOICE)     { $env:DEFAULT_VOICE = "jeff" }

Write-Host "Starting Jeff Bot Voice Worker (Chatterbox Turbo) on http://$($env:VOICE_WORKER_HOST):$($env:VOICE_WORKER_PORT)" -ForegroundColor Cyan
Write-Host "Drop 5-15s .wav clips into .\voices\ to clone voices live (no restart needed!)." -ForegroundColor DarkGray

.\venv\Scripts\python.exe server.py
