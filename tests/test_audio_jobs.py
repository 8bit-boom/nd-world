"""Tests for durable background audio-processing jobs (AudioJob in
app/models.py, the engine in app/audio_jobs.py, and the routes it backs in
app/routers/sessions.py and app/routers/ai.py) — an opt-in alternative to
the blocking upload routes for a recording long enough that waiting on one
HTTP request isn't practical. The actual work runs as a fire-and-forget
asyncio task in the server process, independent of any one connection, so
it survives the browser tab that started it closing. These tests poll the
status route in a short retry loop rather than sleeping a fixed amount —
each poll request pumps the same event loop the background task runs on,
so the task actually gets a chance to progress between polls.
"""
import asyncio
import io
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _poll_until_terminal(client, url, timeout=5.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        r = client.get(url)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"job never reached a terminal status, last seen: {data}")


@pytest.fixture(autouse=True)
def _fake_ai(monkeypatch):
    async def fake_transcribe(path):
        assert path.is_file(), f"audio should still exist while transcribing: {path}"
        return "the party met elena at the bazaar"

    async def fake_summarize(transcript, model=""):
        return "The party met Elena at the bazaar."

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)


# ── app/audio_jobs.py engine, exercised directly ────────────────────────────
#
# create_job() calls asyncio.create_task(), which requires an already-
# running event loop — true inside a real request (TestClient runs one per
# call, as the HTTP-level tests below rely on) but not in a plain sync test
# function. These engine-level tests use @pytest.mark.asyncio + await
# asyncio.sleep(...) to poll instead, giving the fire-and-forget task a
# loop to actually run on.

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


@pytest.mark.asyncio
async def test_create_job_runs_to_completion_session_recap(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.transcript == "the party met elena at the bazaar"
    assert job.recap == "The party met Elena at the bazaar."
    assert not audio.exists()  # delete_after=True


@pytest.mark.asyncio
async def test_create_job_attachment_purpose_has_no_recap_and_keeps_file(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=False, attachment_url="/uploads/ai_attachments/clip.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.transcript == "the party met elena at the bazaar"
    assert job.recap == ""  # attachment purpose never summarizes
    assert job.attachment_url == "/uploads/ai_attachments/clip.mp3"
    assert audio.exists()  # delete_after=False


@pytest.mark.asyncio
async def test_job_ends_in_error_on_empty_transcript(client, seed, tmp_path, monkeypatch):
    async def empty_transcribe(path):
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", empty_transcribe)

    audio = tmp_path / "silent.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="silent.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "whisper" in job.error.lower() or "transcribe" in job.error.lower()


@pytest.mark.asyncio
async def test_job_ends_in_error_on_exception(client, seed, tmp_path, monkeypatch):
    async def raising_transcribe(path):
        raise RuntimeError("boom")
    monkeypatch.setattr(ai_module, "transcribe_audio", raising_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "boom" in job.error
    assert not audio.exists()  # still cleaned up even on failure


def test_sweep_interrupted_jobs_marks_in_progress_as_error(client, seed):
    db = SessionLocal()
    try:
        stuck = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribing", filename="x.mp3")
        done = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="y.mp3")
        db.add_all([stuck, done])
        db.commit()
        db.refresh(stuck)
        db.refresh(done)
        stuck_id, done_id = stuck.id, done.id
    finally:
        db.close()

    audio_jobs.sweep_interrupted_jobs()

    db = SessionLocal()
    try:
        s = db.get(AudioJob, stuck_id)
        d = db.get(AudioJob, done_id)
        assert s.status == "error"
        assert "restart" in s.error.lower()
        assert d.status == "done"
        assert d.error == ""
    finally:
        db.close()


# ── Sessions routes: /api/sessions/ai/audio-jobs* ───────────────────────────

def test_session_job_create_poll_and_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["purpose"] == "session_recap"
    assert data["transcript"] == "the party met elena at the bazaar"
    assert data["recap"] == "The party met Elena at the bazaar."

    r = client.get("/api/sessions/ai/audio-jobs")
    assert r.status_code == 200
    assert any(j["id"] == job_id for j in r.json())


def test_session_job_chunked_create(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "a" * 32
    part_a, part_b = b"first-half-", b"second-half"
    r0 = client.post("/api/sessions/ai/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(part_a), "application/octet-stream")})
    r1 = client.post("/api/sessions/ai/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files={"file": ("part", io.BytesIO(part_b), "application/octet-stream")})
    assert r0.status_code == 200 and r1.status_code == 200

    r = client.post("/api/sessions/ai/audio-jobs/complete",
                     data={"upload_id": upload_id, "filename": "big.mp3", "total_chunks": "2"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done"


def test_session_job_links_to_game_session_when_given(client, seed):
    from app.models import GameSession
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs", data={"game_session_id": str(gs_id)},
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["game_session_id"] == gs_id


def test_session_job_rejects_unsupported_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 400


def test_session_job_oversized_file_rejected(client, seed, monkeypatch):
    monkeypatch.setattr("app.routers.sessions.MAX_SESSION_AUDIO_BYTES", 4)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"way too much data"), "audio/mpeg")})
    assert r.status_code == 413


def test_session_job_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 403
    r2 = client.get("/api/sessions/ai/audio-jobs")
    assert r2.status_code == 403


def test_session_job_cross_world_isolation(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/sessions/ai/audio-jobs/{job_id}")
    assert r.status_code == 404
    listed = client.get("/api/sessions/ai/audio-jobs").json()
    assert all(j["id"] != job_id for j in listed)


def test_session_job_list_excludes_attachment_purpose_jobs(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_a.id, purpose="attachment", status="done", filename="voice.mp3"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    listed = client.get("/api/sessions/ai/audio-jobs").json()
    assert all(j["filename"] != "voice.mp3" for j in listed)


# ── AI attachments routes: /api/ai/attachments/audio-jobs* ─────────────────

def test_attachment_job_create_poll_and_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/ai/attachments/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["purpose"] == "attachment"
    assert data["transcript"] == "the party met elena at the bazaar"
    assert data["attachment_url"].startswith("/uploads/ai_attachments/")

    r = client.get("/api/ai/attachments/audio-jobs")
    assert any(j["id"] == job_id for j in r.json())


def test_attachment_job_chunked_create(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "b" * 32
    r0 = client.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(b"first-"), "application/octet-stream")})
    r1 = client.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files={"file": ("part", io.BytesIO(b"second"), "application/octet-stream")})
    assert r0.status_code == 200 and r1.status_code == 200
    r = client.post("/api/ai/attachments/audio-jobs/complete",
                     data={"upload_id": upload_id, "filename": "big.mp3", "total_chunks": "2"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/ai/attachments/audio-jobs/{job_id}")
    assert data["status"] == "done"


def test_attachment_job_rejects_non_audio_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 400


def test_attachment_job_player_denied_by_default_then_allowed(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 403

    _set_world(seed.world_a.id, players_can_ask_ai=True)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200


def test_attachment_job_cross_world_isolation(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/ai/attachments/audio-jobs/{job_id}")
    assert r.status_code == 404


def test_attachment_job_list_excludes_session_recap_purpose_jobs(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="recap.mp3"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    listed = client.get("/api/ai/attachments/audio-jobs").json()
    assert all(j["filename"] != "recap.mp3" for j in listed)
