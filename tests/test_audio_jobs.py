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
    async def fake_transcribe(path, glossary=""):
        assert path.is_file(), f"audio should still exist while transcribing: {path}"
        return "the party met elena at the bazaar"

    async def fake_summarize(transcript, model="", extra_instructions=""):
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
    async def empty_transcribe(path, glossary=""):
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
    async def raising_transcribe(path, glossary=""):
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


# ── Unified Background Jobs page + routes: app/routers/audio_jobs.py ───────
# Spans both purposes (unlike the purpose-scoped list routes above) and adds
# cancel — the one thing the smaller inline panels don't offer.

@pytest.fixture
def _hanging_transcribe(monkeypatch):
    """A transcribe_audio that never resolves on its own — only cancellation
    can end it — so a test can reliably catch a job mid-"transcribing"
    instead of racing a fast mock that might finish before the cancel
    request lands."""
    release = asyncio.Event()

    async def hang(path, glossary=""):
        await release.wait()
        return "unused"

    monkeypatch.setattr(ai_module, "transcribe_audio", hang)
    return release


def _wait_for_status(client, url, status, timeout=5.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        r = client.get(url)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] == status:
            return data
        time.sleep(0.02)
    raise AssertionError(f"never reached status={status!r}, last seen: {data}")


def test_background_jobs_page_renders_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/background-jobs")
    assert r.status_code == 200
    assert "Background Jobs" in r.text


def test_background_jobs_page_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/background-jobs")
    assert r.status_code == 403


def test_unified_list_spans_both_purposes(client, seed):
    db = SessionLocal()
    try:
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="recap.mp3"),
            AudioJob(world_id=seed.world_a.id, purpose="attachment", status="done", filename="voice.mp3"),
        ])
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/audio-jobs")
    assert r.status_code == 200
    names = {j["filename"] for j in r.json()["jobs"]}
    assert {"recap.mp3", "voice.mp3"} <= names


def test_unified_status_requires_gm(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 403


def test_unified_list_cross_world_isolation(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="a-only.mp3"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/audio-jobs")
    assert r.status_code == 200
    assert all(j["filename"] != "a-only.mp3" for j in r.json()["jobs"])


def test_cancel_stops_an_in_progress_job(client, seed, _hanging_transcribe):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    _wait_for_status(client, f"/api/audio-jobs/{job_id}", "transcribing")

    r = client.post(f"/api/audio-jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text

    data = None
    deadline = time.time() + 5
    while time.time() < deadline:
        r = client.get(f"/api/audio-jobs/{job_id}")
        data = r.json()
        if data["status"] != "transcribing":
            break
        time.sleep(0.02)
    assert data["status"] == "cancelled", data
    assert "cancel" in data["error"].lower()


def test_cancel_rejects_a_job_thats_not_running(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/cancel")
    assert r.status_code == 400


def test_cancel_requires_gm(client, seed, _hanging_transcribe):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]
    _wait_for_status(client, f"/api/audio-jobs/{job_id}", "transcribing")

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/cancel")
    assert r.status_code == 403

    # Clean up: the hanging task would otherwise linger past this test.
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/api/audio-jobs/{job_id}/cancel")


# ── Per-job model selection + resummarize (retry with a different model) ───
#
# A job's own summarize_transcript() call passes model= (see
# app.audio_jobs._run_job) — the `_fake_ai` fixture above accepts model="" by
# default and ignores it; tests here that need to assert *which* model was
# passed install their own capturing fake instead.

@pytest.mark.asyncio
async def test_create_job_stores_and_uses_chosen_model(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions=""):
        captured["model"] = model
        return "recap text"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, model="llama3.1",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.model == "llama3.1"
    assert captured["model"] == "llama3.1"


@pytest.mark.asyncio
async def test_create_job_blank_model_stored_as_none(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.model is None


@pytest.mark.asyncio
async def test_resummarize_job_uses_saved_transcript_and_new_model(client, seed, tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done"
    original_transcript = job.transcript

    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions=""):
        captured["transcript"] = transcript
        captured["model"] = model
        return "A different, better recap."

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    updated = await audio_jobs.resummarize_job(job_id, model="llama3.1")
    assert updated.status == "done"
    assert updated.recap == "A different, better recap."
    assert updated.model == "llama3.1"
    assert captured["transcript"] == original_transcript
    assert captured["model"] == "llama3.1"


@pytest.mark.asyncio
async def test_resummarize_job_falls_back_to_the_jobs_own_model_when_blank(client, seed, tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, model="gemma4:26b",
    )
    await _await_terminal(job_id)

    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions=""):
        captured["model"] = model
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)
    await audio_jobs.resummarize_job(job_id, model="")
    assert captured["model"] == "gemma4:26b"


@pytest.mark.asyncio
async def test_resummarize_job_rejects_attachment_purpose(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=False, attachment_url="/uploads/ai_attachments/clip.mp3",
    )
    await _await_terminal(job_id)
    with pytest.raises(ValueError):
        await audio_jobs.resummarize_job(job_id)


@pytest.mark.asyncio
async def test_resummarize_job_rejects_missing_transcript(client, seed, tmp_path, monkeypatch):
    async def empty_transcribe(path, glossary=""):
        return ""

    monkeypatch.setattr(ai_module, "transcribe_audio", empty_transcribe)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    with pytest.raises(ValueError):
        await audio_jobs.resummarize_job(job_id)


@pytest.mark.asyncio
async def test_resummarize_job_rejects_unknown_job(client, seed):
    with pytest.raises(ValueError):
        await audio_jobs.resummarize_job(999999)


def test_session_job_create_accepts_a_model_field(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")},
                     data={"model": "llama3.1"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.model == "llama3.1"
    finally:
        db.close()


def test_resummarize_route_gm_only(client, seed):
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

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 403


def test_resummarize_route_round_trip(client, seed, monkeypatch):
    async def fake_summarize_capture(transcript, model="", extra_instructions=""):
        return f"Recap via {model or 'default'}: {transcript}"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="error",
                        filename="x.mp3", transcript="hello there", error="[AI error: Ollama 404]")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": "llama3.1"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "done"
    assert data["recap"] == "Recap via llama3.1: hello there"
    assert data["model"] == "llama3.1"
    assert data["error"] == ""


def test_resummarize_route_rejects_job_with_no_transcript_yet(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="pending", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 400


def test_delete_removes_a_finished_job(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert audio_jobs.delete_job(job_id) is True
    db = SessionLocal()
    try:
        assert db.get(AudioJob, job_id) is None
    finally:
        db.close()


def test_delete_refuses_an_in_progress_job(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribing", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert audio_jobs.delete_job(job_id) is False
    db = SessionLocal()
    try:
        assert db.get(AudioJob, job_id) is not None
    finally:
        db.close()


def test_delete_returns_false_for_unknown_job():
    assert audio_jobs.delete_job(999999) is False


def test_delete_route_round_trip(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200, r.text
    r2 = client.get(f"/api/audio-jobs/{job_id}")
    assert r2.status_code == 404


def test_delete_route_rejects_in_progress_job(client, seed, _hanging_transcribe):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]
    _wait_for_status(client, f"/api/audio-jobs/{job_id}", "transcribing")

    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 400

    client.post(f"/api/audio-jobs/{job_id}/cancel")


def test_delete_route_requires_gm(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 403


def test_delete_route_cross_world_isolation(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_b.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 404


def test_resummarize_route_cross_world_isolation(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_b.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 404
