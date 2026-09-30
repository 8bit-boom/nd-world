"""Regression tests for docs/LIVE_RECORDING_AUDIT.md item 3: the raw-audio
side of /api/sessions/{id}/live-transcript/append was already idempotent on
(recording_id, segment_index) — a retried upload overwrites the same file
instead of duplicating it — but the TRANSCRIPT append was not: it ran
unconditionally, so a client retry after a lost *response* (the server had
already transcribed and committed, but the connection dropped before the
reply arrived) duplicated that chunk's text in live_transcript. The client's
own 3-attempt retry ladder makes this reachable in practice, and it's more
likely than it sounds given this panel offers 15-minute chunks over a
possibly-slow self-hosted Whisper backend behind a proxy with its own
timeout.

Fix: GameSession.live_transcript_segments_json tracks which
"<recording_id>:<segment_index>" keys have already been folded into
live_transcript; a repeat of an already-recorded key short-circuits BEFORE
calling Whisper and returns the existing transcript unchanged."""
import json

from app import ai as ai_module
from app.database import SessionLocal, engine
from app.models import GameSession

from .conftest import GM_PASSWORD, login


def _make_session(world, title="Session 1"):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world.id, title=title, session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


_RID = "cd" * 16  # a valid 32-hex recording id (uploads.CHUNK_ID_RE shape)


def _append(client, session_id, idx, data=b"chunk-bytes", rid=_RID, filename="chunk.webm"):
    import io
    form = {}
    if rid is not None:
        form["recording_id"] = rid
    if idx is not None:
        form["segment_index"] = str(idx)
    return client.post(
        f"/api/sessions/{session_id}/live-transcript/append",
        data=form,
        files={"file": (filename, io.BytesIO(data), "audio/webm")},
    )


def _get_transcript(session_id):
    db = SessionLocal()
    try:
        return db.get(GameSession, session_id).live_transcript
    finally:
        db.close()


def test_retrying_the_same_segment_does_not_duplicate_its_text(client, seed, monkeypatch):
    call_count = {"n": 0}

    async def fake_transcribe(path, glossary="", **kwargs):
        call_count["n"] += 1
        return "The party enters the tavern."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    r1 = _append(client, session_id, 0)
    assert r1.status_code == 200
    assert r1.json()["chunk_text"] == "The party enters the tavern."
    assert r1.json()["transcript"] == "The party enters the tavern."

    # Simulates the client's retry ladder re-POSTing the identical segment
    # after its first response was lost (not the request — the server
    # already transcribed and committed above).
    r2 = _append(client, session_id, 0)
    assert r2.status_code == 200
    assert r2.json()["chunk_text"] == ""  # nothing NEW to append
    assert r2.json()["transcript"] == "The party enters the tavern."  # unchanged, not duplicated

    assert _get_transcript(session_id) == "The party enters the tavern."
    assert call_count["n"] == 1  # Whisper was not re-invoked for the duplicate


def test_a_different_segment_index_still_appends_normally(client, seed, monkeypatch):
    texts = iter(["First chunk.", "Second chunk."])

    async def fake_transcribe(path, glossary="", **kwargs):
        return next(texts)
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    assert _append(client, session_id, 0).status_code == 200
    r = _append(client, session_id, 1)
    assert r.status_code == 200
    assert r.json()["transcript"] == "First chunk. Second chunk."


def test_a_different_recording_id_is_not_treated_as_a_duplicate(client, seed, monkeypatch):
    """Two separate recordings in the same session (Stop, then Start again)
    each mint their own recording_id — segment_index resets to 0 for the new
    one and must not collide with the first recording's segment 0."""
    texts = iter(["Recording one, chunk zero.", "Recording two, chunk zero."])

    async def fake_transcribe(path, glossary="", **kwargs):
        return next(texts)
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    other_rid = "ef" * 16
    assert _append(client, session_id, 0, rid=_RID).status_code == 200
    r = _append(client, session_id, 0, rid=other_rid)
    assert r.status_code == 200
    assert r.json()["transcript"] == "Recording one, chunk zero. Recording two, chunk zero."


def test_legacy_client_without_recording_fields_still_appends_unconditionally(client, seed, monkeypatch):
    """An older client (or any caller that never sends recording_id/
    segment_index) must keep getting exactly the old transcribe-and-append
    behavior — the dedup check is opt-in based on those fields being present
    and valid, never a hard requirement."""
    texts = iter(["Legacy chunk one.", "Legacy chunk two."])

    async def fake_transcribe(path, glossary="", **kwargs):
        return next(texts)
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    assert _append(client, session_id, None, rid=None).status_code == 200
    r = _append(client, session_id, None, rid=None)
    assert r.status_code == 200
    # Both calls appended — no recording_id/segment_index means no dedup key.
    assert r.json()["transcript"] == "Legacy chunk one. Legacy chunk two."


def test_clear_resets_the_segment_dedup_list_too(client, seed, monkeypatch):
    """Otherwise a fresh recording that happens to reuse a cleared
    recording's (recording_id, segment_index) pair could never have its text
    appended again."""
    async def fake_transcribe(path, glossary="", **kwargs):
        return "some text"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    assert _append(client, session_id, 0).status_code == 200
    assert client.post(f"/api/sessions/{session_id}/live-transcript/clear").status_code == 200

    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        assert gs.live_transcript == ""
        assert json.loads(gs.live_transcript_segments_json or "[]") == []
    finally:
        db.close()

    r = _append(client, session_id, 0)
    assert r.status_code == 200
    assert r.json()["transcript"] == "some text"


def test_whisper_call_does_not_hold_a_pooled_db_connection(client, seed, monkeypatch):
    """Reliability regression: this route used to take `db:
    Session = Depends(get_db)`, which checks a connection out of the
    SQLAlchemy pool (5 + 10 overflow, see database.py's _set_sqlite_pragma
    docstring) for the entire request — including this await, which for a
    real Whisper call can take a long time or hang. Enough of those in
    flight exhausts the pool and every other request site-wide (even the
    4s spotlight poll every open tab makes) starts queuing/timing out
    behind it — a plausible mechanism for a site that "was loading long"
    during a live-recorded session. The fix bookends the call with two
    short-lived sessions instead of holding one open across it; this
    asserts zero connections are checked out from the pool at the moment
    Whisper is "running"."""
    checked_out_during_call = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        checked_out_during_call["n"] = engine.pool.checkedout()
        return "some text"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    r = _append(client, session_id, 0)
    assert r.status_code == 200
    assert checked_out_during_call["n"] == 0


# ── docs/STT_LIVE_AUDIT_2026-09.md fixes ─────────────────────────────────────

def test_transcription_holds_the_whisper_job_semaphore(client, seed, monkeypatch):
    """Live chunks now take the same single-slot semaphore the background
    transcription jobs hold (finding 1): the STT backend serves one piece
    of audio at a time, so letting a live chunk stack against a running
    session-recap job just queues both with no benefit."""
    seen = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        seen["value"] = ai_module.whisper_job_semaphore._value
        return "text"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    r = _append(client, session_id, 0)
    assert r.status_code == 200
    assert seen["value"] == 0  # held for the duration of the call


def test_silent_segment_records_its_key_so_a_retry_skips_whisper(client, seed, monkeypatch):
    """Finding 4: a chunk that transcribes to "" with the raw archive off
    used to skip the commit entirely, so its segment key never landed and
    a retried silent chunk re-burned a full Whisper pass for text that was
    always going to be empty."""
    calls = {"n": 0}

    async def fake_transcribe(path, glossary="", **kwargs):
        calls["n"] += 1
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    r1 = _append(client, session_id, 0)
    assert r1.status_code == 200 and r1.json()["transcript"] == ""
    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        assert json.loads(gs.live_transcript_segments_json or "[]") == [f"{_RID}:0"]
    finally:
        db.close()

    r2 = _append(client, session_id, 0)
    assert r2.status_code == 200
    assert calls["n"] == 1  # the retry hit the committed key, not Whisper


def test_concurrent_duplicate_upload_gets_409_not_a_second_transcription(client, seed, monkeypatch):
    """Finding 1's server half: the in-flight guard. A retry that arrives
    while the first upload of the same segment is still transcribing (the
    exact shape a reverse proxy creates by giving up on the response while
    the server keeps working) must get a cheap 409, not start a second
    concurrent transcription of the same audio."""
    import threading
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_transcribe(path, glossary="", **kwargs):
        calls["n"] += 1
        started.set()
        # Blocks the first (and only) call's request until the test lets it
        # finish; a timeout keeps a broken guard from hanging the suite.
        release.wait(30)
        return "the only transcription"
    # _transcribe_chunk awaits transcribe_audio on the request's event loop;
    # a plain-sync fake would block it, so wrap the block in a thread.
    import asyncio

    async def fake_async(path, glossary="", **kwargs):
        return await asyncio.to_thread(fake_transcribe, path, **{"glossary": glossary})
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_async)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    import threading as _t
    result_a = {}

    def _post_a():
        result_a["r"] = _append(client, session_id, 0)
    th = _t.Thread(target=_post_a)
    th.start()
    assert started.wait(30), "first upload never reached transcription"

    r_b = _append(client, session_id, 0)  # the proxy-orphaned twin, re-POSTed
    assert r_b.status_code == 409
    assert "Still transcribing" in r_b.json()["detail"]

    release.set()
    th.join(30)
    assert result_a["r"].status_code == 200
    assert calls["n"] == 1  # exactly one transcription served both uploads

    # Once committed, the ordinary idempotency path takes over.
    r_c = _append(client, session_id, 0)
    assert r_c.status_code == 200
    assert r_c.json()["transcript"] == "the only transcription"
    assert calls["n"] == 1


def test_stale_snapshot_twin_cannot_append_its_text_twice(client, seed, monkeypatch):
    """Re-audit F1 (docs/STT_LIVE_AUDIT_2026-09.md): a twin request whose
    `already_appended` snapshot was read at request START — before the
    first upload committed, which is exactly what an in-flight-marker TTL
    expiry (or a non-sequential client) produces — must not append its
    re-transcribed text on top of the first twin's. Simulated here without
    thread racing: the first _live_transcript_segment_keys call (the
    request-start snapshot) is stubbed to see an empty list, the commit-time
    re-check sees the real one."""
    from app.routers import sessions as sessions_router
    calls = {"n": 0}

    async def fake_transcribe(path, glossary="", **kwargs):
        calls["n"] += 1
        return "the only words"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)

    r1 = _append(client, session_id, 0)
    assert r1.status_code == 200 and _get_transcript(session_id) == "the only words"

    real_keys = sessions_router._live_transcript_segment_keys
    key_reads = {"n": 0}

    def stale_first_read(gs):
        key_reads["n"] += 1
        if key_reads["n"] == 1:
            return []  # the twin's request-start snapshot: key not yet visible
        return real_keys(gs)  # the commit-time re-check: first twin already committed
    monkeypatch.setattr(sessions_router, "_live_transcript_segment_keys", stale_first_read)

    r2 = _append(client, session_id, 0)
    assert r2.status_code == 200
    assert calls["n"] == 2  # the wasted re-transcription happened — accepted cost
    assert _get_transcript(session_id) == "the only words"  # …but not appended twice
    assert r2.json()["transcript"] == "the only words"
