"""Tests for POST /api/sessions/{id}/prep/generate — the AI session-prep
checklist generator (app/routers/sessions.py's _session_prep_context +
app.ai.generate_session_prep, mocked here). Draws context from the most
recent PRIOR session's recap/facts, this world's open quests, and the
assigned party, then drafts a checklist without writing anything — the
client adds confirmed items through the existing prep/add route, one at a
time, same as a GM typing a task in by hand.
"""
from app.database import SessionLocal
from app.models import Fact, GameSession, Party, PlayerCharacter, Quest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_session(world_id, session_num, **kwargs):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title=f"Session {session_num}", session_num=session_num, **kwargs)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def test_generate_uses_prior_session_recap_and_facts(client, seed, monkeypatch):
    prior_id = _make_session(seed.world_a.id, 1, summary="The party raided the bazaar.")
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_a.id, game_session_id=prior_id, content="Elena is secretly a smuggler.", visible_to_players=False))
        db.commit()
    finally:
        db.close()
    current_id = _make_session(seed.world_a.id, 2)

    from app import ai as ai_module
    captured = {}

    async def fake_generate(context_text, model=""):
        captured["context"] = context_text
        return ["Decide how Elena reacts to being caught", "Prep a bazaar chase scene"]
    monkeypatch.setattr(ai_module, "generate_session_prep", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/sessions/{current_id}/prep/generate")
    assert r.status_code == 200
    data = r.json()
    assert data["tasks"] == ["Decide how Elena reacts to being caught", "Prep a bazaar chase scene"]
    assert "raided the bazaar" in captured["context"]
    assert "Elena is secretly a smuggler" in captured["context"]

    # Draft-only — nothing written to prep_json yet.
    db = SessionLocal()
    try:
        gs = db.get(GameSession, current_id)
        assert gs.prep_json in (None, "[]")
    finally:
        db.close()


def test_generate_includes_open_quests_and_party(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Quest(world_id=seed.world_a.id, title="Find the missing shipment", status="active", summary="Cargo vanished en route."))
        db.add(Quest(world_id=seed.world_a.id, title="Old resolved thread", status="complete"))
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Rex")
        db.add(pc)
        db.commit()
        db.refresh(pc)
        import json
        party = Party(world_id=seed.world_a.id, name="The Crew", member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.commit()
        db.refresh(party)
        party_id = party.id
    finally:
        db.close()
    session_id = _make_session(seed.world_a.id, 1, party_id=party_id)

    from app import ai as ai_module
    captured = {}

    async def fake_generate(context_text, model=""):
        captured["context"] = context_text
        return ["Advance the shipment quest"]
    monkeypatch.setattr(ai_module, "generate_session_prep", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/sessions/{session_id}/prep/generate")
    assert r.status_code == 200
    assert "Find the missing shipment" in captured["context"]
    assert "Old resolved thread" not in captured["context"]  # not active
    assert "Rex" in captured["context"]


def test_generate_rejects_when_nothing_to_work_with(client, seed):
    session_id = _make_session(seed.world_a.id, 1)  # no prior session, no quests, no party
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/sessions/{session_id}/prep/generate")
    assert r.status_code == 400


def test_generate_surfaces_model_failure(client, seed, monkeypatch):
    _make_session(seed.world_a.id, 1, summary="Something happened.")
    session_id = _make_session(seed.world_a.id, 2)

    from app import ai as ai_module

    async def fake_generate(context_text, model=""):
        raise ValueError("Could not generate a prep checklist — try again.")
    monkeypatch.setattr(ai_module, "generate_session_prep", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/sessions/{session_id}/prep/generate")
    assert r.status_code == 502


def test_generate_404s_for_nonexistent_session(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/999999/prep/generate")
    assert r.status_code == 404


def test_generate_requires_gm(client, seed):
    session_id = _make_session(seed.world_a.id, 1, summary="Something happened.")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/sessions/{session_id}/prep/generate")
    assert r.status_code == 403


def test_generate_ignores_prior_session_from_a_higher_session_num(client, seed, monkeypatch):
    """Only a session with a lower session_num counts as "prior" — a later
    one (e.g. imported out of order) shouldn't leak into "what just
    happened" context."""
    _make_session(seed.world_a.id, 5, summary="A later session, not the immediate predecessor.")
    current_id = _make_session(seed.world_a.id, 2, summary=None)
    _make_session(seed.world_a.id, 1, summary="The actual immediately-prior session.")

    from app import ai as ai_module
    captured = {}

    async def fake_generate(context_text, model=""):
        captured["context"] = context_text
        return ["ok"]
    monkeypatch.setattr(ai_module, "generate_session_prep", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/sessions/{current_id}/prep/generate")
    assert r.status_code == 200
    assert "actual immediately-prior session" in captured["context"]
    assert "later session" not in captured["context"]
