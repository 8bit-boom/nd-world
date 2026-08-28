"""Tests for the FTS5 upgrade to app.retrieval.find_relevant_entities (RAG
retrieval for AI Chat's /api/ai/world-context-smart, Chronicler, and session
Summarize/Condense — see app/retrieval.py, extracted from app.main): it
matches an entity's full `body` text (the old per-word ILIKE only checked
name/summary/tags), ranks by SQLite's own relevance instead of table order,
stays in sync with inserts/updates/deletes via the entity_fts triggers, and
falls back to the old ILIKE matcher if FTS5 itself ever fails.
"""
from app.database import SessionLocal
from app.retrieval import find_relevant_entities, find_relevant_entities_ilike
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


def test_matches_entity_body_text_not_just_name_summary_tags(client, seed):
    """The pre-FTS5 matcher never checked `body` at all — this is the
    headline fix."""
    eid = _make_entity(
        seed.world_a.id, name="Old Man Harrow", kind="character",
        summary="A hermit.",
        body="He speaks constantly of a hidden vault called the Undermarket, sealed beneath the old cistern.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Undermarket", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    ids = {e["id"] for e in r.json()["entities"]}
    assert eid in ids


def test_ranks_a_name_match_above_a_body_only_match(client, seed):
    body_only_id = _make_entity(
        seed.world_a.id, name="Some Merchant", kind="character",
        body="They once traveled with a smuggler named Kestrel Vane.",
    )
    name_match_id = _make_entity(seed.world_a.id, name="Kestrel Vane", kind="character", summary="A smuggler.")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Kestrel Vane", "limit": 10, "notes_limit": 0})
    entities = r.json()["entities"]
    ids_in_order = [e["id"] for e in entities]
    assert name_match_id in ids_in_order and body_only_id in ids_in_order
    assert ids_in_order.index(name_match_id) < ids_in_order.index(body_only_id)


def test_index_stays_in_sync_after_update(client, seed):
    """The entity_fts_au trigger must fire on an ORM-issued UPDATE, not
    just on raw SQL."""
    eid = _make_entity(seed.world_a.id, name="Blank Entity", kind="character", body="Nothing notable.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r1 = client.post("/api/ai/world-context-smart", json={"query": "moonflower", "limit": 10, "notes_limit": 0})
    assert eid not in {e["id"] for e in r1.json()["entities"]}

    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        e.body = "Grows moonflower petals in a hidden garden."
        db.commit()
    finally:
        db.close()

    r2 = client.post("/api/ai/world-context-smart", json={"query": "moonflower", "limit": 10, "notes_limit": 0})
    assert eid in {e["id"] for e in r2.json()["entities"]}


def test_index_stays_in_sync_after_delete(client, seed):
    """The entity_fts_ad trigger must remove a deleted entity from the
    index, not leave a dangling/stale match."""
    eid = _make_entity(seed.world_a.id, name="Doomed Entity", kind="character", body="Contains zorblatt.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r1 = client.post("/api/ai/world-context-smart", json={"query": "zorblatt", "limit": 10, "notes_limit": 0})
    assert eid in {e["id"] for e in r1.json()["entities"]}

    db = SessionLocal()
    try:
        db.query(Entity).filter(Entity.id == eid).delete()
        db.commit()
    finally:
        db.close()

    r2 = client.post("/api/ai/world-context-smart", json={"query": "zorblatt", "limit": 10, "notes_limit": 0})
    assert eid not in {e["id"] for e in r2.json()["entities"]}


def test_ilike_fallback_still_matches_name_summary_tags(client, seed):
    """Direct coverage of the fallback path itself (used when FTS5 fails),
    independent of whether FTS5 is actually available in this environment."""
    db = SessionLocal()
    try:
        eid = _make_entity(seed.world_a.id, name="Fallback Target", kind="character", summary="Findable via ILIKE.")
        results = find_relevant_entities_ilike(db, seed.world_a.id, ["fallback"], 10)
        assert eid in {e.id for e in results}
    finally:
        db.close()


def test_find_relevant_entities_falls_back_when_fts_raises(client, seed, monkeypatch):
    import app.retrieval as retrieval_module
    eid = _make_entity(seed.world_a.id, name="Resilient Entity", kind="character", summary="Still findable.")

    def _broken_fts(db, world_id, words, limit, user=None):
        raise Exception("simulated FTS5 failure")
    monkeypatch.setattr(retrieval_module, "find_relevant_entities_fts", _broken_fts)

    db = SessionLocal()
    try:
        results = find_relevant_entities(db, seed.world_a.id, "Resilient", limit=10)
        assert eid in {e.id for e in results}
    finally:
        db.close()
