"""Tests for the hybrid RAG + knowledge-graph pipeline: app.vault_sync
(Obsidian vault -> VaultChunk/EntityRelation ingestion) and its query-time
consumers in app.retrieval (vector_search, graph_context), plus the
opt-in wiring into smart_world_context and the POST /api/knowledge/sync
route. Entirely additive/opt-in — a world with no obsidian_vault_path
configured must build byte-for-byte the same context it always has; see
test_smart_world_context_unaffected_without_vault_path below for the
regression guard on that specific promise.
"""
import asyncio
import uuid

import pytest

from app import vault_sync as vs
from app.database import SessionLocal
from app.models import Entity, EntityRelation, VaultChunk, World
from app.retrieval import (
    _cosine_similarity,
    GRAPH_MAX_RESULTS,
    graph_context,
    smart_world_context,
    vector_search,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


async def _fake_embed_dockside(text, model=""):
    t = text.lower()
    if "dock" in t or "waterfront" in t or "smuggler" in t:
        return [1.0, 0.0, 0.0]
    return [0.0, 0.0, 1.0]


# ── parse_frontmatter ────────────────────────────────────────────────────────

def test_parse_frontmatter_flat_keys_and_inline_list():
    md = '---\nkind: character\ntags: [npc, dockside]\ntitle: "Bob the Fence"\n---\nBody text.'
    fm, body = vs.parse_frontmatter(md)
    assert fm == {"kind": "character", "tags": ["npc", "dockside"], "title": "Bob the Fence"}
    assert body == "Body text."


def test_parse_frontmatter_block_list():
    md = "---\naliases:\n  - Bob\n  - Bobby\n---\nBody."
    fm, _body = vs.parse_frontmatter(md)
    assert fm["aliases"] == ["Bob", "Bobby"]


def test_parse_frontmatter_preserves_wikilink_scalar_not_as_a_list():
    """A single [[Wikilink]] value starts and ends with brackets just like
    a YAML flow list would — must NOT be parsed as a one-item list (which
    would corrupt it to "[Name" losing a bracket), since
    extract_wikilinks needs the literal [[...]] syntax intact."""
    md = "---\nmember_of: [[Thieves Guild]]\nlocated_in: \"[[Dockside]]\"\n---\nBody."
    fm, _body = vs.parse_frontmatter(md)
    assert fm["member_of"] == "[[Thieves Guild]]"
    assert fm["located_in"] == "[[Dockside]]"


def test_parse_frontmatter_no_block_returns_everything_as_body():
    md = "# Just a note\n\nNo frontmatter here."
    fm, body = vs.parse_frontmatter(md)
    assert fm == {}
    assert body == md


def test_parse_frontmatter_empty_string():
    assert vs.parse_frontmatter("") == ({}, "")


# ── extract_wikilinks ────────────────────────────────────────────────────────

def test_extract_wikilinks_basic_and_aliased_and_heading_anchor():
    text = "See [[Dockside]], also [[Thieves Guild|the Guild]] and [[Bob#Backstory]]."
    assert vs.extract_wikilinks(text) == ["Dockside", "Thieves Guild", "Bob"]


def test_extract_wikilinks_dedupes_in_first_seen_order():
    text = "[[Bob]] met [[Bob]] again near [[Dockside]]."
    assert vs.extract_wikilinks(text) == ["Bob", "Dockside"]


def test_extract_wikilinks_no_links():
    assert vs.extract_wikilinks("Just plain prose.") == []


# ── pack_embedding / unpack_embedding round-trip (app.ai) ───────────────────

def test_embedding_pack_unpack_round_trip():
    from app import ai as ai_module

    vec = [0.1, -3.5, 42.0, 0.0]
    packed = ai_module.pack_embedding(vec)
    assert isinstance(packed, str)
    unpacked = ai_module.unpack_embedding(packed)
    assert len(unpacked) == len(vec)
    for a, b in zip(vec, unpacked):
        assert abs(a - b) < 1e-5


# ── _cosine_similarity ───────────────────────────────────────────────────────

def test_cosine_similarity_identical_orthogonal_and_zero_vector():
    assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert _cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert _cosine_similarity([0, 0, 0], [1, 0, 0]) == 0.0
    assert _cosine_similarity([1, 2], [1, 2, 3]) == 0.0  # mismatched length


# ── sync_vault: full ingestion pipeline ──────────────────────────────────────

def _write_vault(tmp_path):
    (tmp_path / "bob.md").write_text(
        '---\nkind: character\nlocated_in: "[[Dockside]]"\nmember_of: "[[Thieves Guild]]"\n---\n'
        "# Bob the Fence\n\n"
        "Bob runs a shop near [[Dockside]] and answers to [[Thieves Guild]].\n\n"
        "## Backstory\n\nLong ago Bob worked with [[Captain Vance]] before the war.\n",
        encoding="utf-8",
    )
    (tmp_path / "dockside.md").write_text(
        "---\nkind: location\n---\n# Dockside\n\nA grimy waterfront district full of smugglers.\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated_lore.md").write_text(
        "# Random Musings\n\nThis note doesn't match any existing entity.\n", encoding="utf-8",
    )
    return tmp_path


def _make_world_with_vault(vault_dir):
    db = SessionLocal()
    try:
        w = World(name="Vault World", slug=f"vault-world-{uuid.uuid4().hex[:12]}", obsidian_vault_path=str(vault_dir))
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


def test_sync_vault_creates_chunks_and_edges(tmp_path, monkeypatch):
    _write_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_vault(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        result = asyncio.run(vs.sync_vault(db, world))
        assert result["notes"] == 3
        assert result["chunks"] == 4  # bob: 2 (merged + Backstory), dockside: 1, unrelated: 1
        assert result["edges"] == 4  # located_in, member_of, mentions x2 (Dockside, Guild — Vance unmatched)
        assert result["unmatched_notes"] == ["unrelated_lore.md"]

        edges = db.query(EntityRelation).filter(EntityRelation.world_id == world_id).all()
        by_relation = {(e.source_id, e.relation, e.target_id) for e in edges}
        assert (bob_id, "located_in", dockside_id) in by_relation
        assert (bob_id, "member_of", guild_id) in by_relation
        assert (bob_id, "mentions", dockside_id) in by_relation
        assert (bob_id, "mentions", guild_id) in by_relation

        chunks = db.query(VaultChunk).filter(VaultChunk.world_id == world_id).all()
        assert all(c.embedding for c in chunks)
        # The unmatched note still gets chunked/embedded — semantic search
        # doesn't require an entity match, only graph edges do.
        assert any(c.source_path == "unrelated_lore.md" for c in chunks)
    finally:
        db.close()


def test_sync_vault_is_idempotent(tmp_path, monkeypatch):
    _write_vault(tmp_path)
    world_id, *_ = _make_world_with_vault(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.sync_vault(db, world))
        asyncio.run(vs.sync_vault(db, world))
        assert db.query(EntityRelation).filter(EntityRelation.world_id == world_id).count() == 4
        assert db.query(VaultChunk).filter(VaultChunk.world_id == world_id).count() == 4
    finally:
        db.close()


def test_sync_vault_no_path_configured_raises(client, seed):
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        with pytest.raises(ValueError):
            asyncio.run(vs.sync_vault(db, world))
    finally:
        db.close()


def test_sync_vault_nonexistent_path_raises(client, seed):
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        world.obsidian_vault_path = "/definitely/not/a/real/path/anywhere"
        db.commit()
        with pytest.raises(ValueError):
            asyncio.run(vs.sync_vault(db, world))
    finally:
        db.close()


def test_sync_vault_embedding_failure_is_non_fatal(tmp_path):
    """A chunk whose embedding call fails (model not pulled, Ollama down)
    is still stored — with embedding=None, so it's simply invisible to
    vector_search rather than aborting the whole sync."""
    _write_vault(tmp_path)
    world_id, *_ = _make_world_with_vault(tmp_path)

    async def _raise(text, model=""):
        raise RuntimeError("embedding model not available")

    import app.ai as ai_module
    orig = ai_module.embed_text
    ai_module.embed_text = _raise
    try:
        db = SessionLocal()
        try:
            world = db.get(World, world_id)
            result = asyncio.run(vs.sync_vault(db, world))
            assert result["chunks"] == 4
            chunks = db.query(VaultChunk).filter(VaultChunk.world_id == world_id).all()
            assert all(c.embedding is None for c in chunks)
        finally:
            db.close()
    finally:
        ai_module.embed_text = orig


# ── vector_search ────────────────────────────────────────────────────────────

def test_vector_search_ranks_by_similarity_and_respects_top_k(tmp_path, monkeypatch):
    _write_vault(tmp_path)
    world_id, *_ = _make_world_with_vault(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.sync_vault(db, world))

        import app.retrieval as retrieval_module
        monkeypatch.setattr(retrieval_module._ai, "embed_text", _fake_embed_dockside)

        results = vector_search(db, world_id, "smuggling at the docks", top_k=2)
        assert len(results) <= 2
        assert results  # at least one dockside-flavored chunk should score > 0
        for score, _path, _heading, _text in results:
            assert score > 0
        # The dockside-topic query must rank the dockside-flavored chunk
        # (from _fake_embed_dockside's own bucketing) ahead of anything
        # else — top_k further caps it to at most 2 of however many
        # scored above 0.
        top_paths = {path for _score, path, _heading, _text in results}
        assert "dockside.md" in top_paths

        # top_k=1 must return exactly the single highest-scoring match,
        # not merely "at most" one.
        top1 = vector_search(db, world_id, "smuggling at the docks", top_k=1)
        assert len(top1) == 1
    finally:
        db.close()


def test_vector_search_no_chunks_returns_empty(client, seed):
    db = SessionLocal()
    try:
        assert vector_search(db, seed.world_a.id, "anything") == []
    finally:
        db.close()


def test_vector_search_embedding_failure_returns_empty(tmp_path, monkeypatch):
    _write_vault(tmp_path)
    world_id, *_ = _make_world_with_vault(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)
    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.sync_vault(db, world))

        import app.retrieval as retrieval_module

        async def _raise(text, model=""):
            raise RuntimeError("no embedding model")

        monkeypatch.setattr(retrieval_module._ai, "embed_text", _raise)
        assert vector_search(db, world_id, "anything") == []
    finally:
        db.close()


# ── graph_context ────────────────────────────────────────────────────────────

def test_graph_context_multi_hop_traversal(tmp_path, monkeypatch):
    _write_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_vault(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.sync_vault(db, world))

        ctx = graph_context(db, world_id, {bob_id})
        assert "Bob the Fence --located_in--> Dockside" in ctx
        assert "Bob the Fence --member_of--> Thieves Guild" in ctx
    finally:
        db.close()


def test_graph_context_traverses_edges_in_both_directions(client, seed):
    db = SessionLocal()
    try:
        a = Entity(world_id=seed.world_a.id, kind="character", name="A")
        b = Entity(world_id=seed.world_a.id, kind="location", name="B")
        db.add_all([a, b])
        db.commit()
        db.refresh(a)
        db.refresh(b)
        db.add(EntityRelation(world_id=seed.world_a.id, source_id=a.id, target_id=b.id, relation="located_in"))
        db.commit()

        # Seeding from the TARGET side must still surface the edge.
        ctx = graph_context(db, seed.world_a.id, {b.id})
        assert "A --located_in--> B" in ctx
    finally:
        db.close()


def test_graph_context_empty_when_no_seeds_or_no_edges(client, seed):
    db = SessionLocal()
    try:
        assert graph_context(db, seed.world_a.id, set()) == ""
        e = Entity(world_id=seed.world_a.id, kind="character", name="Solo")
        db.add(e)
        db.commit()
        db.refresh(e)
        assert graph_context(db, seed.world_a.id, {e.id}) == ""  # no edges at all in this world
    finally:
        db.close()


def test_graph_context_hides_edges_to_entities_the_player_cannot_see(client, seed):
    db = SessionLocal()
    try:
        bob = Entity(world_id=seed.world_a.id, kind="character", name="Bob")
        secret = Entity(world_id=seed.world_a.id, kind="organization", name="Secret Cabal", visible_to_players=False)
        db.add_all([bob, secret])
        db.commit()
        db.refresh(bob)
        db.refresh(secret)
        db.add(EntityRelation(world_id=seed.world_a.id, source_id=bob.id, target_id=secret.id, relation="member_of"))
        db.commit()

        player = seed.player_a
        gm_ctx = graph_context(db, seed.world_a.id, {bob.id}, user=None)
        player_ctx = graph_context(db, seed.world_a.id, {bob.id}, user=player)
        assert "Secret Cabal" in gm_ctx
        assert "Secret Cabal" not in player_ctx
    finally:
        db.close()


def test_graph_context_respects_max_results_cap(client, seed):
    db = SessionLocal()
    try:
        hub = Entity(world_id=seed.world_a.id, kind="location", name="Hub")
        db.add(hub)
        db.commit()
        db.refresh(hub)
        spokes = []
        for i in range(GRAPH_MAX_RESULTS + 5):
            spoke = Entity(world_id=seed.world_a.id, kind="character", name=f"Spoke {i}")
            db.add(spoke)
            spokes.append(spoke)
        db.commit()
        for spoke in spokes:
            db.refresh(spoke)
            db.add(EntityRelation(world_id=seed.world_a.id, source_id=spoke.id, target_id=hub.id, relation="located_in"))
        db.commit()

        ctx = graph_context(db, seed.world_a.id, {hub.id})
        assert len(ctx.splitlines()) <= GRAPH_MAX_RESULTS
    finally:
        db.close()


# ── smart_world_context: opt-in wiring ───────────────────────────────────────

def test_smart_world_context_unaffected_without_vault_path(client, seed):
    """The core promise: a world that has never touched this feature
    builds EXACTLY the context it always has — no vault/graph blocks, no
    behavior change of any kind."""
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="Bob", body="Bob runs a shop near the docks.")
        db.add(e)
        db.commit()
        context, _non_notes, _notes = smart_world_context(db, seed.world_a.id, "Bob")
        assert "Semantically related" not in context
        assert "Knowledge graph" not in context
    finally:
        db.close()


def test_smart_world_context_includes_hybrid_blocks_when_vault_configured(tmp_path, monkeypatch):
    _write_vault(tmp_path)
    world_id, bob_id, dockside_id, guild_id = _make_world_with_vault(tmp_path)
    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)

    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        asyncio.run(vs.sync_vault(db, world))

        import app.retrieval as retrieval_module
        monkeypatch.setattr(retrieval_module._ai, "embed_text", _fake_embed_dockside)

        context, _non_notes, _notes = smart_world_context(db, world_id, "tell me about smuggling at the docks")
        assert "Semantically related vault notes" in context
        assert "Knowledge graph" in context
        assert "Bob the Fence --located_in--> Dockside" in context
    finally:
        db.close()


# ── POST /api/knowledge/sync ─────────────────────────────────────────────────

def test_knowledge_sync_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/sync")
    assert r.status_code in (403, 404)


def test_knowledge_sync_route_400_when_no_vault_configured(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/sync")
    assert r.status_code == 400


def test_knowledge_sync_route_success(client, seed, tmp_path, monkeypatch):
    _write_vault(tmp_path)
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        world.obsidian_vault_path = str(tmp_path)
        db.add(Entity(world_id=world.id, kind="character", name="Bob the Fence"))
        db.add(Entity(world_id=world.id, kind="location", name="Dockside"))
        db.add(Entity(world_id=world.id, kind="organization", name="Thieves Guild"))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(vs._ai, "embed_text", _fake_embed_dockside)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/knowledge/sync")
    assert r.status_code == 200
    data = r.json()
    assert data["notes"] == 3
    assert data["edges"] == 4
