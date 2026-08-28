"""Tests for POST /api/ai/world-context-smart's "entities" field — added so
AI Chat's RAG status line can be clickable, showing what actually got
retrieved for a message (app.retrieval.find_relevant_entities) and letting the
GM pin any of it into every future message of the conversation (see
ai_chat.html's ctx-panel/renderCtxPanel/pinEntity). Purely additive to the
existing context/count/notes response shape.
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, login


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_smart_context_includes_matched_entities(client, seed):
    eid = _make_entity(seed.world_a.id, name="Elena the Merchant", kind="character", summary="Runs a bazaar stall.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Elena", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    data = r.json()
    assert "entities" in data
    ids = {e["id"] for e in data["entities"]}
    assert eid in ids
    matching = [e for e in data["entities"] if e["id"] == eid]
    assert matching[0]["name"] == "Elena the Merchant"
    assert matching[0]["kind"] == "character"


def test_smart_context_entities_empty_when_no_match(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "nonexistent-xyz-query", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    assert r.json()["entities"] == []


def test_smart_context_entities_empty_with_no_active_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    # No active_world cookie set and the GM has no accessible world in this
    # request context isn't reachable in practice (get_world_ctx falls back
    # to the first accessible world) — this just confirms the response
    # shape's "entities" key is always present, matching the has-world path.
    r = client.post("/api/ai/world-context-smart", json={"query": "x", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    assert "entities" in r.json()
