"""
Jeff Bot — Desktop GPU Voice Worker (Windows + RTX 5070 Ti / CUDA 12.8+)

Runs Chatterbox Turbo (350M, single-step decoder) on your NVIDIA GPU with
zero-shot voice cloning from any 5-15s clip dropped into voice_worker/voices/.

Endpoints:
  GET  /health  -> GPU & engine health + available cloned voices
  GET  /voices  -> list of cloned voice names in voice_worker/voices/
  POST /tts     -> {"text": "...", "voice": "jeff"} -> audio/wav bytes
"""

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import soundfile as sf
import torch
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("jeff-voice-worker")

BASE_DIR = Path(__file__).resolve().parent
VOICES_DIR = Path(os.getenv("VOICES_DIR", str(BASE_DIR / "voices")))
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "jeff").strip().lower()
WORKER_TOKEN = os.getenv("VOICE_WORKER_TOKEN", "").strip()
SUPPORTED_EXTS = (".wav", ".mp3", ".flac", ".ogg")

_model = None
_model_sr: int = 24000
_device: str = "cpu"
_gpu_name: str = "CPU"
_gpu_lock = asyncio.Lock()


def discover_voices() -> dict[str, Path]:
    """Scans VOICES_DIR for reference audio clips; filename stem becomes the voice name."""
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    for p in sorted(VOICES_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            found[p.stem.lower()] = p
    return found


def _verify_auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not WORKER_TOKEN:
        return
    if not authorization or authorization != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def _load_model() -> None:
    global _model, _model_sr, _device, _gpu_name
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    if torch.cuda.is_available():
        _device = "cuda"
        _gpu_name = torch.cuda.get_device_name(0)
        logger.info(f"Loading ChatterboxTurboTTS on {_gpu_name} (CUDA {torch.version.cuda})...")
    else:
        _device = "cpu"
        _gpu_name = "CPU (CUDA unavailable — check PyTorch cu128 install!)"
        logger.warning("CUDA not detected! Falling back to CPU (will be much slower).")

    _model = ChatterboxTurboTTS.from_pretrained(device=_device)
    _model_sr = int(getattr(_model, "sr", 24000))
    logger.info(f"ChatterboxTurboTTS loaded (sample rate {_model_sr} Hz).")

    # Warmup pass so the very first Discord request has zero cold-start lag
    voices = discover_voices()
    prompt_path = str(voices[DEFAULT_VOICE]) if DEFAULT_VOICE in voices else (
        str(next(iter(voices.values()))) if voices else None
    )
    t0 = time.monotonic()
    with torch.inference_mode():
        if prompt_path:
            _model.generate("Ready.", audio_prompt_path=prompt_path)
        else:
            _model.generate("Ready.")
    logger.info(f"Warmup complete in {int((time.monotonic() - t0) * 1000)} ms. Cloned voices: {list(voices.keys())}")


def _generate_wav_sync(text: str, prompt_path: Optional[str]) -> bytes:
    with torch.inference_mode():
        if prompt_path:
            wav = _model.generate(text=text, audio_prompt_path=prompt_path)
        else:
            wav = _model.generate(text=text)

    if isinstance(wav, torch.Tensor):
        audio_np = wav.detach().float().cpu().squeeze().numpy()
    else:
        audio_np = wav

    buf = io.BytesIO()
    sf.write(buf, audio_np, _model_sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(_load_model)
    yield


app = FastAPI(title="Jeff Bot Voice Worker (Chatterbox Turbo)", lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1200)
    voice: Optional[str] = Field(default=None, description="Voice filename stem from voices/ directory")


@app.get("/health")
async def health(_: None = Depends(_verify_auth)):
    voices = list(discover_voices().keys())
    vram_mb = (
        round(torch.cuda.memory_allocated(0) / (1024 * 1024))
        if _device == "cuda" and torch.cuda.is_available()
        else 0
    )
    return {
        "status": "ok" if _model is not None else "loading",
        "engine": "chatterbox-turbo",
        "device": _device,
        "gpu": _gpu_name,
        "vram_allocated_mb": vram_mb,
        "default_voice": DEFAULT_VOICE,
        "voices": voices,
    }


@app.get("/voices")
async def list_voices(_: None = Depends(_verify_auth)):
    return {
        "default": DEFAULT_VOICE,
        "voices": list(discover_voices().keys()),
    }


@app.post("/tts")
async def synthesize(req: TTSRequest, _: None = Depends(_verify_auth)):
    if _model is None:
        raise HTTPException(status_code=503, detail="Model still loading")

    voices = discover_voices()
    requested = (req.voice or DEFAULT_VOICE).strip().lower()
    prompt_path: Optional[str] = None
    used_voice = "builtin"

    if requested in voices:
        prompt_path = str(voices[requested])
        used_voice = requested
    elif DEFAULT_VOICE in voices:
        prompt_path = str(voices[DEFAULT_VOICE])
        used_voice = DEFAULT_VOICE
    elif voices:
        first_name, first_path = next(iter(voices.items()))
        prompt_path = str(first_path)
        used_voice = first_name

    t0 = time.monotonic()
    async with _gpu_lock:
        wav_bytes = await asyncio.to_thread(_generate_wav_sync, req.text, prompt_path)
    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info(f"TTS [{used_voice}] {len(req.text)} chars -> {len(wav_bytes):,} bytes in {latency_ms} ms")

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "X-TTS-Engine": "chatterbox-turbo",
            "X-TTS-Voice": used_voice,
            "X-TTS-Latency-Ms": str(latency_ms),
        },
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("VOICE_WORKER_HOST", "0.0.0.0")
    port = int(os.getenv("VOICE_WORKER_PORT", "8100"))
    uvicorn.run("server:app", host=host, port=port, log_level="info")
