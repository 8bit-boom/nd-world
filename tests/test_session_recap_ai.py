"""Tests for the AI session-recap assist (GM: expand notes / condense /
summarize-from-facts on /sessions) and the player-facing session log
(/session-log), whose whole point is that it NEVER exposes the GM's raw
GameSession.summary — only an AI recap synthesized fresh from Facts already
marked visible_to_players for that session."""
import time

from app import ai as ai_module
from app.database import SessionLocal
from app.models import Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


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
    captured = {}

    async def fake_expand(notes, model="", think=True, extra_instructions=""):
        assert notes == "went to the tavern, met Elyra"
        captured["think"] = think
        return "The party visited the tavern and met Elyra."
    monkeypatch.setattr(ai_module, "expand_recap_notes", fake_expand)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/expand-notes", json={"notes": "went to the tavern, met Elyra"})
    assert r.status_code == 200
    assert r.json()["recap"] == "The party visited the tavern and met Elyra."
    assert captured["think"] is True  # checkbox is checked by default


def test_expand_notes_think_off(client, seed, monkeypatch):
    captured = {}

    async def fake_expand(notes, model="", think=True, extra_instructions=""):
        captured["think"] = think
        return "recap"
    monkeypatch.setattr(ai_module, "expand_recap_notes", fake_expand)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/expand-notes", json={"notes": "notes", "think": False})
    assert r.status_code == 200
    assert captured["think"] is False


def test_expand_notes_requires_notes(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/expand-notes", json={"notes": "  "})
    assert r.status_code == 400


def test_condense_recap(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        assert options is None
        captured["model"] = model
        captured["think"] = think
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "A very long recap..."})
    assert r.status_code == 200
    assert r.json()["recap"] == "Short version."
    assert captured["model"] == ""
    assert captured["think"] is True  # checkbox is checked by default


def test_condense_recap_passes_model_and_think(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["model"] = model
        captured["think"] = think
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "text", "model": "llama3.1", "think": False})
    assert r.status_code == 200
    assert captured["model"] == "llama3.1"
    assert captured["think"] is False


def test_condense_recap_fit_context_sizes_num_ctx_to_the_pasted_text(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["options"] = options
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    _login_gm_in(client, seed, seed.world_a)
    recap = "word " * 2000  # long enough that the estimate clears the floor
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": recap, "fit_context": True})
    assert r.status_code == 200
    assert r.json()["recap"] == "Short version."
    expected = ai_module.context_sized_options(recap)
    assert captured["options"] == expected
    assert captured["options"]["num_ctx"] > 2000  # comfortably covers the input, not just the floor


def test_condense_recap_fit_context_false_by_default(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["options"] = options
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "short recap"})
    assert r.status_code == 200
    assert captured["options"] is None


def test_condense_recap_rejects_input_past_the_max_auto_ctx_ceiling(client, seed, monkeypatch):
    """docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 3.3:
    clamping context_sized_options' num_ctx to MAX_AUTO_NUM_CTX alone would
    silently truncate input this far over the ceiling and risk garbage
    output — condense_recap is a single unchunked call with nothing else
    protecting it, so this must 400 instead."""
    async def should_not_be_called(recap, model="", options=None, think=True, **kwargs):
        raise AssertionError("condense_recap must not be called for oversized input")
    monkeypatch.setattr(ai_module, "condense_recap", should_not_be_called)

    _login_gm_in(client, seed, seed.world_a)
    huge_recap = "word " * 40000  # ~200k chars, well past the default ~32k-token ceiling
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": huge_recap})
    assert r.status_code == 400
    assert "too long to condense" in r.json()["detail"]
    assert "Summarize" in r.json()["detail"]


def test_condense_recap_short_input_is_unaffected_by_the_ceiling(client, seed, monkeypatch):
    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        return "Short version."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "a perfectly normal recap"})
    assert r.status_code == 200


def test_condense_job_create_rejects_input_past_the_max_auto_ctx_ceiling(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    huge_recap = "word " * 40000
    r = client.post("/api/sessions/ai/condense-job", json={"recap": huge_recap})
    assert r.status_code == 400
    assert "too long to condense" in r.json()["detail"]


def test_summarize_from_facts_gm(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Fact one", True)
    _add_fact(seed.world_a, session_id, "Fact two (secret)", False)

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions="", think=True):
        captured["facts"] = facts
        captured["think"] = think
        return "Woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-from-facts")
    assert r.status_code == 200
    assert r.json()["recap"] == "Woven recap."
    # GM's summarize call includes every fact, secret or not.
    assert set(captured["facts"]) == {"Fact one", "Fact two (secret)"}
    assert captured["think"] is True  # checkbox is checked by default, even with no body sent at all


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


def test_summarize_from_audio_reports_recap_failed_on_a_failure_sentinel(client, seed, monkeypatch):
    """Transcribe succeeds but summarize itself returns a failure sentinel
    (see is_failure_sentinel) — recap_failed must be True so the client
    knows to offer a retry instead of treating the sentinel text as an
    applyable draft. The transcript is still real and returned as-is."""
    async def fake_transcribe(path, glossary="", **kwargs):
        return "a real transcript"
    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI unavailable: ConnectionError: refused]"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client)
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "a real transcript"
    assert body["recap"] == "[AI unavailable: ConnectionError: refused]"
    assert body["recap_failed"] is True


def test_summarize_from_audio_recap_failed_false_on_success(client, seed, monkeypatch):
    async def fake_transcribe(path, glossary="", **kwargs):
        return "a real transcript"
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "A real recap."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = _upload_audio(client)
    assert r.status_code == 200
    assert r.json()["recap_failed"] is False


# ── /api/sessions/ai/summarize-transcript (retry the summarize step only) ──
#
# For when .../summarize-from-audio(/complete) transcribed fine but
# summarize itself failed — re-runs just that step over the already-
# transcribed text, without re-uploading/re-transcribing the recording.

def test_summarize_transcript_only_reruns_summarize_over_existing_text(client, seed, monkeypatch):
    captured = {}

    async def fake_summarize(transcript, model="", extra_instructions="", think=True, **kwargs):
        captured["transcript"] = transcript
        captured["think"] = think
        return "A recap from the saved transcript."
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/summarize-transcript", json={
        "transcript": "a previously transcribed session", "think": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["transcript"] == "a previously transcribed session"
    assert body["recap"] == "A recap from the saved transcript."
    assert body["recap_failed"] is False
    assert captured["transcript"] == "a previously transcribed session"
    assert captured["think"] is False


def test_summarize_transcript_only_reports_recap_failed_again_if_still_failing(client, seed, monkeypatch):
    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI unavailable: ConnectionError: refused]"
    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/summarize-transcript", json={"transcript": "a transcript"})
    assert r.status_code == 200
    assert r.json()["recap_failed"] is True


def test_summarize_transcript_only_rejects_blank_transcript(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/ai/summarize-transcript", json={"transcript": "   "})
    assert r.status_code == 400


def test_summarize_transcript_only_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/sessions/ai/summarize-transcript", json={"transcript": "x"})
    assert r.status_code == 403


def test_new_session_form_wires_the_retry_without_reupload_button(client, seed):
    """JS-source assertion (no server-rendered failure state to drive
    through a plain HTTP test client) that the retry button, its failure/
    draft state toggle, and the new route are actually wired into the page
    — same style as test_recap_mention_check.py's own button-presence
    checks."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/sessions/new")
    assert r.status_code == 200
    assert 'onclick="aiRetrySummaryFromTranscript()"' in r.text
    assert "function aiRetrySummaryFromTranscript()" in r.text
    assert "/api/sessions/ai/summarize-transcript" in r.text
    assert "function _showRecapFailed(errorText)" in r.text
    assert "function _showRecapDraft(text)" in r.text
    assert "data.recap_failed" in r.text


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


def test_live_transcript_append_prioritizes_this_sessions_featured_entities_in_the_glossary(client, seed, monkeypatch):
    """docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 2.7: this
    route is the one place a game_session_id is naturally on hand for
    _glossary_for_world's new prioritization — confirm it's actually
    threaded through, not just added to the function signature and never
    wired up."""
    import json
    from app.models import Entity

    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        aaric = Entity(world_id=seed.world_a.id, kind="character", name="Aaric Alderman")
        zora = Entity(world_id=seed.world_a.id, kind="character", name="Zora Zeal")
        db.add_all([aaric, zora])
        db.commit()
        db.refresh(zora)
        gs = db.get(GameSession, session_id)
        gs.npcs_json = json.dumps([{"entity_id": zora.id, "name": "Zora Zeal", "kind": "entity"}])
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        captured["glossary"] = glossary
        return "some transcript"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _append_chunk(client, session_id)
    assert r.status_code == 200
    glossary = captured["glossary"]
    assert glossary.index("Zora Zeal") < glossary.index("Aaric Alderman")


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


# ── Live transcript as a background job (docs/DYNAMIC_THINKING_AND_
# PIPELINE_PLAN.md Part 2 item 3.2) ─────────────────────────────────────────

def test_summarize_live_transcript_job_creates_a_session_recap_job(client, seed):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = "raw messy asr text about the tavern"
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-live-transcript-job")
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    db = SessionLocal()
    try:
        from app.models import AudioJob
        job = db.get(AudioJob, job_id)
        assert job.purpose == "session_recap"
        assert job.filename == "Live Transcript"
        assert job.transcript == "raw messy asr text about the tavern"
        assert job.game_session_id == session_id
        assert job.world_id == seed.world_a.id
    finally:
        db.close()


def test_summarize_live_transcript_job_empty_rejected(client, seed):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = ""
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-live-transcript-job")
    assert r.status_code == 400


def test_summarize_live_transcript_job_requires_existing_session(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/sessions/999999/ai/summarize-live-transcript-job")
    assert r.status_code == 404


def test_summarize_live_transcript_job_threads_think_and_model(client, seed):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = "raw text"
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(
        f"/api/sessions/{session_id}/ai/summarize-live-transcript-job",
        json={"think": False, "model": "llama3.1", "extra_instructions": "keep it brief"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    db = SessionLocal()
    try:
        from app.models import AudioJob
        job = db.get(AudioJob, job_id)
        assert job.think is False
        assert job.model == "llama3.1"
        assert job.extra_instructions == "keep it brief"
    finally:
        db.close()


def test_summarize_live_transcript_job_polled_via_the_shared_session_jobs_route(client, seed, monkeypatch):
    """The new job appears in the same list/status routes every other
    session-scoped job (session_recap/condense) already uses — no new
    polling endpoint needed since purpose="session_recap" is already in
    _SESSION_JOB_PURPOSES."""
    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "a woven recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        db.get(GameSession, session_id).live_transcript = "raw text"
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-live-transcript-job")
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["recap"] == "a woven recap"


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


# ── Download summary/live transcript as .md (GM-only) ───────────────────────

def _set_live_transcript(session_id, text):
    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        gs.live_transcript = text
        db.commit()
    finally:
        db.close()


def test_download_summary_md(client, seed):
    session_id = _make_session(seed.world_a, title="Session 1")
    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/sessions/{session_id}/summary.md")
    assert r.status_code == 200
    assert r.text == "RAW GM-ONLY SECRET TEXT never meant for players"
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'filename="Session 1-summary.md"' in r.headers["content-disposition"]


def test_download_transcript_md(client, seed):
    session_id = _make_session(seed.world_a, title="Session 1")
    _set_live_transcript(session_id, "Raw live transcript text.")
    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/sessions/{session_id}/transcript.md")
    assert r.status_code == 200
    assert r.text == "Raw live transcript text."
    assert 'filename="Session 1-transcript.md"' in r.headers["content-disposition"]


def test_download_summary_md_404_when_empty(client, seed):
    session_id = _make_session(seed.world_a)
    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        gs.summary = ""
        db.commit()
    finally:
        db.close()
    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/sessions/{session_id}/summary.md")
    assert r.status_code == 404


def test_download_transcript_md_404_when_empty(client, seed):
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/sessions/{session_id}/transcript.md")
    assert r.status_code == 404


def test_download_md_player_forbidden(client, seed):
    session_id = _make_session(seed.world_a)
    _set_live_transcript(session_id, "secret")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/sessions/{session_id}/summary.md").status_code == 403
    assert client.get(f"/sessions/{session_id}/transcript.md").status_code == 403


def test_download_md_404_for_unknown_session(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/sessions/999999/summary.md")
    assert r.status_code == 404
    r2 = client.get("/sessions/999999/transcript.md")
    assert r2.status_code == 404


def test_recap_draft_element_lookups_are_null_safe(client, seed):
    """_showRecapDraft/_showRecapFailed/_setRecapPreviewText/
    _setRecapTranscriptText previously called document.getElementById(id)
    .textContent/.style directly — if any single id was ever unexpectedly
    missing from the DOM this threw "Cannot read properties of null" and
    aborted the WHOLE function, so a "Use this" tap looked like a total
    no-op (see test_use_this_background_job_button_surfaces_errors_
    instead_of_silent_no_op, which is exactly how this was diagnosed in
    practice). Every one of those call sites must now go through the
    null-safe _setText/_setStyle helpers instead."""
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    page = client.get(f"/sessions/{session_id}").text
    assert "function _setText(id, text)" in page
    assert "function _setStyle(id, prop, value)" in page
    setup_body = page.split("function _setText(id, text)", 1)[1][:400]
    assert "if (el) el.textContent = text;" in setup_body
    # No leftover unguarded document.getElementById(...).textContent/.style
    # inside the four draft-rendering functions specifically.
    for fn in ("_setRecapPreviewText", "_setRecapTranscriptText", "_showRecapFailed", "_showRecapDraft"):
        body = page.split(f"function {fn}(", 1)[1].split("\n}\n", 1)[0]
        assert "document.getElementById(" not in body, f"{fn} still has an unguarded getElementById"


def test_use_this_background_job_button_surfaces_errors_instead_of_silent_no_op(client, seed):
    """The "Use this" button on a finished Background Job (audio-jobs.js'
    onUse callback) previously had no error handling — any exception while
    loading the job's recap/transcript into the draft-review panel looked
    like nothing happened, with no way to tell why without devtools. It
    must now be wrapped so a failure surfaces as text in #ai-recap-status
    instead of failing silently."""
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    page = client.get(f"/sessions/{session_id}").text
    assert "onUse: (job) => {" in page
    body = page.split("onUse: (job) => {", 1)[1].split("},", 1)[0]
    assert "try {" in body
    assert "catch (e)" in body
    assert "ai-recap-status" in body
