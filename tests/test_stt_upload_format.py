"""Oversized / long audio is re-encoded before it goes to Studio (its request limit is 25 MiB and pieces are capped
at ten minutes). The re-encode is now mono 16 kHz Opus (Ogg), ~3 KB/s at 24k - about 2.7x smaller than the old
64 kbps MP3 with better fidelity (measured: scratchpad codec comparison, see docs/DEPLOYMENT.md). Studio has not
been verified to decode Ogg Opus uploads, so if it refuses one the same file is retried as MP3, which is then
remembered. STT_UPLOAD_FORMAT=mp3 skips Opus entirely."""
import asyncio
import io
import json
import shutil
import subprocess
import wave

import pytest

from app import ai as _ai
from app import unsloth_extras as ux


def _has_libopus() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libopus" in out


needs_opus = pytest.mark.skipif(not _has_libopus(), reason="needs ffmpeg with libopus")


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(_ai, "_stt_opus_rejected", False)
    monkeypatch.delenv("STT_UPLOAD_FORMAT", raising=False)
    monkeypatch.delenv("STT_OPUS_BITRATE", raising=False)
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")


# ── settings ─────────────────────────────────────────────────────────────────

def test_opus_is_the_default_upload_format_and_mp3_can_be_forced(monkeypatch):
    assert _ai._stt_upload_format() == "opus"
    monkeypatch.setenv("STT_UPLOAD_FORMAT", "mp3")
    assert _ai._stt_upload_format() == "mp3"
    monkeypatch.setenv("STT_UPLOAD_FORMAT", "flac")        # not an option: the default
    assert _ai._stt_upload_format() == "opus"


def test_a_remembered_rejection_means_mp3(monkeypatch):
    monkeypatch.setattr(_ai, "_stt_opus_rejected", True)
    assert _ai._stt_upload_format() == "mp3"


@pytest.mark.parametrize("raw, kbps", [("", 24), ("24k", 24), ("16K", 16), ("8k", 8), ("128k", 128), ("300k", 128),
                                       ("7k", 24), ("0", 24), ("fast", 24), ("24k; rm -rf /", 24), ("-3k", 24)])
def test_the_opus_upload_bitrate_is_bounded(monkeypatch, raw, kbps):
    monkeypatch.setenv("STT_OPUS_BITRATE", raw)
    assert _ai._stt_opus_kbps() == kbps


# ── the real encoder ─────────────────────────────────────────────────────────

def _wav_file(path, seconds, rate=16000, channels=1, noise=True):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
           "-i", (f"anoisesrc=d={seconds}:c=pink:r={rate}:a=0.3" if noise else f"sine=f=300:d={seconds}:r={rate}"),
           "-ac", str(channels), "-c:a", "pcm_s16le", str(path)]
    subprocess.run(cmd, check=True)
    return path


def _probe(path, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return json.loads(out)


@needs_opus
def test_the_opus_encode_is_mono_ogg_opus_and_much_smaller(tmp_path):
    src = _wav_file(tmp_path / "in.wav", 30, rate=44100, channels=2)
    out = asyncio.run(_ai._transcode_audio_to_opus(src, tmp_path))
    meta = _probe(out, "stream=codec_name,channels:format=format_name")
    assert out.suffix == ".ogg" and meta["streams"][0]["codec_name"] == "opus" and meta["streams"][0]["channels"] == 1
    assert "ogg" in meta["format"]["format_name"]
    assert out.stat().st_size < 30 * 5000, "about 3 KB/s, against the 8 KB/s of the old 64k MP3"


@needs_opus
def test_the_opus_bitrate_setting_changes_the_size(tmp_path, monkeypatch):
    src = _wav_file(tmp_path / "in.wav", 30)
    monkeypatch.setenv("STT_OPUS_BITRATE", "12k")
    small = asyncio.run(_ai._transcode_audio_to_opus(src, tmp_path)).stat().st_size
    monkeypatch.setenv("STT_OPUS_BITRATE", "64k")
    big = asyncio.run(_ai._transcode_audio_to_opus(src, tmp_path)).stat().st_size
    assert small < big


@needs_opus
def test_a_video_file_gives_an_audio_only_opus(tmp_path):
    video = tmp_path / "clip.mp4"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=3:s=160x120:r=10",
                        "-f", "lavfi", "-i", "sine=f=300:d=3", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)])
    if r.returncode != 0:
        pytest.skip("this ffmpeg cannot make a test video")
    out = asyncio.run(_ai._transcode_audio_to_opus(video, tmp_path))
    streams = _probe(out, "stream=codec_type")["streams"]
    assert [s["codec_type"] for s in streams] == ["audio"]


@needs_opus
def test_a_long_recording_goes_to_studio_as_a_few_small_ogg_pieces(tmp_path, monkeypatch):
    """Real ffmpeg end to end: 1000 s of audio (over the 15 minute re-split trigger and over the 23 MiB byte
    budget as WAV) -> Opus -> stream-copy split at 600 s -> two requests, each far under the request limit."""
    src = _wav_file(tmp_path / "session.wav", 1000)
    sent = []

    async def fake_stt(audio, name, model="small"):
        sent.append((name, len(audio)))
        return f"text of {name}"

    monkeypatch.setattr(ux, "stt", fake_stt)
    text = asyncio.run(_ai._transcribe_one_file(src))
    assert [n.rsplit(".", 1)[-1] for n, _ in sent] == ["ogg", "ogg"] and len(sent) == 2
    assert sum(b for _, b in sent) < 4_500_000, "about 3 MB of Opus, against ~8 MB as 64k MP3"
    assert all(b < _ai._UNSLOTH_STT_MAX_BYTES for _, b in sent) and text.count("\n") == 1


# ── fallback to MP3 (no ffmpeg needed: the encoders and Studio are stubbed) ──

def _big_file(tmp_path):
    f = tmp_path / "session.flac"
    f.write_bytes(b"b" * (24 * 1024 * 1024))                # over the byte budget -> re-encode path
    return f


def _stub_pipeline(monkeypatch, tmp_path, *, opus_encode_fails=False):
    made = []

    async def probe(p):
        return 300.0

    async def to_opus(path, tmpdir):
        made.append("opus")
        if opus_encode_fails:
            raise _ai._OpusUnusable("ffmpeg has no libopus")
        out = tmpdir / "x-nd-stt.ogg"
        out.write_bytes(b"o" * 1000)
        return out

    async def to_mp3(path, tmpdir):
        made.append("mp3")
        out = tmpdir / "x-nd-stt.mp3"
        out.write_bytes(b"m" * 1000)
        return out

    monkeypatch.setattr(_ai, "_probe_audio_duration", probe)
    monkeypatch.setattr(_ai, "_transcode_audio_to_opus", to_opus)
    monkeypatch.setattr(_ai, "_transcode_audio_to_mp3", to_mp3)
    return made


def _studio(monkeypatch, behave):
    sent = []

    async def fake_stt(audio, name, model="small"):
        sent.append(name)
        return behave(name)

    monkeypatch.setattr(ux, "stt", fake_stt)
    return sent


def test_opus_is_what_a_long_file_is_sent_as(tmp_path, monkeypatch):
    made = _stub_pipeline(monkeypatch, tmp_path)
    sent = _studio(monkeypatch, lambda name: "hello")
    assert asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path))) == "hello"
    assert made == ["opus"] and sent == ["x-nd-stt.ogg"]


@pytest.mark.parametrize("status", [400, 415, 422, 500])
def test_a_refused_opus_upload_is_retried_as_mp3_and_remembered(tmp_path, monkeypatch, status):
    made = _stub_pipeline(monkeypatch, tmp_path)

    def behave(name):
        if name.endswith(".ogg"):
            raise ux.StudioError("Could not decode audio", status)
        return "from mp3"

    sent = _studio(monkeypatch, behave)
    assert asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path))) == "from mp3"
    assert made == ["opus", "mp3"] and sent == ["x-nd-stt.ogg", "x-nd-stt.mp3"]
    assert _ai._stt_opus_rejected is True
    # the next file does not try Opus again
    made.clear(); sent.clear()
    assert asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path))) == "from mp3"
    assert made == ["mp3"] and sent == ["x-nd-stt.mp3"]


def test_a_missing_opus_encoder_falls_back_to_mp3(tmp_path, monkeypatch):
    made = _stub_pipeline(monkeypatch, tmp_path, opus_encode_fails=True)
    sent = _studio(monkeypatch, lambda name: "ok")
    assert asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path))) == "ok"
    assert made == ["opus", "mp3"] and sent == ["x-nd-stt.mp3"] and _ai._stt_opus_rejected is True


def test_when_mp3_fails_too_that_is_the_error_and_opus_is_not_blamed(tmp_path, monkeypatch):
    _stub_pipeline(monkeypatch, tmp_path)

    def behave(name):
        raise ux.StudioError("model exploded" if name.endswith(".mp3") else "Could not decode audio", 500)

    _studio(monkeypatch, behave)
    with pytest.raises(_ai.SttError) as ei:
        asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path)))
    assert "model exploded" in str(ei.value)
    assert _ai._stt_opus_rejected is False, "only remember Opus as unusable when MP3 got through"


@pytest.mark.parametrize("status, msg", [(409, "STT model 'small' is not downloaded."), (401, "Invalid or expired API key"),
                                         (503, "Unsloth Studio unreachable: ConnectError"), (429, "busy")])
def test_setup_and_transient_errors_are_not_mistaken_for_a_format_problem(tmp_path, monkeypatch, status, msg):
    made = _stub_pipeline(monkeypatch, tmp_path)

    def behave(name):
        raise ux.StudioError(msg, status)

    _studio(monkeypatch, behave)
    with pytest.raises(_ai.SttError) as ei:
        asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path)))
    assert msg in str(ei.value) and made == ["opus"], "no second attempt, no MP3"
    assert _ai._stt_opus_rejected is False


def test_stt_upload_format_mp3_never_touches_opus(tmp_path, monkeypatch):
    monkeypatch.setenv("STT_UPLOAD_FORMAT", "mp3")
    made = _stub_pipeline(monkeypatch, tmp_path)
    sent = _studio(monkeypatch, lambda name: "mp3 text")
    assert asyncio.run(_ai._transcribe_one_file(_big_file(tmp_path))) == "mp3 text"
    assert made == ["mp3"] and sent == ["x-nd-stt.mp3"]


def test_a_file_that_fits_is_still_sent_as_it_is(tmp_path, monkeypatch):
    made = _stub_pipeline(monkeypatch, tmp_path)

    async def probe(p):
        return 120.0
    monkeypatch.setattr(_ai, "_probe_audio_duration", probe)
    f = tmp_path / "clip.webm"
    f.write_bytes(b"w" * 50_000)
    sent = _studio(monkeypatch, lambda name: "plain")
    assert asyncio.run(_ai._transcribe_one_file(f)) == "plain"
    assert made == [] and sent == ["clip.webm"], "no re-encode when it fits"
