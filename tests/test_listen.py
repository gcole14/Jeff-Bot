import asyncio
import io
import math
import struct
import time
import wave
from types import SimpleNamespace

import pytest
from aiohttp import web

from ai import DeepSeekClient
from listen import (
    DISCORD_BYTES_PER_SEC,
    PhraseSegmenter,
    STTClient,
    VoiceConversationMemory,
    VoiceListenerManager,
    extract_wake_question,
    generate_wake_chime_wav,
    is_stop_phrase,
    pcm48k_stereo_to_wav16k_mono,
    pcm_rms,
)


def _tone_frame_48k_stereo(amp: int = 4000, freq: float = 440.0, duration_s: float = 0.02) -> bytes:
    """Generates one 20ms frame (3840 bytes) of 48kHz stereo 16-bit signed PCM."""
    n = int(48000 * duration_s)
    buf = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2.0 * math.pi * freq * (i / 48000.0)))
        buf.extend(struct.pack("<hh", v, v))
    return bytes(buf)


def _silent_frame_48k_stereo(duration_s: float = 0.02) -> bytes:
    return b"\x00" * int(DISCORD_BYTES_PER_SEC * duration_s)


# -------------------------------------------------------------------------
# Wake-word & Stop-phrase matching
# -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript,expected",
    [
        ("Hey Jeff, how did I do last game?", "how did I do last game?"),
        ("Yo Jeff who is inting the hardest?", "who is inting the hardest?"),
        ("Okay Jeff, what should I build against Zed?", "what should I build against Zed?"),
        ("OK Jeff! Roast Cookmysock", "Roast Cookmysock"),
        ("Jeff, how many deaths did I average?", "how many deaths did I average?"),
        ("Hey Geoff, how did I do?", "how did I do?"),
        ("Yo chef, who carried that game?", "who carried that game?"),
        ("Um, hey Jeff, what's my win rate on Ahri?", "what's my win rate on Ahri?"),
        ("Hey Jeff!", ""),
        ("Jeff.", ""),
        ("Yo Geoff?", ""),
    ],
)
def test_extract_wake_question_matches(transcript: str, expected: str):
    assert extract_wake_question(transcript) == expected


@pytest.mark.parametrize(
    "transcript",
    [
        "",
        "   ",
        "Watch out mid, enemy jungler is bot side.",
        "I think Jeff played pretty well earlier.",
        "That death was so unlucky.",
        "Can you gank top?",
        "Let's do baron after this wave.",
    ],
)
def test_extract_wake_question_ignores_non_wake(transcript: str):
    assert extract_wake_question(transcript) is None


@pytest.mark.parametrize(
    "transcript,expected",
    [
        ("Jeff stop", True),
        ("Hey Jeff, stop!", True),
        ("Jeff shut up", True),
        ("Yo Jeff, shut up.", True),
        ("Stop Jeff", True),
        ("Shut up Geoff", True),
        ("Hey chef, stop talking", True),
        ("Okay Jeff, be quiet", True),
        ("Jeff cancel", True),
        ("Stop pushing mid!", False),
        ("Shut up and hit the nexus", False),
        ("Hey Jeff, why did I stop scaling?", False),
    ],
)
def test_is_stop_phrase(transcript: str, expected: bool):
    assert is_stop_phrase(transcript) is expected


# -------------------------------------------------------------------------
# Phrase splitting (0.7s silence, 0.4s min, 15s max, per-user isolation)
# -------------------------------------------------------------------------

def test_phrase_segmenter_splits_on_silence_and_drops_short():
    seg = PhraseSegmenter(silence_s=0.7, min_s=0.4, max_s=15.0, rms_threshold=300, preroll_frames=2)
    voiced = _tone_frame_48k_stereo(amp=5000)
    silent = _silent_frame_48k_stereo()

    # 1. Short blip (~0.2s) followed by 0.7s silence -> dropped (< 0.4s)
    t = 100.0
    for _ in range(10):  # 0.2s
        assert seg.push_frame(1, "Grant", voiced, now=t) is None
        t += 0.02
    assert seg.flush_expired(now=t + 0.75) == []

    # 2. Real phrase (~0.6s) + 0.7s silence -> emitted cleanly
    t = 200.0
    seg.push_frame(1, "Grant", silent, now=t)  # pre-roll
    t += 0.02
    for _ in range(30):  # 0.60s of speech
        assert seg.push_frame(1, "Grant", voiced, now=t) is None
        t += 0.02

    # Still under 0.7s silence -> nothing yet
    assert seg.flush_expired(now=t + 0.3) == []
    # Past 0.7s silence -> flushes
    flushed = seg.flush_expired(now=t + 0.75)
    assert len(flushed) == 1
    assert flushed[0].user_id == 1
    assert flushed[0].display_name == "Grant"
    assert flushed[0].duration_s >= 0.4


def test_phrase_segmenter_caps_at_max_seconds():
    seg = PhraseSegmenter(silence_s=0.7, min_s=0.4, max_s=1.0, rms_threshold=300, preroll_frames=0)
    voiced = _tone_frame_48k_stereo(amp=5000)
    t = 10.0
    emitted = None
    for _ in range(60):  # 1.2s total
        res = seg.push_frame(7, "Player", voiced, now=t)
        t += 0.02
        if res is not None:
            emitted = res
            break
    assert emitted is not None
    assert 0.98 <= emitted.duration_s <= 1.05


def test_phrase_segmenter_isolates_multiple_speakers():
    seg = PhraseSegmenter(silence_s=0.7, min_s=0.4, max_s=15.0, rms_threshold=300, preroll_frames=0)
    frame_a = _tone_frame_48k_stereo(amp=4000, freq=300.0)
    frame_b = _tone_frame_48k_stereo(amp=7000, freq=900.0)

    t = 50.0
    for _ in range(25):  # 0.5s each interleaved
        seg.push_frame(101, "Alice", frame_a, now=t)
        seg.push_frame(202, "Bob", frame_b, now=t)
        t += 0.02

    # Alice stops talking, Bob keeps talking
    for _ in range(40):  # 0.8s more for Bob only
        seg.push_frame(202, "Bob", frame_b, now=t)
        t += 0.02

    flushed = seg.flush_expired(now=t)
    assert len(flushed) == 1
    assert flushed[0].user_id == 101
    assert flushed[0].pcm == frame_a * 25

    flushed_b = seg.flush_expired(now=t + 0.8)
    assert len(flushed_b) == 1
    assert flushed_b[0].user_id == 202
    assert flushed_b[0].pcm == frame_b * 65


# -------------------------------------------------------------------------
# Audio Conversion & Wake Chime
# -------------------------------------------------------------------------

def test_pcm48k_to_wav16k_mono_and_chime():
    pcm = _tone_frame_48k_stereo(amp=6000) * 25  # 0.5s
    assert pcm_rms(pcm) > 1000
    assert pcm_rms(_silent_frame_48k_stereo()) == 0

    wav_bytes = pcm48k_stereo_to_wav16k_mono(pcm)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert 7500 <= wf.getnframes() <= 8500

    chime = generate_wake_chime_wav()
    with wave.open(io.BytesIO(chime), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 24000
        assert wf.getnframes() > 2000


# -------------------------------------------------------------------------
# Conversation Memory & 8s Follow-up Window
# -------------------------------------------------------------------------

def test_followup_window_and_history():
    mem = VoiceConversationMemory(max_turns=3, followup_s=8.0)
    mem.open_followup(guild_id=1, user_id=42, now=100.0)
    assert mem.is_followup_active(1, 42, now=105.0) is True
    assert mem.is_followup_active(1, 99, now=105.0) is False  # different speaker
    assert mem.is_followup_active(1, 42, now=109.0) is False  # expired

    for i in range(5):
        mem.add_exchange(1, "Grant", f"q{i}", f"a{i}", now=200.0 + i)
    hist = mem.get_history(1, now=210.0)
    assert len(hist) == 3
    assert [q for _, q, _ in hist] == ["q2", "q3", "q4"]


# -------------------------------------------------------------------------
# PC-Off Fallback & 30s Health Caching + Worker /stt Round-Trip
# -------------------------------------------------------------------------

def test_stt_client_pc_off_fallback_and_health_cache():
    async def _run():
        health_calls = 0
        stt_calls = 0
        worker_online = True
        stt_enabled = True

        async def handle_health(request):
            nonlocal health_calls
            health_calls += 1
            if not worker_online:
                return web.Response(status=503, text="down")
            return web.json_response({
                "status": "ok",
                "gpu": "RTX 5070 Ti",
                "stt_available": stt_enabled,
                "stt_model": "openai/whisper-large-v3-turbo",
            })

        async def handle_stt(request):
            nonlocal stt_calls
            stt_calls += 1
            body = await request.read()
            prompt = request.query.get("prompt", "")
            assert len(body) > 44
            return web.json_response({"text": f"Hey Jeff ({len(prompt)} chars)", "latency_ms": 210})

        app = web.Application()
        app.router.add_get("/health", handle_health)
        app.router.add_post("/stt", handle_stt)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        client = STTClient(worker_url=f"http://127.0.0.1:{port}", health_interval_s=30.0)
        try:
            # 1. First health check succeeds and caches for 30s
            assert await client.is_available() is True
            assert await client.is_available() is True
            assert health_calls == 1

            # 2. Transcription works while worker is up
            wav = pcm48k_stereo_to_wav16k_mono(_tone_frame_48k_stereo() * 25)
            text = await client.transcribe(wav, prompt="Hey Jeff, Cookmysock")
            assert text and text.startswith("Hey Jeff")
            assert stt_calls == 1

            # 3. PC turns off -> forced health check marks unavailable, and transcribe sends NOTHING
            worker_online = False
            assert await client.is_available(force=True) is False
            assert await client.transcribe(wav, prompt="Hey Jeff") is None
            assert stt_calls == 1  # did NOT hit /stt while worker is down!

            # 4. PC turns back on -> health recovers
            worker_online = True
            assert await client.is_available(force=True) is True
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_listener_manager_pc_off_disables_listening_and_deafens():
    async def _run():
        deaf_calls: list[tuple[int, bool]] = []
        stopped_listening = []

        class FakeVC:
            def __init__(self):
                self._listening = True
                self.channel = SimpleNamespace(members=[])

            def is_connected(self):
                return True

            def is_listening(self):
                return self._listening

            def stop_listening(self):
                self._listening = False
                stopped_listening.append(True)

        fake_vc = FakeVC()

        class FakeVoiceManager:
            tts = SimpleNamespace(primary_supports_tags=False)

            def is_connected(self, gid):
                return True

            def get_voice_client(self, gid):
                return fake_vc

            def connected_channel(self, gid):
                return fake_vc.channel

            async def set_self_deaf(self, gid, deaf):
                deaf_calls.append((gid, deaf))
                return True

        # Point STTClient at an unconfigured (empty) URL -> simulates PC off
        stt = STTClient(worker_url="", health_interval_s=30.0)
        mgr = VoiceListenerManager(
            voice_manager=FakeVoiceManager(),
            stt_client=stt,
            deepseek=SimpleNamespace(is_configured=False),
            aggregator=SimpleNamespace(get_roster=lambda: []),
            db=None,
            enabled=True,
        )
        try:
            assert await mgr.should_join_undeafened() is False
            active = await mgr.on_voice_join(guild_id=123)
            assert active is False
            assert mgr.is_listening(123) is False
            assert (123, True) in deaf_calls  # self-deafened cleanly when PC is off
            assert stopped_listening == [True]
        finally:
            await mgr.close()

    asyncio.run(_run())


def test_answer_voice_question_includes_linked_riot_id_and_history():
    async def _run():
        captured = {}

        async def _fake_chat(system_prompt, user_prompt, **kwargs):
            captured["user_prompt"] = user_prompt
            return "You went 12 and 2 on Ahri."

        client = DeepSeekClient(api_key="test-key")
        client._chat = _fake_chat  # type: ignore
        reply = await client.answer_voice_question(
            "How did I do?",
            "Cookmysock#cook: 3W-1L",
            "Grant",
            speaker_riot_id="Cookmysock#cook",
            history=[("Grant", "Who carried?", "You did on Ahri.")],
        )
        assert reply == "You went 12 and 2 on Ahri."
        assert "Cookmysock#cook" in captured["user_prompt"]
        assert "Who carried?" in captured["user_prompt"]
        assert "You did on Ahri." in captured["user_prompt"]

    asyncio.run(_run())
