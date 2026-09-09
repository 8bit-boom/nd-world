"""Tests for Facts tagging (app.models.Fact.tags, mirroring Entity.tags'
own comma-separated convention) — added on top of the session-grouping and
Sessions-integration work so a GM with 40-50 facts on one session can filter
by topic (an NPC name, "combat", "loot") instead of scrolling everything.
Covers: the AI recap parser suggesting tags per drafted fact, the plain
quick-add/edit routes accepting a tags field, the bulk-save routes storing
whichever shape (list or comma-string) a caller sends, and the tag-cloud/
tag-chip rendering on both /facts and the embedded per-session panel.
"""
import types

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import Fact, GameSession
from app.routers.facts import _normalize_tags_field

from .conftest import GM_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _make_session(world_id, session_num, title):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title=title, session_num=session_num)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _make_fact(world_id, content, tags=None, game_session_id=None):
    db = SessionLocal()
    try:
        f = Fact(world_id=world_id, content=content, tags=tags, game_session_id=game_session_id, visible_to_players=True)
        db.add(f)
        db.commit()
        db.refresh(f)
        return f.id
    finally:
        db.close()


# ── _normalize_tags_field ────────────────────────────────────────────────

def test_normalize_tags_field_accepts_list():
    assert _normalize_tags_field(["Elyra", " tavern ", "", "plot"]) == "Elyra, tavern, plot"


def test_normalize_tags_field_accepts_string():
    assert _normalize_tags_field("elyra, tavern") == "elyra, tavern"


def test_normalize_tags_field_empty_or_bad_input_is_none():
    assert _normalize_tags_field("") is None
    assert _normalize_tags_field("   ") is None
    assert _normalize_tags_field([]) is None
    assert _normalize_tags_field(None) is None
    assert _normalize_tags_field(42) is None


# ── AI recap parser suggests tags ────────────────────────────────────────

class _FakeResp:
    def __init__(self, content):
        self.message = types.SimpleNamespace(content=content)


class _FakeTaggedFactsClient:
    def __init__(self, content):
        self._content = content

    async def chat(self, **kwargs):
        return _FakeResp(self._content)

    async def show(self, model):
        return types.SimpleNamespace(capabilities=["thinking"])


@pytest.mark.asyncio
async def test_parse_facts_from_recap_joins_ai_suggested_tags(monkeypatch):
    content = (
        '{"facts": [{"content": "The party met Elyra.", "visible_to_players": true, '
        '"tags": ["Elyra", " tavern ", ""]}]}'
    )
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeTaggedFactsClient(content))
    facts = await ai_module.parse_facts_from_recap("met Elyra at the tavern")
    assert len(facts) == 1
    assert facts[0]["tags"] == "Elyra, tavern"


@pytest.mark.asyncio
async def test_parse_facts_from_recap_missing_tags_defaults_empty(monkeypatch):
    # An older/smaller model that ignores the tags instruction and omits the
    # key entirely must not crash the parse — empty tags, not an error.
    content = '{"facts": [{"content": "The party met Elyra.", "visible_to_players": true}]}'
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeTaggedFactsClient(content))
    facts = await ai_module.parse_facts_from_recap("met Elyra")
    assert facts[0]["tags"] == ""


# ── Plain quick-add / edit routes ────────────────────────────────────────

def test_quick_add_stores_tags(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/facts/new", data={"content": "Met Elyra", "tags": "Elyra, Tavern"})
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.tags == "Elyra, Tavern"
    finally:
        db.close()


def test_quick_add_without_tags_stores_none(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/facts/new", data={"content": "No tags here"})
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.tags is None
    finally:
        db.close()


def test_edit_updates_tags(client, seed):
    fact_id = _make_fact(seed.world_a.id, "Original", tags="old-tag")
    _login_gm_in(client, seed, seed.world_a)
    client.post(f"/facts/{fact_id}/edit", data={"content": "Original", "tags": "new-tag, other"})
    db = SessionLocal()
    try:
        assert db.get(Fact, fact_id).tags == "new-tag, other"
    finally:
        db.close()


def test_edit_clearing_tags_field_removes_them(client, seed):
    fact_id = _make_fact(seed.world_a.id, "Original", tags="old-tag")
    _login_gm_in(client, seed, seed.world_a)
    client.post(f"/facts/{fact_id}/edit", data={"content": "Original", "tags": ""})
    db = SessionLocal()
    try:
        assert db.get(Fact, fact_id).tags is None
    finally:
        db.close()


# ── Bulk-save routes (draft review / job confirm) ────────────────────────

def test_bulk_save_accepts_list_tags(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/bulk", json={
        "facts": [{"content": "Fact one", "visible_to_players": True, "tags": ["Elyra", "plot"]}],
    })
    assert r.status_code == 200
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.tags == "Elyra, plot"
    finally:
        db.close()


def test_bulk_save_accepts_string_tags(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/bulk", json={
        "facts": [{"content": "Fact two", "visible_to_players": True, "tags": "combat, loot"}],
    })
    assert r.status_code == 200
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.tags == "combat, loot"
    finally:
        db.close()


def test_bulk_save_without_tags_leaves_them_none(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/bulk", json={
        "facts": [{"content": "Fact three", "visible_to_players": True}],
    })
    assert r.status_code == 200
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.tags is None
    finally:
        db.close()


# ── Tag cloud + chip rendering ────────────────────────────────────────────

def test_facts_page_renders_tag_cloud_and_chips(client, seed):
    _make_fact(seed.world_a.id, "Met Elyra at the tavern", tags="elyra, tavern")
    _make_fact(seed.world_a.id, "Elyra revealed as a cult agent", tags="elyra, plot")
    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    # The tag cloud: "elyra" appears on both facts, so it's the top tag.
    assert 'factsFilterByTag("elyra")' in html
    assert 'factsFilterByTag("tavern")' in html
    # Per-card chips render the fact's own tags.
    assert '<span class="tag">elyra</span>' in html
    assert '<span class="tag">tavern</span>' in html


def test_facts_page_with_no_tags_has_no_tag_cloud(client, seed):
    _make_fact(seed.world_a.id, "Untagged fact")
    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert "data-facts-tag-chip" not in html


def test_session_panel_renders_tag_cloud_and_chips(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    _make_fact(seed.world_a.id, "Met Elyra at the tavern", tags="elyra, tavern", game_session_id=s1)
    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/sessions/{s1}").text
    assert 'sessionFactsFilterByTag("elyra")' in html
    assert '<span class="tag">elyra</span>' in html
    assert 'id="session-facts-search"' in html


def test_session_panel_tags_scoped_to_its_own_session(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    s2 = _make_session(seed.world_a.id, 2, "Second")
    _make_fact(seed.world_a.id, "s1 fact", tags="s1-only-tag", game_session_id=s1)
    _make_fact(seed.world_a.id, "s2 fact", tags="s2-only-tag", game_session_id=s2)
    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/sessions/{s1}").text
    assert "s1-only-tag" in html
    assert "s2-only-tag" not in html


def test_facts_from_another_world_dont_leak_into_tag_cloud(client, seed):
    _make_fact(seed.world_a.id, "World A fact", tags="a-only-tag")
    _make_fact(seed.world_b.id, "World B fact", tags="b-only-tag")
    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert "a-only-tag" in html
    assert "b-only-tag" not in html
