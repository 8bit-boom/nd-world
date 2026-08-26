"""Tests for the per-world Whisper spoken-language pin: GET/POST
/api/ai/whisper/language (app/routers/ai.py) and the "language" field it
threads into app.ai.transcribe_audio's request to whisper.cpp.

Root cause this closes: whisper.cpp's own server hardcodes language="en" as
its default and only overrides it when the client sends this field
explicitly (examples/server/server.cpp) — so a transcription request that
never sends "language" gets silently forced through English decoding
regardless of what's actually being spoken, which is what produced a
garbled, looping transcript for a non-English session rather than a clean
WhisperError. transcribe_audio's own "language" defaulting to "auto" (see
test_whisper.py) fixes that for everyone; this file covers the GM-facing
per-world pin on top of that default.
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


# ── GET/POST /api/ai/whisper/language ───────────────────────────────────────

def test_language_empty_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/language")
    assert r.status_code == 200
    assert r.json() == {"language": ""}


def test_language_save_and_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/whisper/language", json={"language": "ru"})
    assert r.status_code == 200
    assert r.json()["language"] == "ru"
    r2 = client.get("/api/ai/whisper/language")
    assert r2.json() == {"language": "ru"}


def test_language_save_strips_whitespace(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/language", json={"language": "  ru  \n"})
    r = client.get("/api/ai/whisper/language")
    assert r.json()["language"] == "ru"


def test_language_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/language")
    assert r.status_code == 403
    r2 = client.post("/api/ai/whisper/language", json={"language": "ru"})
    assert r2.status_code == 403


def test_language_scoped_per_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/language", json={"language": "ru"})

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/whisper/language")
    assert r.json() == {"language": ""}


def test_language_can_be_cleared_back_to_auto(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/language", json={"language": "ru"})
    client.post("/api/ai/whisper/language", json={"language": ""})
    r = client.get("/api/ai/whisper/language")
    assert r.json() == {"language": ""}


# ── audio_jobs looks up the job's world language at transcription time ─────

@pytest.mark.asyncio
async def test_background_job_uses_worlds_pinned_language(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_language = "ru"
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", language="", **kwargs):
        captured["language"] = language
        return "партия встретила эльфа"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True, attachment_url="/uploads/x.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured.get("language") == "ru"


@pytest.mark.asyncio
async def test_background_job_language_blank_when_world_has_no_pin(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_transcribe(path, glossary="", language="", **kwargs):
        captured["language"] = language
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
    assert captured.get("language") == ""


# ── sessions.py's direct (non-job) transcribe routes thread it too ─────────

def test_summarize_from_audio_route_uses_worlds_pinned_language(client, seed, monkeypatch):
    import io

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_language = "ru"
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", language="", **kwargs):
        captured["language"] = language
        return "транскрипт"

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
    assert captured.get("language") == "ru"
