"""Tests for opt-in server-side speech enhancement (DeepFilterNet):
GET/POST /api/ai/whisper/denoise (app/routers/ai.py), the availability
feature-detection in app.ai.speech_enhancement_available(), and the
"denoise" flag threaded into app.ai.transcribe_audio's per-world callers
(app/audio_jobs.py, app/routers/sessions.py).

This sandbox/CI environment does NOT have torch/deepfilternet installed
(they live in the optional requirements-denoise.txt layer — see
docs/DEPLOYMENT.md), so speech_enhancement_available() is reliably False
here. That's used directly rather than faked: the POST-rejects-when-
unavailable test needs no monkeypatching to be true right now, and a
POST-succeeds-when-available test monkeypatches the availability check
instead of the real dependency.
"""
import asyncio
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


async def _await_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


# ── speech_enhancement_available() ──────────────────────────────────────────

def test_speech_enhancement_unavailable_in_this_environment():
    """Documents/locks in the sandbox's real state: no torch/deepfilternet
    installed, so the feature-detection must report False, not raise."""
    assert ai_module.speech_enhancement_available() is False


# ── GET/POST /api/ai/whisper/denoise ────────────────────────────────────────

def test_denoise_disabled_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/denoise")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "available": False}


def test_denoise_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/denoise")
    assert r.status_code == 403
    r2 = client.post("/api/ai/whisper/denoise", json={"enabled": True})
    assert r2.status_code == 403


def test_denoise_enable_rejected_when_unavailable(client, seed):
    """The real, unmocked state of this environment: no dependency
    installed, so enabling must 400 rather than silently set a flag that
    would never actually do anything (see World.whisper_denoise's own
    docstring in app/models.py)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/whisper/denoise", json={"enabled": True})
    assert r.status_code == 400
    r2 = client.get("/api/ai/whisper/denoise")
    assert r2.json()["enabled"] is False


def test_denoise_enable_succeeds_when_available(client, seed, monkeypatch):
    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._ai, "speech_enhancement_available", lambda: True)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/whisper/denoise", json={"enabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": True}
    r2 = client.get("/api/ai/whisper/denoise")
    assert r2.json() == {"enabled": True, "available": True}


def test_denoise_can_be_disabled_again(client, seed, monkeypatch):
    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._ai, "speech_enhancement_available", lambda: True)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/denoise", json={"enabled": True})
    r = client.post("/api/ai/whisper/denoise", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_denoise_scoped_per_world(client, seed, monkeypatch):
    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._ai, "speech_enhancement_available", lambda: True)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/denoise", json={"enabled": True})

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/whisper/denoise")
    assert r.json()["enabled"] is False


# ── denoise_audio_file: graceful no-op when unavailable/failing ────────────

@pytest.mark.asyncio
async def test_denoise_audio_file_noop_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "speech_enhancement_available", lambda: True)

    def boom(path):
        raise RuntimeError("model not actually installed")
    monkeypatch.setattr(ai_module, "_denoise_audio_file_sync", boom)

    src = tmp_path / "clip.wav"
    src.write_bytes(b"x")
    out = await ai_module.denoise_audio_file(src)
    assert out == src


def test_denoise_audio_file_sync_always_outputs_wav(tmp_path, monkeypatch):
    """A browser mic recording is typically .webm/opus (see ndMicRecorder
    in base.html — no explicit mimeType is requested, so MediaRecorder
    picks the browser's own default), which torchaudio's save path can
    decode but can't reliably ENCODE back out. Forcing the denoised
    output to always be .wav — a format every backend can write — avoids
    a silent no-op (caught by denoise_audio_file's own try/except) on
    exactly the recordings this feature exists for. Verified here by
    injecting fake df.enhance/df.io/df.model modules, since the real
    dependency isn't installed in this environment."""
    import sys
    import types

    calls = {}

    fake_enhance_mod = types.ModuleType("df.enhance")
    fake_enhance_mod.enhance = lambda model, df_state, audio: audio

    class _FakeMeta:
        sample_rate = 44100

    fake_io_mod = types.ModuleType("df.io")
    fake_io_mod.load_audio = lambda path, sr=None: ("audio-tensor", _FakeMeta())
    fake_io_mod.resample = lambda audio, orig_sr, new_sr: audio

    def _fake_save_audio(path, audio, sr, log=False):
        calls["save_path"] = path
        calls["save_sr"] = sr

    fake_io_mod.save_audio = _fake_save_audio

    class _FakeModelParams:
        sr = 48000

    fake_model_mod = types.ModuleType("df.model")
    fake_model_mod.ModelParams = _FakeModelParams

    monkeypatch.setitem(sys.modules, "df.enhance", fake_enhance_mod)
    monkeypatch.setitem(sys.modules, "df.io", fake_io_mod)
    monkeypatch.setitem(sys.modules, "df.model", fake_model_mod)
    monkeypatch.setattr(ai_module, "_init_denoise_model", lambda: (object(), object()))

    src = tmp_path / "recording.webm"
    src.write_bytes(b"x")
    out = ai_module._denoise_audio_file_sync(src)

    assert out.name == "recording.denoised.wav"
    assert calls["save_path"] == str(out)
    assert calls["save_sr"] == 44100


@pytest.mark.asyncio
async def test_transcribe_one_file_sends_denoised_filename_not_original(tmp_path, monkeypatch):
    """The multipart upload's filename must match send_path (the actual
    bytes being POSTed) not the original path — a mismatch (e.g. claiming
    "recording.webm" while the body is really WAV, after denoising forces
    .wav — see test above) is at best misleading and risks confusing a
    stricter format prober on whisper.cpp's side than ffmpeg's own
    content-sniffing."""
    from .test_whisper import _patch_httpx, _FakeResponse

    ai_module.set_whisper_override("http://127.0.0.1:8090")
    captured = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "ok"}))

    src = tmp_path / "recording.webm"
    src.write_bytes(b"original webm bytes")
    denoised = tmp_path / "recording.denoised.wav"
    denoised.write_bytes(b"denoised wav bytes")

    async def fake_denoise(path):
        return denoised
    monkeypatch.setattr(ai_module, "denoise_audio_file", fake_denoise)

    await ai_module._transcribe_one_file(src, glossary="", language="", denoise=True)

    sent_filename = captured["post_kwargs"]["files"]["file"][0]
    assert sent_filename == "recording.denoised.wav"


# ── audio_jobs/sessions.py thread the per-world denoise flag through ───────

@pytest.mark.asyncio
async def test_background_job_uses_worlds_denoise_setting(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_denoise = True
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", language="", denoise=False, **kwargs):
        captured["denoise"] = denoise
        return "the party met an elf"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True, attachment_url="/uploads/x.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured.get("denoise") is True


@pytest.mark.asyncio
async def test_background_job_denoise_false_when_world_has_no_toggle(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_transcribe(path, glossary="", language="", denoise=False, **kwargs):
        captured["denoise"] = denoise
        return "the party met an elf"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True, attachment_url="/uploads/x.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured.get("denoise") is False


def test_summarize_from_audio_route_uses_worlds_denoise_setting(client, seed, monkeypatch):
    import io

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_denoise = True
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", language="", denoise=False, **kwargs):
        captured["denoise"] = denoise
        return "transcript"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "recap"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/api/sessions/ai/summarize-from-audio",
        files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    assert captured.get("denoise") is True
