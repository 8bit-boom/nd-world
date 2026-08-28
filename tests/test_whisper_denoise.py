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
