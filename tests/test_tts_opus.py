"""Studio's /v1/audio/speech only produces WAV. nd-world turns that into Ogg Opus before storing it (clips,
NPC voice lines, recap songs): far smaller than WAV and better than MP3 at the same size. If ffmpeg cannot
do it, or TTS_OUTPUT_FORMAT=wav, the WAV is kept - speech must never be lost to the conversion."""
import asyncio
import json
import shutil
import subprocess

import httpx
import pytest

from app import unsloth_extras as ux

from .conftest import GM_PASSWORD, login
from .test_unsloth_extras import _WAV, _patch_transport, _reset_auth_state, _strict_speech_studio

OPUS = b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead" + b"\x00" * 40


def _has_libopus() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libopus" in out


needs_opus = pytest.mark.skipif(not _has_libopus(), reason="needs ffmpeg with libopus")


# ── the converter ────────────────────────────────────────────────────────────

@needs_opus
def test_ffmpeg_turns_wav_into_ogg_opus():
    wav = ux._tone_wav_bytes(3.0)
    out = asyncio.run(ux._wav_to_opus(wav))
    assert out and out[:4] == b"OggS" and b"OpusHead" in out[:200], "an Ogg container holding Opus"
    assert len(out) < len(wav) / 4


@needs_opus
def test_the_opus_bitrate_is_configurable(monkeypatch):
    wav = ux._tone_wav_bytes(4.0)
    monkeypatch.setenv("TTS_OPUS_BITRATE", "12k")
    small = asyncio.run(ux._wav_to_opus(wav))
    monkeypatch.setenv("TTS_OPUS_BITRATE", "128k")
    big = asyncio.run(ux._wav_to_opus(wav))
    assert small and big and len(small) < len(big)


def test_a_bad_bitrate_falls_back_to_the_default(monkeypatch):
    for bad in ("", "fast", "0", "-5k", "999999k", "48k; rm -rf /"):
        monkeypatch.setenv("TTS_OPUS_BITRATE", bad)
        assert ux._opus_bitrate() == "48k", bad
    monkeypatch.setenv("TTS_OPUS_BITRATE", "64K")
    assert ux._opus_bitrate() == "64k"


def test_the_converter_reports_none_when_ffmpeg_is_missing_or_fails(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(ux._wav_to_opus(_WAV)) is None


# ── tts() ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_still_asks_studio_for_wav_but_returns_opus(monkeypatch):
    seen, got = [], []

    async def fake_convert(wav):
        got.append(wav)
        return OPUS

    _patch_transport(monkeypatch, _strict_speech_studio(seen))
    monkeypatch.setattr(ux, "_wav_to_opus", fake_convert)
    monkeypatch.delenv("TTS_OUTPUT_FORMAT", raising=False)
    audio, ct = await ux.tts("hello there", model="unsloth/orpheus-3b", voice="tara")
    assert seen == ["wav"], "Studio is still asked for the only format it has"
    assert got == [_WAV] and audio == OPUS and ct == ux.OPUS_CONTENT_TYPE


@pytest.mark.asyncio
async def test_tts_keeps_the_wav_when_conversion_fails(monkeypatch):
    async def no_convert(wav):
        return None

    _patch_transport(monkeypatch, _strict_speech_studio([]))
    monkeypatch.setattr(ux, "_wav_to_opus", no_convert)
    audio, ct = await ux.tts("hi", model="m")
    assert audio == _WAV and ct == "audio/wav"


@pytest.mark.asyncio
async def test_tts_output_format_wav_skips_the_conversion(monkeypatch):
    async def must_not_run(wav):
        raise AssertionError("converted although TTS_OUTPUT_FORMAT=wav")

    _patch_transport(monkeypatch, _strict_speech_studio([]))
    monkeypatch.setattr(ux, "_wav_to_opus", must_not_run)
    monkeypatch.setenv("TTS_OUTPUT_FORMAT", "wav")
    audio, ct = await ux.tts("hi", model="m")
    assert audio == _WAV and ct == "audio/wav"


@pytest.mark.asyncio
async def test_audio_that_is_not_wav_is_never_sent_to_ffmpeg(monkeypatch):
    async def must_not_run(wav):
        raise AssertionError("ffmpeg was handed something that is not a WAV")

    _patch_transport(monkeypatch, lambda r: httpx.Response(
        200, content=b"ID3 whatever", headers={"content-type": "audio/mpeg"}))
    monkeypatch.setattr(ux, "_wav_to_opus", must_not_run)
    audio, ct = await ux.tts("hi", model="m")
    assert audio == b"ID3 whatever" and ct == "audio/mpeg"


@pytest.mark.parametrize("content_type, ext", [
    ("audio/ogg; codecs=opus", ".opus"), ("audio/opus", ".opus"), ("audio/ogg", ".opus"),
    ("audio/wav", ".wav"), ("audio/x-wav", ".wav"), ("audio/wave", ".wav"),
    ("audio/mpeg", ".mp3"), ("", ".mp3"), (None, ".mp3"),
])
def test_the_file_extension_follows_the_content_type(content_type, ext):
    assert ux.audio_extension(content_type) == ext


@pytest.mark.asyncio
async def test_test_tts_says_so_when_it_could_not_make_opus(monkeypatch):
    _reset_auth_state(monkeypatch)

    async def no_convert(wav):
        return None

    _patch_transport(monkeypatch, _strict_speech_studio([]))
    monkeypatch.setattr(ux, "_wav_to_opus", no_convert)
    monkeypatch.delenv("TTS_OUTPUT_FORMAT", raising=False)
    out = await ux.tts_health("m", voice="v")
    assert out["ok"] is True and "wav" in out["message"].lower() and "ffmpeg" in out["message"].lower(), out

    async def convert(wav):
        return OPUS
    monkeypatch.setattr(ux, "_wav_to_opus", convert)
    out = await ux.tts_health("m", voice="v")
    assert out["ok"] is True and "ffmpeg" not in out["message"].lower(), out


# ── the stored clip ──────────────────────────────────────────────────────────

def test_the_tts_route_stores_and_serves_an_opus_clip(client, seed, monkeypatch):
    async def fake_tts(text, model, voice="", response_format="wav", speed=1.0, instructions="", language=""):
        return OPUS, ux.OPUS_CONTENT_TYPE

    monkeypatch.setattr("app.routers.ai._unsloth_extras.tts", fake_tts)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/tts", json={"text": "The harbor gates close at dusk.", "name": "Gate line"})
    assert r.status_code == 200, r.text
    url = r.json()["file_url"]
    assert url.startswith("/uploads/audio/") and url.endswith(".opus")
    served = client.get(url)
    assert served.status_code == 200 and served.content == OPUS
    assert served.headers["content-type"].startswith("audio/ogg"), "browsers need an audio type for <audio>"


# ── Test TTS reports what Studio actually sent ───────────────────────────────

def _make_wav(channels=1, rate=24000, seconds=1.0, bits=16, header="normal") -> bytes:
    import io
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(rate * seconds) * channels * (bits // 8))
    data = bytearray(buf.getvalue())
    if header == "streaming":                       # a server that does not know the length up front
        data[4:8] = struct.pack("<I", 0xFFFFFFFF)
        i = data.find(b"data")
        data[i + 4:i + 8] = struct.pack("<I", 0xFFFFFFFF)
    return bytes(data)


@pytest.mark.parametrize("kwargs, expected", [
    (dict(channels=1, rate=24000, seconds=1.0), (1, 24000, 16, 1.0)),
    (dict(channels=2, rate=44100, seconds=0.5), (2, 44100, 16, 0.5)),
    (dict(channels=1, rate=24000, seconds=2.0, header="streaming"), (1, 24000, 16, 2.0)),
])
def test_wav_info_reads_channels_rate_bits_and_length(kwargs, expected):
    info = ux.wav_info(_make_wav(**kwargs))
    assert (info["channels"], info["sample_rate"], info["bits"], round(info["seconds"], 2)) == expected


@pytest.mark.parametrize("junk", [b"", b"RIFF", b"ID3 not a wav", _WAV, _make_wav()[:30], b"RIFF\x00\x00\x00\x00WAVEjunk"])
def test_wav_info_gives_none_for_anything_that_is_not_a_readable_wav(junk):
    assert ux.wav_info(junk) is None


@pytest.mark.parametrize("n, label", [(1, "mono"), (2, "stereo"), (6, "6 channels")])
def test_channel_labels(n, label):
    assert ux.channel_label(n) == label


@pytest.mark.asyncio
async def test_test_tts_shows_channels_and_sample_rate(monkeypatch):
    _reset_auth_state(monkeypatch)
    wav = _make_wav(channels=1, rate=24000, seconds=1.0)

    def handler(request):
        return httpx.Response(200, content=wav, headers={"content-type": "audio/wav"})

    async def convert(w):
        return OPUS
    _patch_transport(monkeypatch, handler)
    monkeypatch.setattr(ux, "_wav_to_opus", convert)
    monkeypatch.delenv("TTS_OUTPUT_FORMAT", raising=False)
    out = await ux.tts_health("unsloth/orpheus-3b", voice="tara")
    assert out["ok"] is True
    assert "mono" in out["message"] and "24000 Hz" in out["message"] and "16-bit" in out["message"], out["message"]
    assert "Opus" in out["message"] and "bytes" in out["message"]
    assert out["audio"] == {"format": "wav", "channels": 1, "channel_label": "mono", "sample_rate": 24000,
                            "bits": 16, "seconds": 1.0, "bytes": len(wav)}
    assert out["saved_as"]["format"] == "opus" and out["saved_as"]["bytes"] == len(OPUS)


@pytest.mark.asyncio
async def test_test_tts_says_stereo_when_studio_sends_stereo(monkeypatch):
    _reset_auth_state(monkeypatch)
    wav = _make_wav(channels=2, rate=44100, seconds=0.5)
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, content=wav, headers={"content-type": "audio/wav"}))
    monkeypatch.setenv("TTS_OUTPUT_FORMAT", "wav")
    out = await ux.tts_health("m")
    assert "stereo" in out["message"] and "44100 Hz" in out["message"], out["message"]
    assert out["saved_as"]["format"] == "wav" and out["audio"]["channels"] == 2


@pytest.mark.asyncio
async def test_test_tts_still_reports_when_studio_audio_is_not_a_wav(monkeypatch):
    _reset_auth_state(monkeypatch)
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, content=b"ID3 whatever", headers={"content-type": "audio/mpeg"}))
    out = await ux.tts_health("m")
    assert out["ok"] is True and "audio/mpeg" in out["message"] and "bytes" in out["message"]
    assert "audio" not in out or out["audio"].get("channels") is None
