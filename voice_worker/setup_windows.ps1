# Jeff Bot Desktop GPU Voice Worker — Windows + RTX 5070 Ti (CUDA 12.8+) Setup
$ErrorActionPreference = "Stop"

Write-Host "=== Jeff Bot Voice Worker Setup (RTX 5070 Ti / CUDA 12.8) ===" -ForegroundColor Cyan

# Recommend Python 3.11 for broadest Chatterbox wheel compatibility
$py = "python"
if (Get-Command py -ErrorAction SilentlyContinue) {
    $has311 = & py -3.11 --version 2>$null
    if ($LASTEXITCODE -eq 0) {
        $py = "py -3.11"
        Write-Host "Using Python 3.11 ($has311)" -ForegroundColor Green
    }
}

if (-not (Test-Path ".\venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    Invoke-Expression "$py -m venv venv"
}

$pip = ".\venv\Scripts\pip.exe"
$python = ".\venv\Scripts\python.exe"

& $python -m pip install --upgrade pip

Write-Host "Installing PyTorch with CUDA 12.8 (required for RTX 50-series Blackwell sm_120)..." -ForegroundColor Yellow
& $pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

Write-Host "Installing Chatterbox Turbo + FastAPI..." -ForegroundColor Yellow
# --no-deps: chatterbox-tts pins torch==2.6.0 (no sm_120 support); its other deps are in requirements.txt
& $pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed" }
& $pip install --no-deps chatterbox-tts==0.1.7
if ($LASTEXITCODE -ne 0) { throw "chatterbox-tts install failed" }

if (-not (Test-Path ".\voices")) {
    New-Item -ItemType Directory -Path ".\voices" | Out-Null
}

Write-Host "`nVerifying CUDA on your GPU..." -ForegroundColor Yellow
& $python -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('Device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE')"

Write-Host "`nSetup complete!" -ForegroundColor Green
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Drop any clean 5-15s .wav voice clip into voice_worker\voices\jeff.wav"
Write-Host "  2. Run .\start_windows.ps1"
Write-Host "  3. Set VOICE_WORKER_URL=http://<YOUR-WINDOWS-LAN-OR-TAILSCALE-IP>:8100 in Jeff Bot's .env on your CM3588"
