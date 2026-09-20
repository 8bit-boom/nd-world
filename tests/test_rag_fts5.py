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
from app.models import Entity, User

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


def test_matches_via_entity_aliases(client, seed):
    """Entity.aliases (comma-separated alternate names, GM-editable) is the
    app's own built-in synonym mechanism — a player asking about "Vosk"
    for an entity properly named "Hunter Edmund Vosk, the Greyfather"
    should find it via the short form even though neither the body nor
    summary spells it out, and the FTS index used to leave `aliases`
    entirely unindexed."""
    eid = _make_entity(
        seed.world_a.id, name="Hunter Edmund Vosk, the Greyfather", kind="character",
        summary="A grim, silent hunter.", body="He rarely speaks of his past.",
        aliases="Vosk, the Greyfather",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Vosk", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    ids = {e["id"] for e in r.json()["entities"]}
    assert eid in ids


def test_ilike_fallback_matches_via_aliases_too(client, seed):
    db = SessionLocal()
    try:
        eid = _make_entity(
            seed.world_a.id, name="Findable By Alias", kind="character", aliases="ShortForm",
        )
        results = find_relevant_entities_ilike(db, seed.world_a.id, ["shortform"], 10)
        assert eid in {e.id for e in results}
    finally:
        db.close()


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


def test_ranks_a_name_match_above_a_body_that_merely_repeats_the_word(client, seed):
    """A real bug: plain `rank` weights every entity_fts column (name,
    summary, body, tags) equally, so a body repeating a query word several
    times in passing could outscore the entity literally NAMED after it —
    the entity-level version of the bug _HEADING_MATCH_WEIGHT already
    fixed for Rules sections. Verified directly against real FTS5 before
    the weighted-bm25 fix: an entity named "Weapon Traits" (body doesn't
    repeat the word) lost to one named "Ashfall Rifle" whose body says
    "weapon" 8 times."""
    # Both non-note kind, so relative order within the response's combined
    # list reflects FTS rank alone — a note vs. non-note would confound
    # this with the separate non_notes-before-notes list ordering.
    body_repeats_id = _make_entity(
        seed.world_a.id, name="Ashfall Rifle", kind="item",
        body="weapon " * 8,
    )
    name_match_id = _make_entity(seed.world_a.id, name="Weapon Traits", kind="item", body="A reference table.")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "weapon", "limit": 10, "notes_limit": 0})
    ids_in_order = [e["id"] for e in r.json()["entities"]]
    assert name_match_id in ids_in_order and body_repeats_id in ids_in_order
    assert ids_in_order.index(name_match_id) < ids_in_order.index(body_repeats_id)


def test_index_stays_in_sync_after_update(client, seed):
    """The entity_fts_au trigger must fire on an ORM-issued UPDATE, not
    just on raw SQL. Checks find_relevant_entities directly rather than
    through the world-context-smart route — that route's response also
    includes a non-English-query top-up (see docs/DYNAMIC_THINKING_AND_
    PIPELINE_PLAN.md Part 2 item 2.3) that guarantees SOME entities back
    once the world has more than the search alone found, which would make
    a route-level "not in" assertion here a false negative unrelated to
    FTS sync at all."""
    eid = _make_entity(seed.world_a.id, name="Blank Entity", kind="character", body="Nothing notable.")

    db = SessionLocal()
    try:
        before = find_relevant_entities(db, seed.world_a.id, "moonflower", limit=10)
        assert eid not in {e.id for e in before}

        e = db.get(Entity, eid)
        e.body = "Grows moonflower petals in a hidden garden."
        db.commit()

        after = find_relevant_entities(db, seed.world_a.id, "moonflower", limit=10)
        assert eid in {e.id for e in after}
    finally:
        db.close()


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


def test_player_visibility_does_not_starve_out_a_lower_ranked_visible_match(client, seed):
    """A real bug: find_relevant_entities_fts applied its own SQL LIMIT
    BEFORE the Python-side visibility filter ran (unlike the ILIKE
    fallback, whose _visibility_filter is part of the same query the LIMIT
    is applied to). For a real non-GM user, the top-`limit` FTS matches by
    rank could be entirely GM-only — silently shrinking the result below
    `limit`, or to nothing, even when a lower-ranked but actually-visible
    match existed further down. Here, 5 identically-scored hidden entities
    outrank (tie with, in insertion order) one visible entity; a player
    asking with limit=1 must still get the visible one, not an empty list."""
    for i in range(5):
        _make_entity(
            seed.world_a.id, name=f"NPC {i}", kind="character",
            body="Mentions a dragon here.", visible_to_players=False,
        )
    visible_id = _make_entity(
        seed.world_a.id, name="NPC visible", kind="character",
        body="Mentions a dragon here.", visible_to_players=True,
    )
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "dragon", limit=1, user=player)
        assert [e.id for e in results] == [visible_id]
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
