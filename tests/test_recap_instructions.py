"""Tests for the per-world recap instructions: GET/POST
/api/ai/recap-instructions (app/routers/ai.py), the extra steering
app.ai.summarize_transcript's system prompt gets from it (e.g. "write the
summary in Spanish"), and that both the background-job and direct
summarize-from-audio paths thread the active world's setting through.

Also covers the per-JOB extra_instructions layered on top of the world's
standing recap_instructions (app.audio_jobs._combined_recap_instructions,
and its identical small copy app.routers.sessions._combine_recap_instructions)
— a one-off note for a single summarize/resummarize call, e.g. "this
session was mostly downtime, keep it short", that doesn't replace the
world's own persistent setting.
"""
import asyncio
import io
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob, World
from app.routers.sessions import _combine_recap_instructions as _sessions_combine

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
async def test_summarize_transcript_chunked_path_applies_instructions_to_every_part(monkeypatch):
    """Chunked path: with no separate combine call (see
    test_transcript_chunking.py for why — a single combine call and an
    iterative refine chain were both tried and reverted), a GM's
    instructions have to reach every per-part call directly, since each
    part's own summary is final as written."""
    systems_seen = []

    async def fake_generate_chat(messages, system="", model="", options=None):
        systems_seen.append(system)
        return "part summary"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 20)

    long_transcript = "word " * 30  # forces >1 chunk at a 20-char budget
    await ai_module.summarize_transcript(long_transcript, extra_instructions="write in French")

    assert len(systems_seen) >= 2
    assert all("write in French" in s for s in systems_seen)


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


# ── _combined_recap_instructions (both copies: audio_jobs.py and its small
# per-module copy in routers/sessions.py) — world-level and job-level notes
# are additive, neither replaces the other. ─────────────────────────────────

@pytest.mark.parametrize("combine", [audio_jobs._combined_recap_instructions, _sessions_combine])
def test_combine_world_only(combine):
    assert combine("Write in French", "") == "Write in French"


@pytest.mark.parametrize("combine", [audio_jobs._combined_recap_instructions, _sessions_combine])
def test_combine_job_only(combine):
    assert combine("", "Focus on combat") == "Focus on combat"


@pytest.mark.parametrize("combine", [audio_jobs._combined_recap_instructions, _sessions_combine])
def test_combine_both_world_first(combine):
    result = combine("Write in French", "Focus on combat")
    assert "Write in French" in result
    assert "Focus on combat" in result
    assert result.index("Write in French") < result.index("Focus on combat")


@pytest.mark.parametrize("combine", [audio_jobs._combined_recap_instructions, _sessions_combine])
def test_combine_neither_is_empty(combine):
    assert combine("", "") == ""


@pytest.mark.parametrize("combine", [audio_jobs._combined_recap_instructions, _sessions_combine])
def test_combine_strips_whitespace_only_input(combine):
    assert combine("  ", "  ") == ""


# ── Per-job extra_instructions layered on top of the world default ─────────

@pytest.mark.asyncio
async def test_background_job_combines_world_and_job_instructions(client, seed, tmp_path, monkeypatch):
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
        audio_path=audio, delete_after=True, extra_instructions="Focus on combat",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.extra_instructions == "Focus on combat"
    assert "Write in French" in captured["extra_instructions"]
    assert "Focus on combat" in captured["extra_instructions"]


@pytest.mark.asyncio
async def test_resummarize_job_combines_world_and_job_instructions(client, seed, tmp_path, monkeypatch):
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

    audio_jobs.start_resummarize_job(job_id, extra_instructions="Focus on combat")
    await _await_terminal(job_id)
    assert "Write in French" in captured["extra_instructions"]
    assert "Focus on combat" in captured["extra_instructions"]


@pytest.mark.asyncio
async def test_resummarize_job_blank_instructions_keeps_previous(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            status="done", transcript="the party fought goblins", extra_instructions="Focus on combat",
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
        return "recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio_jobs.start_resummarize_job(job_id, extra_instructions="")
    updated = await _await_terminal(job_id)
    assert updated.extra_instructions == "Focus on combat"
    assert captured["extra_instructions"] == "Focus on combat"


@pytest.mark.asyncio
async def test_resummarize_job_new_instructions_override_previous(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            status="done", transcript="the party fought goblins", extra_instructions="Focus on combat",
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
        return "recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio_jobs.start_resummarize_job(job_id, extra_instructions="Focus on loot instead")
    updated = await _await_terminal(job_id)
    assert updated.extra_instructions == "Focus on loot instead"
    assert captured["extra_instructions"] == "Focus on loot instead"


def test_session_job_create_route_accepts_extra_instructions(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")},
                     data={"extra_instructions": "Focus on combat"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    db = SessionLocal()
    try:
        deadline = time.time() + 5
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in ("done", "error"):
                break
            time.sleep(0.02)
        assert job.extra_instructions == "Focus on combat"
    finally:
        db.close()


def test_resummarize_route_accepts_extra_instructions(client, seed, monkeypatch):
    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        return f"Recap ({extra_instructions}): {transcript}"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": "", "extra_instructions": "Focus on loot"})
    assert r.status_code == 200, r.text

    deadline = time.time() + 5
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/audio-jobs/{job_id}").json()
        if data["status"] == "done":
            break
        time.sleep(0.02)
    assert data["status"] == "done"
    assert "Focus on loot" in data["recap"]
    assert data["extra_instructions"] == "Focus on loot"


def test_direct_summarize_from_audio_combines_world_and_request_instructions(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.recap_instructions = "Write in French"
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_summarize(transcript, extra_instructions="", **kwargs):
        captured["extra_instructions"] = extra_instructions
        return "un recap"

    async def fake_transcribe(path, glossary="", **kwargs):
        return "the party fought goblins"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/summarize-from-audio",
                     data={"extra_instructions": "Focus on combat"},
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    assert "Write in French" in captured["extra_instructions"]
    assert "Focus on combat" in captured["extra_instructions"]
