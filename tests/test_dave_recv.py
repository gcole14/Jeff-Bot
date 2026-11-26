import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from nacl.exceptions import CryptoError

import dave_recv


class _FakeDavey:
    class MediaType:
        audio = "AUDIO"


class _FakeSession:
    def __init__(self, *, ready: bool = True, fail_mode: str | None = None):
        self.ready = ready
        self.epoch = 3
        self.fail_mode = fail_mode
        self.calls: list[tuple[int, str, bytes]] = []

    def decrypt(self, user_id: int, media_type: str, packet: bytes) -> bytes:
        self.calls.append((user_id, media_type, packet))
        if self.fail_mode == "passthrough":
            raise ValueError("UnencryptedWhenPassthroughDisabled")
        if self.fail_mode == "error":
            raise ValueError("Failed to decrypt: NoDecryptorForUser")
        assert packet.startswith(b"E2EE:")
        return packet[5:]

    def get_user_ids(self) -> list[int]:
        return [111, 222]


class _FakeReader:
    def __init__(self, vc, decryptor, updated_keys):
        self.voice_client = vc
        self.decryptor = decryptor
        self._updated_keys = updated_keys

    def update_secret_key(self, key: bytes) -> None:
        self._updated_keys.append(key)


def _make_reader(
    *,
    session: _FakeSession | None,
    proto_ver: int = 1,
    ssrc_map: dict[int, int] | None = None,
    transport_fn=None,
    secret_key: bytes = b"k" * 32,
):
    conn = SimpleNamespace(dave_session=session, dave_protocol_version=proto_ver)
    mapping = ssrc_map if ssrc_map is not None else {42: 111}
    vc = SimpleNamespace(
        _connection=conn,
        secret_key=list(secret_key),
        _ssrc_to_id=mapping,
        _get_id_from_ssrc=lambda ssrc: mapping.get(ssrc),
    )

    updated_keys: list[bytes] = []
    reader = _FakeReader(
        vc,
        SimpleNamespace(decrypt_rtp=transport_fn or (lambda pkt: pkt.payload)),
        updated_keys,
    )
    state = dave_recv._wrap_reader_decryptor(reader, _FakeDavey, b"\xf8\xff\xfe", CryptoError)
    return reader, state, vc


def test_strip_rtp_padding():
    assert dave_recv.strip_rtp_padding(b"abc\x00\x00\x03") == b"abc"
    assert dave_recv.strip_rtp_padding(b"hello\x01") == b"hello"
    assert dave_recv.strip_rtp_padding(b"") == b""
    assert dave_recv.strip_rtp_padding(b"ab\x10") == b"ab\x10"  # invalid pad len > len(data)


def test_dave_decrypt_e2ee_and_padding():
    sess = _FakeSession(ready=True)
    reader, state, _ = _make_reader(session=sess, proto_ver=1)

    pkt = SimpleNamespace(ssrc=42, padding=True, payload=b"E2EE:opusframe\x00\x02")
    out = reader.decryptor.decrypt_rtp(pkt)

    assert out == b"opusframe"
    assert state["ok"] == 1
    assert state["padded"] == 1
    assert sess.calls == [(111, "AUDIO", b"E2EE:opusframe")]


def test_dave_decrypt_silence_bypasses_davey():
    sess = _FakeSession(ready=True)
    reader, state, _ = _make_reader(session=sess, proto_ver=1)

    pkt = SimpleNamespace(ssrc=42, padding=False, payload=b"\xf8\xff\xfe")
    out = reader.decryptor.decrypt_rtp(pkt)

    assert out == b"\xf8\xff\xfe"
    assert sess.calls == []
    assert state["ok"] == 0


def test_dave_decrypt_non_e2ee_passes_through():
    sess = _FakeSession(ready=True)
    reader, state, _ = _make_reader(session=sess, proto_ver=0)

    pkt = SimpleNamespace(ssrc=42, padding=False, payload=b"plain-opus")
    out = reader.decryptor.decrypt_rtp(pkt)

    assert out == b"plain-opus"
    assert state["bypass"] == 1
    assert sess.calls == []


def test_dave_decrypt_not_ready_and_unknown_ssrc_return_silence():
    sess = _FakeSession(ready=False)
    reader, state, _ = _make_reader(session=sess, proto_ver=1)
    assert reader.decryptor.decrypt_rtp(SimpleNamespace(ssrc=42, padding=False, payload=b"E2EE:x")) == b"\xf8\xff\xfe"
    assert state["not_ready"] == 1

    sess2 = _FakeSession(ready=True)
    reader2, state2, _ = _make_reader(session=sess2, proto_ver=1, ssrc_map={})
    assert reader2.decryptor.decrypt_rtp(SimpleNamespace(ssrc=99, padding=False, payload=b"E2EE:x")) == b"\xf8\xff\xfe"
    assert state2["nouser"] == 1


def test_dave_decrypt_passthrough_vs_real_error():
    sess_pt = _FakeSession(ready=True, fail_mode="passthrough")
    reader_pt, state_pt, _ = _make_reader(session=sess_pt, proto_ver=1)
    assert reader_pt.decryptor.decrypt_rtp(SimpleNamespace(ssrc=42, padding=False, payload=b"plain")) == b"plain"
    assert state_pt["passthru"] == 1

    sess_err = _FakeSession(ready=True, fail_mode="error")
    reader_err, state_err, _ = _make_reader(session=sess_err, proto_ver=1)
    assert reader_err.decryptor.decrypt_rtp(SimpleNamespace(ssrc=42, padding=False, payload=b"bad")) == b"\xf8\xff\xfe"
    assert state_err["fail"] == 1


def test_transport_crypto_error_resyncs_key():
    sess = _FakeSession(ready=True)

    def _boom(_pkt):
        raise CryptoError("bad key")

    reader, state, vc = _make_reader(session=sess, proto_ver=1, transport_fn=_boom, secret_key=b"A" * 32)
    vc.secret_key = list(b"B" * 32)
    out = reader.decryptor.decrypt_rtp(SimpleNamespace(ssrc=42, padding=False, payload=b"x"))

    assert out == b"\xf8\xff\xfe"
    assert state["reheal"] == 1
    assert reader._updated_keys == [b"B" * 32]


def test_install_patch_and_per_user_wave_sink(tmp_path: Path):
    assert dave_recv.install_dave_recv_patch() is True
    assert dave_recv.ensure_opus_loaded() is True

    sink = dave_recv.make_per_user_wave_sink()
    alice = SimpleNamespace(id=101, display_name="Alice!", bot=False)
    bob = SimpleNamespace(id=202, display_name="Bob", bot=False)
    jeff = SimpleNamespace(id=999, display_name="Jeff Bot", bot=True)

    frame = b"\x01\x02" * 1920  # 3840 bytes = 20ms stereo 48kHz 16-bit PCM
    sink.write(alice, SimpleNamespace(pcm=frame))
    sink.write(alice, SimpleNamespace(pcm=frame))
    sink.write(bob, SimpleNamespace(pcm=frame))
    sink.write(jeff, SimpleNamespace(pcm=frame))  # ignored (bot)

    tracks = sink.export_wavs(tmp_path, prefix="test")
    assert len(tracks) == 2
    by_id = {t.user_id: t for t in tracks}
    assert set(by_id.keys()) == {101, 202}
    assert by_id[101].packets == 2
    assert by_id[101].bytes_pcm == len(frame) * 2
    assert by_id[202].packets == 1

    with wave.open(str(by_id[101].path), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 48000
        assert wf.readframes(wf.getnframes()) == frame * 2
