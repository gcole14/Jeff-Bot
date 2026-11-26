import asyncio
import importlib.util
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import discord

from config import VOICE_ENABLED, VOICE_IDLE_MINUTES
from dave_recv import ensure_opus_loaded, install_dave_recv_patch
from tts import HybridTTSClient, TTSResult

logger = logging.getLogger(__name__)

try:
    from discord.ext import voice_recv as _voice_recv
except ImportError:
    _voice_recv = None


def voice_recv_available() -> bool:
    return _voice_recv is not None


def check_voice_prereqs() -> tuple[bool, list[str]]:
    """Checks whether runtime dependencies for Discord voice playback are installed."""
    missing: list[str] = []
    if importlib.util.find_spec("nacl") is None:
        missing.append("PyNaCl (`pip install PyNaCl`)")
    if importlib.util.find_spec("davey") is None:
        missing.append("davey (`pip install davey` — required for Discord DAVE E2EE voice)")
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg (`brew install ffmpeg` or `apt install ffmpeg`)")
    return len(missing) == 0, missing


@dataclass
class _PlaybackItem:
    audio_bytes: bytes
    audio_format: str  # "wav" or "mp3"
    done_event: Optional[asyncio.Event] = None


class _GuildVoiceState:
    def __init__(self, guild_id: int, voice_client: discord.VoiceClient):
        self.guild_id = guild_id
        self.voice_client = voice_client
        self.queue: asyncio.Queue[Optional[_PlaybackItem]] = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self.last_activity: float = time.monotonic()


class VoiceManager:
    """Manages Discord voice channel connections and queued TTS playback per guild."""

    def __init__(
        self,
        tts_client: HybridTTSClient,
        enabled: bool = VOICE_ENABLED,
        idle_minutes: int = VOICE_IDLE_MINUTES,
    ):
        self.tts = tts_client
        self.enabled = enabled
        self.idle_minutes = max(1, idle_minutes)
        self.auto_speak: bool = True
        self._guilds: dict[int, _GuildVoiceState] = {}
        self._idle_task: Optional[asyncio.Task] = None
        self._on_join_hook: Optional[Callable[[int], Awaitable[Any]]] = None
        self._on_leave_hook: Optional[Callable[[int], Awaitable[Any]]] = None

    def set_lifecycle_hooks(
        self,
        *,
        on_join: Optional[Callable[[int], Awaitable[Any]]] = None,
        on_leave: Optional[Callable[[int], Awaitable[Any]]] = None,
    ) -> None:
        self._on_join_hook = on_join
        self._on_leave_hook = on_leave

    def get_voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        state = self._guilds.get(guild_id)
        return state.voice_client if state else None

    def is_connected(self, guild_id: int) -> bool:
        state = self._guilds.get(guild_id)
        return bool(state and state.voice_client and state.voice_client.is_connected())

    def connected_channel(self, guild_id: int) -> Optional[discord.VoiceChannel]:
        state = self._guilds.get(guild_id)
        if state and state.voice_client and state.voice_client.is_connected():
            return state.voice_client.channel
        return None

    def connected_guild_ids(self) -> list[int]:
        return [gid for gid in list(self._guilds.keys()) if self.is_connected(gid)]

    def _ensure_idle_watcher(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watchdog())

    async def _idle_watchdog(self) -> None:
        try:
            while self._guilds:
                await asyncio.sleep(60)
                now = time.monotonic()
                idle_sec = self.idle_minutes * 60
                for gid in list(self._guilds.keys()):
                    state = self._guilds.get(gid)
                    if not state:
                        continue
                    vc = state.voice_client
                    if not vc or not vc.is_connected():
                        await self.leave(gid)
                        continue
                    humans = [m for m in vc.channel.members if not m.bot] if vc.channel else []
                    if not humans or (now - state.last_activity > idle_sec and not vc.is_playing()):
                        logger.info(f"Auto-leaving voice in guild {gid} (idle or empty channel).")
                        await self.leave(gid)
        except asyncio.CancelledError:
            pass

    async def set_self_deaf(self, guild_id: int, self_deaf: bool) -> bool:
        """Updates Jeff's self-deafen state in a connected guild if it differs from current state."""
        state = self._guilds.get(guild_id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            return False
        vc = state.voice_client
        guild = vc.guild
        if not guild:
            return False
        me_voice = guild.me.voice if guild.me else None
        if me_voice is not None and bool(me_voice.self_deaf) == bool(self_deaf):
            return True
        try:
            await guild.change_voice_state(channel=vc.channel, self_deaf=self_deaf)
            return True
        except Exception as e:
            logger.debug(f"Could not update self_deaf={self_deaf} in guild {guild_id}: {e}")
            return False

    async def join(self, channel: discord.VoiceChannel, *, self_deaf: bool = True) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Voice is disabled (`VOICE_ENABLED=false`)."
        ok, missing = check_voice_prereqs()
        if not ok:
            return False, "Missing voice prerequisites: " + ", ".join(missing)

        ensure_opus_loaded()
        if _voice_recv is not None:
            install_dave_recv_patch()

        guild_id = channel.guild.id
        existing = self._guilds.get(guild_id)
        try:
            if existing and existing.voice_client and existing.voice_client.is_connected():
                if existing.voice_client.channel != channel:
                    await existing.voice_client.move_to(channel)
                await self.set_self_deaf(guild_id, self_deaf)
                existing.last_activity = time.monotonic()
                if self._on_join_hook:
                    await self._on_join_hook(guild_id)
                return True, f"Moved to **{channel.name}**."

            # Handle stale guild voice_client if discord.py still holds one
            if channel.guild.voice_client is not None:
                await channel.guild.voice_client.disconnect(force=True)

            connect_kwargs: dict[str, Any] = {"timeout": 15.0, "self_deaf": self_deaf}
            if _voice_recv is not None:
                connect_kwargs["cls"] = _voice_recv.VoiceRecvClient

            vc = await channel.connect(**connect_kwargs)
            state = _GuildVoiceState(guild_id, vc)
            state.worker_task = asyncio.create_task(self._playback_loop(state))
            self._guilds[guild_id] = state
            self._ensure_idle_watcher()
            if self._on_join_hook:
                await self._on_join_hook(guild_id)
            return True, f"Joined **{channel.name}**!"
        except Exception as e:
            logger.exception(f"Failed to join voice channel {channel}: {e}")
            return False, f"Could not join voice channel: {e}"

    async def leave(self, guild_id: int) -> tuple[bool, str]:
        if self._on_leave_hook:
            try:
                await self._on_leave_hook(guild_id)
            except Exception as e:
                logger.debug(f"Voice on_leave hook warning: {e}")

        state = self._guilds.pop(guild_id, None)
        if not state:
            return False, "Not currently in a voice channel."

        if state.worker_task and not state.worker_task.done():
            state.worker_task.cancel()
        self._drain_queue(state)
        try:
            if state.voice_client and state.voice_client.is_connected():
                state.voice_client.stop()
                await state.voice_client.disconnect(force=True)
        except Exception as e:
            logger.debug(f"Voice disconnect cleanup warning: {e}")
        return True, "Left the voice channel."

    @staticmethod
    def _drain_queue(state: _GuildVoiceState) -> None:
        while True:
            try:
                item = state.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item and item.done_event and not item.done_event.is_set():
                item.done_event.set()

    def stop_speaking(self, guild_id: int) -> bool:
        """Immediately interrupts Jeff's current speech and clears queued lines without stopping listening."""
        state = self._guilds.get(guild_id)
        if not state or not state.voice_client:
            return False
        self._drain_queue(state)
        vc = state.voice_client
        # VoiceRecvClient.stop() stops BOTH playing and listening; use stop_playing() so listening stays active!
        if hasattr(vc, "stop_playing"):
            vc.stop_playing()
        else:
            vc.stop()
        state.last_activity = time.monotonic()
        return True

    async def play_chime(self, guild_id: int, wav_bytes: bytes) -> bool:
        """Plays a short soft chime when idle (non-blocking, skipped if already speaking)."""
        state = self._guilds.get(guild_id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            return False
        if state.voice_client.is_playing() or not state.queue.empty():
            return False
        await state.queue.put(_PlaybackItem(audio_bytes=wav_bytes, audio_format="wav"))
        return True

    async def _playback_loop(self, state: _GuildVoiceState) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                item = await state.queue.get()
                if item is None:
                    break
                vc = state.voice_client
                if not vc or not vc.is_connected():
                    if item.done_event:
                        item.done_event.set()
                    continue

                tmp_path = None
                try:
                    suffix = ".mp3" if item.audio_format == "mp3" else ".wav"
                    fd, tmp_path = tempfile.mkstemp(prefix="jeff_tts_", suffix=suffix)
                    with os.fdopen(fd, "wb") as f:
                        f.write(item.audio_bytes)

                    done = asyncio.Event()

                    def _after(err: Optional[Exception]) -> None:
                        if err:
                            logger.warning(f"Voice playback error: {err}")
                        loop.call_soon_threadsafe(done.set)

                    source = discord.FFmpegPCMAudio(tmp_path)
                    vc.play(source, after=_after)
                    state.last_activity = time.monotonic()
                    await done.wait()
                    state.last_activity = time.monotonic()
                except Exception as e:
                    logger.warning(f"Voice playback failed: {e}")
                finally:
                    if item.done_event and not item.done_event.is_set():
                        item.done_event.set()
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
        except asyncio.CancelledError:
            pass

    async def speak(
        self,
        guild_id: int,
        text: str,
        voice: Optional[str] = None,
        *,
        wait_finished: bool = False,
    ) -> Optional[TTSResult]:
        """Synthesize and enqueue audio for a connected guild. Returns TTSResult or None."""
        state = self._guilds.get(guild_id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            return None
        if not self.tts.has_any_backend:
            return None

        result = await self.tts.synthesize(text, voice=voice)
        if result is None:
            return None
        state.last_activity = time.monotonic()
        done_event = asyncio.Event() if wait_finished else None
        await state.queue.put(
            _PlaybackItem(
                audio_bytes=result.audio_bytes,
                audio_format=result.audio_format,
                done_event=done_event,
            )
        )
        if done_event is not None:
            await done_event.wait()
        return result

    def speak_if_connected(self, guild_id: Optional[int], text: Optional[str]) -> None:
        """Fire-and-forget voice readout when Jeff is in VC and auto_speak is on."""
        if not self.enabled or not self.auto_speak or not text:
            return
        target_ids = [guild_id] if guild_id else self.connected_guild_ids()
        for gid in target_ids:
            if gid and self.is_connected(gid):
                asyncio.create_task(self._safe_speak(gid, text))

    async def _safe_speak(self, guild_id: int, text: str) -> None:
        try:
            await self.speak(guild_id, text)
        except Exception as e:
            logger.debug(f"Auto-speak skipped: {e}")

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Automatically disconnect if everyone else leaves Jeff's voice channel."""
        guild_id = member.guild.id
        state = self._guilds.get(guild_id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            return

        # If the bot itself was forcibly disconnected by an admin
        if member.bot and state.voice_client.user and member.id == state.voice_client.user.id:
            if after.channel is None:
                await self.leave(guild_id)
            return

        channel = state.voice_client.channel
        if channel and before.channel == channel and after.channel != channel:
            humans = [m for m in channel.members if not m.bot]
            if not humans:
                logger.info(f"All humans left '{channel.name}' — disconnecting Jeff Bot.")
                await self.leave(guild_id)

    async def close(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        for gid in list(self._guilds.keys()):
            await self.leave(gid)
        await self.tts.close()
