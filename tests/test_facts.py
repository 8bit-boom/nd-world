"""Tests for the Facts feature: GM-only CRUD, the local-model recap parser
(mocked — no real Ollama needed), and world/role scoping."""
import datetime as _dt
import json
import time

from app import ai as ai_module
from app.database import SessionLocal
from app.models import AudioJob, Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_gm_can_create_fact_via_quick_add(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/facts/new", data={"content": "The party met Elyra.", "visible_to_players": "1"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        facts = db.query(Fact).filter(Fact.world_id == seed.world_a.id).all()
        assert len(facts) == 1
        assert facts[0].content == "The party met Elyra."
        assert facts[0].visible_to_players is True
    finally:
        db.close()


def test_quick_add_defaults_hidden_when_checkbox_omitted(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/facts/new", data={"content": "Elyra secretly serves the cult."}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.visible_to_players is False
    finally:
        db.close()


def test_player_cannot_create_fact(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/facts/new", data={"content": "Sneaky"}, follow_redirects=False)
    assert r.status_code == 403

    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0
    finally:
        db.close()


def test_player_cannot_view_facts_page(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/facts")
    assert r.status_code == 403


def test_gm_can_edit_and_delete_fact(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/facts/new", data={"content": "Original", "visible_to_players": "1"})
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        fact_id = fact.id
    finally:
        db.close()

    r = client.post(f"/facts/{fact_id}/edit", data={"content": "Edited"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        fact = db.get(Fact, fact_id)
        assert fact.content == "Edited"
        assert fact.visible_to_players is False  # checkbox omitted on edit
    finally:
        db.close()

    r = client.post(f"/facts/{fact_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(Fact, fact_id) is None
    finally:
        db.close()


def test_facts_scoped_to_active_world(client, seed):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_b.id, content="World B secret", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/facts")
    assert r.status_code == 200
    assert "World B secret" not in r.text


def test_api_facts_parse_returns_draft_without_saving(client, seed, monkeypatch):
    async def fake_parse(raw_text, model=""):
        assert "tavern" in raw_text
        return [
            {"content": "The party visited the tavern.", "visible_to_players": True},
            {"content": "Elyra is secretly a cult agent.", "visible_to_players": False},
        ]
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/parse", json={"text": "went to the tavern, met Elyra, she's a cult agent"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["facts"]) == 2
    assert data["facts"][1]["visible_to_players"] is False

    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0  # parse never writes to the DB
    finally:
        db.close()


def test_api_facts_parse_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(raw_text, model=""):
        raise ValueError("Could not parse facts from that recap — try rephrasing it.")
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/parse", json={"text": "gibberish"})
    assert r.status_code == 502


def test_api_facts_bulk_saves_reviewed_drafts(client, seed):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        session_id = gs.id
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/bulk", json={
        "facts": [
            {"content": "Fact one", "visible_to_players": True},
            {"content": "Fact two", "visible_to_players": False},
            {"content": "  ", "visible_to_players": True},  # blank, dropped
        ],
        "game_session_id": session_id,
    })
    assert r.status_code == 200
    assert r.json()["created"] == 2

    db = SessionLocal()
    try:
        facts = db.query(Fact).filter(Fact.world_id == seed.world_a.id).all()
        assert len(facts) == 2
        assert all(f.game_session_id == session_id for f in facts)
    finally:
        db.close()


def test_player_cannot_call_facts_api(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/facts/parse", json={"text": "whatever"})
    assert r.status_code == 403
    r = client.post("/api/facts/bulk", json={"facts": [{"content": "x", "visible_to_players": True}]})
    assert r.status_code == 403


# ── POST /api/facts/parse-job + GET /api/facts/last-parse ───────────────────
# The parse as a durable background job (see app/audio_jobs.py
# create_facts_parse_job's docstring): the synchronous route held one HTTP
# request open for the whole model call and tripped Cloudflare Tunnel's
# ~100s timeout on long recaps, losing everything; the job row also gives
# the parsed draft a persistent home (result_json) a reload can restore.

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_api_facts_parse_job_creates_job_and_returns_id(client, seed, monkeypatch):
    async def fake_parse(raw_text, model=""):
        return [
            {"content": "The party visited the tavern.", "visible_to_players": True},
            {"content": "Elyra is secretly a cult agent.", "visible_to_players": False},
        ]
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        session_id = gs.id
    finally:
        db.close()

    _login_gm(client, seed)
    r = client.post("/api/facts/parse-job", json={
        "text": "went to the tavern, met Elyra, she's a cult agent",
        "game_session_id": session_id,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # The row is persisted immediately, with the input text and session
    # attribution — not just handed to the browser.
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job is not None
        assert job.world_id == seed.world_a.id
        assert job.purpose == "facts_parse"
        assert job.filename == "Facts"
        assert job.transcript == "went to the tavern, met Elyra, she's a cult agent"
        assert job.game_session_id == session_id
    finally:
        db.close()

    # The background run lands the draft in result_json (via the ordinary
    # audio-jobs status route the page polls) — and no Fact rows exist yet:
    # parsing still never writes facts, only Confirm & Save does.
    deadline = time.time() + 5.0
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/audio-jobs/{job_id}").json()
        if data["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert data["status"] == "done", data.get("error")
    assert len(json.loads(data["result_json"])) == 2
    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0
    finally:
        db.close()


def test_api_facts_parse_job_blank_text_is_400(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/facts/parse-job", json={"text": "   "})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(AudioJob).count() == 0  # nothing created, not even an erroring job
    finally:
        db.close()


def test_api_facts_parse_job_requires_world_and_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/facts/parse-job", json={"text": "whatever"})
    assert r.status_code == 403
    r = client.get("/api/facts/last-parse")
    assert r.status_code == 403


def test_api_facts_last_parse_returns_latest_done_job(client, seed):
    db = SessionLocal()
    try:
        older = AudioJob(world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
                         status="done", result_json='[{"content": "older", "visible_to_players": true}]')
        newer = AudioJob(world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
                         status="done", result_json='[{"content": "newer", "visible_to_players": false}]')
        errored = AudioJob(world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
                           status="error", error="boom", result_json="")
        db.add_all([older, newer, errored])
        db.commit()
        # Identical microsecond timestamps would make "latest" ambiguous —
        # space them out deterministically.
        older.created_at = _dt.datetime(2026, 1, 1, 12, 0, 0)
        newer.created_at = _dt.datetime(2026, 1, 2, 12, 0, 0)
        db.commit()
        newer_id = newer.id  # read before close() detaches the instances
    finally:
        db.close()

    _login_gm(client, seed)
    r = client.get("/api/facts/last-parse")
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == newer_id
    assert data["facts"] == [{"content": "newer", "visible_to_players": False}]
    assert data["created_at"] == "2026-01-02T12:00:00"


def test_api_facts_last_parse_404_when_none(client, seed):
    _login_gm(client, seed)
    assert client.get("/api/facts/last-parse").status_code == 404


def test_api_facts_last_parse_scoped_to_active_world(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_b.id, purpose="facts_parse", filename="Facts",
                        status="done", result_json="[]"))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)
    assert client.get("/api/facts/last-parse").status_code == 404


def test_facts_page_wires_background_parse_and_restore(client, seed):
    """The page must drive the JOB-based flow (parse-job + status polling +
    Restore last parse), not the old blocking /api/facts/parse — that route
    is what produced HTTP 524s on long recaps."""
    _login_gm(client, seed)
    html = client.get("/facts").text
    assert "id=\"parse-btn\"" in html
    assert "/api/facts/parse-job" in html
    assert "pollFactsParseJob" in html
    assert "Parsing in background… (poll" in html
    assert "id=\"restore-parse-btn\"" in html
    assert "restoreLastParse" in html
    assert "/api/facts/last-parse" in html
    # Empty parse outcome is explained, not silently swallowed.
    assert "No in-character facts found" in html


def test_facts_page_ships_recap_templates(client, seed):
    """The template chips and their skeletons ship with the page — a GM
    clicking one gets a parser-shaped skeleton (discrete lines the splitter
    turns into one fact each) instead of a blank textarea."""
    _login_gm(client, seed)
    html = client.get("/facts").text
    for key in ("standard", "timeline", "bullets", "combat", "investigation"):
        assert f'data-tpl="{key}"' in html
    assert "RECAP_TEMPLATES" in html
    # A couple of distinctive skeleton lines per template, so a regression
    # that empties the skeletons is caught even without executing the JS.
    assert "Hidden from players: <the secret behind it>" in html
    assert "Persons of interest: <names and why>" in html
    assert "Clues found:" in html
