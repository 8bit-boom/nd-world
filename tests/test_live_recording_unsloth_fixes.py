"""Regression tests for the live-recording / Unsloth Studio audit (2026-10).

What these pin, in the order the audit reported it:
  1. the live-recording routes check the SESSION's world and the caller's edit
     level there (a cross-world assistant and a read-only assistant used to get 200);
  2. the raw-audio archive is written BEFORE transcription, so a failing STT
     backend never costs the audio, and the failure reason reaches the GM;
  3. STT failures are classified (503 = try again later, 400 = fix the setup);
  4. a pre-flight tells the GM the STT backend is not ready before a session;
     a Studio-only install defaults its STT backend to Studio;
  5. the archive is not served from /uploads directly.
"""
import json
import re
from pathlib import Path

import pytest

from app import ai as _ai
from app import unsloth_extras as ux
from app.database import SessionLocal
from app.models import GameSession, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent


# ── helpers ──────────────────────────────────────────────────────────────────

def _session(world, live_transcript=""):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world.id, title="S", session_num=1, live_transcript=live_transcript)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _role(user, world, role):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(WorldMembership.world_id == world.id,
                                             WorldMembership.user_id == user.id).first()
        if m is None:
            db.add(WorldMembership(world_id=world.id, user_id=user.id, role=role))
        else:
            m.role = role
        db.commit()
    finally:
        db.close()


def _sessions_level(world, **levels):
    db = SessionLocal()
    try:
        db.get(World, world.id).section_access_json = json.dumps({"sessions": levels})
        db.commit()
    finally:
        db.close()


def _stub_transcribe(monkeypatch, text="spoken words"):
    calls = []

    async def fake(path, glossary="", language="", on_progress=None, **kw):
        calls.append(path.name)
        return text

    monkeypatch.setattr(_ai, "transcribe_audio", fake)
    return calls


def _login(client, user, world, password=PLAYER_PASSWORD):
    client.cookies.clear()
    login(client, user.email, password)
    client.cookies.set("active_world", world.slug)


def _append(client, sid, rec="a", idx=0, save=False, body=b"x" * 200):
    return client.post(f"/api/sessions/{sid}/live-transcript/append",
                       files={"file": ("recording.webm", body, "audio/webm")},
                       data={"recording_id": rec * 32, "segment_index": str(idx), "save_audio": "1" if save else ""})


def _text(sid):
    db = SessionLocal()
    try:
        return db.get(GameSession, sid).live_transcript or ""
    finally:
        db.close()


# ── 1. who may use the live-recording routes ─────────────────────────────────

def test_an_assistant_of_another_world_cannot_append_to_this_worlds_session(client, seed, monkeypatch):
    calls = _stub_transcribe(monkeypatch)
    sid = _session(seed.world_a)
    _role(seed.player_b, seed.world_b, "assistant")
    _login(client, seed.player_b, seed.world_b)
    assert _append(client, sid).status_code == 404
    assert calls == [] and _text(sid) == "", "no Studio work and no text for someone outside the world"


def test_an_assistant_with_sessions_dialed_to_read_cannot_append(client, seed, monkeypatch):
    calls = _stub_transcribe(monkeypatch)
    sid = _session(seed.world_a)
    _role(seed.player_a, seed.world_a, "assistant")
    _sessions_level(seed.world_a, assistant="read")
    _login(client, seed.player_a, seed.world_a)
    assert _append(client, sid).status_code == 403
    assert calls == [] and _text(sid) == ""


def test_an_assistant_of_this_world_can_append_and_a_gm_can_anywhere(client, seed, monkeypatch):
    _stub_transcribe(monkeypatch)
    sid = _session(seed.world_a)
    _role(seed.player_a, seed.world_a, "assistant")
    _login(client, seed.player_a, seed.world_a)
    assert _append(client, sid).status_code == 200 and _text(sid) == "spoken words"
    _login(client, seed.gm, seed.world_b, GM_PASSWORD)           # GM with another world active
    assert _append(client, sid, idx=1).status_code == 200


def test_a_plain_player_still_cannot_append(client, seed, monkeypatch):
    _stub_transcribe(monkeypatch)
    sid = _session(seed.world_a)
    _login(client, seed.player_a, seed.world_a)
    assert _append(client, sid).status_code == 403


def test_the_raw_recording_is_not_listed_or_served_across_worlds(client, seed, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "w.db"))
    _stub_transcribe(monkeypatch)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    assert _append(client, sid, save=True, body=b"SECRET-TABLE-TALK" * 20).status_code == 200
    _role(seed.player_b, seed.world_b, "assistant")
    _login(client, seed.player_b, seed.world_b)
    assert client.get(f"/api/sessions/{sid}/live-audio").status_code == 404
    d = client.get(f"/api/sessions/{sid}/live-audio/download")
    assert d.status_code == 404 and b"SECRET" not in d.content


def test_an_assistant_of_this_world_can_read_the_raw_recording(client, seed, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "w.db"))
    _stub_transcribe(monkeypatch)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    _append(client, sid, save=True)
    _role(seed.player_a, seed.world_a, "assistant")
    _login(client, seed.player_a, seed.world_a)
    assert client.get(f"/api/sessions/{sid}/live-audio").json()["count"] == 1
    assert client.get(f"/api/sessions/{sid}/live-audio/download").status_code == 200
    _sessions_level(seed.world_a, assistant="none")
    assert client.get(f"/api/sessions/{sid}/live-audio").status_code == 403


def test_clear_and_summarize_also_check_the_sessions_world(client, seed, monkeypatch):
    sid = _session(seed.world_a, live_transcript="the party met a stranger")
    _role(seed.player_b, seed.world_b, "assistant")
    _login(client, seed.player_b, seed.world_b)
    assert client.post(f"/api/sessions/{sid}/live-transcript/clear").status_code in (403, 404)
    assert _text(sid) == "the party met a stranger"
    r = client.post(f"/api/sessions/{sid}/ai/summarize-live-transcript-job", json={})
    assert r.status_code in (403, 404)


# ── 2. the archive survives an STT failure; the reason is visible ────────────

def _fail_transcription(monkeypatch, retryable):
    async def failing(path, **kw):
        raise _ai.SttError("Unsloth Studio STT: STT model 'small' is not downloaded.", retryable=retryable)

    monkeypatch.setattr(_ai, "transcribe_audio", failing)


def test_the_raw_audio_is_kept_even_when_transcription_fails(client, seed, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "w.db"))
    _fail_transcription(monkeypatch, retryable=False)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    r = _append(client, sid, save=True, body=b"AUDIO" * 100)
    assert r.status_code == 400 and "not downloaded" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}/live-audio").json()["count"] == 1
    assert _text(sid) == ""
    # the STT backend recovers; the client's retry of the SAME segment transcribes it once, archive not duplicated
    _stub_transcribe(monkeypatch, "hello table")
    assert _append(client, sid, save=True, body=b"AUDIO" * 100).status_code == 200
    assert _text(sid) == "hello table"
    assert client.get(f"/api/sessions/{sid}/live-audio").json()["count"] == 1


def test_a_full_disk_fails_before_any_stt_work_is_spent(client, seed, monkeypatch, tmp_path):
    from app.routers import sessions as sess
    monkeypatch.setenv("DB_PATH", str(tmp_path / "w.db"))
    calls = _stub_transcribe(monkeypatch)

    def no_space(file, dest, max_bytes=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sess, "copy_upload_bounded", no_space)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    r = _append(client, sid, save=True)
    assert r.status_code == 507 and "space" in r.json()["detail"].lower()
    assert calls == [], "the archive is written first, so a full disk never re-burns Studio time"


# ── 3. retry-later vs fix-the-setup ──────────────────────────────────────────

@pytest.mark.parametrize("retryable, status", [(True, 503), (False, 400)])
def test_the_append_route_tells_transient_from_config_failures(client, seed, monkeypatch, retryable, status):
    _fail_transcription(monkeypatch, retryable)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    assert _append(client, sid).status_code == status


@pytest.mark.parametrize("studio_status, message, retryable", [
    (503, "Unsloth Studio unreachable: ConnectError: refused", True),
    (500, "boom", True), (502, "bad gateway", True), (429, "slow down", True), (408, "timeout", True),
    (401, "Invalid API key", False), (409, "STT model 'small' is not downloaded", False),
    (422, "STT model must be owner/model", False), (400, "bad audio", False),
])
def test_studio_failures_are_classified_by_what_the_gm_can_do(tmp_path, monkeypatch, studio_status, message, retryable):
    import asyncio
    f = tmp_path / "c.webm"
    f.write_bytes(b"x" * 1000)
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")

    async def fake_probe(p):
        return None

    async def fake_stt(audio, name, model="small"):
        raise ux.StudioError(message, studio_status)

    monkeypatch.setattr(_ai, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ux, "stt", fake_stt)
    with pytest.raises(_ai.SttError) as ei:
        asyncio.run(_ai._transcribe_one_file(f))
    assert ei.value.retryable is retryable and message in str(ei.value)


def test_the_retryable_flag_defaults_off_and_survives_the_chunked_wrapper():
    assert _ai.SttError("x").retryable is False
    assert _ai.SttError("x", retryable=True).retryable is True
    src = (ROOT / "app" / "ai.py").read_text()
    assert "retryable=exc.unreachable or _status_is_transient(" in src, "Studio outages and 5xx/429 are transient"
    assert "retryable=exc.retryable" in src, "the chunked-transcription wrapper must not drop the flag"


# ── 4. pre-flight ────────────────────────────────

def test_the_check_route_reports_a_ready_studio_with_the_real_chunk_format(client, seed, monkeypatch):
    seen = []

    async def fake_stt(audio, name, model="small"):
        seen.append(name)
        return ""

    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    monkeypatch.setattr(_ai, "_llm_url_override", "http://studio")
    monkeypatch.setattr(ux, "stt", fake_stt)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    d = client.get(f"/api/sessions/{sid}/live-transcript/check").json()
    assert d["ok"] is True and d["backend"] == "unsloth" and d["model"]
    assert seen and seen[0].endswith(".webm"), "live chunks are webm/opus — the check must send that, not a WAV"


def test_the_check_route_says_what_is_wrong(client, seed, monkeypatch):
    async def fake_stt(audio, name, model="small"):
        raise ux.StudioError("STT model 'small' is not downloaded. Download it in Settings, then Voice.", 409)

    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    monkeypatch.setattr(_ai, "_llm_url_override", "http://studio")
    monkeypatch.setattr(ux, "stt", fake_stt)
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    d = client.get(f"/api/sessions/{sid}/live-transcript/check").json()
    assert d["ok"] is False and "not downloaded" in d["message"]


def test_the_check_route_says_so_when_no_studio_key_is_set(client, seed, monkeypatch):
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    monkeypatch.setattr(_ai, "effective_llm_api_key", lambda: "")
    d = client.get(f"/api/sessions/{sid}/live-transcript/check").json()
    assert d["ok"] is False and d["backend"] == "unsloth" and "key" in d["message"].lower()


def test_the_check_route_is_not_open_across_worlds(client, seed):
    sid = _session(seed.world_a)
    _role(seed.player_b, seed.world_b, "assistant")
    _login(client, seed.player_b, seed.world_b)
    assert client.get(f"/api/sessions/{sid}/live-transcript/check").status_code == 404


def test_the_webm_check_tone_is_real_opus_in_webm():
    import asyncio
    data = asyncio.run(ux._tone_webm_bytes())
    if data is None:
        pytest.skip("ffmpeg not available here")
    assert data[:4] == b"\x1a\x45\xdf\xa3", "EBML header — the container a browser MediaRecorder produces"


# ── 5. the archive is not a public static file ───────────────────────────────

def test_uploads_does_not_serve_the_live_archive(client, seed, monkeypatch, tmp_path):
    from app import main as app_main
    monkeypatch.setattr(app_main, "UPLOADS_DIR", tmp_path)
    (tmp_path / "live" / "1" / ("d" * 32)).mkdir(parents=True)
    (tmp_path / "live" / "1" / ("d" * 32) / "000000.webm").write_bytes(b"TABLE-AUDIO")
    (tmp_path / "ok.txt").write_bytes(b"fine")
    _login(client, seed.player_a, seed.world_a)
    assert client.get("/uploads/ok.txt").status_code == 200
    assert client.get(f"/uploads/live/1/{'d' * 32}/000000.webm").status_code == 404


# ── the browser side (source assertions, repo convention) ────────────────────

def _page(client, seed):
    sid = _session(seed.world_a)
    _login(client, seed.gm, seed.world_a, GM_PASSWORD)
    return client.get(f"/sessions/{sid}").text


def test_the_panel_shows_why_chunks_failed(client, seed):
    page = _page(client, seed)
    assert "_liveLastFailure" in page
    assert "err.httpStatus = res.status" in page, "the status code travels with the error"
    body = page.split("async function liveProcessQueue", 1)[1].split("function liveStartSegment", 1)[0]
    assert "e.httpStatus === 503" in body, "an unavailable backend is waited out, not counted as a failed upload"
    assert "attempt = 4" in body, "a setup problem (4xx) parks the chunk at once — three retries cannot fix it"
    refresh = page.split("function liveRefreshStatus()", 1)[1][:900]
    assert "_liveLastFailure" in refresh


def test_the_panel_runs_a_preflight_and_says_when_transcription_lags(client, seed):
    page = _page(client, seed)
    assert 'id="live-stt-warning"' in page
    assert "/live-transcript/check" in page and "liveCheckBackend" in page
    assert "slower than the recording" in page
    assert "ndSeconds" in page


def test_the_panel_no_longer_blames_whisper_for_every_backend(client, seed):
    page = _page(client, seed)
    assert "chunk(s) waiting for Whisper" not in page and "chunk(s) waiting for transcription" in page


def test_member_section_level_follows_the_membership_in_that_world(seed):
    """The level comes from the caller's role in the world asked about - not the request's active world."""
    from app.deps import member_section_level
    from app.models import User
    db = SessionLocal()
    try:
        world_a, world_b = db.get(World, seed.world_a.id), db.get(World, seed.world_b.id)
        gm, player_a = db.get(User, seed.gm.id), db.get(User, seed.player_a.id)
        assert member_section_level(db, gm, world_b, "sessions") == "edit"
        assert member_section_level(db, player_a, world_b, "sessions") == "none", "not a member of world B"
        player_default = member_section_level(db, player_a, world_a, "sessions")
        assert player_default != "edit", "a plain player never edits sessions"
        _role(player_a, world_a, "assistant")
        assert member_section_level(db, player_a, world_a, "sessions") == "edit"
        _role(player_a, world_a, "owner")
        assert member_section_level(db, player_a, world_a, "sessions") == "edit", "an owner is at least an assistant"
        _sessions_level(world_a, assistant="read", player="none")
        db.expire_all()
        assert member_section_level(db, player_a, world_a, "sessions") == "read"
        _role(player_a, world_a, "player")
        db.expire_all()
        assert member_section_level(db, player_a, world_a, "sessions") == "none", "the player column, not the assistant one"
    finally:
        db.close()
