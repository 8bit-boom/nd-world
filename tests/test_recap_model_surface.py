"""Tests for the "recap" entry in app.ai.DEFAULT_SURFACES (Models tab >
Default Models > Recap): a GM-configured fallback model for session-recap/
condense work, used whenever a specific call doesn't pin its own model.

Covers both places that read it:
  - app/audio_jobs.py's _run_job, for background condense/summarize jobs.
  - app/routers/sessions.py's _recap_model helper, for the direct
    (non-job) condense-recap / summarize-from-audio / summarize-live-
    transcript / summarize-from-facts / session-log-recap routes — none of
    which go through _run_job at all.

Before this feature, an unspecified model on any of these fell straight
through to app.ai.resolve_model's single instance-wide default, with no way
to pin a different model for recap work specifically (unlike chat/ask_ai/
image, which already had their own per-surface default)."""
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob, Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    """Same isolation as tests/test_ai_stream.py — app.ai persists per-
    surface defaults to a JSON file next to the DB, not the DB itself, so
    point it at a throwaway path per test to avoid cross-test leakage."""
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _make_session(world, title="Session 1", num=1):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world.id, title=title, session_num=num, summary="s")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _add_fact(world, session_id, content, visible=True):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=world.id, game_session_id=session_id, content=content, visible_to_players=visible))
        db.commit()
    finally:
        db.close()


# ── app/audio_jobs.py's _run_job ─────────────────────────────────────────────

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
            import asyncio
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_job_with_no_model_falls_back_to_recap_surface_default(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        return "a transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "recap text"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    ai_module.set_default("recap", "recap-default-model")

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["model"] == "recap-default-model"


@pytest.mark.asyncio
async def test_job_with_explicit_model_ignores_recap_surface_default(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        return "a transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "recap text"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    ai_module.set_default("recap", "recap-default-model")

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, model="explicit-model",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["model"] == "explicit-model"


@pytest.mark.asyncio
async def test_job_with_no_default_set_falls_through_to_empty(client, seed, tmp_path, monkeypatch):
    """No "recap" default configured at all — same as before this feature,
    an empty model string reaches summarize_transcript and resolve_model's
    own instance-wide default takes over from there."""
    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        return "a transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "recap text"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["model"] == ""


# ── app/routers/sessions.py's direct (non-job) call sites ──────────────────

def test_condense_recap_falls_back_to_recap_surface_default(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["model"] = model
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    ai_module.set_default("recap", "recap-default-model")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "A very long recap..."})
    assert r.status_code == 200
    assert captured["model"] == "recap-default-model"


def test_condense_recap_explicit_model_overrides_recap_surface_default(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["model"] = model
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    ai_module.set_default("recap", "recap-default-model")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "text", "model": "explicit-model"})
    assert r.status_code == 200
    assert captured["model"] == "explicit-model"


def test_summarize_from_audio_falls_back_to_recap_surface_default(client, seed, monkeypatch):
    import io
    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        return "a transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "A recap."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    ai_module.set_default("recap", "recap-default-model")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/summarize-from-audio",
                     files={"file": ("session.wav", io.BytesIO(b"fake"), "audio/wav")})
    assert r.status_code == 200
    assert captured["model"] == "recap-default-model"


def test_summarize_live_transcript_falls_back_to_recap_surface_default(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = "raw messy asr text"
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    ai_module.set_default("recap", "recap-default-model")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-live-transcript")
    assert r.status_code == 200
    assert captured["model"] == "recap-default-model"


def test_summarize_from_facts_falls_back_to_recap_surface_default(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Fact one")

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions="", think=True):
        captured["model"] = model
        return "Woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    ai_module.set_default("recap", "recap-default-model")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-from-facts")
    assert r.status_code == 200
    assert captured["model"] == "recap-default-model"


def test_session_log_recap_falls_back_to_recap_surface_default(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", visible=True)

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions=""):
        captured["model"] = model
        return "A narrated recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    ai_module.set_default("recap", "recap-default-model")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    assert captured["model"] == "recap-default-model"


# ── Models tab > Default Models > Recap: GET/POST /api/ai/defaults ─────────

def test_recap_surface_accepted_by_defaults_endpoint(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/defaults", json={"surface": "recap", "model_id": "my-recap-model"})
    assert r.status_code == 200
    assert r.json()["defaults"]["recap"] == "my-recap-model"
    r2 = client.get("/api/ai/defaults")
    assert r2.json()["recap"] == "my-recap-model"
