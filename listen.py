"""
Jeff Bot — Voice Listener, Phrase Splitter, Wake-Word Router, and Desktop STT Client.

Pipeline:
  Discord per-person DAVE-decrypted PCM (48 kHz stereo, strictly in-memory)
    -> PhraseSegmenter (per-user buffers, 0.7s silence split, 0.4s min, 15s cap)
    -> 16 kHz mono WAV (in-memory)
    -> Desktop GPU Voice Worker POST /stt (Whisper large-v3-turbo, health cached 30s)
    -> Stop-phrase check ("Jeff stop" / "Jeff shut up") -> instant playback cutoff
    -> Wake-word check ("Hey Jeff" / "Yo Jeff" / "Okay Jeff" / "Jeff") or 8s follow-up window
    -> DeepSeek answer_voice_question (with linked Riot ID + short conversation memory)
    -> VoiceManager.speak (existing TTS pipeline)

Nothing is ever written to disk. Audio lives only in memory until transcribed and discarded.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import re
import ssl
import struct
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
import discord

try:
    import audioop  # type: ignore
except ImportError:  # Python 3.13+
    audioop = None

try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

from config import (
    VOICE_LISTEN_CHIME,
    VOICE_LISTEN_ENABLED,
    VOICE_LISTEN_FOLLOWUP_S,
    VOICE_LISTEN_HEALTH_INTERVAL_S,
    VOICE_LISTEN_MAX_S,
    VOICE_LISTEN_MIN_S,
    VOICE_LISTEN_RMS_THRESHOLD,
    VOICE_LISTEN_SILENCE_S,
    VOICE_WORKER_TOKEN,
    VOICE_WORKER_URL,
)
from dave_recv import ensure_opus_loaded, get_dave_recv_stats, install_dave_recv_patch

logger = logging.getLogger(__name__)

# Discord decoded PCM format: 48 kHz, 16-bit signed LE, stereo -> 192,000 bytes/sec
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2
DISCORD_BYTES_PER_SEC = DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
STT_SAMPLE_RATE = 16000

_CHIME_WAV_CACHE: Optional[bytes] = None


# -------------------------------------------------------------------------
# Pure In-Memory Audio Helpers
# -------------------------------------------------------------------------

def pcm_rms(pcm: bytes) -> int:
    """Computes RMS energy of 16-bit signed LE PCM bytes."""
    if len(pcm) < 2:
        return 0
    if audioop is not None:
        return int(audioop.rms(pcm, 2))
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0
    mean_sq = sum(s * s for s in samples) / len(samples)
    return int(math.sqrt(mean_sq))


def pcm48k_stereo_to_wav16k_mono(pcm: bytes) -> bytes:
    """Converts 48 kHz stereo 16-bit LE PCM to 16 kHz mono 16-bit WAV bytes in memory."""
    aligned_len = len(pcm) - (len(pcm) % 4)
    raw = pcm[:aligned_len]
    if not raw:
        mono_16k = b""
    elif audioop is not None:
        mono_48k = audioop.tomono(raw, 2, 0.5, 0.5)
        mono_16k, _ = audioop.ratecv(mono_48k, 2, 1, DISCORD_SAMPLE_RATE, STT_SAMPLE_RATE, None)
    else:
        # Pure-Python fallback: stereo->mono + 3:1 box decimation (48kHz -> 16kHz)
        src = array.array("h")
        src.frombytes(raw)
        n_frames = len(src) // 2
        out = array.array("h")
        for i in range(0, n_frames - 2, 3):
            s0 = (src[i * 2] + src[i * 2 + 1]) >> 1
            s1 = (src[(i + 1) * 2] + src[(i + 1) * 2 + 1]) >> 1
            s2 = (src[(i + 2) * 2] + src[(i + 2) * 2 + 1]) >> 1
            out.append((s0 + s1 + s2) // 3)
        mono_16k = out.tobytes()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(STT_SAMPLE_RATE)
        wf.writeframes(mono_16k)
    return buf.getvalue()


def generate_wake_chime_wav() -> bytes:
    """Generates a soft ~140 ms two-tone chime (D5 -> A5) as WAV bytes in memory."""
    global _CHIME_WAV_CACHE
    if _CHIME_WAV_CACHE is not None:
        return _CHIME_WAV_CACHE

    sr = 24000
    notes = [(587.33, 0.065), (880.0, 0.085)]
    amp = 0.16 * 32767
    samples = array.array("h")

    for freq, dur in notes:
        n = int(sr * dur)
        attack = max(1, int(sr * 0.008))
        release = max(1, int(sr * 0.025))
        for i in range(n):
            env = 1.0
            if i < attack:
                env = i / attack
            elif i > n - release:
                env = max(0.0, (n - i) / release)
            val = math.sin(2.0 * math.pi * freq * (i / sr)) * env * amp
            samples.append(max(-32767, min(32767, int(val))))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    _CHIME_WAV_CACHE = buf.getvalue()
    return _CHIME_WAV_CACHE


# -------------------------------------------------------------------------
# Wake-Word & Stop-Phrase Matching
# -------------------------------------------------------------------------

_WAKE_NAME = r"(?:jeff(?:'?s)?|geoff(?:'?s)?|chef|jef|jeph|jeffrey)"
_GREET = r"(?:hey|yo|okay|ok|o\.k\.|hi|hello|alright|all\s+right)"
_FILLER = r"(?:um+|uh+|so|well|wait|listen|yo|hey|okay|ok)"

# Explicit greeting + Jeff anywhere near the start (up to 2 short lead-in words)
_GREET_WAKE_RE = re.compile(
    rf"^\s*(?:{_FILLER}[\s,.\-!?]+){{0,2}}{_GREET}[\s,.\-!?]+{_WAKE_NAME}(?:[\s,.:;\-!?]+|$)",
    re.IGNORECASE,
)

# Bare "Jeff, ..." / "Jeff ..." right at the start of the phrase (optional um/uh)
_BARE_WAKE_RE = re.compile(
    rf"^\s*(?:(?:um+|uh+)[\s,.\-!?]+)?{_WAKE_NAME}(?:[\s,.:;\-!?]+|$)",
    re.IGNORECASE,
)

# Stop phrases: "Jeff stop", "Hey Jeff shut up", "Stop Jeff", "Shut up Jeff", etc.
_STOP_ACTION = r"(?:stop(?:\s+talking)?|shut\s+up|be\s+quiet|quiet|hush|cancel|never\s*mind|zip\s+it)"
_STOP_AFTER_NAME_RE = re.compile(
    rf"^\s*(?:{_GREET}[\s,.\-!?]+)?{_WAKE_NAME}[\s,.:;\-!?]+(?:please[\s,]+)?{_STOP_ACTION}\b",
    re.IGNORECASE,
)
_STOP_BEFORE_NAME_RE = re.compile(
    rf"^\s*(?:{_GREET}[\s,.\-!?]+)?{_STOP_ACTION}[\s,.:;\-!?]+{_WAKE_NAME}\b",
    re.IGNORECASE,
)


def is_stop_phrase(text: str) -> bool:
    """Returns True if transcript is an explicit stop command addressed to Jeff."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return bool(_STOP_AFTER_NAME_RE.search(cleaned) or _STOP_BEFORE_NAME_RE.search(cleaned))


def extract_wake_question(text: str) -> Optional[str]:
    """
    Checks if `text` addresses Jeff ("Jeff / hey Jeff / yo Jeff / okay Jeff", plus common mishearings).

    Returns:
      - None: wake word was not detected.
      - "":   wake word was heard alone (e.g. "Hey Jeff") with no question yet.
      - str:  the remainder of the utterance after stripping the wake prefix.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    m = _GREET_WAKE_RE.search(cleaned)
    if not m:
        m = _BARE_WAKE_RE.search(cleaned)
    if not m:
        return None

    remainder = cleaned[m.end() :].lstrip(" ,.:;!?-\t\n").strip()
    # Strip trailing filler punctuation-only artifacts
    if not re.search(r"[a-zA-Z0-9]", remainder):
        return ""
    return remainder


# -------------------------------------------------------------------------
# Per-User Phrase Segmenter (Strictly In-Memory)
# -------------------------------------------------------------------------

@dataclass
class CompletedPhrase:
    user_id: int
    display_name: str
    pcm: bytes
    duration_s: float


class _UserAudioBuffer:
    __slots__ = (
        "user_id",
        "display_name",
        "preroll",
        "chunks",
        "total_bytes",
        "active",
        "started_at",
        "last_voice_at",
        "voiced_frames",
        "trailing_silent_frames",
    )

    def __init__(self, user_id: int, display_name: str, preroll_frames: int = 10) -> None:
        self.user_id = user_id
        self.display_name = display_name
        self.preroll: deque[bytes] = deque(maxlen=preroll_frames)
        self.chunks: list[bytes] = []
        self.total_bytes: int = 0
        self.active: bool = False
        self.started_at: float = 0.0
        self.last_voice_at: float = 0.0
        self.voiced_frames: int = 0
        self.trailing_silent_frames: int = 0

    def reset(self) -> None:
        self.chunks.clear()
        self.total_bytes = 0
        self.active = False
        self.started_at = 0.0
        self.last_voice_at = 0.0
        self.voiced_frames = 0
        self.trailing_silent_frames = 0


class PhraseSegmenter:
    """
    Splits continuous per-user PCM streams into discrete spoken phrases in memory.

    Rules:
      - Separate buffer per user_id so voices never mix.
      - Phrase ends after `silence_s` (~0.7s) of silence (or packet gap).
      - Phrases shorter than `min_s` (~0.4s) are dropped.
      - Phrases are capped at `max_s` (~15s) and flushed immediately at the cap.
    """

    def __init__(
        self,
        *,
        silence_s: float = VOICE_LISTEN_SILENCE_S,
        min_s: float = VOICE_LISTEN_MIN_S,
        max_s: float = VOICE_LISTEN_MAX_S,
        rms_threshold: int = VOICE_LISTEN_RMS_THRESHOLD,
        preroll_frames: int = 10,
        bytes_per_sec: int = DISCORD_BYTES_PER_SEC,
    ) -> None:
        self.silence_s = max(0.2, silence_s)
        self.min_s = max(0.1, min_s)
        self.max_s = max(1.0, max_s)
        self.rms_threshold = max(1, rms_threshold)
        self.preroll_frames = max(0, preroll_frames)
        self.bytes_per_sec = bytes_per_sec
        self._lock = threading.Lock()
        self._users: dict[int, _UserAudioBuffer] = {}

    def _finalize(self, buf: _UserAudioBuffer) -> Optional[CompletedPhrase]:
        if not buf.active or not buf.chunks:
            buf.reset()
            return None

        # Trim excess trailing silence (keep up to 8 frames / ~160ms so final consonants stay intact)
        keep_tail = 8
        if buf.trailing_silent_frames > keep_tail:
            trim = buf.trailing_silent_frames - keep_tail
            chunks = buf.chunks[:-trim] if trim < len(buf.chunks) else buf.chunks
        else:
            chunks = list(buf.chunks)

        pcm = b"".join(chunks)
        voiced_frames = buf.voiced_frames
        display_name = buf.display_name
        user_id = buf.user_id
        buf.reset()

        duration_s = len(pcm) / self.bytes_per_sec if self.bytes_per_sec else 0.0
        if duration_s < self.min_s or voiced_frames < 4:
            return None

        return CompletedPhrase(
            user_id=user_id,
            display_name=display_name,
            pcm=pcm,
            duration_s=round(duration_s, 3),
        )

    def push_frame(
        self,
        user_id: int,
        display_name: str,
        pcm: bytes,
        now: Optional[float] = None,
    ) -> Optional[CompletedPhrase]:
        if not pcm:
            return None
        ts = time.monotonic() if now is None else now
        is_voiced = pcm_rms(pcm) >= self.rms_threshold

        with self._lock:
            buf = self._users.get(user_id)
            if buf is None:
                buf = _UserAudioBuffer(user_id, display_name, preroll_frames=self.preroll_frames)
                self._users[user_id] = buf
            else:
                buf.display_name = display_name

            if not buf.active:
                if not is_voiced:
                    buf.preroll.append(pcm)
                    return None
                # Start a new phrase, seeding with pre-roll so the first syllable isn't clipped
                buf.active = True
                buf.started_at = ts
                buf.last_voice_at = ts
                for pre in buf.preroll:
                    buf.chunks.append(pre)
                    buf.total_bytes += len(pre)
                buf.preroll.clear()
                buf.chunks.append(pcm)
                buf.total_bytes += len(pcm)
                buf.voiced_frames = 1
                buf.trailing_silent_frames = 0
                return None

            # Already active
            buf.chunks.append(pcm)
            buf.total_bytes += len(pcm)
            if is_voiced:
                buf.last_voice_at = ts
                buf.voiced_frames += 1
                buf.trailing_silent_frames = 0
            else:
                buf.trailing_silent_frames += 1

            duration_s = buf.total_bytes / self.bytes_per_sec
            if duration_s >= self.max_s:
                return self._finalize(buf)
            if not is_voiced and (ts - buf.last_voice_at) >= self.silence_s:
                return self._finalize(buf)
            return None

    def flush_expired(self, now: Optional[float] = None) -> list[CompletedPhrase]:
        """Flushes any active user buffers whose silence timeout has elapsed (e.g. packet gap)."""
        ts = time.monotonic() if now is None else now
        ready: list[CompletedPhrase] = []
        with self._lock:
            for buf in self._users.values():
                if buf.active and (ts - buf.last_voice_at) >= self.silence_s:
                    phrase = self._finalize(buf)
                    if phrase is not None:
                        ready.append(phrase)
        return ready

    def clear_user(self, user_id: int) -> None:
        with self._lock:
            self._users.pop(user_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._users.clear()


# -------------------------------------------------------------------------
# Conversation Memory & Follow-Up Window
# -------------------------------------------------------------------------

class VoiceConversationMemory:
    """Short per-guild voice Q&A memory plus per-speaker 8-second follow-up window."""

    def __init__(self, *, max_turns: int = 6, history_ttl_s: float = 600.0, followup_s: float = VOICE_LISTEN_FOLLOWUP_S):
        self.max_turns = max_turns
        self.history_ttl_s = history_ttl_s
        self.followup_s = followup_s
        self._history: dict[int, deque[tuple[float, str, str, str]]] = {}
        self._followup: dict[int, tuple[int, float]] = {}

    def add_exchange(self, guild_id: int, speaker: str, question: str, reply: str, now: Optional[float] = None) -> None:
        ts = time.monotonic() if now is None else now
        dq = self._history.setdefault(guild_id, deque(maxlen=self.max_turns))
        dq.append((ts, speaker, question, reply))

    def get_history(self, guild_id: int, now: Optional[float] = None) -> list[tuple[str, str, str]]:
        ts = time.monotonic() if now is None else now
        dq = self._history.get(guild_id)
        if not dq:
            return []
        cutoff = ts - self.history_ttl_s
        return [(spk, q, a) for (t, spk, q, a) in dq if t >= cutoff]

    def open_followup(self, guild_id: int, user_id: int, now: Optional[float] = None) -> None:
        ts = time.monotonic() if now is None else now
        self._followup[guild_id] = (user_id, ts + self.followup_s)

    def is_followup_active(self, guild_id: int, user_id: int, now: Optional[float] = None) -> bool:
        entry = self._followup.get(guild_id)
        if not entry:
            return False
        active_uid, expires_at = entry
        ts = time.monotonic() if now is None else now
        if ts > expires_at:
            self._followup.pop(guild_id, None)
            return False
        return active_uid == user_id

    def clear_followup(self, guild_id: int) -> None:
        self._followup.pop(guild_id, None)

    def clear_guild(self, guild_id: int) -> None:
        self._history.pop(guild_id, None)
        self._followup.pop(guild_id, None)


# -------------------------------------------------------------------------
# Desktop GPU Worker STT Client (Health Cached 30s)
# -------------------------------------------------------------------------

class STTClient:
    """Async HTTP client for the PC voice worker's `POST /stt` + cached `/health`."""

    def __init__(
        self,
        worker_url: Optional[str] = None,
        worker_token: Optional[str] = None,
        health_interval_s: float = VOICE_LISTEN_HEALTH_INTERVAL_S,
    ) -> None:
        self.worker_url = (VOICE_WORKER_URL if worker_url is None else worker_url).strip().rstrip("/")
        self.worker_token = (VOICE_WORKER_TOKEN if worker_token is None else worker_token).strip()
        self.health_interval_s = max(5.0, float(health_interval_s))

        self._session: Optional[aiohttp.ClientSession] = None
        self._available: bool = False
        self._last_checked: float = 0.0
        self._model_name: Optional[str] = None
        self._detail: str = "Not checked yet"

    @property
    def configured(self) -> bool:
        return bool(self.worker_url)

    @property
    def cached_available(self) -> bool:
        return self._available

    @property
    def model_name(self) -> Optional[str]:
        return self._model_name

    @property
    def detail(self) -> str:
        return self._detail

    def _headers(self, content_type: Optional[str] = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self.worker_token:
            headers["Authorization"] = f"Bearer {self.worker_token}"
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT) if _SSL_CONTEXT else None
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def check_health(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not self.worker_url:
            self._available = False
            self._model_name = None
            self._detail = "Not configured (VOICE_WORKER_URL empty)"
            self._last_checked = now
            return {
                "configured": False,
                "available": False,
                "model": None,
                "detail": self._detail,
            }

        if not force and self._last_checked > 0 and (now - self._last_checked) < self.health_interval_s:
            return {
                "configured": True,
                "available": self._available,
                "model": self._model_name,
                "detail": self._detail,
            }

        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=1.5, total=3.0)
            async with session.get(
                f"{self.worker_url}/health",
                headers=self._headers(),
                timeout=timeout,
            ) as resp:
                self._last_checked = time.monotonic()
                if resp.status == 200:
                    data = await resp.json()
                    stt_ok = bool(data.get("stt_available"))
                    self._available = stt_ok
                    self._model_name = data.get("stt_model")
                    if stt_ok:
                        gpu = data.get("gpu") or data.get("device") or "GPU"
                        self._detail = f"{self._model_name or 'Whisper'} on {gpu}"
                    else:
                        err = data.get("stt_error") or "not enabled on worker"
                        self._detail = f"Worker online, STT unavailable ({err})"
                else:
                    self._available = False
                    self._model_name = None
                    self._detail = f"Worker HTTP {resp.status}"
        except Exception as e:
            self._last_checked = time.monotonic()
            self._available = False
            self._model_name = None
            self._detail = f"PC worker offline ({type(e).__name__})"

        return {
            "configured": True,
            "available": self._available,
            "model": self._model_name,
            "detail": self._detail,
        }

    async def is_available(self, *, force: bool = False) -> bool:
        st = await self.check_health(force=force)
        return bool(st["available"])

    async def transcribe(self, wav_bytes: bytes, prompt: Optional[str] = None) -> Optional[str]:
        """Sends in-memory WAV bytes to the PC worker's POST /stt. Never sends while worker is down."""
        if not wav_bytes or not await self.is_available():
            return None

        params = {"prompt": prompt[:380]} if prompt else None
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(connect=1.5, total=15.0)
            async with session.post(
                f"{self.worker_url}/stt",
                params=params,
                data=wav_bytes,
                headers=self._headers("audio/wav"),
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return str(data.get("text") or "").strip()
                body = await resp.text()
                logger.warning("STT worker returned HTTP %d: %s", resp.status, body[:160])
                if resp.status >= 500:
                    self._available = False
                    self._last_checked = time.monotonic()
                    self._detail = f"STT HTTP {resp.status}"
                return None
        except Exception as e:
            logger.info("STT worker unreachable (%s) — pausing listening until next health check.", e)
            self._available = False
            self._last_checked = time.monotonic()
            self._detail = f"PC worker offline ({type(e).__name__})"
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# -------------------------------------------------------------------------
# Live Discord Sink + Per-Guild VoiceListenerManager
# -------------------------------------------------------------------------

class _GuildListenState:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.segmenter = PhraseSegmenter()
        self.active: bool = False
        self.sink: Any = None
        self.monitor_task: Optional[asyncio.Task] = None
        self.phrase_queue: asyncio.Queue[CompletedPhrase] = asyncio.Queue(maxsize=32)
        self.consumer_task: Optional[asyncio.Task] = None
        self.reply_lock = asyncio.Lock()


class VoiceListenerManager:
    """
    Coordinates per-guild hands-free listening ("Hey Jeff"), wake chime, follow-up window,
    stop phrases, and automatic PC-off fallback.
    """

    def __init__(
        self,
        *,
        voice_manager: Any,
        stt_client: STTClient,
        deepseek: Any,
        aggregator: Any,
        db: Any,
        enabled: bool = VOICE_LISTEN_ENABLED,
        chime_enabled: bool = VOICE_LISTEN_CHIME,
    ) -> None:
        self.voice_manager = voice_manager
        self.stt = stt_client
        self.deepseek = deepseek
        self.aggregator = aggregator
        self.db = db
        self.desired_enabled: bool = enabled
        self.chime_enabled: bool = chime_enabled

        self.memory = VoiceConversationMemory()
        self._guilds: dict[int, _GuildListenState] = {}
        self._cached_squad_context: str = ""
        self._squad_context_at: float = 0.0
        self._squad_context_ttl_s: float = 60.0

    def is_listening(self, guild_id: int) -> bool:
        st = self._guilds.get(guild_id)
        return bool(st and st.active)

    def any_listening(self) -> bool:
        return any(st.active for st in self._guilds.values())

    async def should_join_undeafened(self) -> bool:
        """True iff listening is enabled and the PC STT worker is currently up."""
        if not self.desired_enabled:
            return False
        return await self.stt.is_available()

    def _build_stt_prompt(self, guild_id: int) -> str:
        terms = ["Hey Jeff", "Yo Jeff", "Okay Jeff", "Jeff stop", "League of Legends"]
        try:
            for rid in self.aggregator.get_roster():
                name = rid.split("#", 1)[0].strip()
                if name and name not in terms:
                    terms.append(name)
        except Exception:
            pass
        ch = self.voice_manager.connected_channel(guild_id)
        if ch:
            for m in ch.members:
                if not m.bot and m.display_name and m.display_name not in terms:
                    terms.append(m.display_name)
        return ", ".join(terms[:18])

    def _make_sink(self, gstate: _GuildListenState, loop: asyncio.AbstractEventLoop) -> Any:
        from discord.ext import voice_recv

        def _enqueue_phrase(phrase: CompletedPhrase) -> None:
            def _put() -> None:
                if gstate.phrase_queue.full():
                    try:
                        gstate.phrase_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    gstate.phrase_queue.put_nowait(phrase)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(_put)

        class _PhraseAudioSink(voice_recv.AudioSink):
            def wants_opus(self) -> bool:
                return False

            def write(self, user: Optional[discord.abc.User], data: Any) -> None:
                pcm: bytes = getattr(data, "pcm", b"")
                if not pcm:
                    return
                if user is not None and getattr(user, "bot", False):
                    return

                if user is not None:
                    uid = int(user.id)
                    name = getattr(user, "display_name", None) or getattr(user, "name", None) or str(uid)
                else:
                    packet = getattr(data, "packet", None)
                    ssrc = getattr(packet, "ssrc", None)
                    vc = self.voice_client
                    uid = vc._get_id_from_ssrc(ssrc) if (vc and ssrc is not None) else None
                    if uid is None:
                        return
                    uid = int(uid)
                    member = vc.guild.get_member(uid) if (vc and vc.guild) else None
                    if member and member.bot:
                        return
                    name = member.display_name if member else f"user_{uid}"

                completed = gstate.segmenter.push_frame(uid, name, pcm)
                if completed is not None:
                    _enqueue_phrase(completed)

            def cleanup(self) -> None:
                pass

        return _PhraseAudioSink()

    async def _start_sink(self, gstate: _GuildListenState) -> bool:
        if gstate.active:
            return True
        try:
            from discord.ext import voice_recv
        except ImportError:
            return False

        vc = self.voice_manager.get_voice_client(gstate.guild_id)
        if not vc or not vc.is_connected() or not isinstance(vc, voice_recv.VoiceRecvClient):
            return False
        if not ensure_opus_loaded():
            logger.warning("Cannot start voice listener: libopus not loaded.")
            return False

        install_dave_recv_patch()
        await self.voice_manager.set_self_deaf(gstate.guild_id, False)

        loop = asyncio.get_running_loop()
        sink = self._make_sink(gstate, loop)
        if vc.is_listening():
            vc.stop_listening()
        vc.listen(sink)
        gstate.sink = sink
        gstate.active = True
        logger.info("Voice listening started in guild %s.", gstate.guild_id)
        return True

    async def _stop_sink(self, gstate: _GuildListenState, *, deafen: bool = True) -> None:
        was_active = gstate.active
        gstate.active = False
        gstate.sink = None
        gstate.segmenter.clear_all()

        vc = self.voice_manager.get_voice_client(gstate.guild_id)
        if vc is not None and getattr(vc, "is_listening", lambda: False)():
            try:
                vc.stop_listening()
            except Exception as e:
                logger.debug("stop_listening cleanup warning: %s", e)

        if deafen and vc is not None and vc.is_connected():
            await self.voice_manager.set_self_deaf(gstate.guild_id, True)

        if was_active:
            logger.info("Voice listening paused/stopped in guild %s (deaf=%s).", gstate.guild_id, deafen)

    async def _monitor_loop(self, gstate: _GuildListenState) -> None:
        """Flushes silent phrase buffers every 150 ms and re-checks PC STT health every 30 s."""
        last_health = time.monotonic()
        try:
            while True:
                await asyncio.sleep(0.15)
                if not self.voice_manager.is_connected(gstate.guild_id):
                    break

                if gstate.active:
                    for phrase in gstate.segmenter.flush_expired():
                        if not gstate.phrase_queue.full():
                            gstate.phrase_queue.put_nowait(phrase)

                now = time.monotonic()
                if (now - last_health) >= self.stt.health_interval_s:
                    last_health = now
                    await self._sync_guild_state(gstate, force_health=True)
        except asyncio.CancelledError:
            pass

    async def _sync_guild_state(self, gstate: _GuildListenState, *, force_health: bool = False) -> bool:
        if not self.voice_manager.is_connected(gstate.guild_id):
            await self._stop_sink(gstate, deafen=False)
            return False

        if not self.desired_enabled:
            await self._stop_sink(gstate, deafen=True)
            return False

        stt_up = await self.stt.is_available(force=force_health)
        if stt_up:
            return await self._start_sink(gstate)
        await self._stop_sink(gstate, deafen=True)
        return False

    async def _consumer_loop(self, gstate: _GuildListenState) -> None:
        try:
            while True:
                phrase = await gstate.phrase_queue.get()
                if not gstate.active:
                    continue
                try:
                    await self._handle_phrase(gstate, phrase)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Error handling voice phrase in guild %s: %s", gstate.guild_id, e)
        except asyncio.CancelledError:
            pass

    async def _get_squad_context(self) -> str:
        now = time.monotonic()
        if self._cached_squad_context and (now - self._squad_context_at) < self._squad_context_ttl_s:
            return self._cached_squad_context
        ctx = await self.aggregator.build_squad_context(days=14)
        self._cached_squad_context = ctx
        self._squad_context_at = time.monotonic()
        return ctx

    async def _handle_phrase(self, gstate: _GuildListenState, phrase: CompletedPhrase) -> None:
        wav_bytes = await asyncio.to_thread(pcm48k_stereo_to_wav16k_mono, phrase.pcm)
        prompt = self._build_stt_prompt(gstate.guild_id)
        text = await self.stt.transcribe(wav_bytes, prompt=prompt)

        if not self.stt.cached_available:
            # Worker just went offline mid-request; switch off listening & deafen immediately
            await self._stop_sink(gstate, deafen=True)
            return

        if not text:
            return

        logger.debug("Voice transcript [%s] (%.2fs): %r", phrase.display_name, phrase.duration_s, text)

        # 1. Stop phrases cut off playback immediately without waiting for any LLM lock
        if is_stop_phrase(text):
            logger.info("Voice stop phrase from %s: %r", phrase.display_name, text)
            self.memory.clear_followup(gstate.guild_id)
            self.voice_manager.stop_speaking(gstate.guild_id)
            return

        # 2. Wake word or active 8-second follow-up window for the same speaker
        extracted = extract_wake_question(text)
        is_followup = False
        if extracted is not None:
            question = extracted
        elif self.memory.is_followup_active(gstate.guild_id, phrase.user_id):
            question = text.strip()
            is_followup = True
        else:
            return

        logger.info(
            "Voice trigger [%s%s]: %r",
            phrase.display_name,
            " follow-up" if is_followup else "",
            question or "(bare wake word)",
        )

        # Soft chime on explicit wake word
        if not is_followup and self.chime_enabled:
            await self.voice_manager.play_chime(gstate.guild_id, generate_wake_chime_wav())

        # Bare "Hey Jeff" with no question yet -> open the 8s follow-up window and wait
        if not question:
            self.memory.open_followup(gstate.guild_id, phrase.user_id)
            return

        if not self.deepseek.is_configured:
            logger.warning("DeepSeek not configured; skipping voice answer.")
            return

        # Answer through DeepSeek + existing TTS pipeline
        asyncio.create_task(self._answer_and_speak(gstate, phrase.user_id, phrase.display_name, question))

    async def _answer_and_speak(
        self,
        gstate: _GuildListenState,
        user_id: int,
        display_name: str,
        question: str,
    ) -> None:
        async with gstate.reply_lock:
            try:
                linked_riot_id = self.db.get_linked_riot_id(user_id) if self.db else None
                context = await self._get_squad_context()
                history = self.memory.get_history(gstate.guild_id)

                reply = await self.deepseek.answer_voice_question(
                    question,
                    context,
                    display_name,
                    allow_paralinguistic=self.voice_manager.tts.primary_supports_tags,
                    speaker_riot_id=linked_riot_id,
                    history=history,
                )
                if not reply:
                    return

                self.memory.add_exchange(gstate.guild_id, display_name, question, reply)
                # Keep follow-up open while Jeff talks, then give a full 8s after he finishes
                self.memory.open_followup(gstate.guild_id, user_id)
                res = await self.voice_manager.speak(gstate.guild_id, reply, wait_finished=True)
                if res is not None:
                    self.memory.open_followup(gstate.guild_id, user_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Voice answer failed for %s: %s", display_name, e)

    async def on_voice_join(self, guild_id: int) -> bool:
        gstate = self._guilds.get(guild_id)
        if gstate is None:
            gstate = _GuildListenState(guild_id)
            self._guilds[guild_id] = gstate

        active = await self._sync_guild_state(gstate, force_health=False)

        if gstate.monitor_task is None or gstate.monitor_task.done():
            gstate.monitor_task = asyncio.create_task(self._monitor_loop(gstate))
        if gstate.consumer_task is None or gstate.consumer_task.done():
            gstate.consumer_task = asyncio.create_task(self._consumer_loop(gstate))
        return active

    async def on_voice_leave(self, guild_id: int) -> None:
        gstate = self._guilds.pop(guild_id, None)
        self.memory.clear_guild(guild_id)
        if not gstate:
            return
        if gstate.monitor_task and not gstate.monitor_task.done():
            gstate.monitor_task.cancel()
        if gstate.consumer_task and not gstate.consumer_task.done():
            gstate.consumer_task.cancel()
        await self._stop_sink(gstate, deafen=False)

    async def set_enabled(self, enabled: bool, guild_id: Optional[int] = None) -> dict[str, Any]:
        """Toggles listening on/off. Returns live status dict for the slash command reply."""
        self.desired_enabled = enabled
        stt_health = await self.stt.check_health(force=True)

        target_ids = [guild_id] if (guild_id and guild_id in self._guilds) else list(self._guilds.keys())
        active_now = False
        for gid in target_ids:
            gstate = self._guilds.get(gid)
            if gstate:
                if await self._sync_guild_state(gstate, force_health=False):
                    active_now = True

        # If connected to voice in guild_id but not yet tracked in _guilds, register now
        if guild_id and guild_id not in self._guilds and self.voice_manager.is_connected(guild_id):
            active_now = await self.on_voice_join(guild_id)

        return {
            "desired_enabled": self.desired_enabled,
            "active": active_now,
            "stt": stt_health,
        }

    async def get_status(self, guild_id: Optional[int] = None) -> dict[str, Any]:
        stt_health = await self.stt.check_health(force=True)
        active = self.is_listening(guild_id) if guild_id else self.any_listening()
        vc = self.voice_manager.get_voice_client(guild_id) if guild_id else None
        dave_stats = get_dave_recv_stats(vc) if (vc and active) else None
        return {
            "desired_enabled": self.desired_enabled,
            "active": active,
            "chime_enabled": self.chime_enabled,
            "stt": stt_health,
            "dave": dave_stats,
        }

    async def close(self) -> None:
        for gid in list(self._guilds.keys()):
            await self.on_voice_leave(gid)
        await self.stt.close()
