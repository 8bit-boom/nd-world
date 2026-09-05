"""Tests for the folk-tale/song feature: app.ai_assist's folk_tale op,
POST /api/facts/folk-tale (app/routers/facts.py), and the subtype passthrough
on POST /api/ai/save-note (app/main.py) that /entity/{id} then holds a saved
tale under. The Sessions page's own 🎵 button (sessions/detail.html) just
calls the existing POST /api/ai/assist route with op=folk_tale — covered
here at the engine level (run_assist) and via the assist route's own
existing generic tests (tests/test_ai_assist.py already exercises "some
free-text op" end to end); this file focuses on what's new: the op itself,
and the Facts-specific route.
"""
import pytest

from app import ai as ai_module
from app import ai_assist as assist_module
from app import audio_jobs as audio_jobs_module
from app.database import SessionLocal
from app.models import Entity, Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


# ── Engine: the folk_tale op ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_folk_tale_requires_content():
    with pytest.raises(ValueError, match="Nothing to work on"):
        await assist_module.run_assist("folk_tale", content="   ")


@pytest.mark.asyncio
async def test_folk_tale_returns_generated_text(monkeypatch):
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        captured["system"] = system
        captured["user"] = messages[0]["content"]
        return "Hear now the ballad of the party who felled the corp-lord."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await assist_module.run_assist("folk_tale", content="The party defeated the corp-lord.")
    assert result["mode"] == "text"
    assert "ballad" in result["text"]
    assert "folk tale" in captured["system"].lower() or "song" in captured["system"].lower()
    assert "The party defeated the corp-lord." in captured["user"]


@pytest.mark.asyncio
async def test_folk_tale_failure_sentinel_passes_through(monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        return "[AI unavailable: ConnectionError: boom]"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await assist_module.run_assist("folk_tale", content="text")
    assert ai_module.is_failure_sentinel(result["text"])


# ── POST /api/facts/folk-tale ─────────────────────────────────────────────────

def test_facts_folk_tale_uses_all_facts_by_default(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_a.id, content="The party met Elyra.", visible_to_players=True))
        db.add(Fact(world_id=seed.world_a.id, content="Elyra is a cult agent.", visible_to_players=False))
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["op"] = op
        captured["content"] = kwargs.get("content")
        return {"op": op, "mode": "text", "text": "A tale of Elyra.", "model": "m"}

    import app.routers.facts as facts_module
    monkeypatch.setattr(facts_module._ai_assist, "run_assist", fake_run_assist)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/folk-tale", json={})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "A tale of Elyra."
    assert captured["op"] == "folk_tale"
    assert "The party met Elyra." in captured["content"]
    assert "Elyra is a cult agent." in captured["content"]


def test_facts_folk_tale_scopes_to_one_session(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        gs1 = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        gs2 = GameSession(world_id=seed.world_a.id, title="Session 2", session_num=2)
        db.add(gs1)
        db.add(gs2)
        db.commit()
        db.refresh(gs1)
        db.refresh(gs2)
        db.add(Fact(world_id=seed.world_a.id, game_session_id=gs1.id, content="Session one fact.", visible_to_players=True))
        db.add(Fact(world_id=seed.world_a.id, game_session_id=gs2.id, content="Session two fact.", visible_to_players=True))
        db.commit()
        session1_id = gs1.id
    finally:
        db.close()

    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["content"] = kwargs.get("content")
        return {"op": op, "mode": "text", "text": "tale", "model": "m"}

    import app.routers.facts as facts_module
    monkeypatch.setattr(facts_module._ai_assist, "run_assist", fake_run_assist)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/folk-tale", json={"game_session_id": session1_id})
    assert r.status_code == 200
    assert "Session one fact." in captured["content"]
    assert "Session two fact." not in captured["content"]


def test_facts_folk_tale_no_facts_400(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/folk-tale", json={})
    assert r.status_code == 400


def test_facts_folk_tale_surfaces_model_failure(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_a.id, content="Something happened.", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    import app.routers.facts as facts_module

    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "[AI unavailable: boom]", "model": "m"}
    monkeypatch.setattr(facts_module._ai_assist, "run_assist", fake_run_assist)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/folk-tale", json={})
    assert r.status_code == 502


def test_facts_folk_tale_rag_threading(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_a.id, content="Something happened.", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    captured = {}
    import app.routers.facts as facts_module

    async def fake_run_assist(op, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return {"op": op, "mode": "text", "text": "tale", "model": "m"}

    def fake_rag(world_id, query, entity_limit, notes_limit, **kwargs):
        captured["rag"] = (world_id, entity_limit, notes_limit)
        return "- [npc] Elyra: an enchanter"

    monkeypatch.setattr(facts_module._ai_assist, "run_assist", fake_run_assist)
    monkeypatch.setattr(audio_jobs_module, "_build_rag_context", fake_rag)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/folk-tale", json={"use_rag": True, "rag_entity_limit": 7, "rag_notes_limit": 2})
    assert r.status_code == 200
    assert captured["rag"] == (seed.world_a.id, 7, 2)
    assert captured["world_context"] == "- [npc] Elyra: an enchanter"

    captured.clear()
    r2 = client.post("/api/facts/folk-tale", json={"use_rag": False})
    assert r2.status_code == 200
    assert captured["world_context"] == ""
    assert "rag" not in captured


def test_player_cannot_call_facts_folk_tale(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/facts/folk-tale", json={})
    assert r.status_code == 403


# ── POST /api/ai/save-note subtype passthrough ───────────────────────────────

def test_save_note_accepts_optional_subtype(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/save-note", json={"title": "The Ballad of Elyra", "content": "Hear now...", "subtype": "tale"})
    assert r.status_code == 200
    data = r.json()
    db = SessionLocal()
    try:
        note = db.get(Entity, data["id"])
        assert note.kind == "note"
        assert note.subtype == "tale"
        assert note.name == "The Ballad of Elyra"
        assert note.body == "Hear now..."
        assert note.visible_to_players is True  # default, unchanged for this new field
    finally:
        db.close()


def test_save_note_without_subtype_still_works(client, seed):
    """Pre-existing caller (the King in Yellow easter egg) never sends
    subtype — must keep creating a NULL-subtype note exactly as before."""
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/save-note", json={"title": "A Note", "content": "Some content"})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        note = db.get(Entity, r.json()["id"])
        assert note.subtype is None
    finally:
        db.close()
