"""Tests for POST /api/ai/entities-from-text-batch — the "📝 Generate
Entities from Text (AI)" section on /import: turns one passage of text
(session notes, a homebrew doc) into MULTIPLE classified drafts
({"entities": [...], "player_characters": [...]}) in a single AI call
(mocked here — no real Ollama needed). Same draft-then-review-then-write
shape as /api/ai/entity-from-text (see tests/test_entity_from_text.py):
the actual write reuses the existing POST /api/import/execute (kind=batch),
not a new write path — these tests only cover the new draft route itself
plus app.ai.parse_entities_batch_from_text's own parsing/validation.
"""
import json
import types

import pytest

from app import ai as ai_module
from app.constants import KINDS

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._content, Exception):
            raise self._content
        return types.SimpleNamespace(message=types.SimpleNamespace(content=self._content))

    async def show(self, model):
        return types.SimpleNamespace(capabilities=[])


# ── app.ai.parse_entities_batch_from_text ──────────────────────────────────

@pytest.mark.asyncio
async def test_parse_entities_batch_success(monkeypatch):
    fake = _FakeClient(json.dumps({
        "entities": [
            {"kind": "location", "name": "The Neon Bazaar", "summary": "A night market.", "body": "Stalls lit by neon."},
            {"kind": "creature", "name": "Rustfang", "summary": "A cyber-wolf.", "body": "Hunts in packs."},
        ],
        "player_characters": [
            {"name": "Anders", "race": "Human", "char_class": "Hunter", "level": 2, "backstory": "A hunter."},
        ],
    }))
    ai_module_client = fake
    monkeypatch.setattr(ai_module, "_client", lambda: ai_module_client)
    result = await ai_module.parse_entities_batch_from_text("some document text", KINDS)
    assert len(result["entities"]) == 2
    assert result["entities"][0]["name"] == "The Neon Bazaar"
    assert result["entities"][0]["kind"] == "location"
    assert len(result["player_characters"]) == 1
    assert result["player_characters"][0]["name"] == "Anders"
    assert result["player_characters"][0]["level"] == 2


@pytest.mark.asyncio
async def test_parse_entities_batch_skips_invalid_items(monkeypatch):
    fake = _FakeClient(json.dumps({
        "entities": [
            {"kind": "location", "name": "Valid Place"},
            {"kind": "not-a-real-kind", "name": "Bad Kind"},
            {"kind": "location", "name": ""},
        ],
        "player_characters": [
            {"name": "Valid PC"},
            {"name": ""},
        ],
    }))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await ai_module.parse_entities_batch_from_text("text", KINDS)
    assert len(result["entities"]) == 1
    assert result["entities"][0]["name"] == "Valid Place"
    assert len(result["player_characters"]) == 1
    assert result["player_characters"][0]["name"] == "Valid PC"


@pytest.mark.asyncio
async def test_parse_entities_batch_empty_arrays_ok(monkeypatch):
    fake = _FakeClient(json.dumps({"entities": [], "player_characters": []}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await ai_module.parse_entities_batch_from_text("nothing interesting here", KINDS)
    assert result == {"entities": [], "player_characters": []}


@pytest.mark.asyncio
async def test_parse_entities_batch_caps_item_count(monkeypatch):
    many_entities = [{"kind": "item", "name": f"Item {i}"} for i in range(ai_module.MAX_BATCH_ENTITIES + 10)]
    fake = _FakeClient(json.dumps({"entities": many_entities, "player_characters": []}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await ai_module.parse_entities_batch_from_text("text", KINDS)
    assert len(result["entities"]) == ai_module.MAX_BATCH_ENTITIES


@pytest.mark.asyncio
async def test_parse_entities_batch_truncates_long_text(monkeypatch):
    fake = _FakeClient(json.dumps({"entities": [], "player_characters": []}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    huge_text = "x" * (ai_module.MAX_BATCH_TEXT_CHARS + 5000)
    await ai_module.parse_entities_batch_from_text(huge_text, KINDS)
    sent_text = fake.calls[0]["messages"][1]["content"]
    assert len(sent_text) == ai_module.MAX_BATCH_TEXT_CHARS


@pytest.mark.asyncio
async def test_parse_entities_batch_malformed_json_raises(monkeypatch):
    fake = _FakeClient("not json")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError):
        await ai_module.parse_entities_batch_from_text("text", KINDS)


@pytest.mark.asyncio
async def test_parse_entities_batch_ollama_error_raises(monkeypatch):
    import ollama as _ollama
    fake = _FakeClient(_ollama.ResponseError("boom", 500))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError):
        await ai_module.parse_entities_batch_from_text("text", KINDS)


# ── POST /api/ai/entities-from-text-batch ──────────────────────────────────

def test_entities_batch_route_returns_draft_without_saving(client, seed, monkeypatch):
    async def fake_parse(raw_text, kinds, model=""):
        assert "session notes" in raw_text
        assert kinds == KINDS
        return {
            "entities": [{"kind": "location", "subtype": "", "name": "The Neon Bazaar",
                          "summary": "A night market.", "body": "Stalls lit by neon.",
                          "tags": "", "folder": "", "visible_to_players": True}],
            "player_characters": [{"name": "Anders", "player_name": "", "race": "Human", "char_class": "Hunter",
                                    "level": 2, "xp": 0, "backstory": "A hunter.", "notes": ""}],
        }
    monkeypatch.setattr(ai_module, "parse_entities_batch_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entities-from-text-batch", json={"text": "here are my session notes: ..."})
    assert r.status_code == 200
    data = r.json()
    assert data["entities"][0]["name"] == "The Neon Bazaar"
    assert data["player_characters"][0]["name"] == "Anders"

    from app.database import SessionLocal
    from app.models import Entity, PlayerCharacter
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.world_id == seed.world_a.id).count() == 0
        assert db.query(PlayerCharacter).filter(PlayerCharacter.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_entities_batch_route_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(raw_text, kinds, model=""):
        raise ValueError("Could not extract anything usable from that text.")
    monkeypatch.setattr(ai_module, "parse_entities_batch_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entities-from-text-batch", json={"text": "gibberish"})
    assert r.status_code == 502


def test_entities_batch_route_rejects_blank_text(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entities-from-text-batch", json={"text": "   "})
    assert r.status_code == 400


def test_player_cannot_call_entities_batch_route(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entities-from-text-batch", json={"text": "some notes"})
    assert r.status_code == 403


def test_entities_batch_draft_round_trips_through_import_execute_batch(client, seed, monkeypatch):
    """The full flow end to end: draft -> GM reviews (unchanged here) ->
    POST /api/import/execute (kind=batch) creates both the entity and the
    player character, exactly like the import.html "Create Selected" flow."""
    async def fake_parse(raw_text, kinds, model=""):
        return {
            "entities": [{"kind": "location", "subtype": "", "name": "The Neon Bazaar",
                          "summary": "A night market.", "body": "Stalls lit by neon.",
                          "tags": "", "folder": "", "visible_to_players": True}],
            "player_characters": [{"name": "Anders", "player_name": "", "race": "Human", "char_class": "Hunter",
                                    "level": 2, "xp": 0, "backstory": "A hunter.", "notes": ""}],
        }
    monkeypatch.setattr(ai_module, "parse_entities_batch_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entities-from-text-batch", json={"text": "session notes..."})
    draft = r.json()

    imports = (
        [{"kind": "entity_single", "data": d} for d in draft["entities"]]
        + [{"kind": "player_character", "data": d} for d in draft["player_characters"]]
    )
    r2 = client.post("/api/import/execute", json={
        "json_text": json.dumps({"imports": imports}), "kind": "batch",
    })
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True
    assert all(res["ok"] for res in body["results"])

    from app.database import SessionLocal
    from app.models import Entity, PlayerCharacter
    db = SessionLocal()
    try:
        e = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.name == "The Neon Bazaar").first()
        assert e is not None
        assert e.kind == "location"
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == seed.world_a.id, PlayerCharacter.name == "Anders").first()
        assert pc is not None
        assert pc.char_class == "Hunter"
    finally:
        db.close()
