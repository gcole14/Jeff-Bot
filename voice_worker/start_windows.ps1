# Launch Jeff Bot Desktop GPU Voice Worker on port 8100
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Run .\setup_windows.ps1 first!" -ForegroundColor Red
    exit 1
}

if (-not $env:VOICE_WORKER_HOST) { $env:VOICE_WORKER_HOST = "0.0.0.0" }
if (-not $env:VOICE_WORKER_PORT) { $env:VOICE_WORKER_PORT = "8100" }
if (-not $env:DEFAULT_VOICE)     { $env:DEFAULT_VOICE = "jeff" }
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

$inUse = Get-NetTCPConnection -LocalPort ([int]$env:VOICE_WORKER_PORT) -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    $owner = Get-Process -Id $inUse[0].OwningProcess -ErrorAction SilentlyContinue
    Write-Host "Port $($env:VOICE_WORKER_PORT) is already in use by $($owner.ProcessName) (PID $($inUse[0].OwningProcess))." -ForegroundColor Yellow
    Write-Host "The voice worker is probably already running in another window. Close it first, or run: Stop-Process -Id $($inUse[0].OwningProcess)" -ForegroundColor Yellow
    exit 1
}
$Host.UI.RawUI.WindowTitle = "Jeff Voice Worker (port $($env:VOICE_WORKER_PORT))"

Write-Host "Starting Jeff Bot Voice Worker (Chatterbox Turbo) on http://$($env:VOICE_WORKER_HOST):$($env:VOICE_WORKER_PORT)" -ForegroundColor Cyan
Write-Host "Drop 5-15s .wav clips into .\voices\ to clone voices live (no restart needed!)." -ForegroundColor DarkGray

.\venv\Scripts\python.exe server.py
