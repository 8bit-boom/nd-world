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
