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
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

# Captured before the autouse _fake_ai fixture below ever runs, so tests that
# need the REAL map-reduce chunking logic (not _fake_ai's flat fake) can
# restore it for just that one test.
_REAL_SUMMARIZE_TRANSCRIPT = ai_module.summarize_transcript


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
    async def fake_transcribe(path, glossary="", **kwargs):
        assert path.is_file(), f"audio should still exist while transcribing: {path}"
        return "the party met elena at the bazaar"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
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
async def test_create_job_marks_error_when_summarize_returns_failure_sentinel(client, seed, tmp_path, monkeypatch):
    # summarize_transcript never raises on an Ollama-side failure — it
    # returns a "[AI ...]" sentinel string instead. Without checking for
    # that, a failed summarize would land as status="done" with the error
    # text sitting in the recap field, same gap as resummarize_job had.
    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI error: Ollama unreachable]"

    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == "[AI error: Ollama unreachable]"


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
    async def empty_transcribe(path, glossary="", **kwargs):
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
async def test_job_saves_partial_transcript_on_mid_chunk_whisper_failure(client, seed, tmp_path, monkeypatch):
    """A WhisperError carrying partial_transcript (transcribe_audio's
    chunked path, on a failure after at least one chunk succeeded) must be
    saved to the job row — not just the error message — so the GM can
    resummarize from what was actually transcribed instead of losing it
    along with the failure."""
    async def failing_transcribe(path, glossary="", **kwargs):
        raise ai_module.WhisperError(
            "Whisper failed on part 3 of 4: container restarted. The first 2 part(s) were transcribed.",
            partial_transcript="part 0\npart 1",
        )
    monkeypatch.setattr(ai_module, "transcribe_audio", failing_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "part 3 of 4" in job.error
    assert job.transcript == "part 0\npart 1"


@pytest.mark.asyncio
async def test_job_error_has_no_transcript_when_whisper_error_has_no_partial(client, seed, tmp_path, monkeypatch):
    async def failing_transcribe(path, glossary="", **kwargs):
        raise ai_module.WhisperError("whisper unreachable")
    monkeypatch.setattr(ai_module, "transcribe_audio", failing_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert not job.transcript


@pytest.mark.asyncio
async def test_job_ends_in_error_on_exception(client, seed, tmp_path, monkeypatch):
    async def raising_transcribe(path, glossary="", **kwargs):
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


# ── Chunk progress (map-reduce summarization) — see test_transcript_chunking
# .py for the underlying app.ai.summarize_transcript(on_progress=...) unit
# tests; these confirm audio_jobs.py actually persists it to the DB row.

@pytest.mark.asyncio
async def test_chunk_progress_visible_mid_summarize_and_cleared_when_done(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)

    hang_on_second_chunk = asyncio.Event()
    release_second_chunk = asyncio.Event()
    call_count = {"n": 0}

    async def fake_transcribe(path, glossary="", **kwargs):
        return ("The party explored the ruins. " * 30).strip()

    async def fake_generate_chat(messages, system="", model="", options=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            hang_on_second_chunk.set()
            await release_second_chunk.wait()
        return "[part]"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    # Override the file's autouse _fake_ai fixture, which replaces
    # summarize_transcript wholesale — this test needs the REAL per-part
    # chunking logic (driving fake_generate_chat above) to actually run.
    monkeypatch.setattr(ai_module, "summarize_transcript", _REAL_SUMMARIZE_TRANSCRIPT)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )

    await asyncio.wait_for(hang_on_second_chunk.wait(), timeout=5)
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "summarizing"
        assert job.chunk_current == 2
        assert job.chunk_total is not None and job.chunk_total > 1
    finally:
        db.close()

    release_second_chunk.set()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.chunk_current is None
    assert job.chunk_total is None


def test_unified_job_status_route_exposes_chunk_progress(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="summarizing",
                        filename="x.mp3", chunk_current=2, chunk_total=5)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["chunk_current"] == 2
    assert r.json()["chunk_total"] == 5


def test_session_job_status_route_exposes_chunk_progress(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="summarizing",
                        filename="x.mp3", chunk_current=1, chunk_total=3)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/sessions/ai/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["chunk_current"] == 1
    assert r.json()["chunk_total"] == 3


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


def _session_audio_jobs_dir_for_test():
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_jobs"


def test_sweep_orphaned_job_audio_removes_old_files(tmp_path):
    """A file left behind because a crash/restart skipped _run_job's own
    cleanup finally (sweep_interrupted_jobs only fixes the DB row, not the
    file) must eventually be removed, or an orphan like this leaks disk
    forever."""
    jobs_dir = _session_audio_jobs_dir_for_test()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    old_file = jobs_dir / "orphaned-old.mp3"
    old_file.write_bytes(b"x")
    old_cutoff_time = time.time() - audio_jobs._SESSION_AUDIO_JOBS_CUTOFF_SECONDS - 3600
    os.utime(old_file, (old_cutoff_time, old_cutoff_time))

    audio_jobs.sweep_orphaned_job_audio()

    assert not old_file.exists()


def test_sweep_orphaned_job_audio_keeps_recent_files(tmp_path):
    """A file that's recent enough to plausibly belong to a job still
    genuinely in flight (this sweep runs at startup, before any new job
    could have been created) must survive — sweep_interrupted_jobs already
    errored out anything that was actually running at shutdown, but the
    cutoff is deliberately generous rather than exact."""
    jobs_dir = _session_audio_jobs_dir_for_test()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    recent_file = jobs_dir / "recent.mp3"
    recent_file.write_bytes(b"x")

    audio_jobs.sweep_orphaned_job_audio()

    assert recent_file.exists()
    recent_file.unlink()


def test_sweep_orphaned_job_audio_is_a_noop_when_dir_missing(tmp_path):
    jobs_dir = _session_audio_jobs_dir_for_test()
    shutil.rmtree(jobs_dir, ignore_errors=True)
    audio_jobs.sweep_orphaned_job_audio()  # must not raise


def test_sweep_orphaned_job_audio_leaves_ai_attachments_alone(tmp_path):
    """Attachment jobs run with delete_after=False — the file IS the
    attachment, not working storage — so this sweep must only ever touch
    uploads/session_audio/_jobs/, never uploads/ai_attachments/."""
    jobs_dir = _session_audio_jobs_dir_for_test()
    attachments_dir = jobs_dir.parent.parent / "ai_attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    old_attachment = attachments_dir / "old-attachment.mp3"
    old_attachment.write_bytes(b"x")
    old_cutoff_time = time.time() - audio_jobs._SESSION_AUDIO_JOBS_CUTOFF_SECONDS - 3600
    os.utime(old_attachment, (old_cutoff_time, old_cutoff_time))

    audio_jobs.sweep_orphaned_job_audio()

    assert old_attachment.exists()
    old_attachment.unlink()


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

    async def hang(path, glossary="", **kwargs):
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


# ── _forget_task — the done-callback's registry race + stuck-pending fix ───

class _FakeTask:
    def __init__(self, cancelled=False):
        self._cancelled = cancelled

    def cancelled(self):
        return self._cancelled


def test_forget_task_does_not_evict_a_newer_task_for_the_same_job_id():
    """The race this fixes: a resummarize starts in the brief window
    between the prior task finishing (row already out of
    IN_PROGRESS_STATUSES) and its own done-callback actually running
    (asyncio schedules callbacks via call_soon, not synchronously) — the
    OLD task's callback must not evict the NEW task's registry entry, or
    the new task becomes only weakly-referenced and cancel_job can no
    longer find it."""
    old_task = _FakeTask()
    new_task = _FakeTask()
    audio_jobs._running_tasks[999999] = new_task
    try:
        audio_jobs._forget_task(999999, old_task)
        assert audio_jobs._running_tasks.get(999999) is new_task
    finally:
        audio_jobs._running_tasks.pop(999999, None)


def test_forget_task_removes_its_own_entry():
    task = _FakeTask()
    audio_jobs._running_tasks[999999] = task
    audio_jobs._forget_task(999999, task)
    assert 999999 not in audio_jobs._running_tasks


def test_forget_task_reconciles_a_job_cancelled_before_its_body_ever_ran(client, seed, tmp_path):
    """asyncio.Task.cancel() on a task whose coroutine body hasn't started
    yet skips the body entirely — neither _run_job's own `except
    CancelledError` nor its `finally: audio_path.unlink(...)` ever runs.
    Without this reconciliation the row stays "pending" forever (both
    cancel_job and delete_job refuse a row in IN_PROGRESS_STATUSES) and
    the uploaded audio leaks."""
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="pending", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    task = _FakeTask(cancelled=True)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task, audio, True)

    assert job_id not in audio_jobs._running_tasks
    assert not audio.exists()
    db = SessionLocal()
    try:
        updated = db.get(AudioJob, job_id)
        assert updated.status == "cancelled"
        assert updated.error == "Cancelled by GM."
        assert updated.finished_at is not None
    finally:
        db.close()


def test_forget_task_keeps_audio_when_delete_after_is_false(client, seed, tmp_path):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="pending", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    task = _FakeTask(cancelled=True)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task, audio, False)
    assert audio.exists()


def test_forget_task_does_not_touch_a_job_that_finished_normally(client, seed):
    """A task that ran its body to completion (whether cancelled partway
    through and handled by _run_job's own CancelledError branch, or simply
    finished with done/error) has already moved the row out of
    IN_PROGRESS_STATUSES before this callback fires — the reconciliation
    must be a no-op then, not overwrite whatever real outcome was
    recorded."""
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3", recap="a real recap")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = _FakeTask(cancelled=False)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task)

    db = SessionLocal()
    try:
        updated = db.get(AudioJob, job_id)
        assert updated.status == "done"
        assert updated.recap == "a real recap"
    finally:
        db.close()


# ── Per-job model selection + resummarize (retry with a different model) ───
#
# A job's own summarize_transcript() call passes model= (see
# app.audio_jobs._run_job) — the `_fake_ai` fixture above accepts model="" by
# default and ignores it; tests here that need to assert *which* model was
# passed install their own capturing fake instead.

@pytest.mark.asyncio
async def test_create_job_stores_and_uses_chosen_model(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
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

    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        captured["transcript"] = transcript
        captured["model"] = model
        return "A different, better recap."

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    started = audio_jobs.start_resummarize_job(job_id, model="llama3.1")
    assert started.status == "summarizing"
    updated = await _await_terminal(job_id)
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

    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)
    audio_jobs.start_resummarize_job(job_id, model="")
    await _await_terminal(job_id)
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
        audio_jobs.start_resummarize_job(job_id)


@pytest.mark.asyncio
async def test_resummarize_job_rejects_missing_transcript(client, seed, tmp_path, monkeypatch):
    async def empty_transcribe(path, glossary="", **kwargs):
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
        audio_jobs.start_resummarize_job(job_id)


@pytest.mark.asyncio
async def test_resummarize_job_rejects_unknown_job(client, seed):
    with pytest.raises(ValueError):
        audio_jobs.start_resummarize_job(999999)


@pytest.mark.asyncio
async def test_resummarize_job_marks_error_when_summarize_returns_failure_sentinel(client, seed, tmp_path, monkeypatch):
    # summarize_transcript never raises on an Ollama-side failure — it
    # returns a "[AI ...]" sentinel string instead. Without checking for
    # that, a failed re-summarize would land as status="done" with the
    # error text sitting in the recap field.
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done"

    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI error: Ollama 404: model 'gemma4:26b' not found]"

    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    audio_jobs.start_resummarize_job(job_id, model="gemma4:26b")
    updated = await _await_terminal(job_id)
    assert updated.status == "error"
    assert updated.error == "[AI error: Ollama 404: model 'gemma4:26b' not found]"


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
    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
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
    # The route only starts the job now — see start_resummarize_job's
    # docstring for why running the whole (possibly multi-minute)
    # map-reduce summarize inline used to risk tripping a reverse proxy's
    # own timeout. The caller polls for the actual result.
    assert r.json()["status"] == "summarizing"

    data = _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["recap"] == "Recap via llama3.1: hello there"
    assert data["model"] == "llama3.1"
    assert data["error"] == ""


def test_resummarize_route_does_not_block_on_a_slow_summarize(client, seed, monkeypatch):
    """Direct regression test for the reported bug: retrying a summary used
    to await the whole (possibly multi-minute) map-reduce summarize inline
    inside this one request, which a reverse proxy's own timeout could trip
    long before Ollama finished (surfaced to the GM as a raw "HTTP 524").
    A slow-but-bounded fake here proves the HTTP response comes back well
    before summarize_transcript finishes, not just eventually."""
    release = asyncio.Event()

    async def slow_summarize(transcript, model="", extra_instructions="", **kwargs):
        await release.wait()
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", slow_summarize)

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
    try:
        r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "summarizing"
    finally:
        release.set()  # let the background task finish so nothing lingers past this test
    _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")


def test_resummarize_route_rejects_a_job_already_in_progress(client, seed, monkeypatch):
    release = asyncio.Event()

    async def slow_summarize(transcript, model="", extra_instructions="", **kwargs):
        await release.wait()
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", slow_summarize)

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
    try:
        r1 = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
        assert r1.status_code == 200, r1.text
        r2 = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
        assert r2.status_code == 400
    finally:
        release.set()
    _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")


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


# ── run_started_at / finished_at (Background Jobs' "took Xm Ys" display) ───
#
# run_started_at marks the start of the CURRENT run (reset on every
# resummarize, unlike created_at which never changes) so a job resummarized
# days after its first run doesn't report a multi-day "duration" for what
# was mostly idle time between runs. finished_at is set once that run
# reaches a terminal status.

@pytest.mark.asyncio
async def test_run_timing_set_on_successful_completion(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    before = datetime.utcnow()
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    after = datetime.utcnow()
    assert job.run_started_at is not None
    assert job.finished_at is not None
    assert before <= job.run_started_at <= job.finished_at <= after


@pytest.mark.asyncio
async def test_run_timing_set_on_error_paths(client, seed, tmp_path, monkeypatch):
    async def raising_transcribe(path, glossary="", **kwargs):
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
    assert job.run_started_at is not None
    assert job.finished_at is not None
    assert job.finished_at >= job.run_started_at


@pytest.mark.asyncio
async def test_resummarize_resets_run_started_at_and_keeps_created_at(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    first = await _await_terminal(job_id)
    original_created_at = first.created_at
    first_run_started_at = first.run_started_at
    first_finished_at = first.finished_at

    audio_jobs.start_resummarize_job(job_id)
    second = await _await_terminal(job_id)
    assert second.created_at == original_created_at  # original creation time never changes
    assert second.run_started_at is not None
    assert second.run_started_at >= first_finished_at  # a fresh run, not the original start
    assert second.finished_at is not None
    assert second.finished_at >= second.run_started_at


def test_sweep_interrupted_jobs_sets_finished_at(client, seed):
    db = SessionLocal()
    try:
        stuck = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribing", filename="x.mp3")
        db.add(stuck)
        db.commit()
        db.refresh(stuck)
        stuck_id = stuck.id
    finally:
        db.close()

    audio_jobs.sweep_interrupted_jobs()

    db = SessionLocal()
    try:
        s = db.get(AudioJob, stuck_id)
        assert s.status == "error"
        assert s.finished_at is not None
    finally:
        db.close()


def test_unified_status_route_exposes_timing_and_instructions_fields(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3",
            extra_instructions="Focus on combat",
            run_started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["extra_instructions"] == "Focus on combat"
    assert data["run_started_at"] is not None
    assert data["finished_at"] is not None


# ── Per-job extra_instructions on create_job ────────────────────────────────

@pytest.mark.asyncio
async def test_create_job_stores_extra_instructions(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, extra_instructions="Focus on combat",
    )
    job = await _await_terminal(job_id)
    assert job.extra_instructions == "Focus on combat"


@pytest.mark.asyncio
async def test_create_job_blank_extra_instructions_stored_as_none(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.extra_instructions is None


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
