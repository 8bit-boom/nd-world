"""RAG (world lore retrieval) for the player-facing standalone AI Chat page
(/ai-chat, app/templates/ai_chat_player.html) — previously that page sent a
fixed generic system prompt with zero world-lore injection (see
test_player_ai_tools_access.py for the base players_can_use_ai_chat/
players_can_ask_ai access tests, which this file doesn't repeat).

New pieces under test:
- app.retrieval.smart_world_context's `user` parameter (threaded through
  the initial search AND the non-note/notes top-up queries, all of which
  previously ran unfiltered regardless of `user` — see that function's own
  docstring) — the fix that makes a filtered caller actually get a fully
  filtered result, not just a filtered first pass.
- POST /api/ai/world-context-player (app/routers/ai.py) — the player-safe
  counterpart to the GM/assistant-only, deliberately-unfiltered
  /api/ai/world-context-smart, gated by the same _require_ask_ai_access
  rule as /api/ai/stream itself.
"""
from app.database import SessionLocal
from app.models import Entity, User, entity_player_access
from app.retrieval import smart_world_context

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "item"), name=kwargs.pop("name", "Entity"), **kwargs)
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


def _set_world(world_id, **kw):
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


# ── smart_world_context(user=...) — top-up queries must respect it too ─────

def test_smart_world_context_unfiltered_by_default(client, seed):
    """user=None (the default) stays unfiltered — the existing GM/assistant
    posture of /api/ai/world-context-smart, unchanged by this feature."""
    hidden_id = _make_entity(seed.world_a.id, name="Sealed Armory", visible_to_players=False)
    db = SessionLocal()
    try:
        context, non_notes, notes = smart_world_context(db, seed.world_a.id, "Sealed Armory")
        assert hidden_id in {e.id for e in non_notes}
    finally:
        db.close()


def test_smart_world_context_filters_matched_entities_for_a_player(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Sealed Armory", visible_to_players=False)
    visible_id = _make_entity(seed.world_a.id, name="Open Armory", visible_to_players=True)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context, non_notes, notes = smart_world_context(db, seed.world_a.id, "Armory", user=player)
        ids = {e.id for e in non_notes}
        assert hidden_id not in ids
        assert visible_id in ids
        assert "Sealed Armory" not in context
        assert "Open Armory" in context
    finally:
        db.close()


def test_smart_world_context_topup_respects_visibility_for_a_player(client, seed):
    """The non-note top-up (backfilling entity_limit when the keyword
    search underfills it) previously ran with NO visibility filter at all
    — a query too generic to match anything would top up with literally
    every entity in the world, hidden ones included, regardless of `user`.
    A single hidden entity and a high limit forces the top-up path."""
    hidden_id = _make_entity(seed.world_a.id, name="Zzz Nomatch Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context, non_notes, notes = smart_world_context(
            db, seed.world_a.id, "completely unrelated query text", entity_limit=25, notes_limit=0, user=player,
        )
        assert hidden_id not in {e.id for e in non_notes}
    finally:
        db.close()


def test_smart_world_context_notes_topup_respects_visibility_for_a_player(client, seed):
    """Same gap as the entity top-up, but for the guaranteed-recent-notes
    block — a hidden note must never surface just because it's recent."""
    hidden_note_id = _make_entity(seed.world_a.id, kind="note", name="Secret Note", visible_to_players=False)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context, non_notes, notes = smart_world_context(
            db, seed.world_a.id, "nothing matches this", entity_limit=0, notes_limit=5, user=player,
        )
        assert hidden_note_id not in {e.id for e in notes}
    finally:
        db.close()


def test_smart_world_context_player_still_sees_specifically_shared_hidden_entity(client, seed):
    hidden_id = _make_entity(seed.world_a.id, name="Vault Key", visible_to_players=False)
    _share_with(hidden_id, seed.player_a.id)
    db = SessionLocal()
    try:
        player = db.get(User, seed.player_a.id)
        context, non_notes, notes = smart_world_context(db, seed.world_a.id, "Vault Key", user=player)
        assert hidden_id in {e.id for e in non_notes}
    finally:
        db.close()


# ── POST /api/ai/world-context-player ───────────────────────────────────────

def test_world_context_player_denied_with_both_toggles_off(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "armor"})
    assert r.status_code == 403


def test_world_context_player_allowed_via_ai_chat_toggle(client, seed):
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    _make_entity(seed.world_a.id, name="Chainmail Armor", summary="120 crowns", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "Chainmail Armor cost"})
    assert r.status_code == 200
    data = r.json()
    assert "Chainmail Armor" in data["context"]
    assert "120 crowns" in data["context"]


def test_world_context_player_allowed_via_ask_ai_toggle_alone(client, seed):
    """players_can_ask_ai (the entity-panel toggle) is the OTHER of the two
    ways in, same as /api/ai/stream itself — see _require_ask_ai_access."""
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "anything"})
    assert r.status_code == 200


def test_world_context_player_excludes_gm_only_lore(client, seed):
    """The whole point of this route over the unfiltered GM/assistant
    world-context-smart: a hidden entity must never leak into a player's
    answer just because its text happens to match their question."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    _make_entity(seed.world_a.id, name="Forbidden Armor", summary="GM secret stats", visible_to_players=False)
    _make_entity(seed.world_a.id, name="Leather Armor", summary="15 crowns", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "Armor"})
    assert r.status_code == 200
    context = r.json()["context"]
    assert "Forbidden Armor" not in context
    assert "GM secret stats" not in context
    assert "Leather Armor" in context


def test_world_context_player_gm_sees_everything(client, seed):
    """A GM calling their own player-safe route (e.g. previewing /ai-chat)
    gets the same unfiltered breadth as world-context-smart — no reason to
    hide anything from the GM through this endpoint either."""
    _make_entity(seed.world_a.id, name="Forbidden Armor", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "Forbidden Armor"})
    assert r.status_code == 200
    assert "Forbidden Armor" in r.json()["context"]


def test_world_context_player_empty_query_returns_empty_context(client, seed):
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "   "})
    assert r.status_code == 200
    assert r.json()["context"] == ""


def test_world_context_player_clamps_limits_server_side(client, seed):
    """Never trust the client for these — a huge requested limit is
    clamped, not passed straight through to the DB query."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "armor", "limit": 99999, "notes_limit": 99999})
    assert r.status_code == 200


# ── World-configurable RAG default (ai_chat_rag_entity_limit/notes_limit) ──

def test_world_context_player_uses_built_in_default_when_world_unset(client, seed):
    """World.ai_chat_rag_entity_limit/notes_limit are NULL by default — the
    route falls back to its own built-in default (15/3), not zero/None."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    for i in range(4):
        _make_entity(seed.world_a.id, name=f"Common Sword {i}", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "Common Sword"})
    assert r.status_code == 200
    assert r.json()["count"] == 4  # well under the default 15-entity ceiling


def test_world_context_player_respects_world_configured_entity_limit(client, seed):
    """A GM dialing the world default down to 1 actually narrows what a
    player's question retrieves — not just a cosmetic Settings field."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True, ai_chat_rag_entity_limit=1, ai_chat_rag_notes_limit=0)
    for i in range(4):
        _make_entity(seed.world_a.id, name=f"Common Sword {i}", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "Common Sword"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_world_context_player_world_configured_value_still_clamped(client, seed):
    """A world setting above the hard ceiling is clamped exactly like a
    client-supplied one would be — the ceiling isn't just a client-facing
    courtesy, it's enforced regardless of where the number came from."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True, ai_chat_rag_entity_limit=99999)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "anything"})
    assert r.status_code == 200


def test_migration_heal_adds_ai_chat_rag_limit_columns(tmp_path, monkeypatch):
    """End-to-end _migrate() run against a `worlds` table that predates
    both new columns, same style as test_player_ai_tools_access.py's own
    coverage for the sibling players_can_use_ai_chat column."""
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import database as database_module
    from app.models import Base, World

    db_path = tmp_path / "world_rag_limit_heal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE worlds (id INTEGER PRIMARY KEY, name TEXT, slug TEXT UNIQUE, description TEXT,
            accent TEXT, players_see_party BOOLEAN, players_can_ask_ai BOOLEAN, rules_md TEXT, created_at DATETIME)
    """)
    conn.execute("INSERT INTO worlds (id, name, slug) VALUES (1, 'Test World', 'test-world')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", Session)
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "worlds"])
    database_module._migrate()

    db = Session()
    try:
        w = db.get(World, 1)
        assert w.ai_chat_rag_entity_limit is None
        assert w.ai_chat_rag_notes_limit is None
    finally:
        db.close()


# ── world_edit_post: saving the two number fields ───────────────────────────

def test_world_edit_saves_rag_limits(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name,
        "ai_chat_rag_entity_limit": "8",
        "ai_chat_rag_notes_limit": "2",
    })
    assert r.status_code in (200, 303)
    db = SessionLocal()
    try:
        from app.models import World
        w = db.get(World, seed.world_a.id)
        assert w.ai_chat_rag_entity_limit == 8
        assert w.ai_chat_rag_notes_limit == 2
    finally:
        db.close()


def test_world_edit_blank_rag_limits_clears_to_none(client, seed):
    """Blanking the field out (the default state) restores the built-in
    fallback rather than saving 0 or erroring."""
    _set_world(seed.world_a.id, ai_chat_rag_entity_limit=8, ai_chat_rag_notes_limit=2)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name,
        "ai_chat_rag_entity_limit": "",
        "ai_chat_rag_notes_limit": "",
    })
    assert r.status_code in (200, 303)
    db = SessionLocal()
    try:
        from app.models import World
        w = db.get(World, seed.world_a.id)
        assert w.ai_chat_rag_entity_limit is None
        assert w.ai_chat_rag_notes_limit is None
    finally:
        db.close()


# ── /ai-chat page wiring ─────────────────────────────────────────────────────

def test_ai_chat_player_page_calls_the_context_endpoint(client, seed):
    """Confirms the template's JS actually wires to the new route — a
    silent rename/typo here would leave the page working (RAG failure is
    swallowed) but never actually grounding an answer."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 200
    assert "/api/ai/world-context-player" in r.text
