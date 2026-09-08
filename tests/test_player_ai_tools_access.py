"""Tests for the two new player-facing AI toggles (World.players_can_use_ai_chat,
World.players_can_view_world_summary — app/models.py), both off by default:

- players_can_use_ai_chat widens POST /api/ai/stream's existing gate
  (_require_ask_ai_access in app/routers/ai.py) and unlocks the new
  standalone GET /ai-chat page (app/main.py).
- players_can_view_world_summary unlocks read-only GET /api/ai/world-summary
  for a plain player; generating/clearing (POST/DELETE) stay GM+Assistant
  only regardless of this toggle.

Also covers the generic per-world-flag nav condition mechanism
(app/nav_menus.py's _visible) the new /ai-chat nav entry relies on, and the
migration heal entries for the two new `worlds` columns.
"""
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import ai as ai_module
from app import database as database_module
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


# ── Migration heal ──────────────────────────────────────────────────────────

def test_migration_heal_adds_new_world_columns(tmp_path, monkeypatch):
    """End-to-end _migrate() run against a `worlds` table that predates
    both new columns — same style as test_heal_table.py's own hand-typed-
    ALTER-TABLE coverage (worlds isn't in _GENERICALLY_HEALED_TABLES, so
    every new column needs its own entry in _migrate()'s w_exists block)."""
    from app.models import Base

    db_path = tmp_path / "world_ai_toggle_heal.db"
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

    Base.metadata.create_all(bind=engine)
    database_module._migrate()

    raw = sqlite3.connect(str(db_path))
    cols = [r[1] for r in raw.execute("PRAGMA table_info(worlds)").fetchall()]
    row = raw.execute(
        "SELECT players_can_use_ai_chat, players_can_view_world_summary FROM worlds WHERE id=1"
    ).fetchone()
    raw.close()
    engine.dispose()

    assert "players_can_use_ai_chat" in cols
    assert "players_can_view_world_summary" in cols
    assert row == (0, 0)  # healed with the documented DEFAULT 0 — off for every existing world


# ── /api/ai/stream: players_can_use_ai_chat is a second way in ─────────────

@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


async def _fake_resolve_model(requested):
    return requested or "fake-model", None


async def _fake_stream_chat(messages, system="", model="", options=None, think=False):
    yield "Hello"


def _patch_ai(monkeypatch):
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)


def test_stream_player_allowed_via_ai_chat_toggle_alone(client, seed, monkeypatch):
    """players_can_ask_ai stays False — only players_can_use_ai_chat is on —
    and the shared /api/ai/stream endpoint still opens up, since the two
    toggles gate which UI a GM exposes, not two different access levels."""
    _patch_ai(monkeypatch)
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "Hello" in r.text


def test_stream_player_still_denied_with_both_toggles_off(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403


# ── GET /ai-chat page ────────────────────────────────────────────────────────

def test_ai_chat_page_gm_always_allowed(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 200
    assert "ai-messages" in r.text


def test_ai_chat_page_player_denied_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 403


def test_ai_chat_page_player_allowed_once_gm_enables_it(client, seed):
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 200


def test_ai_chat_page_toggle_is_per_world(client, seed):
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_b.email, PLAYER_PASSWORD)  # player_b is only a member of world_b
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 403


# ── GET /api/ai/world-summary: player read access ───────────────────────────

def test_world_summary_get_player_denied_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/world-summary")
    assert r.status_code == 403


def test_world_summary_get_player_allowed_once_gm_enables_it(client, seed):
    _set_world(seed.world_a.id, players_can_view_world_summary=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/world-summary")
    assert r.status_code == 200
    assert r.json() == {"recap": ""}  # no digest generated yet — still 200, not 403


def test_world_summary_get_toggle_is_per_world(client, seed):
    _set_world(seed.world_a.id, players_can_view_world_summary=True)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/world-summary")
    assert r.status_code == 403


def test_world_summary_post_and_delete_stay_gm_only_even_with_read_toggle_on(client, seed):
    """The read toggle is deliberately read-only — a player can never
    generate or clear the digest themselves, no matter what."""
    _set_world(seed.world_a.id, players_can_view_world_summary=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/ai/world-summary", json={}).status_code == 403
    assert client.delete("/api/ai/world-summary").status_code == 403


def test_world_summary_get_gm_unaffected_by_new_toggle(client, seed):
    """Sanity check: widening the GET gate for players didn't disturb the
    existing GM/Assistant path (_require_can_edit short-circuits first)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/world-summary")
    assert r.status_code == 200


# ── Generic per-world-flag nav condition (app/nav_menus.py) ─────────────────

def test_nav_ai_chat_item_hidden_when_toggle_off(seed):
    from app.nav_menus import resolve_nav_menus

    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        _, ungrouped = resolve_nav_menus(world, False, False, is_gm=False)
    finally:
        db.close()
    assert not any(item["id"] == "ai_chat_player" for item in ungrouped)


def test_nav_ai_chat_item_visible_when_toggle_on(seed):
    from app.nav_menus import resolve_nav_menus

    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        _, ungrouped = resolve_nav_menus(world, False, False, is_gm=False)
    finally:
        db.close()
    item = next((i for i in ungrouped if i["id"] == "ai_chat_player"), None)
    assert item is not None
    assert item["href"] == "/ai-chat"


# ── world_edit.html toggles ──────────────────────────────────────────────────

def test_world_edit_form_renders_new_checkboxes(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/edit")
    assert r.status_code == 200
    assert 'name="players_can_use_ai_chat"' in r.text
    assert 'name="players_can_view_world_summary"' in r.text


def test_world_edit_post_persists_new_toggles(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name,
        "players_can_use_ai_chat": "1",
        "players_can_view_world_summary": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.players_can_use_ai_chat is True
        assert w.players_can_view_world_summary is True
    finally:
        db.close()


def test_world_edit_post_unchecking_turns_toggles_off(client, seed):
    """An absent checkbox field means unchecked — the POST handler must
    turn a previously-True toggle back off, not just ever turn it on."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True, players_can_view_world_summary=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={"name": seed.world_a.name}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.players_can_use_ai_chat is False
        assert w.players_can_view_world_summary is False
    finally:
        db.close()
