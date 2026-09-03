"""Regression test: GET /api/ai/attachments/audio-jobs and
GET /api/ai/attachments/audio-jobs/{id} filtered only by world_id+purpose,
not by uploader — with World.players_can_ask_ai on, any player could read
another user's (including the GM's) voice-memo transcripts. Non-GM callers
must now only see their own AudioJob attachment rows."""
import asyncio
import time

import pytest

from app.database import SessionLocal
from app.models import AudioJob, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _fake_ai(monkeypatch):
    """These tests exercise the job-LISTING filters, not transcription.
    Without fakes, the fire-and-forget jobs hang on the real Whisper backend
    (unconfigured in tests), their tasks outlive this test's pytest-asyncio
    loop, and the late-firing cleanup handler writes its failure to whatever
    test's database is current by raw job id (ids restart at 1 every test) —
    poisoning an unrelated later audio-job test. Mirrors the _fake_ai
    autouse fixture in test_audio_jobs.py so every job reaches a terminal
    status and is forgotten before the test ends."""
    from app import ai as ai_module

    async def fake_transcribe(path, glossary="", **kwargs):
        return "fake attachment transcript"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "Fake attachment recap."

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        return "Condensed: " + recap[:20]

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)


async def _await_terminal(job_ids, timeout=5.0):
    """Let the fire-and-forget job tasks run to a terminal status on this
    test's loop before the test returns — otherwise they linger as pending
    tasks on a loop that is about to close (see _fake_ai above)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = SessionLocal()
        try:
            rows = [db.get(AudioJob, j) for j in job_ids]
        finally:
            db.close()
        if all(r is not None and r.status in ("done", "error") for r in rows):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"attachment jobs never reached a terminal status: {[r and r.status for r in rows]}")


def _enable_ask_ai(world_id):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.players_can_ask_ai = True
        db.commit()
    finally:
        db.close()


def _make_attachment_job(world_id, filename, created_by_user_id, tmp_path):
    from app import audio_jobs
    audio = tmp_path / filename
    audio.write_bytes(b"x")
    return audio_jobs.create_job(
        world_id=world_id, purpose="attachment", filename=filename,
        audio_path=audio, delete_after=True, attachment_url=f"/uploads/{filename}",
        created_by_user_id=created_by_user_id,
    )


@pytest.mark.asyncio
async def test_player_only_sees_their_own_attachment_jobs_in_the_list(client, seed, tmp_path):
    _enable_ask_ai(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_job_id = _make_attachment_job(seed.world_a.id, "gm-memo.mp3", seed.gm.id, tmp_path)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    player_job_id = _make_attachment_job(seed.world_a.id, "player-memo.mp3", seed.player_a.id, tmp_path)

    r = client.get("/api/ai/attachments/audio-jobs")
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()]
    assert player_job_id in ids
    assert gm_job_id not in ids
    await _await_terminal([gm_job_id, player_job_id])


@pytest.mark.asyncio
async def test_player_gets_404_fetching_another_users_attachment_job(client, seed, tmp_path):
    _enable_ask_ai(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_job_id = _make_attachment_job(seed.world_a.id, "gm-memo.mp3", seed.gm.id, tmp_path)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/ai/attachments/audio-jobs/{gm_job_id}")
    assert r.status_code == 404
    await _await_terminal([gm_job_id])


@pytest.mark.asyncio
async def test_gm_still_sees_every_attachment_job(client, seed, tmp_path):
    _enable_ask_ai(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_job_id = _make_attachment_job(seed.world_a.id, "gm-memo.mp3", seed.gm.id, tmp_path)
    player_job_id = _make_attachment_job(seed.world_a.id, "player-memo.mp3", seed.player_a.id, tmp_path)

    r = client.get("/api/ai/attachments/audio-jobs")
    ids = [j["id"] for j in r.json()]
    assert gm_job_id in ids
    assert player_job_id in ids
    assert client.get(f"/api/ai/attachments/audio-jobs/{player_job_id}").status_code == 200
    await _await_terminal([gm_job_id, player_job_id])
