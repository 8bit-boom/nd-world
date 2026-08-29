"""Tests for "choose from Audio Library" on the Session page — lets a GM
start a background transcribe+summarize job (app/routers/sessions.py's
POST /api/sessions/ai/audio-jobs/from-clip) sourced from a recording
already saved in the world's Audio Library (app/models.py's AudioClip)
instead of re-uploading it, plus the new GET /api/audio/clips route
(app/routers/audio.py) that lists clips for the picker to populate. The
job itself reuses the exact same app.audio_jobs.create_job engine the
regular upload-based route already exercises (tests/test_audio_jobs.py) —
these tests focus on the two things genuinely new here: resolving a
clip's stored URL back to a real file on disk, and delete_after=False
(the clip's file must survive the job, unlike an uploaded copy)."""
import io
import os
import time
from pathlib import Path

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import AudioClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 500


@pytest.fixture(autouse=True)
def _fake_ai(monkeypatch):
    """Same fake transcribe/summarize pair test_audio_jobs.py's own
    autouse fixture uses — the real Whisper/Ollama pipeline is out of
    scope here."""
    async def fake_transcribe(path, glossary="", **kwargs):
        assert path.is_file(), f"clip audio should still exist while transcribing: {path}"
        return "the party met elena at the bazaar"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "The party met Elena at the bazaar."

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)


def _uploads_audio_dir() -> Path:
    return Path(os.environ["DB_PATH"]).parent / "uploads" / "audio"


def _add_clip_with_real_file(world_id, filename="session-recording.mp3", **kw):
    audio_dir = _uploads_audio_dir()
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / filename).write_bytes(_MP3_BYTES)
    db = SessionLocal()
    try:
        c = AudioClip(world_id=world_id, name=kw.pop("name", "Session Recording"),
                      file_url=f"/uploads/audio/{filename}", **kw)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id
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


# ── GET /api/audio/clips ─────────────────────────────────────────────────────

def test_clips_list_gm(client, seed):
    _add_clip_with_real_file(seed.world_a.id, filename="a.mp3", name="Clip A")
    _add_clip_with_real_file(seed.world_a.id, filename="b.mp3", name="Clip B")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/audio/clips")
    assert r.status_code == 200
    names = sorted(c["name"] for c in r.json())
    assert names == ["Clip A", "Clip B"]


def test_clips_list_scoped_to_active_world(client, seed):
    _add_clip_with_real_file(seed.world_a.id, filename="a.mp3", name="World A Clip")
    _add_clip_with_real_file(seed.world_b.id, filename="b.mp3", name="World B Clip")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/audio/clips")
    names = [c["name"] for c in r.json()]
    assert names == ["World A Clip"]


def test_clips_list_player_denied(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/audio/clips")
    assert r.status_code == 403


# ── POST /api/sessions/ai/audio-jobs/from-clip ──────────────────────────────

def test_from_clip_runs_to_completion_and_keeps_the_file(client, seed):
    clip_id = _add_clip_with_real_file(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/sessions/ai/audio-jobs/from-clip", data={"clip_id": str(clip_id)})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data.get("error")
    assert data["purpose"] == "session_recap"
    assert data["transcript"] == "the party met elena at the bazaar"
    assert data["recap"] == "The party met Elena at the bazaar."

    # delete_after=False — unlike an uploaded copy, the Library's own file
    # must survive the job untouched.
    assert (_uploads_audio_dir() / "session-recording.mp3").is_file()


def test_from_clip_uses_clip_name_as_filename(client, seed):
    clip_id = _add_clip_with_real_file(seed.world_a.id, name="Tavern Session — Night One")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs/from-clip", data={"clip_id": str(clip_id)})
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["filename"] == "Tavern Session — Night One"


def test_from_clip_links_to_game_session_when_given(client, seed):
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

    clip_id = _add_clip_with_real_file(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs/from-clip",
                     data={"clip_id": str(clip_id), "game_session_id": str(gs_id)})
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["game_session_id"] == gs_id


def test_from_clip_404s_for_clip_in_another_world(client, seed):
    clip_id = _add_clip_with_real_file(seed.world_b.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs/from-clip", data={"clip_id": str(clip_id)})
    assert r.status_code == 404


def test_from_clip_404s_for_nonexistent_clip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs/from-clip", data={"clip_id": "999999"})
    assert r.status_code == 404


def test_from_clip_404s_when_file_missing_on_disk(client, seed):
    """A clip row can outlive its file (a manual disk cleanup, a restored
    DB without the matching uploads dir) — refuse rather than handing
    app.audio_jobs.create_job a path that doesn't exist."""
    db = SessionLocal()
    try:
        c = AudioClip(world_id=seed.world_a.id, name="Ghost Clip", file_url="/uploads/audio/missing.mp3")
        db.add(c)
        db.commit()
        db.refresh(c)
        clip_id = c.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs/from-clip", data={"clip_id": str(clip_id)})
    assert r.status_code == 404


def test_from_clip_player_denied(client, seed):
    """The whole /sessions|/api/sessions surface is GM-only by default
    (not in _is_player_safe) — same as the upload-based audio-jobs route."""
    clip_id = _add_clip_with_real_file(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs/from-clip", data={"clip_id": str(clip_id)})
    assert r.status_code == 403


# ── Shipped JS/template wiring (source assertion) ───────────────────────────

def test_session_page_ships_the_picker_and_js_wiring(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    from app.database import SessionLocal as _SL
    from app.models import GameSession as _GS
    db = _SL()
    try:
        gs = _GS(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        sid = gs.id
    finally:
        db.close()

    page = client.get(f"/sessions/{sid}").text
    assert 'onclick="toggleAudioLibraryPicker()"' in page
    assert 'id="audio-library-clip-select"' in page
    assert "async function aiStartAudioJobFromClip()" in page
    assert "createFromClipUrl: '/api/sessions/ai/audio-jobs/from-clip'" in page


def test_audio_jobs_js_defines_start_job_from_clip():
    js = open("static/js/audio-jobs.js").read()
    assert "async function startJobFromClip(" in js
    assert "opts.createFromClipUrl" in js
    assert "startJob, startJobFromClip, refreshList" in js
