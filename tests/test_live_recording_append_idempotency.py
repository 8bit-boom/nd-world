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
from app.database import SessionLocal
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
