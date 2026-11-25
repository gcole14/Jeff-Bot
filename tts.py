import aiohttp
import asyncio
import logging
import re
import ssl
import time
from dataclasses import dataclass
from typing import Optional

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

from config import (
    VOICE_WORKER_URL,
    VOICE_WORKER_TOKEN,
    VOICE_DEFAULT_VOICE,
    VOICE_FALLBACK_TTS_URL,
    VOICE_FALLBACK_VOICE,
    VOICE_MAX_CHARS,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    ELEVENLABS_MODEL_ID,
)

logger = logging.getLogger(__name__)

# Paralinguistic tags natively supported by Chatterbox Turbo
_CHATTERBOX_TAGS = ("laugh", "chuckle", "sigh", "cough", "gasp", "groan", "clear throat", "sniff")
_TAG_PATTERN = re.compile(
    r"\[(" + "|".join(re.escape(t) for t in _CHATTERBOX_TAGS) + r")\]",
    re.IGNORECASE,
)


@dataclass
class TTSResult:
    audio_bytes: bytes
    audio_format: str          # "wav" or "mp3"
    backend: str               # "desktop_gpu" | "cm3588_kokoro" | "elevenlabs"
    voice: str
    latency_ms: int
    chars: int


def clean_text_for_speech(
    text: str,
    max_chars: int = VOICE_MAX_CHARS,
    keep_paralinguistic: bool = False,
) -> str:
    """Normalizes Discord/Markdown text into natural spoken prose."""
    if not text:
        return ""

    # Preserve paralinguistic tags temporarily if target backend supports them
    placeholders: list[str] = []
    if keep_paralinguistic:
        def _stash(match: re.Match) -> str:
            placeholders.append(f"[{match.group(1).lower()}]")
            return f"@@PTAG{len(placeholders) - 1}@@"
        cleaned = _TAG_PATTERN.sub(_stash, text)
    else:
        cleaned = _TAG_PATTERN.sub(" ", text)

    # Strip URLs, custom Discord emojis, mentions, markdown syntax
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"<a?:\w+:\d+>", "", cleaned)
    cleaned = re.sub(r"<[@#][!&]?\d+>", "", cleaned)
    cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    cleaned = re.sub(r"~~([^~]+)~~", r"\1", cleaned)
    # Remove markdown headings and bullet prefixes
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s+", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*[-•*]\s+", "", cleaned)
    # Strip common status emojis / pictographs that confuse TTS
    cleaned = re.sub(r"[🧠🔥⚠️❌✅🎯📊📋🟢🤝💥🧙🏆🥇🥈🥉👑💎🪙⚙️❓🆕📜▲▼•]", " ", cleaned)
    # Collapse newlines into natural pauses
    cleaned = re.sub(r"\s*\n+\s*", ". ", cleaned)
    cleaned = re.sub(r"\.\s*\.", ".", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    # Restore stashed paralinguistic tags
    for idx, tag in enumerate(placeholders):
        cleaned = cleaned.replace(f"@@PTAG{idx}@@", tag)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    # Truncate cleanly at a sentence boundary if over limit
    if max_chars > 0 and len(cleaned) > max_chars:
        slice_ = cleaned[:max_chars]
        last_punct = max(slice_.rfind(". "), slice_.rfind("! "), slice_.rfind("? "))
        if last_punct >= max_chars // 2:
            cleaned = slice_[: last_punct + 1].strip()
        else:
            cleaned = slice_.rsplit(" ", 1)[0].strip() + "."

    return cleaned


class HybridTTSClient:
    """
    Async hybrid TTS router with automatic failover:
      1. Primary:    Desktop GPU worker (Chatterbox Turbo — fast voice cloning on RTX 5070 Ti)
      2. Fallback 1: Local CM3588 OpenAI-compatible TTS (Kokoro-82M)
      3. Fallback 2: Optional ElevenLabs API (v4 Turbo)
    """

    def __init__(
        self,
        worker_url: Optional[str] = None,
        worker_token: Optional[str] = None,
        default_voice: Optional[str] = None,
        fallback_url: Optional[str] = None,
        fallback_voice: Optional[str] = None,
        elevenlabs_api_key: Optional[str] = None,
        elevenlabs_voice_id: Optional[str] = None,
        elevenlabs_model_id: Optional[str] = None,
        max_chars: int = VOICE_MAX_CHARS,
        primary_cooldown_s: float = 15.0,
    ):
        self.worker_url = (VOICE_WORKER_URL if worker_url is None else worker_url).strip().rstrip("/")
        self.worker_token = (VOICE_WORKER_TOKEN if worker_token is None else worker_token).strip()
        self.active_voice = (VOICE_DEFAULT_VOICE if default_voice is None else default_voice).strip() or "default"
        self.fallback_url = (VOICE_FALLBACK_TTS_URL if fallback_url is None else fallback_url).strip().rstrip("/")
        self.fallback_voice = (VOICE_FALLBACK_VOICE if fallback_voice is None else fallback_voice).strip() or "am_adam"
        self.elevenlabs_api_key = (ELEVENLABS_API_KEY if elevenlabs_api_key is None else elevenlabs_api_key).strip()
        self.elevenlabs_voice_id = (
            ELEVENLABS_VOICE_ID if elevenlabs_voice_id is None else elevenlabs_voice_id
        ).strip()
        self.elevenlabs_model_id = (
            ELEVENLABS_MODEL_ID if elevenlabs_model_id is None else elevenlabs_model_id
        ).strip() or "eleven_v4_turbo"
        self.max_chars = max_chars
        self.primary_cooldown_s = primary_cooldown_s

        self._session: Optional[aiohttp.ClientSession] = None
        self._primary_down_until: float = 0.0
        self._cached_voices: list[str] = []
        self._last_result_meta: Optional[dict] = None
        self.total_chars_spoken: int = 0

    @property
    def has_any_backend(self) -> bool:
        return bool(self.worker_url or self.fallback_url or (self.elevenlabs_api_key and self.elevenlabs_voice_id))

    @property
    def primary_supports_tags(self) -> bool:
        return bool(self.worker_url) and time.monotonic() >= self._primary_down_until

    def _worker_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.worker_token:
            headers["Authorization"] = f"Bearer {self.worker_token}"
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _check_primary_health(self) -> dict:
        if not self.worker_url:
            return {"configured": False, "ok": False, "detail": "Not configured (VOICE_WORKER_URL empty)"}
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=1.5, total=3.0)
            async with session.get(
                f"{self.worker_url}/health",
                headers=self._worker_headers(),
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._primary_down_until = 0.0
                    voices = data.get("voices") or []
                    if isinstance(voices, list):
                        self._cached_voices = [str(v) for v in voices]
                    gpu = data.get("gpu") or data.get("device") or "GPU"
                    engine = data.get("engine") or "chatterbox-turbo"
                    return {
                        "configured": True,
                        "ok": True,
                        "detail": f"{engine} on {gpu}",
                        "voices": self._cached_voices,
                        "raw": data,
                    }
                return {"configured": True, "ok": False, "detail": f"HTTP {resp.status}"}
        except Exception as e:
            self._primary_down_until = time.monotonic() + self.primary_cooldown_s
            return {"configured": True, "ok": False, "detail": f"Offline ({type(e).__name__})"}

    async def _check_fallback_health(self) -> dict:
        if not self.fallback_url:
            return {"configured": False, "ok": False, "detail": "Not configured (VOICE_FALLBACK_TTS_URL empty)"}
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(connect=1.5, total=3.0)
        for path in ("/health", "/v1/audio/voices", "/v1/models"):
            try:
                async with session.get(f"{self.fallback_url}{path}", timeout=timeout) as resp:
                    if resp.status == 200:
                        return {"configured": True, "ok": True, "detail": f"Reachable ({self.fallback_voice})"}
            except Exception:
                continue
        return {"configured": True, "ok": False, "detail": "Unreachable"}

    async def get_status(self) -> dict:
        primary, fallback = await asyncio.gather(
            self._check_primary_health(),
            self._check_fallback_health(),
        )
        el_ok = bool(self.elevenlabs_api_key and self.elevenlabs_voice_id)
        if primary["ok"]:
            active_backend = "Desktop GPU (Chatterbox Turbo)"
        elif fallback["ok"]:
            active_backend = "CM3588 Fallback (Kokoro)"
        elif el_ok:
            active_backend = f"ElevenLabs ({self.elevenlabs_model_id})"
        else:
            active_backend = "None online"

        return {
            "active_backend": active_backend,
            "active_voice": self.active_voice,
            "primary": primary,
            "fallback": fallback,
            "elevenlabs": {
                "configured": el_ok,
                "ok": el_ok,
                "detail": f"{self.elevenlabs_model_id}" if el_ok else "Not configured",
            },
            "voices": list(self._cached_voices),
            "last_result": self._last_result_meta,
            "total_chars_spoken": self.total_chars_spoken,
        }

    async def list_voices(self) -> list[str]:
        if not self.worker_url:
            return list(self._cached_voices)
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=1.5, total=3.0)
            async with session.get(
                f"{self.worker_url}/voices",
                headers=self._worker_headers(),
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    voices = data.get("voices", [])
                    if isinstance(voices, list):
                        self._cached_voices = [str(v) for v in voices]
                        self._primary_down_until = 0.0
        except Exception:
            pass
        return list(self._cached_voices)

    async def _synthesize_desktop_gpu(self, text: str, voice: str) -> Optional[TTSResult]:
        if not self.worker_url:
            return None
        if time.monotonic() < self._primary_down_until:
            return None

        spoken = clean_text_for_speech(text, max_chars=self.max_chars, keep_paralinguistic=True)
        if not spoken:
            return None

        t0 = time.monotonic()
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=1.5, total=30.0)
            payload = {"text": spoken, "voice": voice}
            async with session.post(
                f"{self.worker_url}/tts",
                headers=self._worker_headers(),
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Desktop GPU TTS returned HTTP {resp.status}: {body[:160]}")
                    if resp.status >= 500:
                        self._primary_down_until = time.monotonic() + self.primary_cooldown_s
                    return None
                audio = await resp.read()
                if not audio:
                    return None
                elapsed = int((time.monotonic() - t0) * 1000)
                ctype = resp.headers.get("Content-Type", "")
                fmt = "mp3" if "mpeg" in ctype or "mp3" in ctype else "wav"
                used_voice = resp.headers.get("X-TTS-Voice", voice)
                self._primary_down_until = 0.0
                return TTSResult(
                    audio_bytes=audio,
                    audio_format=fmt,
                    backend="desktop_gpu",
                    voice=used_voice,
                    latency_ms=elapsed,
                    chars=len(spoken),
                )
        except Exception as e:
            logger.info(f"Desktop GPU TTS unreachable ({e}) — falling back for {self.primary_cooldown_s:.0f}s.")
            self._primary_down_until = time.monotonic() + self.primary_cooldown_s
            return None

    async def _synthesize_fallback_kokoro(self, text: str) -> Optional[TTSResult]:
        if not self.fallback_url:
            return None
        spoken = clean_text_for_speech(text, max_chars=self.max_chars, keep_paralinguistic=False)
        if not spoken:
            return None

        t0 = time.monotonic()
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=2.0, total=30.0)
            payload = {
                "model": "kokoro",
                "input": spoken,
                "voice": self.fallback_voice,
                "response_format": "wav",
            }
            async with session.post(
                f"{self.fallback_url}/v1/audio/speech",
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"Fallback TTS returned HTTP {resp.status}: {body[:160]}")
                    return None
                audio = await resp.read()
                if not audio:
                    return None
                elapsed = int((time.monotonic() - t0) * 1000)
                ctype = resp.headers.get("Content-Type", "")
                fmt = "mp3" if "mpeg" in ctype or "mp3" in ctype else "wav"
                return TTSResult(
                    audio_bytes=audio,
                    audio_format=fmt,
                    backend="cm3588_kokoro",
                    voice=self.fallback_voice,
                    latency_ms=elapsed,
                    chars=len(spoken),
                )
        except Exception as e:
            logger.warning(f"Fallback TTS request failed: {e}")
            return None

    async def _synthesize_elevenlabs(self, text: str) -> Optional[TTSResult]:
        if not (self.elevenlabs_api_key and self.elevenlabs_voice_id):
            return None
        spoken = clean_text_for_speech(text, max_chars=self.max_chars, keep_paralinguistic=False)
        if not spoken:
            return None

        t0 = time.monotonic()
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.elevenlabs_voice_id}"
            "?output_format=mp3_44100_128"
        )
        headers = {
            "xi-api-key": self.elevenlabs_api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": spoken,
            "model_id": self.elevenlabs_model_id,
        }
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=3.0, total=25.0)
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"ElevenLabs API {resp.status}: {body[:160]}")
                    return None
                audio = await resp.read()
                if not audio:
                    return None
                elapsed = int((time.monotonic() - t0) * 1000)
                return TTSResult(
                    audio_bytes=audio,
                    audio_format="mp3",
                    backend="elevenlabs",
                    voice=self.elevenlabs_voice_id,
                    latency_ms=elapsed,
                    chars=len(spoken),
                )
        except Exception as e:
            logger.warning(f"ElevenLabs TTS request failed: {e}")
            return None

    async def synthesize(self, text: str, voice: Optional[str] = None) -> Optional[TTSResult]:
        """Synthesize speech using Primary (Desktop GPU) -> Fallback (CM3588 Kokoro) -> ElevenLabs."""
        chosen_voice = (voice or self.active_voice or "default").strip()

        result = await self._synthesize_desktop_gpu(text, chosen_voice)
        if result is None:
            result = await self._synthesize_fallback_kokoro(text)
        if result is None:
            result = await self._synthesize_elevenlabs(text)

        if result is not None:
            self.total_chars_spoken += result.chars
            self._last_result_meta = {
                "backend": result.backend,
                "voice": result.voice,
                "latency_ms": result.latency_ms,
                "chars": result.chars,
            }
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
