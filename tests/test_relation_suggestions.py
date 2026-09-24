"""Tests for the AI-assisted relation-suggestion pass: app.ai.
suggest_relations_from_text (the raw JSON-schema-constrained Ollama call),
app.vault_sync.suggest_relations_for_vault (the per-note orchestration that
anchors/dedupes its results), and the two routes that expose it — POST
/api/knowledge/suggest-relations (draft-only, writes nothing) and POST
/api/knowledge/relations/bulk (the review panel's Confirm & Save action).

This is a companion to tests/test_knowledge_graph.py's sync_vault coverage,
not a replacement: sync_vault's [[wikilink]]/frontmatter pass and this
LLM-prose pass are two independent ways EntityRelation rows get created,
and the interesting seam between them (a confirmed AI suggestion must
survive the NEXT vault resync) gets its own regression test at the bottom.
"""
import asyncio
import types
import uuid

import pytest

from app import ai as ai_module
from app import vault_sync as vs
from app.database import SessionLocal
from app.models import Entity, EntityRelation, World
from app.routers import knowledge as knowledge_router

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── app.ai.suggest_relations_from_text (unit-level, stubbed Ollama) ─────────

class _FakeChatClient:
    """Records every .chat() kwargs dict and answers with a canned JSON
    payload — same idea as test_facts.py's _CaptureChatClient, sized for
    this module's own schema instead of the facts one."""

    def __init__(self, calls, content='{"relations": []}'):
        self._calls = calls
        self._content = content

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        return types.SimpleNamespace(message=types.SimpleNamespace(content=self._content))


@pytest.mark.asyncio
async def test_suggest_relations_from_text_returns_parsed_list():
    calls = []
    content = '{"relations": [{"source": "Bob", "target": "Guild", "relation": "works_for"}]}'
    orig_client = ai_module._client
    ai_module._client = lambda: _FakeChatClient(calls, content)
    try:
        out = await ai_module.suggest_relations_from_text("Bob works for the Guild.", ["Bob", "Guild"], model="m1")
    finally:
        ai_module._client = orig_client
    assert out == [{"source": "Bob", "target": "Guild", "relation": "works_for"}]
    assert calls[0]["model"] == "m1"


@pytest.mark.asyncio
async def test_suggest_relations_from_text_constrains_names_via_schema_enum():
    calls = []
    orig_client = ai_module._client
    ai_module._client = lambda: _FakeChatClient(calls)
    try:
        await ai_module.suggest_relations_from_text("text", ["Alpha", "Beta"])
    finally:
        ai_module._client = orig_client
    schema = calls[0]["format"]
    item_props = schema["properties"]["relations"]["items"]["properties"]
    assert item_props["source"]["enum"] == ["Alpha", "Beta"]
    assert item_props["target"]["enum"] == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_suggest_relations_from_text_fewer_than_two_names_skips_ollama_call():
    calls = []
    orig_client = ai_module._client
    ai_module._client = lambda: _FakeChatClient(calls)
    try:
        assert await ai_module.suggest_relations_from_text("text", ["OnlyOne"]) == []
        assert await ai_module.suggest_relations_from_text("text", []) == []
    finally:
        ai_module._client = orig_client
    assert calls == []


@pytest.mark.asyncio
async def test_suggest_relations_from_text_drops_self_relation():
    calls = []
    content = '{"relations": [{"source": "Bob", "target": "Bob", "relation": "self"}]}'
    orig_client = ai_module._client
    ai_module._client = lambda: _FakeChatClient(calls, content)
    try:
        out = await ai_module.suggest_relations_from_text("text", ["Bob", "Other"])
    finally:
        ai_module._client = orig_client
    assert out == []


@pytest.mark.asyncio
async def test_suggest_relations_from_text_defensively_drops_names_outside_known_set():
    """The schema enum should already make this impossible, but a model/
    runtime that doesn't enforce the grammar as strictly as promised must
    not smuggle an unknown entity name through."""
    calls = []
    content = '{"relations": [{"source": "Bob", "target": "Nobody Known", "relation": "x"}]}'
    orig_client = ai_module._client
    ai_module._client = lambda: _FakeChatClient(calls, content)
    try:
        out = await ai_module.suggest_relations_from_text("text", ["Bob", "Other"])
    finally:
        ai_module._client = orig_client
    assert out == []


@pytest.mark.asyncio
async def test_suggest_relations_from_text_malformed_json_raises_value_error():
    orig_client = ai_module._client
    ai_module._client = lambda: _FakeChatClient([], content="not json")
    try:
        with pytest.raises(ValueError):
            await ai_module.suggest_relations_from_text("text", ["Bob", "Other"])
    finally:
        ai_module._client = orig_client


@pytest.mark.asyncio
async def test_suggest_relations_from_text_ollama_error_raises_value_error():
    import ollama as _ollama

    class _RaisingClient:
        async def chat(self, **kwargs):
            raise _ollama.ResponseError("boom", 500)

    orig_client = ai_module._client
    ai_module._client = lambda: _RaisingClient()
    try:
        with pytest.raises(ValueError):
            await ai_module.suggest_relations_from_text("text", ["Bob", "Other"])
    finally:
        ai_module._client = orig_client


# ── app.vault_sync.suggest_relations_for_vault ───────────────────────────────

def _write_prose_vault(tmp_path):
    # Deliberately no [[wikilinks]] — sync_vault's own pass would capture
    # nothing from this note, so any edge suggest_relations_for_vault
    # surfaces here can only have come from the LLM-prose pass.
    (tmp_path / "bob.md").write_text(
        "# Bob the Fence\n\n"
        "Bob secretly works for the Thieves Guild, though he keeps a shop in Dockside.\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.md").write_text(
        "# Random Musings\n\nThis note doesn't match any existing entity.\n", encoding="utf-8",
    )


def _make_world_with_entities(vault_dir=None):
    db = SessionLocal()
    try:
        w = World(name="Suggest World", slug=f"suggest-world-{uuid.uuid4().hex[:12]}",
                  obsidian_vault_path=str(vault_dir) if vault_dir else None)
        db.add(w)
        db.commit()
        db.refresh(w)
        bob = Entity(world_id=w.id, kind="character", name="Bob the Fence")
        dockside = Entity(world_id=w.id, kind="location", name="Dockside")
        guild = Entity(world_id=w.id, kind="organization", name="Thieves Guild")
        db.add_all([bob, dockside, guild])
        db.commit()
        db.refresh(bob)
        db.refresh(dockside)
        db.refresh(guild)
        return w.id, bob.id, dockside.id, guild.id
    finally:
        db.close()


async def _fake_suggest_bob_relations(text, known_names, model=""):
    if "secretly works" not in text:
        return []
    return [
        {"source": "Bob the Fence", "target": "Thieves Guild", "relation": "works_for"},
        {"source": "Bob the Fence", "target": "Dockside", "relation": "located_in"},
    ]


def test_suggest_relations_for_vault_anchors_to_notes_own_entity(tmp_path, monkeypatch):
    _write_prose_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_entities(tmp_path)
    monkeypatch.setattr(vs._ai, "suggest_relations_from_text", _fake_suggest_bob_relations)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        suggestions = asyncio.run(vs.suggest_relations_for_vault(db, world))
        by_key = {(s["source_id"], s["target_id"], s["relation"]) for s in suggestions}
        assert (bob_id, guild_id, "works_for") in by_key
        assert (bob_id, dockside_id, "located_in") in by_key
        assert len(suggestions) == 2
        assert all(s["source_path"] == "bob.md" for s in suggestions)
    finally:
        db.close()


def test_suggest_relations_for_vault_drops_edge_not_involving_notes_own_entity(tmp_path, monkeypatch):
    _write_prose_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_entities(tmp_path)

    async def _fake_unanchored(text, known_names, model=""):
        # Neither side is Bob (the note's own matched entity) — must be
        # dropped rather than trusted, since nothing anchors it to this
        # note being about that relationship.
        return [{"source": "Dockside", "target": "Thieves Guild", "relation": "rivals"}]

    monkeypatch.setattr(vs._ai, "suggest_relations_from_text", _fake_unanchored)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        assert asyncio.run(vs.suggest_relations_for_vault(db, world)) == []
    finally:
        db.close()


def test_suggest_relations_for_vault_dedupes_against_confirmed_edge(tmp_path, monkeypatch):
    _write_prose_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_entities(tmp_path)
    monkeypatch.setattr(vs._ai, "suggest_relations_from_text", _fake_suggest_bob_relations)

    db = SessionLocal()
    try:
        db.add(EntityRelation(world_id=world_id, source_id=bob_id, target_id=guild_id, relation="works_for"))
        db.commit()
        world = db.get(World, world_id)
        suggestions = asyncio.run(vs.suggest_relations_for_vault(db, world))
        by_key = {(s["source_id"], s["target_id"], s["relation"]) for s in suggestions}
        assert (bob_id, guild_id, "works_for") not in by_key  # already confirmed
        assert (bob_id, dockside_id, "located_in") in by_key  # still suggested
    finally:
        db.close()


def test_suggest_relations_for_vault_unmatched_note_contributes_nothing(tmp_path, monkeypatch):
    _write_prose_vault(tmp_path)
    world_id, *_ = _make_world_with_entities(tmp_path)
    calls = []

    async def _tracking_fake(text, known_names, model=""):
        calls.append(text)
        return []

    monkeypatch.setattr(vs._ai, "suggest_relations_from_text", _tracking_fake)
    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.suggest_relations_for_vault(db, world))
    finally:
        db.close()
    # unrelated.md never matches an entity by title, so it's never sent
    # for extraction at all — only bob.md is.
    assert len(calls) == 1
    assert "secretly works" in calls[0]


def test_suggest_relations_for_vault_one_note_failure_does_not_abort_others(tmp_path, monkeypatch):
    (tmp_path / "bob.md").write_text("# Bob the Fence\n\nBob secretly works for the Thieves Guild.\n", encoding="utf-8")
    (tmp_path / "dockside.md").write_text("# Dockside\n\nA grimy waterfront district.\n", encoding="utf-8")
    world_id, bob_id, dockside_id, guild_id = _make_world_with_entities(tmp_path)

    async def _flaky(text, known_names, model=""):
        if "Bob" in text:
            raise ValueError("model unavailable")
        return [{"source": "Dockside", "target": "Bob the Fence", "relation": "home_of"}]

    monkeypatch.setattr(vs._ai, "suggest_relations_from_text", _flaky)
    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        suggestions = asyncio.run(vs.suggest_relations_for_vault(db, world))
    finally:
        db.close()
    assert len(suggestions) == 1
    assert suggestions[0]["source_path"] == "dockside.md"


def test_suggest_relations_for_vault_no_vault_path_raises(client, seed):
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        with pytest.raises(ValueError):
            asyncio.run(vs.suggest_relations_for_vault(db, world))
    finally:
        db.close()


def test_suggest_relations_for_vault_fewer_than_two_entities_returns_empty_without_scanning(tmp_path):
    _write_prose_vault(tmp_path)
    db = SessionLocal()
    try:
        w = World(name="Lonely World", slug=f"lonely-world-{uuid.uuid4().hex[:12]}", obsidian_vault_path=str(tmp_path))
        db.add(w)
        db.commit()
        db.refresh(w)
        db.add(Entity(world_id=w.id, kind="character", name="Bob the Fence"))
        db.commit()
        assert asyncio.run(vs.suggest_relations_for_vault(db, w)) == []
    finally:
        db.close()


# ── POST /api/knowledge/suggest-relations ────────────────────────────────────

def test_suggest_relations_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/suggest-relations")
    assert r.status_code in (403, 404)


def test_suggest_relations_route_returns_draft_without_writing(client, seed, monkeypatch):
    async def fake_suggest(db, world, model=""):
        return [{
            "source_id": 1, "source_name": "Bob the Fence", "target_id": 2,
            "target_name": "Thieves Guild", "relation": "works_for", "source_path": "bob.md",
        }]
    monkeypatch.setattr(knowledge_router._vault_sync, "suggest_relations_for_vault", fake_suggest)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/suggest-relations")
    assert r.status_code == 200
    data = r.json()
    assert len(data["suggestions"]) == 1
    assert data["suggestions"][0]["relation"] == "works_for"

    assert db_count_relations(seed.world_a.id) == 0


def db_count_relations(world_id):
    db = SessionLocal()
    try:
        return db.query(EntityRelation).filter(EntityRelation.world_id == world_id).count()
    finally:
        db.close()


def test_suggest_relations_route_surfaces_value_error_as_400(client, seed, monkeypatch):
    async def fake_suggest(db, world, model=""):
        raise ValueError("This world has no Obsidian vault path configured.")
    monkeypatch.setattr(knowledge_router._vault_sync, "suggest_relations_for_vault", fake_suggest)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/suggest-relations")
    assert r.status_code == 400


# ── POST /api/knowledge/relations/bulk ───────────────────────────────────────

def test_relations_bulk_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/relations/bulk", json={"relations": []})
    assert r.status_code == 403


def test_relations_bulk_rejects_empty_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/relations/bulk", json={"relations": []})
    assert r.status_code == 400


def test_relations_bulk_creates_and_skips_duplicates(client, seed):
    db = SessionLocal()
    try:
        bob = Entity(world_id=seed.world_a.id, kind="character", name="Bob")
        guild = Entity(world_id=seed.world_a.id, kind="organization", name="Guild")
        dockside = Entity(world_id=seed.world_a.id, kind="location", name="Dockside")
        db.add_all([bob, guild, dockside])
        db.commit()
        db.refresh(bob)
        db.refresh(guild)
        db.refresh(dockside)
        db.add(EntityRelation(world_id=seed.world_a.id, source_id=bob.id, target_id=guild.id, relation="works_for"))
        db.commit()
        bob_id, guild_id, dockside_id = bob.id, guild.id, dockside.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/relations/bulk", json={"relations": [
        {"source_id": bob_id, "target_id": guild_id, "relation": "Works_For"},  # dup, case-insensitive
        {"source_id": bob_id, "target_id": dockside_id, "relation": "located_in"},  # new
        {"source_id": bob_id, "target_id": dockside_id, "relation": "located_in"},  # in-payload dup
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["created"] == 1
    assert data["skipped_duplicates"] == 2

    db = SessionLocal()
    try:
        rows = db.query(EntityRelation).filter(EntityRelation.world_id == seed.world_a.id).all()
        assert len(rows) == 2
        new_row = next(r for r in rows if r.relation == "located_in")
        assert new_row.source_path is None
    finally:
        db.close()


def test_relations_bulk_rejects_entity_from_another_world(client, seed):
    db = SessionLocal()
    try:
        own = Entity(world_id=seed.world_a.id, kind="character", name="Own Entity")
        other = Entity(world_id=seed.world_b.id, kind="character", name="Other World Entity")
        db.add_all([own, other])
        db.commit()
        db.refresh(own)
        db.refresh(other)
        own_id, other_id = own.id, other.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/relations/bulk", json={"relations": [
        {"source_id": own_id, "target_id": other_id, "relation": "steals_from"},
    ]})
    assert r.status_code == 200
    assert r.json() == {"created": 0, "skipped_duplicates": 0}
    assert db_count_relations(seed.world_a.id) == 0


# ── Regression: a confirmed AI suggestion must survive the next resync ──────

async def _fake_embed(text, model=""):
    return [1.0, 0.0]


def test_confirmed_ai_relation_survives_vault_resync(tmp_path, monkeypatch):
    _write_prose_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_entities(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.sync_vault(db, world))  # no wikilinks in this vault -> 0 wikilink-derived edges
        assert db.query(EntityRelation).filter(EntityRelation.world_id == world_id).count() == 0

        # A GM confirms an AI-suggested edge, exactly like the route does.
        db.add(EntityRelation(world_id=world_id, source_id=bob_id, target_id=guild_id,
                               relation="works_for", source_path=None))
        db.commit()
        assert db.query(EntityRelation).filter(EntityRelation.world_id == world_id).count() == 1

        # Rebuilding the vault-derived indexes again must not touch it —
        # only source_path IS NOT NULL rows are ever wiped by sync_vault.
        asyncio.run(vs.sync_vault(db, world))
        remaining = db.query(EntityRelation).filter(EntityRelation.world_id == world_id).all()
        assert len(remaining) == 1
        assert remaining[0].source_id == bob_id
        assert remaining[0].target_id == guild_id
        assert remaining[0].relation == "works_for"
    finally:
        db.close()
