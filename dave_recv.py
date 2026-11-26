"""
DAVE (E2EE) voice-receive patch layer for discord.py 2.7.x + davey 0.1.7 + discord-ext-voice-recv.

Why this exists:
  - discord.py 2.7.1 already runs the full DAVE MLS handshake (opcodes 21-31) and holds the live
    `davey.DaveSession` at `vc._connection.dave_session`.
  - discord.py 2.7.1 requires `davey>=0.1.7,<0.2` (davey 0.2.x breaks sending with discord.py 2.7.1).
  - Upstream `discord-ext-voice-recv` decrypts only the outer transport layer (AEAD XChaCha20-Poly1305)
    and feeds raw MLS/DAVE ciphertext directly into libopus -> "corrupted stream".
  - Existing community package `voice-recv-dave` pins `davey>=0.2.0` in its metadata, even though
    `davey 0.1.7` already exposes `DaveSession.decrypt(user_id, MediaType.audio, packet)`.

This module patches `discord-ext-voice-recv` in-place to:
  1. Strip RFC 3550 RTP padding when the padding bit is set.
  2. Decrypt inbound DAVE audio frames via discord.py's existing `dave_session` BEFORE the jitter
     buffer / FEC path (so FEC lookahead never double-decrypts a packet).
  3. Re-sync the transport secret key on `CryptoError` if Discord rotates keys mid-session.
  4. Guard `PacketDecoder._decode_packet` against `OpusError` so one bad frame never kills the
     packet router thread.
  5. Guard two upstream `discord-ext-voice-recv` edge cases: `_remove_ssrc` when `_reader` is
     MISSING, and `_parse_bede_header` walking past zero-padded extension headers.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import threading
import time
import wave
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import discord
import discord.opus

logger = logging.getLogger(__name__)

_COUNTERS = ("ok", "fail", "nouser", "not_ready", "bypass", "passthru", "reheal", "padded")
_readers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_decode_errors = {"n": 0}
_install_lock = threading.Lock()
_installed = False

_OPUS_CANDIDATE_PATHS = (
    "/opt/homebrew/lib/libopus.dylib",
    "/opt/homebrew/lib/libopus.0.dylib",
    "/usr/local/lib/libopus.dylib",
    "/usr/local/lib/libopus.0.dylib",
    "libopus.so.0",
    "libopus.so",
    "libopus.0.dylib",
    "libopus.dylib",
)


def _should_log(n: int) -> bool:
    return n in (1, 10, 50, 250) or n % 1000 == 0


def ensure_opus_loaded() -> bool:
    """Loads libopus if not already loaded (including Homebrew paths on macOS)."""
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus._load_default()
        if discord.opus.is_loaded():
            return True
    except Exception:
        pass

    for candidate in _OPUS_CANDIDATE_PATHS:
        if os.path.isabs(candidate) and not os.path.exists(candidate):
            continue
        try:
            discord.opus.load_opus(candidate)
            if discord.opus.is_loaded():
                logger.info(f"Loaded libopus from {candidate}")
                return True
        except Exception:
            continue
    return False


def strip_rtp_padding(data: bytes) -> bytes:
    """Strips RFC 3550 trailing RTP padding bytes when the RTP P-bit is set."""
    if not data:
        return data
    pad_len = data[-1]
    if 0 < pad_len <= len(data):
        return data[:-pad_len]
    return data


def _resync_key(reader: Any, vc: Any, state: dict) -> None:
    """Re-syncs the receive transport key from discord.py if Discord rotated it on reconnect."""
    try:
        key = getattr(vc, "secret_key", None)
        if not key:
            return
        key_bytes = bytes(key)
        if key_bytes == state["key"]:
            state["xerr"] += 1
            if _should_log(state["xerr"]):
                logger.warning(
                    "Transport decrypt failed %d time(s) with current secret key; waiting for key update",
                    state["xerr"],
                )
            return
        reader.update_secret_key(key_bytes)
        state["key"] = key_bytes
        state["xerr"] = 0
        state["reheal"] += 1
        logger.info("Voice receive secret key re-synced from VoiceClient (#%d)", state["reheal"])
    except Exception as e:
        logger.debug("Voice receive key re-sync failed: %s", e)


def _wrap_reader_decryptor(reader: Any, davey_mod: Any, silence: bytes, crypto_error: type[Exception]) -> dict:
    """Wraps `reader.decryptor.decrypt_rtp` with RTP padding stripping + DAVE E2EE decryption."""
    vc = reader.voice_client
    transport_decrypt = reader.decryptor.decrypt_rtp
    key = getattr(vc, "secret_key", None)
    state: dict[str, Any] = {name: 0 for name in _COUNTERS}
    state.update(xerr=0, announced=False, key=bytes(key) if key else None)
    audio_media = davey_mod.MediaType.audio

    def decrypt_rtp(packet: Any) -> bytes:
        try:
            data = transport_decrypt(packet)
        except crypto_error:
            _resync_key(reader, vc, state)
            return silence
        state["xerr"] = 0

        if getattr(packet, "padding", False) and data:
            data = strip_rtp_padding(data)
            state["padded"] += 1
            if not data:
                return silence

        conn = getattr(vc, "_connection", None)
        proto_ver = getattr(conn, "dave_protocol_version", 0) or 0
        if proto_ver == 0:
            state["bypass"] += 1
            return data

        if data == silence:
            return data

        session = getattr(conn, "dave_session", None)
        if session is None or not session.ready:
            state["not_ready"] += 1
            return silence

        if not state["announced"]:
            state["announced"] = True
            logger.info(
                "DAVE E2EE active (v%s, epoch=%s): decrypting inbound voice frames",
                proto_ver,
                getattr(session, "epoch", "?"),
            )

        user_id = vc._get_id_from_ssrc(packet.ssrc)
        if user_id is None:
            state["nouser"] += 1
            return silence

        try:
            out = session.decrypt(int(user_id), audio_media, bytes(data))
        except Exception as e:
            err_text = str(e)
            if "Passthrough" in err_text or "Unencrypted" in err_text:
                state["passthru"] += 1
                return data
            state["fail"] += 1
            if _should_log(state["fail"]):
                logger.warning(
                    "DAVE decrypt failed (%d failed, %d ok, user=%s): %s",
                    state["fail"],
                    state["ok"],
                    user_id,
                    e,
                )
            return silence

        state["ok"] += 1
        if state["ok"] == 1:
            logger.info("First DAVE audio frame decrypted (user_id=%s, ssrc=%s)", user_id, packet.ssrc)
        return out

    reader.decryptor.decrypt_rtp = decrypt_rtp
    _readers[reader] = state
    return state


def _install_decrypt_patch() -> bool:
    try:
        import davey
    except ImportError:
        logger.warning("davey is not installed — cannot install DAVE voice-receive decrypt patch.")
        return False

    from discord.ext.voice_recv import reader as reader_module
    from discord.ext.voice_recv.rtp import OPUS_SILENCE
    from nacl.exceptions import CryptoError

    cls = reader_module.AudioReader
    if getattr(cls, "_jeff_dave_patched", False):
        return True

    original_init = cls.__init__

    def _patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            _wrap_reader_decryptor(self, davey, OPUS_SILENCE, CryptoError)
        except Exception as e:
            logger.error("Failed to attach DAVE decryptor to AudioReader: %s", e)

    cls.__init__ = _patched_init
    cls._jeff_dave_patched = True
    return True


def _install_decode_guard() -> None:
    from discord.ext.voice_recv.opus import PacketDecoder
    from discord.opus import Decoder, OpusError

    if getattr(PacketDecoder, "_jeff_opus_guard", False):
        return

    original_decode = PacketDecoder._decode_packet
    pcm_silence = b"\x00" * Decoder.FRAME_SIZE

    def _guarded_decode(self, packet):
        try:
            return original_decode(self, packet)
        except OpusError as e:
            n = _decode_errors["n"] = _decode_errors["n"] + 1
            if _should_log(n):
                logger.warning(
                    "OpusError #%d on ssrc=%s (%s); substituting 20ms silence",
                    n,
                    getattr(self, "ssrc", "?"),
                    e,
                )
            return packet, pcm_silence

    PacketDecoder._decode_packet = _guarded_decode
    PacketDecoder._jeff_opus_guard = True


def _install_voice_recv_stability_guards() -> None:
    """Fixes two upstream discord-ext-voice-recv 0.5.2a179 edge-case crashes."""
    from discord.ext.voice_recv.voice_client import VoiceRecvClient
    from discord.ext.voice_recv.rtp import RTPPacket

    # 1. Upstream _remove_ssrc accesses self._reader.speaking_timer even when not listening
    if not getattr(VoiceRecvClient, "_jeff_ssrc_guard", False):
        def _safe_remove_ssrc(self, *, user_id: int) -> None:
            ssrc = self._id_to_ssrc.pop(user_id, None)
            if ssrc:
                if self._reader:
                    self._reader.speaking_timer.drop_ssrc(ssrc)
                self._ssrc_to_id.pop(ssrc, None)

        VoiceRecvClient._remove_ssrc = _safe_remove_ssrc
        VoiceRecvClient._jeff_ssrc_guard = True

    # 2. Upstream _parse_bede_header can walk past the extension block on zero-padded headers
    if not getattr(RTPPacket, "_jeff_bede_guard", False):
        def _safe_parse_bede_header(self, data: bytes, length: int) -> None:
            offset = 4
            end = min(len(data), 4 + length * 4)
            while offset < end:
                b = data[offset]
                if b == 0:
                    offset += 1
                    continue
                element_id = b >> 4
                if element_id == 15:
                    break
                element_len = 1 + (b & 0x0F)
                val_start = offset + 1
                val_end = val_start + element_len
                if val_end > end:
                    break
                self.extension_data[element_id] = data[val_start:val_end]
                offset = val_end

        RTPPacket._parse_bede_header = _safe_parse_bede_header
        RTPPacket._jeff_bede_guard = True


def install_dave_recv_patch(*, decode_guard: bool = True) -> bool:
    """Installs DAVE decryption + stability guards on discord-ext-voice-recv. Safe to call repeatedly."""
    global _installed
    with _install_lock:
        try:
            import discord.ext.voice_recv  # noqa: F401
        except ImportError:
            logger.info("discord-ext-voice-recv is not installed; voice receive disabled.")
            return False

        ensure_opus_loaded()
        _install_voice_recv_stability_guards()
        if decode_guard:
            _install_decode_guard()
        ok = _install_decrypt_patch()
        if ok and not _installed:
            import davey
            from discord.ext import voice_recv

            logger.info(
                "Installed DAVE voice-receive patch (davey=%s, voice_recv=%s, opus=%s)",
                getattr(davey, "__version__", "?"),
                getattr(voice_recv, "__version__", "?"),
                discord.opus.is_loaded(),
            )
            _installed = True
        return ok


def get_dave_recv_stats(vc: Any = None) -> dict[str, Any]:
    """Returns aggregate DAVE decryption counters and optional live VoiceClient session info."""
    total: dict[str, Any] = {name: 0 for name in _COUNTERS}
    if vc is not None and getattr(vc, "_reader", None) in _readers:
        state = _readers[vc._reader]
        for name in _COUNTERS:
            total[name] = int(state.get(name, 0))
        total["readers"] = 1
    else:
        for state in list(_readers.values()):
            for name in _COUNTERS:
                total[name] += int(state.get(name, 0))
        total["readers"] = len(_readers)

    total["opus_errors"] = _decode_errors["n"]
    total["e2ee_decrypted"] = total["ok"] > 0

    if vc is not None:
        conn = getattr(vc, "_connection", None)
        session = getattr(conn, "dave_session", None)
        total["dave_protocol_version"] = getattr(conn, "dave_protocol_version", 0)
        total["dave_ready"] = bool(session and session.ready)
        total["dave_epoch"] = getattr(session, "epoch", None) if session else None
        try:
            total["dave_users"] = list(session.get_user_ids()) if session else []
        except Exception:
            total["dave_users"] = []
        total["ssrc_map"] = dict(getattr(vc, "_ssrc_to_id", {}))

    return total


def _safe_filename_part(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_")
    return (cleaned or "user")[:32]


@dataclass
class RecordedTrack:
    user_id: int
    display_name: str
    path: Path
    duration_s: float
    packets: int
    bytes_pcm: int


def make_per_user_wave_sink() -> Any:
    """Factory returning a PerUserWaveSink instance (imports voice_recv lazily)."""
    from discord.ext import voice_recv

    class PerUserWaveSink(voice_recv.AudioSink):
        """Records decoded 48kHz stereo 16-bit PCM into separate per-speaker buffers for Phase 0 spike."""

        SAMPLE_RATE = 48000
        CHANNELS = 2
        SAMPLE_WIDTH = 2  # 16-bit signed LE

        def __init__(self) -> None:
            super().__init__()
            self._lock = threading.Lock()
            self._buffers: dict[int, bytearray] = {}
            self._names: dict[int, str] = {}
            self._packets: dict[int, int] = {}
            self._unknown_packets: int = 0
            self.started_at: float = time.monotonic()

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
                    with self._lock:
                        self._unknown_packets += 1
                    return
                uid = int(uid)
                member = vc.guild.get_member(uid) if (vc and vc.guild) else None
                if member and member.bot:
                    return
                name = member.display_name if member else f"user_{uid}"

            with self._lock:
                buf = self._buffers.get(uid)
                if buf is None:
                    buf = bytearray()
                    self._buffers[uid] = buf
                    self._packets[uid] = 0
                buf.extend(pcm)
                self._packets[uid] += 1
                self._names[uid] = name

        def cleanup(self) -> None:
            pass

        def export_wavs(self, out_dir: Path, prefix: Optional[str] = None) -> list[RecordedTrack]:
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = prefix or time.strftime("%Y%m%d_%H%M%S")
            tracks: list[RecordedTrack] = []
            bytes_per_sec = self.SAMPLE_RATE * self.CHANNELS * self.SAMPLE_WIDTH

            with self._lock:
                items = [
                    (uid, self._names.get(uid, str(uid)), bytes(buf), self._packets.get(uid, 0))
                    for uid, buf in self._buffers.items()
                ]

            for uid, display_name, pcm_bytes, pkt_count in sorted(items, key=lambda x: x[1].lower()):
                if not pcm_bytes:
                    continue
                fname = f"{stamp}_{_safe_filename_part(display_name)}_{uid}.wav"
                fpath = out_dir / fname
                with wave.open(str(fpath), "wb") as wf:
                    wf.setnchannels(self.CHANNELS)
                    wf.setsampwidth(self.SAMPLE_WIDTH)
                    wf.setframerate(self.SAMPLE_RATE)
                    wf.writeframes(pcm_bytes)

                tracks.append(
                    RecordedTrack(
                        user_id=uid,
                        display_name=display_name,
                        path=fpath,
                        duration_s=round(len(pcm_bytes) / bytes_per_sec, 2),
                        packets=pkt_count,
                        bytes_pcm=len(pcm_bytes),
                    )
                )
            return tracks

        @property
        def unknown_packets(self) -> int:
            with self._lock:
                return self._unknown_packets

    return PerUserWaveSink()
