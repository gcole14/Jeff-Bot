"""
Jeff Bot — Desktop GPU Voice Worker (Windows + RTX 5070 Ti / CUDA 12.8+)

Runs two GPU models on your NVIDIA card (~4-5 GB total VRAM out of 16 GB):
  1. Chatterbox Turbo (350M) — fast zero-shot cloned TTS from voice_worker/voices/
  2. Whisper large-v3-turbo (transformers) — low-latency English STT for "Hey Jeff"

Endpoints:
  GET  /health  -> GPU, TTS & STT health + available cloned voices
  GET  /voices  -> list of cloned voice names in voice_worker/voices/
  POST /tts     -> {"text": "...", "voice": "jeff"} -> audio/wav bytes
  POST /stt     -> raw audio/wav bytes (?prompt=...) -> {"text": "...", "latency_ms": ...}
"""

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import librosa
import numpy as np
import soundfile as sf
import torch
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
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

STT_ENABLED = os.getenv("STT_ENABLED", "true").strip().lower() not in ("0", "false", "no")
STT_MODEL = os.getenv("STT_MODEL", "openai/whisper-large-v3-turbo").strip()
STT_DEFAULT_PROMPT = os.getenv(
    "STT_DEFAULT_PROMPT",
    "Hey Jeff, Yo Jeff, Okay Jeff, Jeff stop, League of Legends, Cookmysock, 31Sec, SwimboSlice, H1ph0pp3r",
).strip()

_model = None
_model_sr: int = 24000
_stt_model = None
_stt_processor = None
_stt_dtype: torch.dtype = torch.float32
_stt_error: Optional[str] = None
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


def _load_tts_model() -> None:
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
    logger.info(f"TTS warmup complete in {int((time.monotonic() - t0) * 1000)} ms. Cloned voices: {list(voices.keys())}")


def _load_stt_model() -> None:
    global _stt_model, _stt_processor, _stt_dtype, _stt_error
    if not STT_ENABLED:
        logger.info("STT disabled via STT_ENABLED=false.")
        return

    try:
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        _stt_dtype = torch.float16 if _device == "cuda" else torch.float32
        logger.info(f"Loading Whisper STT ({STT_MODEL}) on {_device} ({_stt_dtype})...")
        t0 = time.monotonic()
        _stt_processor = WhisperProcessor.from_pretrained(STT_MODEL)
        try:
            _stt_model = WhisperForConditionalGeneration.from_pretrained(STT_MODEL, dtype=_stt_dtype)
        except TypeError:
            _stt_model = WhisperForConditionalGeneration.from_pretrained(STT_MODEL, torch_dtype=_stt_dtype)
        _stt_model = _stt_model.to(_device)
        _stt_model.eval()

        # Warmup pass on 0.5s silence at 16 kHz
        dummy = np.zeros(8000, dtype=np.float32)
        _transcribe_array_sync(dummy, 16000, STT_DEFAULT_PROMPT)
        _stt_error = None
        logger.info(f"Whisper STT ready in {int((time.monotonic() - t0) * 1000)} ms ({STT_MODEL}).")
    except Exception as e:
        _stt_model = None
        _stt_processor = None
        _stt_error = f"{type(e).__name__}: {e}"
        logger.exception(f"Whisper STT failed to load (TTS will continue working): {e}")


def _load_models() -> None:
    _load_tts_model()
    _load_stt_model()


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


def _strip_prompt_tokens(token_ids: list[int], sot_token_id: Optional[int]) -> list[int]:
    """Drops any prepended `<|startofprev|> ...` prompt tokens before `<|startoftranscript|>`."""
    if sot_token_id is not None and sot_token_id in token_ids:
        idx = token_ids.index(sot_token_id)
        return token_ids[idx:]
    return token_ids


def _transcribe_array_sync(audio: np.ndarray, sample_rate: int, prompt: Optional[str]) -> str:
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return ""
    if sample_rate != 16000:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)

    inputs = _stt_processor(audio, sampling_rate=16000, return_tensors="pt")
    input_features = inputs.input_features.to(device=_device, dtype=_stt_dtype)

    gen_kwargs: dict = {
        "language": "en",
        "task": "transcribe",
        "max_new_tokens": 128,
        "condition_on_prev_tokens": False,
        "no_repeat_ngram_size": 3,
    }

    effective_prompt = (prompt or STT_DEFAULT_PROMPT).strip()
    if effective_prompt:
        try:
            prompt_ids = _stt_processor.get_prompt_ids(effective_prompt, return_tensors="pt")
            if not isinstance(prompt_ids, torch.Tensor):
                prompt_ids = torch.tensor(prompt_ids, dtype=torch.long)
            gen_kwargs["prompt_ids"] = prompt_ids.to(_device)
        except Exception as e:
            logger.debug(f"Could not encode STT prompt_ids (continuing unprompted): {e}")

    with torch.inference_mode():
        predicted_ids = _stt_model.generate(input_features, **gen_kwargs)

    sot_id = getattr(_stt_processor.tokenizer, "convert_tokens_to_ids", lambda _: None)("<|startoftranscript|>")
    seq = predicted_ids[0].tolist()
    seq = _strip_prompt_tokens(seq, sot_id if isinstance(sot_id, int) else None)
    text = _stt_processor.tokenizer.decode(seq, skip_special_tokens=True)
    return text.strip()


def _transcribe_bytes_sync(audio_bytes: bytes, prompt: Optional[str]) -> str:
    audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    return _transcribe_array_sync(audio, int(sr), prompt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(_load_models)
    yield


app = FastAPI(title="Jeff Bot Voice Worker (Chatterbox Turbo + Whisper STT)", lifespan=lifespan)


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
        "stt_available": _stt_model is not None,
        "stt_model": STT_MODEL if _stt_model is not None else None,
        "stt_error": _stt_error,
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
        raise HTTPException(status_code=503, detail="TTS model still loading")

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


@app.post("/stt")
async def transcribe(
    request: Request,
    prompt: Optional[str] = Query(default=None, max_length=400),
    _: None = Depends(_verify_auth),
):
    if _stt_model is None or _stt_processor is None:
        detail = f"STT unavailable ({_stt_error})" if _stt_error else "STT model still loading or disabled"
        raise HTTPException(status_code=503, detail=detail)

    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio body")
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio clip too large (max 10 MB)")

    t0 = time.monotonic()
    try:
        async with _gpu_lock:
            text = await asyncio.to_thread(_transcribe_bytes_sync, audio_bytes, prompt)
    except Exception as e:
        logger.warning(f"STT transcription failed: {e}")
        raise HTTPException(status_code=400, detail=f"Could not decode/transcribe audio: {e}")

    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info(f"STT {len(audio_bytes):,} bytes -> {text!r} in {latency_ms} ms")
    return {
        "text": text,
        "latency_ms": latency_ms,
        "model": STT_MODEL,
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("VOICE_WORKER_HOST", "0.0.0.0")
    port = int(os.getenv("VOICE_WORKER_PORT", "8100"))
    uvicorn.run("server:app", host=host, port=port, log_level="info")
