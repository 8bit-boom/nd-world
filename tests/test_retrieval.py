"""Unit tests for app/retrieval.py — the shared entity-retrieval module
extracted from app.main (plan item AI 1.2), covering the two things that
didn't already have direct test coverage at this layer: the `user`
visibility filter (new — previously only chronicler.py's now-removed
duplicate had this, and only via ILIKE) and the AI 1.1 body-excerpt
behavior in format_context_from_entities. Route-level FTS/ranking/sync
coverage lives in tests/test_rag_fts5.py; this file is about retrieval.py
itself.

Also covers plan item AI 1.11: the "guaranteed recent notes" top-up in
both RAG consumers (app.main.ai_world_context_smart and app.audio_jobs.
_build_rag_context) ordered by Entity.name instead of Entity.updated_at.
desc() — "recent" meaning most-recently-edited, not alphabetically-first.
"""
from datetime import datetime, timedelta

from app import audio_jobs
from app.database import SessionLocal
from app.models import Entity, User, entity_player_access
from app.retrieval import find_relevant_entities, format_context_from_entities

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


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


def _share_with(entity_id, user_id):
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=entity_id, user_id=user_id))
        db.commit()
    finally:
        db.close()


# ── user= visibility filter ─────────────────────────────────────────────────

def test_user_none_is_unfiltered(client, seed):
    """No `user` given (background jobs, already-GM-gated routes) — every
    existing call site's prior behavior, preserved exactly."""
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        results = find_relevant_entities(db, seed.world_a.id, "Secret Vault", limit=10, user=None)
        assert hidden_id in {e.id for e in results}
    finally:
        db.close()


def test_gm_user_sees_hidden_entities(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        gm = db.get(User, seed.gm.id)
        results = find_relevant_entities(db, seed.world_a.id, "Secret Vault", limit=10, user=gm)
        assert hidden_id in {e.id for e in results}
    finally:
        db.close()


def test_player_user_excludes_hidden_entities(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    visible_id = _make_entity(seed.world_a.id, name="Public Vault", visible_to_players=True)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "Vault", limit=10, user=player)
        ids = {e.id for e in results}
        assert hidden_id not in ids
        assert visible_id in ids
    finally:
        db.close()


def test_player_user_sees_hidden_entity_specifically_shared_with_them(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    _share_with(hidden_id, seed.player_a.id)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "Secret Vault", limit=10, user=player)
        assert hidden_id in {e.id for e in results}
    finally:
        db.close()


def test_player_visibility_filter_also_applies_to_the_no_query_words_fallback(client, seed):
    """A query with no words >3 chars (e.g. "hi") skips keyword search
    entirely and falls back to "every entity in the world" — that fallback
    must still respect visibility, not bypass it."""
    hidden_id = _make_entity(seed.world_a.id, name="Secret Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        results = find_relevant_entities(db, seed.world_a.id, "hi", limit=50, user=player)
        assert hidden_id not in {e.id for e in results}
    finally:
        db.close()


# ── AI 1.1 — format_context_from_entities body excerpts ────────────────────

def test_body_excerpt_appended_for_first_entities(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Old Man Harrow", summary="A hermit.",
        body="He speaks constantly of a hidden vault called the Undermarket, sealed beneath the old cistern.",
    )
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e])
        assert "- [character] Old Man Harrow: A hermit." in context
        assert "Undermarket" in context
    finally:
        db.close()


def test_excerpt_count_zero_is_summary_only_like_before(client, seed):
    eid = _make_entity(seed.world_a.id, name="Old Man Harrow", body="Contains the word Undermarket.")
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], excerpt_count=0)
        assert "Undermarket" not in context
        assert "- [character] Old Man Harrow" in context
    finally:
        db.close()


def test_only_first_excerpt_count_entities_get_a_body_excerpt(client, seed):
    ids = [
        _make_entity(seed.world_a.id, name=f"Entity {i}", body=f"UniqueBodyMarker{i}")
        for i in range(3)
    ]
    db = SessionLocal()
    try:
        entities = [db.get(Entity, i) for i in ids]
        context = format_context_from_entities(entities, excerpt_count=1)
        assert "UniqueBodyMarker0" in context
        assert "UniqueBodyMarker1" not in context
        assert "UniqueBodyMarker2" not in context
    finally:
        db.close()


def test_excerpt_truncated_to_per_entity_char_cap(client, seed):
    eid = _make_entity(seed.world_a.id, name="Long Entity", body="X" * 5000)
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], excerpt_count=1, excerpt_chars=100, excerpt_total_budget=8000)
        assert context.count("X") == 100
    finally:
        db.close()


def test_excerpt_total_budget_caps_across_multiple_entities(client, seed):
    ids = [_make_entity(seed.world_a.id, name=f"Entity {i}", body="Y" * 100) for i in range(3)]
    db = SessionLocal()
    try:
        entities = [db.get(Entity, i) for i in ids]
        context = format_context_from_entities(
            entities, excerpt_count=3, excerpt_chars=100, excerpt_total_budget=150,
        )
        assert context.count("Y") == 150
    finally:
        db.close()


def test_entity_with_no_body_gets_no_excerpt_line(client, seed):
    eid = _make_entity(seed.world_a.id, name="Bodyless Entity", summary="Just a summary.")
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e])
        assert context == "- [character] Bodyless Entity: Just a summary."
    finally:
        db.close()


# ── End-to-end: excerpts actually reach the RAG-consuming routes ───────────

def test_world_context_smart_includes_body_excerpt(client, seed):
    _make_entity(
        seed.world_a.id, name="Old Man Harrow", summary="A hermit.",
        body="He speaks constantly of a hidden vault called the Undermarket.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={"query": "Harrow", "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    assert "Undermarket" in r.json()["context"]


def test_world_context_smart_tops_up_entities_for_a_foreign_language_query(client, seed):
    """Mirrors app.audio_jobs._build_rag_context's own non-English top-up
    (see test_build_rag_context_tops_up_entities_for_a_foreign_language_
    query in test_audio_jobs.py) — the same gap existed one surface over on
    AI Chat's RAG (docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2, item
    2.3): a Russian-language chat question against English-named entities
    had no literal keyword overlap for find_relevant_entities to match, so
    it silently came back with no characters/places at all."""
    _make_entity(
        seed.world_a.id, name="Gareth Ashfall", kind="character", summary="A blacksmith.",
        body="Gareth Ashfall runs the forge near the eastern gate.",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    russian_query = "Партия встретила Гарета возле восточных ворот"
    r = client.post("/api/ai/world-context-smart", json={"query": russian_query, "limit": 10, "notes_limit": 0})
    assert r.status_code == 200
    data = r.json()
    assert "Gareth Ashfall" in data["context"]
    assert any(e["name"] == "Gareth Ashfall" for e in data["entities"])


def test_world_context_smart_no_topup_leak_when_search_already_fills_the_limit(client, seed):
    _make_entity(seed.world_a.id, name="Gareth Ashfall", kind="character", body="A blacksmith.")
    _make_entity(seed.world_a.id, name="Completely Unrelated Entity", kind="location", body="Nothing to do with Gareth.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={
        "query": "Gareth Ashfall the blacksmith", "limit": 1, "notes_limit": 0,
    })
    assert r.status_code == 200
    data = r.json()
    assert "Gareth Ashfall" in data["context"]
    assert "Completely Unrelated Entity" not in data["context"]


# ── AI 1.11 — guaranteed-recent-notes top-up ordered by recency ────────────

def _make_note_with_updated_at(world_id, name, updated_at):
    db = SessionLocal()
    try:
        n = Entity(world_id=world_id, kind="note", name=name, body=f"Body of {name}.")
        db.add(n)
        db.commit()
        db.refresh(n)
        n.updated_at = updated_at
        db.commit()
        return n.id
    finally:
        db.close()


def test_world_context_smart_notes_topup_prefers_most_recently_updated(client, seed):
    """Alphabetically "Ancient Note" would win; recency-wise "Zebra Note"
    (updated far more recently) should be the one guaranteed by notes_limit
    when the keyword search itself doesn't match either."""
    now = datetime.utcnow()
    _make_note_with_updated_at(seed.world_a.id, "Ancient Note", now - timedelta(days=30))
    recent_id = _make_note_with_updated_at(seed.world_a.id, "Zebra Note", now)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-smart", json={
        "query": "unrelated-query-xyz", "limit": 10, "notes_limit": 1,
    })
    assert r.status_code == 200
    note_ids = [e["id"] for e in r.json()["entities"] if e["kind"] == "note"]
    assert note_ids == [recent_id]


def test_build_rag_context_notes_topup_prefers_most_recently_updated(client, seed):
    now = datetime.utcnow()
    _make_note_with_updated_at(seed.world_a.id, "Ancient Note", now - timedelta(days=30))
    _make_note_with_updated_at(seed.world_a.id, "Zebra Note", now)

    context = audio_jobs._build_rag_context(
        seed.world_a.id, "unrelated-query-xyz", entity_limit=0, notes_limit=1,
    )
    assert "Zebra Note" in context
    assert "Ancient Note" not in context
