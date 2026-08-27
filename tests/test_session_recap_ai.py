"""Tests for the AI session-recap assist (GM: expand notes / condense /
summarize-from-facts on /sessions) and the player-facing session log
(/session-log), whose whole point is that it NEVER exposes the GM's raw
GameSession.summary — only an AI recap synthesized fresh from Facts already
marked visible_to_players for that session."""
from app import ai as ai_module
from app.database import SessionLocal
from app.models import Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_session(world, title="Session 1", num=1):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world.id, title=title, session_num=num,
                          summary="RAW GM-ONLY SECRET TEXT never meant for players")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _add_fact(world, session_id, content, visible):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=world.id, game_session_id=session_id, content=content, visible_to_players=visible))
        db.commit()
    finally:
        db.close()


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


# ── GM-only AI recap assist ──────────────────────────────────────────────────

def test_expand_notes(client, seed, monkeypatch):
    async def fake_expand(notes, model=""):
        assert notes == "went to the tavern, met Elyra"
        return "The party visited the tavern and met Elyra."
    monkeypatch.setattr(ai_module, "expand_recap_notes", fake_expand)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/expand-notes", json={"notes": "went to the tavern, met Elyra"})
    assert r.status_code == 200
    assert r.json()["recap"] == "The party visited the tavern and met Elyra."


def test_expand_notes_requires_notes(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/expand-notes", json={"notes": "  "})
    assert r.status_code == 400


def test_condense_recap(client, seed, monkeypatch):
    async def fake_condense(recap, model=""):
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "A very long recap..."})
    assert r.status_code == 200
    assert r.json()["recap"] == "Short version."


def test_summarize_from_facts_gm(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Fact one", True)
    _add_fact(seed.world_a, session_id, "Fact two (secret)", False)

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions=""):
        captured["facts"] = facts
        return "Woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-from-facts")
    assert r.status_code == 200
    assert r.json()["recap"] == "Woven recap."
    # GM's summarize call includes every fact, secret or not.
    assert set(captured["facts"]) == {"Fact one", "Fact two (secret)"}


def test_summarize_from_facts_requires_facts(client, seed):
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-from-facts")
    assert r.status_code == 400


def test_player_cannot_call_gm_ai_endpoints(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/sessions/ai/expand-notes", json={"notes": "x"})
    assert r.status_code == 403
    r2 = client.post("/api/sessions/ai/condense-recap", json={"recap": "x"})
    assert r2.status_code == 403


# ── Audio → transcript → recap (file drop/picker or mic recording) ─────────

def _upload_audio(client, filename="audio.wav", data=b"fake-audio-bytes", content_type="audio/wav"):
    import io
    return client.post(
        "/api/sessions/ai/summarize-from-audio",
        files={"file": (filename, io.BytesIO(data), content_type)},
    )


def test_summarize_from_audio_transcribes_and_summarizes(client, seed, monkeypatch):
    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        captured["path_exists_during_call"] = path.is_file()
        return "the party met elena at the bazaar"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["transcript"] = transcript
        return "The party met Elena at the bazaar."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client, filename="session.wav")
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "the party met elena at the bazaar"
    assert body["recap"] == "The party met Elena at the bazaar."
    assert captured["transcript"] == "the party met elena at the bazaar"
    assert captured["path_exists_during_call"] is True


def test_summarize_from_audio_is_session_independent(client, seed, monkeypatch):
    """Works with no session_id in the path at all — usable on the New
    Session form before anything has been saved, like expand-notes/
    condense-recap above."""
    async def fake_transcribe(path, glossary="", **kwargs):
        return "some transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "A recap."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client)
    assert r.status_code == 200


def test_summarize_from_audio_does_not_persist_the_file(client, seed, monkeypatch, tmp_path):
    seen_paths = []

    async def fake_transcribe(path, glossary="", **kwargs):
        seen_paths.append(path)
        return "transcript"
    async def fake_summarize(t, model="", extra_instructions="", **kwargs):
        return "recap"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client)
    assert r.status_code == 200
    assert len(seen_paths) == 1
    assert not seen_paths[0].exists()  # cleaned up after the call


def test_summarize_from_audio_rejects_unsupported_extension(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client, filename="malware.exe", content_type="application/octet-stream")
    assert r.status_code == 400


def test_summarize_from_audio_empty_transcript_rejected(client, seed, monkeypatch):
    async def fake_transcribe(path, glossary="", **kwargs):
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client)
    assert r.status_code == 400


def test_summarize_from_audio_real_failure_surfaces_specific_detail(client, seed, monkeypatch):
    """A real Whisper failure (backend unreachable, timed out, etc — not a
    silent clip) must surface its actual reason, not the generic message
    used for a genuinely empty transcript."""
    async def failing_transcribe(path, glossary="", **kwargs):
        raise ai_module.WhisperError("Could not reach Whisper: ConnectError: refused")
    monkeypatch.setattr(ai_module, "transcribe_audio", failing_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client)
    assert r.status_code == 400
    assert "refused" in r.json()["detail"]


def test_summarize_from_audio_oversized_file_rejected(client, seed, monkeypatch):
    monkeypatch.setattr("app.routers.sessions.MAX_SESSION_AUDIO_BYTES", 10)
    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client, data=b"a" * 100)
    assert r.status_code == 413


def test_summarize_from_audio_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = _upload_audio(client)
    assert r.status_code == 403


# ── Chunked upload path (audio over ndChunkedUpload's threshold) ────────────
# Same reassembly pattern as tests/test_audio_chunked_upload.py — drives the
# two routes directly rather than through the client-side splitting logic.

_PART_A = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xaa" * 5000
_PART_B = b"\xbb" * 5000


def _chunk_file(data):
    import io
    return {"file": ("part", io.BytesIO(data), "application/octet-stream")}


def _upload_two_chunks(client, upload_id):
    r0 = client.post("/api/sessions/ai/summarize-from-audio/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files=_chunk_file(_PART_A))
    r1 = client.post("/api/sessions/ai/summarize-from-audio/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files=_chunk_file(_PART_B))
    return r0, r1


def test_chunk_upload_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/sessions/ai/summarize-from-audio/chunk", data={"upload_id": "a" * 32, "chunk_index": "0"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 403


def test_chunk_complete_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/sessions/ai/summarize-from-audio/complete", data={
        "upload_id": "c" * 32, "filename": "x.wav", "total_chunks": "1",
    })
    assert r.status_code == 403


def test_chunk_complete_rejects_unsupported_extension(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    upload_id = "d" * 32
    client.post("/api/sessions/ai/summarize-from-audio/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    r = client.post("/api/sessions/ai/summarize-from-audio/complete", data={
        "upload_id": upload_id, "filename": "evil.exe", "total_chunks": "1",
    })
    assert r.status_code == 400


def test_chunk_complete_rejects_when_parts_missing(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    upload_id = "e" * 32
    client.post("/api/sessions/ai/summarize-from-audio/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    r = client.post("/api/sessions/ai/summarize-from-audio/complete", data={
        "upload_id": upload_id, "filename": "x.wav", "total_chunks": "2",
    })
    assert r.status_code == 400


def test_chunked_upload_reassembles_and_transcribes(client, seed, monkeypatch):
    async def fake_transcribe(path, glossary="", **kwargs):
        assert path.read_bytes() == _PART_A + _PART_B
        return "reassembled session transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        assert transcript == "reassembled session transcript"
        return "A recap from the reassembled recording."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    upload_id = "f" * 32
    r0, r1 = _upload_two_chunks(client, upload_id)
    assert r0.status_code == 200
    assert r1.status_code == 200

    r = client.post("/api/sessions/ai/summarize-from-audio/complete", data={
        "upload_id": upload_id, "filename": "big-session.wav", "total_chunks": "2",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "reassembled session transcript"
    assert body["recap"] == "A recap from the reassembled recording."

    from app.routers.sessions import _session_audio_chunks_root
    assert not (_session_audio_chunks_root() / upload_id).exists()


def test_chunked_upload_empty_transcript_rejected(client, seed, monkeypatch):
    async def fake_transcribe(path, glossary="", **kwargs):
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    upload_id = "1" * 32
    _upload_two_chunks(client, upload_id)
    r = client.post("/api/sessions/ai/summarize-from-audio/complete", data={
        "upload_id": upload_id, "filename": "silent.wav", "total_chunks": "2",
    })
    assert r.status_code == 400


def test_chunked_upload_rejects_when_reassembled_total_exceeds_limit(client, seed, monkeypatch):
    monkeypatch.setattr("app.routers.sessions.MAX_SESSION_AUDIO_BYTES", len(_PART_A))

    _login_gm_in(client, seed, seed.world_a)
    upload_id = "2" * 32
    _upload_two_chunks(client, upload_id)
    r = client.post("/api/sessions/ai/summarize-from-audio/complete", data={
        "upload_id": upload_id, "filename": "toobig.wav", "total_chunks": "2",
    })
    assert r.status_code == 413


# ── Live session recording: chunk-append, clear, summarize ─────────────────

def _append_chunk(client, session_id, filename="chunk1.webm", data=b"chunk-bytes", content_type="audio/webm"):
    import io
    return client.post(
        f"/api/sessions/{session_id}/live-transcript/append",
        files={"file": (filename, io.BytesIO(data), content_type)},
    )


def test_live_transcript_append_accumulates_across_chunks(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    texts = iter(["The party entered the tavern.", "They met a stranger."])
    async def fake_transcribe(path, glossary="", **kwargs):
        return next(texts)
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r1 = _append_chunk(client, session_id, filename="c1.webm")
    assert r1.status_code == 200
    assert r1.json() == {"chunk_text": "The party entered the tavern.", "transcript": "The party entered the tavern."}

    r2 = _append_chunk(client, session_id, filename="c2.webm")
    assert r2.status_code == 200
    assert r2.json()["transcript"] == "The party entered the tavern. They met a stranger."

    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        assert gs.live_transcript == "The party entered the tavern. They met a stranger."
    finally:
        db.close()


def test_live_transcript_append_silent_chunk_appends_nothing(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    async def fake_transcribe(path, glossary="", **kwargs):
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _append_chunk(client, session_id)
    assert r.status_code == 200
    assert r.json() == {"chunk_text": "", "transcript": ""}


def test_live_transcript_append_real_failure_is_not_swallowed_as_silence(client, seed, monkeypatch):
    """Before this fix, transcribe_audio returning "" for BOTH a silent
    clip and an actual backend failure meant a broken Whisper during a live
    session just silently appended nothing, forever, with no indication
    anything was wrong. A real failure must now surface as an error."""
    session_id = _make_session(seed.world_a)
    async def failing_transcribe(path, glossary="", **kwargs):
        raise ai_module.WhisperError("Could not reach Whisper: ConnectError: refused")
    monkeypatch.setattr(ai_module, "transcribe_audio", failing_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _append_chunk(client, session_id)
    assert r.status_code == 400
    assert "refused" in r.json()["detail"]


def test_live_transcript_append_requires_existing_session(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = _append_chunk(client, 999999)
    assert r.status_code == 404


def test_live_transcript_append_requires_gm(client, seed):
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = _append_chunk(client, session_id)
    assert r.status_code == 403


def test_live_transcript_clear(client, seed):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = "Some accumulated transcript."
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/live-transcript/clear")
    assert r.status_code == 200
    assert r.json() == {"transcript": ""}

    db = SessionLocal()
    try:
        assert db.get(GameSession, session_id).live_transcript == ""
    finally:
        db.close()


def test_summarize_live_transcript(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = "raw messy asr text about the tavern"
        db.commit()
    finally:
        db.close()

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        assert transcript == "raw messy asr text about the tavern"
        return "The party visited the tavern."
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-live-transcript")
    assert r.status_code == 200
    assert r.json()["recap"] == "The party visited the tavern."


def test_summarize_live_transcript_empty_rejected(client, seed):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = ""
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-live-transcript")
    assert r.status_code == 400


# ── Player-facing session log ────────────────────────────────────────────────

def test_session_log_list_reachable_by_player(client, seed):
    _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/session-log")
    assert r.status_code == 200


def test_session_log_detail_reachable_by_player(client, seed):
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/session-log/{session_id}")
    assert r.status_code == 200
    # The raw GM summary text must never appear in the page HTML.
    assert "RAW GM-ONLY SECRET TEXT" not in r.text


def test_player_recap_excludes_gm_only_facts_and_raw_summary(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "The party arrived in Neon City.", True)
    _add_fact(seed.world_a, session_id, "Elyra is secretly working for the cult.", False)

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions=""):
        captured["facts"] = facts
        return "A narrated recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    assert r.json()["recap"] == "A narrated recap."
    assert captured["facts"] == ["The party arrived in Neon City."]


def test_gm_recap_includes_gm_only_facts(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", True)
    _add_fact(seed.world_a, session_id, "Secret fact", False)

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions=""):
        captured["facts"] = facts
        return "Full recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    assert set(captured["facts"]) == {"Public fact", "Secret fact"}


def test_recap_empty_when_no_facts_logged(client, seed):
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    data = r.json()
    assert data["empty"] is True
    assert data["recap"] == ""


def test_player_cannot_reach_session_log_in_other_world(client, seed):
    """Player A is only a member of World A — a session belonging to World B
    (which they can guess the id of) must 404, not leak facts."""
    session_id = _make_session(seed.world_b)
    _add_fact(seed.world_b, session_id, "World B secret", True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/session-log/{session_id}")
    assert r.status_code == 404

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.status_code == 404
