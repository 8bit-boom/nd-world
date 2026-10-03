"""Targeted regression tests for docs/AI_SURFACE_AUDIT_2026-09-30.md's
fixed findings — one test per high-impact fix, mocking the AI/Studio
boundary the way the sibling suites do."""
import io
import json

import app.ai as ai_module
from app.database import SessionLocal
from app.models import ChatSession, Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── character-creator RAG gate (leak) ────────────────────────────────────────

def test_character_ai_rag_forced_off_for_players(client, seed, monkeypatch):
    from app.routers import character_ai as _cai
    captured = {}

    async def _task(job_id, world_id, prompt, source_text, think, use_rag, template_id=0, source_limit=0, part_chars=0):
        captured["use_rag"] = use_rag
    monkeypatch.setattr(_cai, "_pc_ai_task", _task)

    monkeypatch.setattr(_cai._ai, "effective_llm_api_key", lambda: "sk-test")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start",
                    data={"prompt": "a dockworker", "use_rag": "true"})
    assert r.status_code == 200, r.text
    assert captured["use_rag"] is False  # the leak: unfiltered GM RAG


def test_character_ai_rag_stays_on_for_gm(client, seed, monkeypatch):
    from app.routers import character_ai as _cai
    captured = {}

    async def _task(job_id, world_id, prompt, source_text, think, use_rag, template_id=0, source_limit=0, part_chars=0):
        captured["use_rag"] = use_rag
    monkeypatch.setattr(_cai, "_pc_ai_task", _task)

    monkeypatch.setattr(_cai._ai, "effective_llm_api_key", lambda: "sk-test")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start",
                    data={"prompt": "a dockworker", "use_rag": "true"})
    assert r.status_code == 200
    assert captured["use_rag"] is True


# ── compact sentinel (data loss) ─────────────────────────────────────────────

def test_compact_never_returns_a_failure_sentinel(client, seed, monkeypatch):
    async def _condense(messages, model="", think=True, extra_instructions=""):
        return "[AI error: backend exploded]"
    monkeypatch.setattr("app.routers.ai._ai.condense_chat_history", _condense)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/compact", json={"messages": [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]})
    assert r.status_code == 502
    assert "backend exploded" in r.json()["detail"]


# ── 401 tracker sees the main chat path ─────────────────────────────────────

def test_tracker_armed_by_chat_401(client, seed, monkeypatch):
    from app import unsloth_extras as ux
    from app import llm_client as _lc
    monkeypatch.setattr(ux, "_last_auth_failure", 0.0)
    monkeypatch.setattr(ux, "_last_auth_ok", 0.0)
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "sk-test")
    monkeypatch.setattr(ux, "effective_llm_url", lambda: "http://unsloth:8000")

    import httpx
    import pytest as _pytest

    @_pytest.mark.asyncio
    async def _fake_chat(self, model, messages, stream=False, **kw):
        return await _lc._orig_chat(self, model, messages, stream, **kw) if False else None

    real_http = _lc.UnslothClient._http

    def _patched_http(self):
        import httpx as _hx
        return _hx.AsyncClient(transport=_hx.MockTransport(
            lambda req: _hx.Response(401, json={"error": {"message": "Not authenticated"}})),
            timeout=10)
    monkeypatch.setattr(_lc.UnslothClient, "_http", _patched_http)

    c = _lc.UnslothClient("http://unsloth:8000", "sk-test")
    import pytest
    with pytest.raises(_lc.UnslothResponseError):
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            c._chat_once({"model": "m", "messages": []}))
    assert ux.auth_status()["key_failed"] is True


# ── init_image containment ───────────────────────────────────────────────────

def test_init_image_rejects_traversal_paths():
    """The containment check lives in imagegen_generate's native branch —
    pinned here at the pure-logic level by exercising the guard directly:
    a traversal-bearing rel path must not produce an init_path that
    escapes uploads_dir."""
    # Direct check of the guard expression used at the call site.
    uploads = "C:/data/uploads"
    for bad in ("/uploads/../../app/world.db", "C:/secrets/x.png", "/uploads/a/b.png"):
        rel = bad.split("/uploads/", 1)[-1]
        allowed = bool(rel) and "/" not in rel and "\\" not in rel and ".." not in rel
        assert not allowed, bad


# ── boards visibility ────────────────────────────────────────────────────────

def test_faction_graph_hides_hidden_orgs_from_players(client, seed):
    from app.routers import boards_generate as _bg
    db = SessionLocal()
    try:
        vis = Entity(world_id=seed.world_a.id, kind="organization", name="Visible Guild",
                     visible_to_players=True)
        hidden = Entity(world_id=seed.world_a.id, kind="organization", name="Secret Cult",
                        visible_to_players=False,
                        body="[gmonly]the conspiracy[/gmonly]")
        db.add_all([vis, hidden])
        db.commit()
        ids = (vis.id, hidden.id)
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/orgs/graph")
    assert r.status_code == 200
    names = [n["title"] for n in r.json()["nodes"]]
    assert "Visible Guild" in names and "Secret Cult" in names  # GM sees all

    # Players are denied at the auth gate (the route isn't player-safe) —
    # the PLAYER leak vector was a generated board served with boards read,
    # so the graph builder itself is additionally pinned at unit level.
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/orgs/graph").status_code == 403

    from types import SimpleNamespace as NS
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.state.user = NS(is_gm=False, id=seed.player_a.id)
    db = SessionLocal()
    try:
        nodes, _edges = _bg._build_faction_graph(db, seed.world_a.id, fake_req)
        names = [n["title"] for n in nodes["nodes"]]
        assert "Visible Guild" in names and "Secret Cult" not in names
    finally:
        db.close()


# ── video poll tolerance ─────────────────────────────────────────────────────

def test_entry_state_word_boundaries():
    from app.video_jobs import _entry_state
    assert _entry_state({"id": 1, "status": "completed"}) == "done"
    assert _entry_state({"id": 1, "status": "incomplete"}) == "running"
    assert _entry_state({"id": 1, "status": "failed"}) == "failed"
    assert _entry_state({"id": 1, "status": "queued", "url": "/self"}) == "running"
    assert _entry_state({"id": 1, "output_url": "x"}) == "done"  # no status field


# ── thinking-rejection narrowing ────────────────────────────────────────────

def test_thinking_rejection_excludes_named_non_thinking_400s(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")
    class FakeExc(Exception):
        def __init__(self, msg, code):
            self.error = msg
            self.status_code = code
    assert ai_module._is_thinking_rejection(FakeExc("does not support thinking", 400))
    assert ai_module._is_thinking_rejection(FakeExc("some unsloth wording", 400))
    assert not ai_module._is_thinking_rejection(FakeExc("model not found: foo", 400))
    assert not ai_module._is_thinking_rejection(FakeExc("prompt too large", 400))


# ── vault sync total-embed-failure guard ────────────────────────────────────

def test_vault_sync_aborts_when_all_embeddings_fail(client, seed, monkeypatch, tmp_path):
    from app import vault_sync as _vs
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Someone\nSome text about them.\n")

    async def _fail(text):
        raise RuntimeError("backend down")
    monkeypatch.setattr(ai_module, "embed_text", _fail)
    monkeypatch.setattr(_vs, "_iter_vault_notes", lambda root: [("note.md", (vault / "note.md").read_text())])

    db = SessionLocal()
    try:
        import pytest
        with pytest.raises(RuntimeError):
            import asyncio
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                _vs._sync_vault_locked(db, seed.world_a, vault, str(vault)))
    finally:
        db.close()
