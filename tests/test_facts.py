"""Tests for the Facts feature: GM-only CRUD, the local-model recap parser
(mocked — no real Ollama needed), and world/role scoping."""
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


# ── POST /api/facts/from-job/{id} — the auto-drafted-on-completion review ──

def _make_recap_job(world_id, game_session_id=None, pending_facts=None):
    import json
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose="session_recap", filename="clip.mp3", status="done",
            recap="A recap.", game_session_id=game_session_id,
            pending_facts_json=json.dumps(pending_facts if pending_facts is not None else []),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def test_from_job_confirms_edited_draft(client, seed):
    job_id = _make_recap_job(seed.world_a.id, pending_facts=[
        {"content": "The party discovered a hidden lab.", "visible_to_players": True},
        {"content": "Elyra is secretly a cult agent.", "visible_to_players": False},
    ])
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/facts/from-job/{job_id}", json={"facts": [
        {"content": "The party discovered a hidden lab.", "visible_to_players": True},
        {"content": "Elyra is secretly a cult agent, EDITED.", "visible_to_players": False},
    ]})
    assert r.status_code == 200
    assert r.json()["created"] == 2

    db = SessionLocal()
    try:
        facts = db.query(Fact).filter(Fact.world_id == seed.world_a.id).order_by(Fact.id).all()
        assert [f.content for f in facts] == [
            "The party discovered a hidden lab.", "Elyra is secretly a cult agent, EDITED.",
        ]
        job = db.get(AudioJob, job_id)
        assert job.pending_facts_json == "[]"
    finally:
        db.close()


def test_from_job_uses_the_jobs_own_session(client, seed):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        session_id = gs.id
    finally:
        db.close()
    job_id = _make_recap_job(seed.world_a.id, game_session_id=session_id, pending_facts=[
        {"content": "A fact.", "visible_to_players": True},
    ])
    _login_gm_in(client, seed, seed.world_a)
    client.post(f"/api/facts/from-job/{job_id}", json={"facts": [
        {"content": "A fact.", "visible_to_players": True},
    ]})
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.game_session_id == session_id
    finally:
        db.close()


def test_from_job_empty_list_dismisses_without_saving(client, seed):
    job_id = _make_recap_job(seed.world_a.id, pending_facts=[
        {"content": "Something drafted.", "visible_to_players": True},
    ])
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/api/facts/from-job/{job_id}", json={"facts": []})
    assert r.status_code == 200
    assert r.json()["created"] == 0

    db = SessionLocal()
    try:
        assert db.query(Fact).filter(Fact.world_id == seed.world_a.id).count() == 0
        job = db.get(AudioJob, job_id)
        assert job.pending_facts_json == "[]"
    finally:
        db.close()


def test_from_job_unknown_job_404s(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/from-job/999999", json={"facts": []})
    assert r.status_code == 404


def test_from_job_requires_gm(client, seed):
    job_id = _make_recap_job(seed.world_a.id, pending_facts=[{"content": "x", "visible_to_players": True}])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/facts/from-job/{job_id}", json={"facts": []})
    assert r.status_code == 403
