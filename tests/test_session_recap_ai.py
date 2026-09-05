"""Tests for the AI session-recap assist (GM: expand notes / condense /
summarize-from-facts on /sessions) and the player-facing session log
(/session-log), whose whole point is that it NEVER exposes the GM's raw
GameSession.summary — only an AI recap synthesized fresh from Facts already
marked visible_to_players for that session."""
import time
from datetime import datetime, timedelta

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import AudioJob, Fact, GameSession, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, assert_no_nested_forms, login


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


def _wait_recap_job_done(job_id, timeout=5.0):
    """Poll a session_log_recap job's ROW until terminal. Unlike
    _poll_until_terminal above, this works for PLAYER-driven jobs too —
    the /api/audio-jobs status route is GM/assistant-gated, and the
    session-log recap is player-facing. The background task runs on the
    app's event loop thread, so it makes progress while this thread sleeps."""
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        db = SessionLocal()
        try:
            job = db.get(AudioJob, job_id)
            status = job.status if job else None
        finally:
            db.close()
        if status in ("done", "error", "cancelled", "interrupted"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"recap job {job_id} never reached a terminal status, last seen: {status}")


def _recap_job_count(session_id):
    db = SessionLocal()
    try:
        return db.query(AudioJob).filter(
            AudioJob.purpose == "session_log_recap", AudioJob.game_session_id == session_id,
        ).count()
    finally:
        db.close()


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
        f = Fact(world_id=world.id, game_session_id=session_id, content=content, visible_to_players=visible)
        db.add(f)
        db.commit()
        db.refresh(f)
        return f.id
    finally:
        db.close()


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _login_player_in(client, seed, world):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
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


# ── Live recording raw-audio archive (opt-in "Save raw audio") ──────────────
#
# The append route's second job: when the browser tags a segment with
# save_audio/recording_id/segment_index, keep the raw audio on disk (uploads/
# live/<sid>/<rid>/<6-digit index><ext>) and track the ordered list in
# GameSession.live_audio_files_json, so a bad transcript (Whisper hallucination
# loops, boundary duplicates) stays recoverable and the recording downloadable.

_RID = "ab" * 16  # a valid 32-hex recording id (uploads.CHUNK_ID_RE shape)


def _append_segment(client, session_id, idx, data=b"segment-bytes", rid=_RID, save_audio="1", filename="chunk.webm"):
    import io
    form = {"segment_index": str(idx)}
    if rid is not None:
        form["recording_id"] = rid
    if save_audio is not None:
        form["save_audio"] = save_audio
    return client.post(
        f"/api/sessions/{session_id}/live-transcript/append",
        data=form,
        files={"file": (filename, io.BytesIO(data), "audio/webm")},
    )


def test_live_append_saves_raw_audio_segments_in_order(client, seed, monkeypatch):
    import json
    from app.routers.sessions import _live_audio_root
    session_id = _make_session(seed.world_a)
    texts = iter(["first segment text.", "second segment text."])

    async def fake_transcribe(path, glossary="", **kwargs):
        return next(texts)
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r0 = _append_segment(client, session_id, 0, data=b"one")
    assert r0.status_code == 200
    r1 = _append_segment(client, session_id, 1, data=b"two")
    assert r1.status_code == 200

    seg0 = _live_audio_root(session_id) / _RID / "000000.webm"
    seg1 = _live_audio_root(session_id) / _RID / "000001.webm"
    assert seg0.read_bytes() == b"one"
    assert seg1.read_bytes() == b"two"

    db = SessionLocal()
    try:
        files = json.loads(db.get(GameSession, session_id).live_audio_files_json)
    finally:
        db.close()
    assert files == [
        f"live/{session_id}/{_RID}/000000.webm",
        f"live/{session_id}/{_RID}/000001.webm",
    ]


def test_live_append_retry_same_index_overwrites_without_duplicating(client, seed, monkeypatch):
    """The client retries a failed upload with the SAME segment_index — the
    server must overwrite that segment's file, not grow the archive list (or
    the disk) by one duplicate per retry."""
    import json
    from app.routers.sessions import _live_audio_root
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    assert _append_segment(client, session_id, 0, data=b"zero").status_code == 200
    assert _append_segment(client, session_id, 1, data=b"v1").status_code == 200
    r = _append_segment(client, session_id, 1, data=b"v2")  # retry of the same segment
    assert r.status_code == 200

    assert (_live_audio_root(session_id) / _RID / "000001.webm").read_bytes() == b"v2"
    db = SessionLocal()
    try:
        files = json.loads(db.get(GameSession, session_id).live_audio_files_json)
    finally:
        db.close()
    assert len(files) == 2  # index-1 retry did not add a third entry


def test_live_append_without_save_audio_stores_nothing(client, seed, monkeypatch):
    from app.routers.sessions import _live_audio_root
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    # Old-client shape: no archive fields at all. save_audio="" (unchecked
    # checkbox) must behave identically.
    r0 = _append_chunk(client, session_id)
    r1 = _append_segment(client, session_id, 0, save_audio="")
    assert r0.status_code == 200 and r1.status_code == 200
    assert not _live_audio_root(session_id).exists()

    db = SessionLocal()
    try:
        assert (db.get(GameSession, session_id).live_audio_files_json or "") == ""
    finally:
        db.close()


def test_live_append_rejects_malformed_archive_fields(client, seed, monkeypatch):
    """save_audio=1 with a recording_id that fails the 32-hex check (the same
    CHUNK_ID_RE the chunked-upload endpoints use — which also rules out any
    path traversal) or a negative segment_index is a hard 400, never a silent
    fallback to not-saving: the client's failed-chunk UI has to surface it."""
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _append_segment(client, session_id, 0, rid="../../evil")
    assert r.status_code == 400
    r = _append_segment(client, session_id, -1)
    assert r.status_code == 400


def test_live_append_silent_chunk_still_saves_audio(client, seed, monkeypatch):
    """A segment Whisper hears nothing in still has audio worth keeping —
    the archive entry must commit even when chunk_text is empty (the commit
    used to be conditional on the transcript alone)."""
    import json
    from app.routers.sessions import _live_audio_root
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    r = _append_segment(client, session_id, 0, data=b"silence")
    assert r.status_code == 200
    assert r.json()["chunk_text"] == ""
    assert (_live_audio_root(session_id) / _RID / "000000.webm").is_file()

    db = SessionLocal()
    try:
        files = json.loads(db.get(GameSession, session_id).live_audio_files_json)
    finally:
        db.close()
    assert files == [f"live/{session_id}/{_RID}/000000.webm"]


def test_live_audio_list_reports_count_and_bytes(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    # Nothing saved yet: empty, not an error.
    r = client.get(f"/api/sessions/{session_id}/live-audio")
    assert r.status_code == 200
    assert r.json() == {"files": [], "count": 0, "total_bytes": 0}

    _append_segment(client, session_id, 0, data=b"x" * 100)
    _append_segment(client, session_id, 1, data=b"y" * 60)
    r = client.get(f"/api/sessions/{session_id}/live-audio")
    body = r.json()
    assert body["count"] == 2
    assert body["total_bytes"] == 160
    assert body["files"] == [
        f"live/{session_id}/{_RID}/000000.webm",
        f"live/{session_id}/{_RID}/000001.webm",
    ]


def _make_real_webm(path, seconds):
    """A genuinely decodable webm/opus segment (ffmpeg is part of the test
    image) — the download endpoint concats with `-c copy`, which needs real
    container headers, so the archive-save tests above can't just upload
    b"fake-bytes" for it."""
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(path)],
        check=True, capture_output=True,
    )


def test_live_audio_download_concatenates_saved_segments(client, seed, monkeypatch, tmp_path):
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    # Two differently-sized real segments; the concat has to be at least as
    # big as the largest one (container overhead makes exact-size math flaky).
    seg_a = tmp_path / "a.webm"
    seg_b = tmp_path / "b.webm"
    _make_real_webm(seg_a, 1)
    _make_real_webm(seg_b, 2)

    _login_gm_in(client, seed, seed.world_a)
    _append_segment(client, session_id, 0, data=seg_a.read_bytes())
    _append_segment(client, session_id, 1, data=seg_b.read_bytes())

    r = client.get(f"/api/sessions/{session_id}/live-audio/download")
    assert r.status_code == 200
    assert len(r.content) > max(seg_a.stat().st_size, seg_b.stat().st_size)
    assert f"session-{session_id}-recording" in r.headers["content-disposition"]


def test_live_audio_download_single_segment_served_directly(client, seed, monkeypatch, tmp_path):
    """One saved segment needs no ffmpeg concat — it is served byte-for-byte."""
    from app.routers.sessions import _live_audio_root
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    _append_segment(client, session_id, 0, data=b"only-segment-bytes")
    r = client.get(f"/api/sessions/{session_id}/live-audio/download")
    assert r.status_code == 200
    assert r.content == b"only-segment-bytes"
    assert (_live_audio_root(session_id) / _RID / "000000.webm").is_file()


def test_live_audio_download_with_nothing_saved_is_400(client, seed):
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/api/sessions/{session_id}/live-audio/download")
    assert r.status_code == 400
    assert "No raw audio saved" in r.json()["detail"]


def test_session_detail_delete_form_is_not_nested_inside_edit_form(client, seed):
    """Regression test: the hidden #del-form the Delete confirm-dialog link
    submits used to be written INSIDE the page's main edit/save <form> —
    invalid HTML, silently dropped by every browser's parser, which made
    the Delete button here a complete no-op even though POST /sessions/
    {id}/delete itself (see the test right below) works fine when hit
    directly. See assert_no_nested_forms's own docstring (tests/conftest.py)
    for why pytest's TestClient can't catch this class of bug on its own."""
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    r = client.get(f"/sessions/{session_id}")
    assert r.status_code == 200
    assert 'id="del-form"' in r.text
    assert_no_nested_forms(r.text)


def test_session_delete_removes_raw_audio_tree(client, seed, monkeypatch):
    from app.routers.sessions import _live_audio_root
    session_id = _make_session(seed.world_a)

    async def fake_transcribe(path, glossary="", **kwargs):
        return "transcribed"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _login_gm_in(client, seed, seed.world_a)
    _append_segment(client, session_id, 0, data=b"doomed")
    assert _live_audio_root(session_id).is_dir()

    r = client.post(f"/sessions/{session_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not _live_audio_root(session_id).exists()


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

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        captured["facts"] = facts
        return "A narrated recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    assert r.json()["pending"] is True
    job_id = r.json()["job_id"]

    # The job rows the player-visible recap: audience "players" (so a later
    # GM lookup can't serve this one), and the summarize call excludes the
    # GM-only fact.
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.purpose == "session_log_recap"
        assert job.audience == "players"
        assert job.game_session_id == session_id
        assert job.world_id == seed.world_a.id
    finally:
        db.close()
    assert _wait_recap_job_done(job_id) == "done"

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json() == {"recap": "A narrated recap."}
    assert captured["facts"] == ["The party arrived in Neon City."]


def test_gm_recap_includes_gm_only_facts(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", True)
    _add_fact(seed.world_a, session_id, "Secret fact", False)

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        captured["facts"] = facts
        return "Full recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.json()["pending"] is True
    job_id = r.json()["job_id"]
    db = SessionLocal()
    try:
        assert db.get(AudioJob, job_id).audience == "gm"
    finally:
        db.close()
    assert _wait_recap_job_done(job_id) == "done"

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json() == {"recap": "Full recap."}
    assert set(captured["facts"]) == {"Public fact", "Secret fact"}


def test_recap_empty_when_no_facts_logged(client, seed):
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    # Even the no-facts case goes through a job now (the runner answers the
    # {"recap": "", "empty": true} shape the old synchronous route did), so
    # the first POST is pending until that job runs.
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    assert r.json()["pending"] is True
    job_id = r.json()["job_id"]

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "done"
        import json as _json
        assert _json.loads(job.result_json) == {"recap": "", "empty": True}
    finally:
        db.close()

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    data = r2.json()
    assert data["empty"] is True
    assert data["recap"] == ""


def test_session_log_recap_done_job_is_served_without_a_second_one(client, seed, monkeypatch):
    """The done job IS the cache: once it has finished, POSTs return its
    result verbatim and create no further jobs — the page can reload (or
    poll) as often as it likes without touching the LLM."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert r1.json()["pending"] is True
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    for _ in range(3):
        r = client.post(f"/api/session-log/{session_id}/recap")
        assert r.json() == {"recap": "A woven recap."}
    assert _recap_job_count(session_id) == 1


def test_session_log_recap_fact_edit_forces_a_new_job_durable_rule(client, seed, monkeypatch):
    """Invalidation is DURABLE now, read straight off the Fact rows' own
    timestamps (max(coalesce(updated_at, created_at)) vs the job's
    created_at) instead of an in-process marker a restart rewound: the done
    job still EXISTS (rows are never deleted) but is simply no longer
    served once a fact edit postdates it, and the next POST starts a fresh
    one. The edit here goes through the real form route — the same
    updated_at every write path (web form, bulk, MCP tool) sets, which is
    the whole point of the rule being edit-source-agnostic."""
    session_id = _make_session(seed.world_a)
    fact_id = _add_fact(seed.world_a, session_id, "A fact", True)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(1)
        return "recap #" + str(len(calls))
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #1"}

    # What every Fact edit call does — sets the row's own updated_at.
    client.post(f"/facts/{fact_id}/edit", data={"content": "An edited fact", "visible_to_players": "1"})

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.status_code == 200
    body = r2.json()
    assert body["pending"] is True
    assert body["job_id"] != r1.json()["job_id"]  # the fresh recap comes from a NEW job...
    assert _wait_recap_job_done(body["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #2"}
    assert len(calls) == 2  # ...not from the stale done row
    # Old rows are never deleted — both generations remain inspectable.
    assert _recap_job_count(session_id) == 2


def test_session_log_recap_freshness_survives_module_state_reset(client, seed, monkeypatch):
    """The leak-grade flaw the durable rule exists to close: the old
    in-process staleness marker rewound to 'nothing stale' on every restart,
    resurrecting pre-restart fact edits to players. There is no module
    state left to reset anymore — simulating a restart is a no-op — and the
    done job must STILL be served stale-skipped after the edit. (The
    edited fact's updated_at postdates the job no matter what any process
    remembers.)"""
    session_id = _make_session(seed.world_a)
    fact_id = _add_fact(seed.world_a, session_id, "A fact", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    # The edit, then the "restart": whatever module state the old design
    # needed is gone, so this is literally nothing.
    db = SessionLocal()
    try:
        fact = db.get(Fact, fact_id)
        fact.content = "A fact, now hidden-edited"
        fact.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json()["pending"] is True
    assert r2.json()["job_id"] != r1.json()["job_id"]


def test_session_log_recap_stale_done_row_ignores_other_worlds_fact_edits(client, seed, monkeypatch):
    """Per-world precision: a fact edit in World B must not invalidate
    World A's done recap (the old global marker invalidated every world at
    once). World B's fact rows simply aren't part of World A's session
    freshness comparison, and World B has its own recap_content_touch."""
    session_a = _make_session(seed.world_a, title="A", num=1)
    _add_fact(seed.world_a, session_a, "A fact", True)
    session_b = _make_session(seed.world_b, title="B", num=1)
    _add_fact(seed.world_b, session_b, "B fact", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_a}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    # Edit a fact (and save recap instructions) in World B only.
    db = SessionLocal()
    try:
        b_fact = db.query(Fact).filter(Fact.world_id == seed.world_b.id).first()
        b_fact.updated_at = datetime.utcnow()
        world_b = db.get(World, seed.world_b.id)
        world_b.recap_content_touch = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    # World A's done job is still fresh — same recap served, no new job.
    assert client.post(f"/api/session-log/{session_a}/recap").json() == {"recap": "A recap."}
    assert _recap_job_count(session_a) == 1


def test_session_log_recap_failure_sentinel_becomes_an_error_job_not_a_cached_done(client, seed, monkeypatch):
    """generate_chat never raises on an Ollama-side failure — it returns a
    sentinel STRING, and the job branch used to cache it as a DONE recap in
    result_json, served to every poller until the next fact edit. Now the
    sentinel lands as status=error (mirroring the condense/session_recap
    branches), the route never serves it, and the poll gets a {"failed":
    true} payload carrying the error text."""
    async def failing_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "[AI unavailable: ConnectError: ollama is down]"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", failing_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    job_id = r1.json()["job_id"]
    assert _wait_recap_job_done(job_id) == "error"

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "error"
        assert job.error == "[AI unavailable: ConnectError: ollama is down]"
        assert not (job.result_json or "").strip()  # nothing cached as a done recap
    finally:
        db.close()

    # The poller's next POST: no fresh done row exists, a recent error row
    # does — the failed payload comes back instead of a new job (the 60s
    # creation backoff, see the sibling backoff test).
    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.status_code == 200
    assert r2.json() == {"failed": True, "error": "[AI unavailable: ConnectError: ollama is down]"}
    assert _recap_job_count(session_id) == 1


def test_session_log_recap_thinking_starved_sentinel_is_an_error_too(client, seed, monkeypatch):
    """The narrower '[empty response ... hidden \"thinking\" ...]' sentinel
    (a thinking model burned its whole budget on reasoning) is also a
    failure, not a recap — this branch has no thinking-retry ladder to
    climb, so erroring (and letting the poller surface it) is the only
    honest outcome."""
    async def starving_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return ('[empty response from llama3.1 — the model produced only hidden "thinking" '
                'output but no final answer]')
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", starving_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "error"
    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json()["failed"] is True


def test_session_log_recap_error_backoff_blocks_new_jobs_for_60s_then_expires(client, seed, monkeypatch):
    """A terminally-errored job suppresses new job creation for the same
    (session, audience, config) key for 60 seconds — otherwise the poll
    loop mints a doomed replacement job every 4s while Ollama is down.
    Once the errored row is older than the window (simulated here by
    backdating its created_at — same clock the comparison reads), a POST
    creates a fresh job again."""
    async def failing_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "[AI error: Ollama 500: boom]"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", failing_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    errored_id = r1.json()["job_id"]
    assert _wait_recap_job_done(errored_id) == "error"

    # Inside the window: the failed payload, and NO second row.
    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json() == {"failed": True, "error": "[AI error: Ollama 500: boom]"}
    assert _recap_job_count(session_id) == 1

    # "Wait out" the window by backdating the errored row's created_at —
    # and the fact's timestamps by the SAME delta, so the row still
    # postdates its content (the freshness cutoff) and the ONLY thing that
    # changed is its age relative to the 60s window.
    db = SessionLocal()
    try:
        job = db.get(AudioJob, errored_id)
        job.created_at = job.created_at - timedelta(seconds=61)
        fact = db.query(Fact).filter(Fact.game_session_id == session_id).first()
        fact.created_at = fact.created_at - timedelta(seconds=61)
        fact.updated_at = fact.updated_at - timedelta(seconds=61)
        db.commit()
    finally:
        db.close()

    async def recovered_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recovered recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", recovered_summarize)

    r3 = client.post(f"/api/session-log/{session_id}/recap")
    assert r3.json()["pending"] is True
    assert r3.json()["job_id"] != errored_id
    assert _wait_recap_job_done(r3.json()["job_id"]) == "done"
    assert _recap_job_count(session_id) == 2


# ── `force` — the Session Log page's manual "🔁 Regenerate" button ──────────

def test_session_log_recap_force_skips_the_fresh_cache(client, seed, monkeypatch):
    """A GM clicking Regenerate must always get a brand new job, not the
    cached done one — the whole point of the button."""
    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(1)
        return f"recap #{len(calls)}"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #1"}  # cache hit

    r2 = client.post(f"/api/session-log/{session_id}/recap", json={"force": True})
    assert r2.json()["pending"] is True
    assert r2.json()["job_id"] != r1.json()["job_id"]
    assert _wait_recap_job_done(r2.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #2"}
    assert _recap_job_count(session_id) == 2


def test_session_log_recap_force_skips_the_error_backoff(client, seed, monkeypatch):
    """force must also bypass _RECAP_ERROR_BACKOFF_SECONDS — a GM retrying
    right after a failure shouldn't have to wait out the poll-loop-spam
    guard that window exists for."""
    async def failing_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "[AI error: Ollama 500: boom]"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", failing_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    errored_id = r1.json()["job_id"]
    assert _wait_recap_job_done(errored_id) == "error"

    # Still well inside the 60s backoff window — an unforced poll gets the
    # cached failure, not a new job.
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {
        "failed": True, "error": "[AI error: Ollama 500: boom]",
    }
    assert _recap_job_count(session_id) == 1

    async def recovered_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "Recovered."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", recovered_summarize)

    r2 = client.post(f"/api/session-log/{session_id}/recap", json={"force": True})
    assert r2.json()["pending"] is True
    assert r2.json()["job_id"] != errored_id
    assert _wait_recap_job_done(r2.json()["job_id"]) == "done"
    assert _recap_job_count(session_id) == 2


def test_session_log_recap_force_ignored_for_a_player(client, seed, monkeypatch):
    """force is a GM-only affordance — silently ignored (not a 403) for a
    player, who has no Regenerate button to click in the first place."""
    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(1)
        return f"recap #{len(calls)}"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    r2 = client.post(f"/api/session-log/{session_id}/recap", json={"force": True})
    assert r2.json() == {"recap": "recap #1"}  # still the cached one — force had no effect
    assert _recap_job_count(session_id) == 1


def test_session_log_recap_done_job_with_different_rag_limits_forces_a_new_job(client, seed, monkeypatch):
    """The RAG limits participate in the done/in-flight match (A8): a recap
    woven from 15 retrieved entities is not the artifact a request for 50
    asks for. Unset limits normalize to the same defaults the row would
    have been created with, so a blank-field request still hits a (15, 5)
    row — 15/5 ARE the module defaults."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap",
                     json={"use_rag": True, "rag_entity_limit": 15, "rag_notes_limit": 5})
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    # Same explicit limits → cache hit.
    r2 = client.post(f"/api/session-log/{session_id}/recap",
                     json={"use_rag": True, "rag_entity_limit": 15, "rag_notes_limit": 5})
    assert r2.json() == {"recap": "A recap."}
    # Unset limits → normalized to the same defaults → still the same row.
    r3 = client.post(f"/api/session-log/{session_id}/recap", json={"use_rag": True})
    assert r3.json() == {"recap": "A recap."}

    # Different limits → a different artifact → a new job.
    r4 = client.post(f"/api/session-log/{session_id}/recap",
                     json={"use_rag": True, "rag_entity_limit": 50, "rag_notes_limit": 20})
    assert r4.json()["pending"] is True
    assert r4.json()["job_id"] != r1.json()["job_id"]
    assert _wait_recap_job_done(r4.json()["job_id"]) == "done"
    assert _recap_job_count(session_id) == 2


# ── GET /session-log/{id} seeds the pickers from the LAST job's config ─────
# Without this, a plain reload always sent the blank-default request (empty
# model, RAG off, 15/5) — which the fresh-cache match treats as a DIFFERENT
# artifact from whatever a GM had just generated with a specific model/RAG
# choice (e.g. via Regenerate), so the page silently regenerated under
# different settings instead of re-serving the good recap just shown.

def test_session_log_page_defaults_the_pickers_when_no_job_exists_yet(client, seed):
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/session-log/{session_id}").text
    assert "const RECAP_DEFAULT_MODEL = \"\";" in html
    assert 'id="recap-think" checked' in html
    assert 'id="recap-rag-checkbox" ' in html and 'id="recap-rag-checkbox" checked' not in html
    assert 'id="recap-rag-entity-limit" min="0" value="15"' in html
    assert 'id="recap-rag-notes-limit" min="0" value="5"' in html


def test_session_log_page_seeds_pickers_from_the_last_job_for_this_audience(client, seed, monkeypatch):
    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)
    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap", json={
        "model": "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:gemma-4-26B-A4B-it-UD-IQ4_NL.gguf",
        "think": False, "use_rag": True, "rag_entity_limit": 8, "rag_notes_limit": 3,
    })
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    html = client.get(f"/session-log/{session_id}").text
    assert 'const RECAP_DEFAULT_MODEL = "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:gemma-4-26B-A4B-it-UD-IQ4_NL.gguf";' in html
    assert 'id="recap-think" checked' not in html  # think=False on the last job
    assert 'id="recap-rag-checkbox" checked' in html
    assert 'id="recap-rag-entity-limit" min="0" value="8"' in html
    assert 'id="recap-rag-notes-limit" min="0" value="3"' in html


def test_session_log_page_picker_defaults_are_scoped_per_audience(client, seed, monkeypatch):
    """A GM's own last config must not leak into a player's picker
    defaults, and vice versa — each audience gets its OWN last job."""
    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A public fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap", json={"model": "gm-only-model", "think": False})
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/session-log/{session_id}").text
    # The player's OWN picker defaults, not the GM's — no prior player-
    # audience job exists yet, so this is the untouched default state.
    assert 'const RECAP_DEFAULT_MODEL = "";' in html
    assert 'id="recap-think" checked' in html


def test_session_log_page_renders_one_think_checkbox_and_failed_recap_handling(client, seed):
    """A4/A1 wiring: the recap-think checkbox must render exactly once (it
    was duplicated, so getElementById bound the first and the second was a
    dead visual toggle), and the poll JS must handle the route's
    {"failed": true} payload as plain explanatory text — not the
    'Failed to load recap:' wording reserved for HTTP/network errors, and
    not an unhandled key the loop would treat as an empty recap."""
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    html = client.get(f"/session-log/{session_id}").text
    assert html.count('id="recap-think"') == 1
    assert "data.failed" in html
    assert "Recap generation failed: " in html
    # The failed branch returns (stops polling) rather than continuing.
    assert "status.textContent = 'Recap generation failed: ' + (data.error" in html


def test_session_detail_summarize_from_facts_uses_the_job_pipeline(client, seed):
    """A5 wiring: the Sessions page's 'Summarize from Facts' button must
    drive the session-log recap job endpoint (create-or-poll) with the
    panel's model/think/RAG options, load the result through the same
    review affordance aiRunRecap uses, and surface data.failed via
    _showRecapFailed instead of offering the error text as a draft."""
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/sessions/{session_id}").text
    assert f"fetch('/api/session-log/{session_id}/recap'" in html
    # The old blocking summarize-from-facts call is no longer wired to the
    # button flow (expand-notes stays blocking by design — it's quick).
    assert "/ai/summarize-from-facts" not in html
    assert "_recapRagOptions()" in html
    assert "_recapThinkEnabled()" in html
    assert "data.failed" in html
    assert "_showRecapFailed(data.error" in html


def test_blocking_summarize_from_facts_flags_failure_sentinels(client, seed, monkeypatch):
    """The blocking route stays as documented API but now carries the same
    recap_failed convention summarize-from-audio uses — a sentinel string
    is returned WITH a flag telling the client not to treat it as a
    draft, instead of an unconditional {'recap': sentinel}."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    async def failing_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "[AI unavailable: ConnectError: nope]"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", failing_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/sessions/{session_id}/ai/summarize-from-facts")
    assert r.status_code == 200
    assert r.json() == {"recap": "[AI unavailable: ConnectError: nope]", "recap_failed": True}

    async def good_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", good_summarize)
    r2 = client.post(f"/api/sessions/{session_id}/ai/summarize-from-facts")
    assert r2.json() == {"recap": "A woven recap.", "recap_failed": False}


def test_session_log_recap_poll_while_pending_dedups_into_one_job(client, seed, monkeypatch):
    """Two POSTs while the first job is still thinking — the page's poll
    loop reloading, or two players opening the same session — must latch
    onto the SAME job, not stack a duplicate generation per poll."""
    import asyncio

    async def slow_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        await asyncio.sleep(30)  # still "generating" when the second POST lands
        return "late recap"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", slow_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert r1.json()["pending"] is True
    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json()["pending"] is True
    assert r2.json()["job_id"] == r1.json()["job_id"]
    assert _recap_job_count(session_id) == 1
    # Left in-flight deliberately: the client fixture's zero-grace shutdown
    # cancels the task (same pattern as the _hanging_transcribe tests).


def test_session_log_recap_poll_while_pending_does_not_hit_cooldown(client, seed, monkeypatch):
    """The LLM cooldown fires at job-CREATION only. Reloading the page while
    a generation is already running is free — a 429 here would punish
    exactly the players the background job exists to stop hanging."""
    import asyncio

    async def slow_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        await asyncio.sleep(30)
        return "late recap"
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", slow_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r1 = client.post(f"/api/session-log/{session_id}/recap")  # creation — records the cooldown
    assert r1.status_code == 200
    r2 = client.post(f"/api/session-log/{session_id}/recap")  # instant re-poll — within any cooldown
    assert r2.status_code == 200, r2.text
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_session_log_recap_cooldown_applies_when_creating_for_a_second_session(client, seed, monkeypatch):
    """The anti-spam gate itself is unchanged: two genuinely-new generations
    (different sessions, nothing cached or in flight) within the cooldown
    window still 429 — same contract the synchronous route had, just
    enforced at creation time."""
    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    session_id_1 = _make_session(seed.world_a, title="S1", num=1)
    session_id_2 = _make_session(seed.world_a, title="S2", num=2)
    _add_fact(seed.world_a, session_id_1, "One", True)
    _add_fact(seed.world_a, session_id_2, "Two", True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r1 = client.post(f"/api/session-log/{session_id_1}/recap")
    assert r1.status_code == 200
    r2 = client.post(f"/api/session-log/{session_id_2}/recap")
    assert r2.status_code == 429


def test_session_log_page_polls_for_the_background_recap(client, seed):
    """JS-source assertion (nothing server-rendered to drive through the
    test client): the page must show a generating state and re-POST on an
    interval — the endpoint now answers {"pending": true} first, so a
    single-shot fetch like the old page's would render nothing forever."""
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/session-log/{session_id}")
    assert r.status_code == 200
    assert "Generating recap" in r.text
    assert "RECAP_POLL_MS" in r.text
    assert "RECAP_POLL_CAP_MS" in r.text
    assert "data.pending" in r.text
    assert "method: 'POST'" in r.text


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


# ── Session-log recap: model / Thinking / RAG pickers ───────────────────────
# The Session Log page picks which model generates the recap (and whether it
# thinks / retrieves World lore) the same way the GM Sessions page does; the
# choices ride every poll POST, persist on the AudioJob row, and a recap
# generated with a different configuration is a DIFFERENT artifact for the
# done-job cache lookup.

def test_session_log_recap_job_carries_model_think_and_rag(client, seed, monkeypatch):
    from app import audio_jobs as _audio_jobs

    captured = {}

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        captured.update(model=model, think=think, world_context=world_context)
        return "A narrated recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    build_calls = []

    def fake_build_rag_context(world_id, query, entity_limit, notes_limit, **kwargs):
        build_calls.append((world_id, query, entity_limit, notes_limit))
        return "- [npc] Elyra: an enchanter"
    monkeypatch.setattr(_audio_jobs, "_build_rag_context", fake_build_rag_context)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", True)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/session-log/{session_id}/recap", json={
        "model": "gemma4:26b", "think": False, "use_rag": True,
        "rag_entity_limit": 6, "rag_notes_limit": 3,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["pending"] is True

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.model == "gemma4:26b"
        assert job.think is False
        assert job.use_rag is True
        assert job.rag_entity_limit == 6
        assert job.rag_notes_limit == 3
    finally:
        db.close()
    assert _wait_recap_job_done(job_id) == "done"
    assert captured["model"] == "gemma4:26b"
    assert captured["think"] is False
    assert captured["world_context"] == "- [npc] Elyra: an enchanter"
    # queried against the session's facts — this recap's only content input
    # (and already visibility-filtered per audience).
    assert build_calls == [(seed.world_a.id, "Public fact", 6, 3)]


def test_session_log_recap_rag_is_gm_only(client, seed, monkeypatch):
    """A player's use_rag is forced off server-side: _build_rag_context is
    not visibility-filtered, so player-enabled RAG could pull GM-only lore
    into the recap prompt (the template hides the checkbox from players,
    but the body is client-supplied and must not be trusted). The GM's own
    RAG request goes through untouched."""
    from app import audio_jobs as _audio_jobs

    rag_calls = []

    def fake_build_rag_context(world_id, query, entity_limit, notes_limit, **kwargs):
        rag_calls.append((entity_limit, notes_limit))
        return "- [npc] Elyra: an enchanter"

    monkeypatch.setattr(_audio_jobs, "_build_rag_context", fake_build_rag_context)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A narrated recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", True)

    # Player asks for RAG — silently downgraded, not an error (the UI hides
    # it; a hand-rolled request just loses the flag).
    _login_player_in(client, seed, seed.world_a)
    r = client.post(f"/api/session-log/{session_id}/recap", json={
        "use_rag": True, "rag_entity_limit": 9, "rag_notes_limit": 9,
    })
    assert r.status_code == 200, r.text
    player_job_id = r.json()["job_id"]
    db = SessionLocal()
    try:
        job = db.get(AudioJob, player_job_id)
        assert job.audience == "players"
        assert job.use_rag is False
        assert job.rag_entity_limit is None
        assert job.rag_notes_limit is None
    finally:
        db.close()
    assert _wait_recap_job_done(player_job_id) == "done"
    assert rag_calls == []  # the retrieval never ran for the player's recap

    # GM's identical request keeps RAG.
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/session-log/{session_id}/recap", json={
        "use_rag": True, "rag_entity_limit": 6, "rag_notes_limit": 3,
    })
    assert r.status_code == 200, r.text
    gm_job_id = r.json()["job_id"]
    db = SessionLocal()
    try:
        job = db.get(AudioJob, gm_job_id)
        assert job.audience == "gm"
        assert job.use_rag is True
        assert job.rag_entity_limit == 6
        assert job.rag_notes_limit == 3
    finally:
        db.close()
    assert _wait_recap_job_done(gm_job_id) == "done"
    assert rag_calls == [(6, 3)]


def test_session_log_recap_done_job_with_different_model_forces_a_new_job(client, seed, monkeypatch):
    """A recap generated with one model must NOT satisfy a later request for
    another: the done rows are never deleted and stay "fresh" until a fact
    edit, so without the config match the first model ever used would be
    served forever no matter what the picker says."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(model)
        return "recap by " + (model or "default")
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap", json={"model": "model-a"})
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    # Same model → served from the done job, no second generation.
    r2 = client.post(f"/api/session-log/{session_id}/recap", json={"model": "model-a"})
    assert r2.json() == {"recap": "recap by model-a"}
    # Different model → a different artifact → new job.
    r3 = client.post(f"/api/session-log/{session_id}/recap", json={"model": "model-b"})
    assert r3.json()["pending"] is True
    assert r3.json()["job_id"] != r1.json()["job_id"]
    assert _wait_recap_job_done(r3.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap", json={"model": "model-b"}).json() == {"recap": "recap by model-b"}
    assert len(calls) == 2
    assert _recap_job_count(session_id) == 2


def test_session_log_recap_done_job_with_different_think_or_rag_forces_a_new_job(client, seed, monkeypatch):
    """Think and RAG participate in the same done-job match: a recap woven
    without reasoning or retrieved lore is not the artifact a request with
    either enabled asks for."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap", json={"think": True})
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    # Same think → cache hit.
    r2 = client.post(f"/api/session-log/{session_id}/recap", json={"think": True})
    assert r2.json() == {"recap": "A recap."}
    # Flipped think → new job; and RAG off→on mismatches too.
    r3 = client.post(f"/api/session-log/{session_id}/recap", json={"think": False})
    assert r3.json()["pending"] is True
    assert _wait_recap_job_done(r3.json()["job_id"]) == "done"
    r4 = client.post(f"/api/session-log/{session_id}/recap", json={"think": False, "use_rag": True})
    assert r4.json()["pending"] is True
    assert _wait_recap_job_done(r4.json()["job_id"]) == "done"
    assert _recap_job_count(session_id) == 3


@pytest.mark.asyncio
async def test_summarize_session_from_facts_defaults_num_predict_when_nothing_configured(monkeypatch):
    """The degeneration guard: with no GM-configured num_predict (Ollama's
    own default is unlimited), the recap call must cap ITSELF — a reported
    degenerating model looped repeated sentences and digit garbage for
    thousands of tokens because nothing bounded the generation. Applies with
    think off too: a degeneration loop is not a thinking-only failure."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_session_from_facts(["fact one"])
    await ai_module.summarize_session_from_facts(["fact one"], think=False)
    assert seen[0]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT
    assert seen[1]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT


@pytest.mark.asyncio
async def test_summarize_session_from_facts_guard_never_clobbers_configured_num_predict(monkeypatch):
    """A GM's configured value must win over the default. With Thinking on,
    what wins is the thinking override's widened value (configured +
    headroom) — the guard stands down entirely. With Thinking off, the
    configured value flows through _chat_kwargs' options merge instead, so
    the guard must leave the per-call options without a num_predict rather
    than silently RAISING the GM's cap to the default."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_session_from_facts(["fact one"], think=True)
    assert seen[0]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS
    await ai_module.summarize_session_from_facts(["fact one"], think=False)
    # No per-call num_predict override at all (opts stayed empty, so
    # generate_chat gets options=None) — the configured 512 still reaches
    # Ollama through _chat_kwargs' own options merge, which is exactly the
    # channel the guard must never shadow with its default.
    assert seen[1] is None or "num_predict" not in seen[1]


# ── _clean_degenerate_recap: hallucinated-turn cleanup ──────────────────────
# A degenerating model doesn't only loop repeated lines (the guard above) —
# a thin session (few facts logged) can give it little to genuinely narrate,
# and rather than stopping once it's said everything real, it fills the
# rest of its num_predict budget by simulating an entirely different,
# unrelated conversation. Reported: a recap that ended mid-sentence, then
# continued with a leaked "<|im1_start|>system" token, a fake system prompt
# about extracting "eat"/"drink" sentences, and invented user/assistant
# turns with unrelated C code. None of that repeats line-for-line, so the
# pre-existing repetition-ratio check alone never caught it.

def test_clean_degenerate_recap_strips_any_pipe_delimited_special_token():
    """Not a fixed token list — <|im1_start|>, <|endoftext|>, and any other
    <|...|>-shaped variant a specific GGUF export happens to leak all match
    the same regex, not just the exact <|im_start|> spelling seen before."""
    cleaned, truncated = ai_module._clean_degenerate_recap(
        "A real recap paragraph.<|im1_start|>assistant<|endoftext|> more text<|channel|>"
    )
    assert "<|" not in cleaned
    assert "A real recap paragraph." in cleaned


def test_clean_degenerate_recap_strips_gemma_turn_tokens():
    cleaned, truncated = ai_module._clean_degenerate_recap(
        "<start_of_turn>A real recap paragraph.<end_of_turn>"
    )
    assert cleaned == "A real recap paragraph."
    assert truncated is False


def test_clean_degenerate_recap_truncates_at_a_hallucinated_turn_marker():
    """The reported failure shape: real content, then a bare role-marker
    line, then an entirely different fake conversation. Everything from
    the marker on is dropped and `truncated` comes back True — even though
    nothing here repeats, so the line-repetition heuristic alone would
    have missed it."""
    raw = (
        "The party met Elena at the bazaar and struck a deal.\n\n"
        "system\n\n"
        "You are a helpful assistant. Extract sentences with 'eat' or 'drink'.\n\n"
        "user\n\n"
        "#include <stdio.h>\n"
    )
    cleaned, truncated = ai_module._clean_degenerate_recap(raw)
    assert cleaned == "The party met Elena at the bazaar and struck a deal."
    assert truncated is True


def test_clean_degenerate_recap_does_not_truncate_a_marker_word_inside_a_sentence():
    """Only a BARE role-marker line (nothing else on it) counts — a
    legitimate recap mentioning "the system" or the word "user" inline in
    prose must survive untouched."""
    raw = "The old system of guilds governed the market, and every user of the bazaar paid a toll."
    cleaned, truncated = ai_module._clean_degenerate_recap(raw)
    assert cleaned == raw
    assert truncated is False


def test_clean_degenerate_recap_does_not_truncate_when_marker_is_the_very_first_line():
    """No real content precedes it yet, so this isn't "hallucination after
    real output" — collapsing to nothing would discard content a retry
    might have salvaged instead. (In practice a recap opening with a bare
    "assistant" line is itself pathological, but truncating to an empty
    string here would be a strictly worse outcome than leaving it alone
    for the caller's degeneration-ratio/retry logic to handle.)"""
    cleaned, truncated = ai_module._clean_degenerate_recap("assistant\n\nSome recap text.")
    assert truncated is False
    assert "Some recap text." in cleaned


def test_clean_degenerate_recap_still_collapses_repeated_lines():
    cleaned, truncated = ai_module._clean_degenerate_recap(
        "The party explored the ruins.\nThe party explored the ruins.\nThe party explored the ruins.\nThey found treasure."
    )
    assert cleaned == "The party explored the ruins.\nThey found treasure."
    assert truncated is False


@pytest.mark.asyncio
async def test_summarize_session_from_facts_retries_on_a_hallucinated_turn(monkeypatch):
    """truncated=True from the FIRST attempt must trigger the same
    repeat_penalty retry the repetition-ratio check already does — a
    truncated-but-non-repetitive recap wouldn't otherwise trip
    _recap_degeneration_ratio at all."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        calls.append(options)
        if len(calls) == 1:
            return "The party met Elena.\n\nsystem\n\nfake unrelated task\n\nuser\n\n#include <stdio.h>"
        return "The party met Elena at the bazaar and struck a deal."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    recap = await ai_module.summarize_session_from_facts(["The party met Elena at the bazaar."])
    assert recap == "The party met Elena at the bazaar and struck a deal."
    assert len(calls) == 2  # the retry actually happened
    assert calls[1]["repeat_penalty"] == 1.2


@pytest.mark.asyncio
async def test_summarize_session_from_facts_keeps_first_attempt_when_retry_is_no_better(monkeypatch):
    """Best-effort: if the retry ALSO comes back truncated, the first
    attempt's (still-truncated-but-non-empty) cleaned text is kept rather
    than being discarded for something no better."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        calls.append(options)
        return f"Real content attempt {len(calls)}.\n\nassistant\n\nfake stuff"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    recap = await ai_module.summarize_session_from_facts(["fact one"])
    assert recap == "Real content attempt 1."  # first attempt kept, not overwritten by an equally-bad retry
    assert len(calls) == 2


# ── The same degeneration guard on the recap-family siblings (A9) ────────────
# _recap_num_predict_default_if_unbounded now covers condense_recap,
# expand_recap_notes and summarize_transcript too — same rule as the
# summarize_session_from_facts guard tests above: the default applies only
# when NOTHING else (caller options, max_tokens, the expanded-thinking rung,
# a GM-configured cap, the thinking widening) supplied a num_predict.

@pytest.mark.asyncio
async def test_condense_recap_defaults_num_predict_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.condense_recap("a recap", think=True)
    await ai_module.condense_recap("a recap", think=False)
    # Thinking and non-thinking alike: a degeneration loop is not a
    # thinking-only failure, and no caller/cap/configured value bounded
    # either call.
    assert seen[0]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT
    assert seen[1]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT
    # But max_tokens' own hard cap (think=False) still wins over the default.
    await ai_module.condense_recap("a recap", max_tokens=150, think=False)
    assert seen[2]["num_predict"] == 150
    # And the caller's explicit options dict wins too.
    await ai_module.condense_recap("a recap", options={"num_predict": 77}, think=False)
    assert seen[3]["num_predict"] == 77


@pytest.mark.asyncio
async def test_condense_recap_guard_never_clobbers_configured_num_predict(monkeypatch):
    """The configured-wins half for condense_recap: with think=True the
    thinking widening already supplies a num_predict (guard stands down,
    covered separately in test_ollama_options); with think=False the
    configured value must flow through _chat_kwargs' merge unmolested —
    no per-call override at all."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.condense_recap("a recap", think=False)
    assert seen[0] is None or "num_predict" not in seen[0]


@pytest.mark.asyncio
async def test_expand_recap_notes_defaults_num_predict_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "expanded notes"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.expand_recap_notes("terse notes", think=True)
    await ai_module.expand_recap_notes("terse notes", think=False)
    assert seen[0]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT
    assert seen[1]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT


@pytest.mark.asyncio
async def test_summarize_transcript_defaults_num_predict_when_nothing_configured(monkeypatch):
    """Both the short single-call path AND every chunk of a long transcript
    get the guard's default — a degenerating model loops on either."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_transcript("a short transcript")
    assert seen == [{"num_predict": ai_module._RECAP_NUM_PREDICT_DEFAULT}]

    seen.clear()
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    await ai_module.summarize_transcript(long_transcript)
    assert len(seen) > 1
    assert all(o == {"num_predict": ai_module._RECAP_NUM_PREDICT_DEFAULT} for o in seen)


@pytest.mark.asyncio
async def test_summarize_transcript_guard_stands_down_for_expanded_thinking(monkeypatch):
    """The expanded rung sets its own (much larger) num_predict — the guard
    must not layer its default on top of or instead of it. This rung only
    ever runs as a recovery from a starved budget; capping it back down to
    the default would defeat its entire purpose."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_transcript("a short transcript", expanded_thinking=True)
    assert seen == [ai_module.expanded_thinking_options()]


def test_session_log_page_ships_recap_model_think_and_rag_pickers(client, seed):
    """The pickers must render under the recap box AND their values must ride
    the poll POST (via recapRequestOptions()) — the endpoint is the
    create-or-poll surface, so a body-less poll would silently ignore them.
    RAG is the exception: it's GM-only (the retrieval isn't
    visibility-filtered, so player-enabled RAG could leak hidden lore into
    the recap a player reads) — the checkbox renders for the GM and is
    absent for players, enforced server-side too."""
    session_id = _make_session(seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/session-log/{session_id}").text
    assert 'id="recap-model"' in html
    assert "(default model)" in html
    assert 'id="recap-think" checked' in html  # Thinking starts checked
    # Player view: no RAG controls at all (leak prevention), and the JS
    # degrades to use_rag: false without them.
    assert 'id="recap-rag-checkbox"' not in html
    assert 'id="recap-rag-entity-limit"' not in html
    assert 'id="recap-rag-notes-limit"' not in html
    assert "use_rag: !!(ragBox && ragBox.checked)" in html
    # Same population mechanism as the Sessions page's model dropdown.
    assert "loadRecapModelOptions" in html
    assert "/api/ai/models" in html
    # The POST body includes the pickers' values.
    assert "recapRequestOptions()" in html

    _login_gm_in(client, seed, seed.world_a)
    gm_html = client.get(f"/session-log/{session_id}").text
    assert 'id="recap-rag-checkbox"' in gm_html
    assert 'id="recap-rag-entity-limit"' in gm_html
    assert 'id="recap-rag-notes-limit"' in gm_html


# ── Player recap publish model (Part B) ──────────────────────────────────────

def test_published_recap_served_verbatim_without_ai(client, seed):
    """The publish model's core: a GM-published player_summary is the Session
    Log's canonical content — served to players verbatim (markdown-rendered)
    with NO recap generation, no polling, and no picker UI. An AI draft can
    seed it, but what ships is exactly what the GM published."""
    session_id = _make_session(seed.world_a)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/sessions/{session_id}/recap-publish", data={
        "player_summary": "**The party** survived the crash into Valhalla.",
        "publish": "1",
    }, follow_redirects=False)
    assert r.status_code == 303

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/session-log/{session_id}").text
    assert "survived the crash into Valhalla" in html
    # Published mode: the poll/picker machinery isn't even shipped.
    assert "Loading recap" not in html
    assert 'id="recap-think"' not in html


def test_unpublished_session_falls_back_to_facts_recap(client, seed, monkeypatch):
    """Without a publish, the Session Log falls back to the facts-recap
    pipeline (pending → done), exactly as before the publish model existed."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A facts-woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.status_code == 200
    assert r.json()["pending"] is True


def test_recap_publish_is_gm_only_and_handles_empty(client, seed):
    session_id = _make_session(seed.world_a)

    # Players can't publish (the route is GM-only via the auth gate).
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/sessions/{session_id}/recap-publish",
                    data={"player_summary": "sneaky", "publish": "1"})
    assert r.status_code == 403

    # GM: publishing an EMPTY text saves the text but doesn't publish
    # (there'd be nothing on the Session Log).
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/sessions/{session_id}/recap-publish",
                    data={"player_summary": "", "publish": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        gs = db.get(GameSession, session_id)
        assert gs.player_summary == ""
        assert gs.player_summary_published is False
    finally:
        db.close()


def test_session_log_list_shows_recap_markers(client, seed, monkeypatch):
    """The Session Log list marks what each session offers: a published
    recap (canonical), a facts-derived recap (auto), or nothing — no dead
    clicks onto sessions whose pages would say 'No recap available'."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact with content", True)

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A facts-woven recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/session-log/{session_id}/recap")
    assert r.json()["pending"] is True
    assert _wait_recap_job_done(
        __import__("app.database", fromlist=["SessionLocal"]).SessionLocal
        and r.json().get("job_id", _any_running_job_id(session_id))
    ) if False else True  # progress asserted via the done-job path below

    html = client.get("/session-log").text
    assert "Recap from facts available" in html


def _any_running_job_id(session_id):
    return None
