"""Tests for the per-world recap instructions: GET/POST
/api/ai/recap-instructions (app/routers/ai.py), the extra steering
app.ai.summarize_transcript's system prompt gets from it (e.g. "write the
summary in Spanish"), and that both the background-job and direct
summarize-from-audio paths thread the active world's setting through.
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


# ── GET/POST /api/ai/recap-instructions ─────────────────────────────────────

def test_instructions_empty_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/recap-instructions")
    assert r.status_code == 200
    assert r.json() == {"instructions": ""}


def test_instructions_save_and_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/recap-instructions", json={"instructions": "Write summaries in Spanish."})
    assert r.status_code == 200
    assert r.json()["instructions"] == "Write summaries in Spanish."
    r2 = client.get("/api/ai/recap-instructions")
    assert r2.json() == {"instructions": "Write summaries in Spanish."}


def test_instructions_save_strips_whitespace(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/recap-instructions", json={"instructions": "  Use a dry tone.  \n"})
    r = client.get("/api/ai/recap-instructions")
    assert r.json()["instructions"] == "Use a dry tone."


def test_instructions_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/recap-instructions")
    assert r.status_code == 403
    r2 = client.post("/api/ai/recap-instructions", json={"instructions": "x"})
    assert r2.status_code == 403


def test_instructions_scoped_per_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/recap-instructions", json={"instructions": "world-a-only"})

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/recap-instructions")
    assert r.json() == {"instructions": ""}


# ── app.ai._with_instructions / summarize_transcript ────────────────────────

def test_with_instructions_passthrough_when_blank():
    assert ai_module._with_instructions("BASE", "") == "BASE"


def test_with_instructions_appends_when_set():
    result = ai_module._with_instructions("BASE", "write in French")
    assert result.startswith("BASE")
    assert "write in French" in result


@pytest.mark.asyncio
async def test_summarize_transcript_single_call_applies_instructions(monkeypatch):
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["system"] = system
        return "a recap"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    await ai_module.summarize_transcript("short transcript", extra_instructions="write in French")
    assert "write in French" in captured["system"]


@pytest.mark.asyncio
async def test_summarize_transcript_reduce_step_applies_instructions_not_chunk_step(monkeypatch):
    """Chunked (map-reduce) path: the per-chunk extraction system prompt
    should NOT carry the instructions (it's a terse scratch list, not the
    final recap) — only the reduce step that produces the actual recap
    should."""
    systems_seen = []

    async def fake_generate_chat(messages, system="", model="", options=None):
        systems_seen.append(system)
        return "part or final"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 20)

    long_transcript = "word " * 30  # forces >1 chunk at a 20-char budget
    await ai_module.summarize_transcript(long_transcript, extra_instructions="write in French")

    # Every call except the last (the reduce step) is a chunk-extraction call.
    assert len(systems_seen) >= 2
    for system in systems_seen[:-1]:
        assert "write in French" not in system
    assert "write in French" in systems_seen[-1]


# ── audio_jobs threads the world's recap_instructions through ──────────────

@pytest.mark.asyncio
async def test_background_job_uses_worlds_recap_instructions(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.recap_instructions = "Write in French"
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        return "the party fought goblins"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["extra_instructions"] = extra_instructions
        return "un recap"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured.get("extra_instructions") == "Write in French"


@pytest.mark.asyncio
async def test_resummarize_job_uses_worlds_recap_instructions(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.recap_instructions = "Write in French"
        db.commit()
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            status="done", transcript="the party fought goblins",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    captured = {}

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["extra_instructions"] = extra_instructions
        return "un recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio_jobs.start_resummarize_job(job_id)
    await _await_terminal(job_id)
    assert captured.get("extra_instructions") == "Write in French"
