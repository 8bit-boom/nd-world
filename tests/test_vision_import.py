"""Tests for the vision-based "photo -> draft character/entity" import path:
app.ai.parse_character_from_images / parse_entity_from_images (mocked here —
no real Ollama/vision model needed) and the two routes that front them,
POST /api/ai/character-from-images and /api/ai/entity-from-images. Same
draft-then-review-then-write shape as /api/ai/entity-from-text (see
tests/test_entity_from_text.py) — the actual write reuses the existing
POST /api/import/execute route, not a new write path.
"""
import io
import json
import types

import pytest

from app import ai as ai_module
from app.constants import KINDS

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _png(name="sheet.png"):
    return (name, io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100), "image/png")


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


# ── app.ai.parse_character_from_images ────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_character_from_images_success(monkeypatch):
    fake = _FakeClient(json.dumps({
        "name": "Anders de Valliere", "player_name": "", "race": "", "char_class": "Widow-Bound Hunter",
        "level": 1, "xp": 0, "backstory": "I use a big-game hunter's tracking and killing craft.",
        "notes": "## Abilities\nBeastfall Stride (1 Stam)...",
    }))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    draft = await ai_module.parse_character_from_images([b"fake-image-bytes"], hint="Hunt in the Moonlight sheet")
    assert draft["name"] == "Anders de Valliere"
    assert draft["char_class"] == "Widow-Bound Hunter"
    assert draft["level"] == 1
    assert "Beastfall Stride" in draft["notes"]
    # images travel on the user message, not the system message
    user_msg = fake.calls[0]["messages"][1]
    assert user_msg["images"] == [__import__("base64").b64encode(b"fake-image-bytes").decode()]


@pytest.mark.asyncio
async def test_parse_character_from_images_clamps_level_and_xp(monkeypatch):
    fake = _FakeClient(json.dumps({"name": "Bort", "level": 99, "xp": -5}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    draft = await ai_module.parse_character_from_images([b"x"])
    assert draft["level"] == 20
    assert draft["xp"] == 0


@pytest.mark.asyncio
async def test_parse_character_from_images_caps_image_count(monkeypatch):
    fake = _FakeClient(json.dumps({"name": "Bort"}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    images = [f"img{i}".encode() for i in range(ai_module.MAX_VISION_IMPORT_IMAGES + 5)]
    await ai_module.parse_character_from_images(images)
    sent = fake.calls[0]["messages"][1]["images"]
    assert len(sent) == ai_module.MAX_VISION_IMPORT_IMAGES


@pytest.mark.asyncio
async def test_parse_character_from_images_no_images_raises():
    with pytest.raises(ValueError):
        await ai_module.parse_character_from_images([])


@pytest.mark.asyncio
async def test_parse_character_from_images_missing_name_raises(monkeypatch):
    fake = _FakeClient(json.dumps({"race": "Human"}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError):
        await ai_module.parse_character_from_images([b"x"])


@pytest.mark.asyncio
async def test_parse_character_from_images_malformed_json_raises(monkeypatch):
    fake = _FakeClient("not json")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError):
        await ai_module.parse_character_from_images([b"x"])


# ── app.ai.parse_entity_from_images ────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_entity_from_images_success(monkeypatch):
    fake = _FakeClient(json.dumps({
        "kind": "location", "subtype": "", "name": "The Neon Bazaar",
        "summary": "A sprawling night market.", "body": "Stalls lit by neon signs.",
        "tags": "market", "folder": "", "visible_to_players": True,
    }))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    draft = await ai_module.parse_entity_from_images([b"photo"], KINDS)
    assert draft["kind"] == "location"
    assert draft["name"] == "The Neon Bazaar"
    assert draft["visible_to_players"] is True


@pytest.mark.asyncio
async def test_parse_entity_from_images_bad_kind_raises(monkeypatch):
    fake = _FakeClient(json.dumps({"kind": "not-a-real-kind", "name": "Thing"}))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError):
        await ai_module.parse_entity_from_images([b"x"], KINDS)


@pytest.mark.asyncio
async def test_parse_entity_from_images_no_images_raises():
    with pytest.raises(ValueError):
        await ai_module.parse_entity_from_images([], KINDS)


@pytest.mark.asyncio
async def test_parse_entity_from_images_ollama_error_raises(monkeypatch):
    import ollama as _ollama
    fake = _FakeClient(_ollama.ResponseError("boom", 500))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError):
        await ai_module.parse_entity_from_images([b"x"], KINDS)


# ── POST /api/ai/character-from-images ────────────────────────────────────

def test_character_from_images_returns_draft_without_saving(client, seed, monkeypatch):
    async def fake_parse(images, hint=""):
        assert images == [b"\x89PNG\r\n\x1a\n" + b"\x00" * 100]
        assert hint == "Hunt in the Moonlight sheet"
        return {
            "name": "Anders de Valliere", "player_name": "", "race": "", "char_class": "Hunter",
            "level": 1, "xp": 0, "backstory": "Flavor text.", "notes": "Full transcription.",
        }
    monkeypatch.setattr(ai_module, "parse_character_from_images", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(
        "/api/ai/character-from-images",
        files=[("files", _png())],
        data={"hint": "Hunt in the Moonlight sheet"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "Anders de Valliere"

    from app.database import SessionLocal
    from app.models import PlayerCharacter
    db = SessionLocal()
    try:
        assert db.query(PlayerCharacter).filter(PlayerCharacter.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_character_from_images_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(images, hint=""):
        raise ValueError("Could not read a character off that photo.")
    monkeypatch.setattr(ai_module, "parse_character_from_images", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/character-from-images", files=[("files", _png())])
    assert r.status_code == 502


def test_character_from_images_rejects_no_files(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/character-from-images", data={"hint": ""})
    assert r.status_code in (400, 422)


def test_character_from_images_rejects_too_many_files(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    files = [("files", _png(f"p{i}.png")) for i in range(ai_module.MAX_VISION_IMPORT_IMAGES + 1)]
    r = client.post("/api/ai/character-from-images", files=files)
    assert r.status_code == 400


def test_character_from_images_rejects_non_image_file(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(
        "/api/ai/character-from-images",
        files=[("files", ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream"))],
    )
    assert r.status_code == 400


def test_player_cannot_call_character_from_images(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/character-from-images", files=[("files", _png())])
    assert r.status_code == 403


def test_assistant_can_call_character_from_images(client, seed, monkeypatch):
    async def fake_parse(images, hint=""):
        return {"name": "Some PC"}
    monkeypatch.setattr(ai_module, "parse_character_from_images", fake_parse)

    from app.database import SessionLocal
    from app.models import WorldMembership
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id,
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/worlds/switch/{seed.world_a.slug}?next=/", follow_redirects=False).status_code == 303
    r = client.post("/api/ai/character-from-images", files=[("files", _png())])
    assert r.status_code == 200


def test_character_from_images_draft_round_trips_through_import_execute(client, seed, monkeypatch):
    async def fake_parse(images, hint=""):
        return {
            "name": "Anders de Valliere", "player_name": "", "race": "", "char_class": "Widow-Bound Hunter",
            "level": 1, "xp": 0, "backstory": "Flavor text.", "notes": "Full transcription of the sheet.",
        }
    monkeypatch.setattr(ai_module, "parse_character_from_images", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/character-from-images", files=[("files", _png())])
    draft = r.json()

    r2 = client.post("/api/import/execute", json={"json_text": json.dumps(draft), "kind": "player_character"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True

    from app.database import SessionLocal
    from app.models import PlayerCharacter
    db = SessionLocal()
    try:
        pc = db.query(PlayerCharacter).filter(
            PlayerCharacter.world_id == seed.world_a.id, PlayerCharacter.name == "Anders de Valliere",
        ).first()
        assert pc is not None
        assert pc.char_class == "Widow-Bound Hunter"
        assert "Full transcription" in pc.notes
    finally:
        db.close()


# ── POST /api/ai/entity-from-images ────────────────────────────────────────

def test_entity_from_images_returns_draft_without_saving(client, seed, monkeypatch):
    async def fake_parse(images, kinds, hint=""):
        assert kinds == KINDS
        return {
            "kind": "location", "subtype": "", "name": "The Neon Bazaar",
            "summary": "A sprawling night market.", "body": "Stalls lit by neon signs.",
            "tags": "market", "folder": "", "visible_to_players": True,
        }
    monkeypatch.setattr(ai_module, "parse_entity_from_images", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-images", files=[("files", _png())])
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "The Neon Bazaar"

    from app.database import SessionLocal
    from app.models import Entity
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_entity_from_images_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(images, kinds, hint=""):
        raise ValueError("Could not turn that photo into an entity.")
    monkeypatch.setattr(ai_module, "parse_entity_from_images", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-images", files=[("files", _png())])
    assert r.status_code == 502


def test_player_cannot_call_entity_from_images(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-from-images", files=[("files", _png())])
    assert r.status_code == 403


def test_entity_from_images_draft_round_trips_through_import_execute(client, seed, monkeypatch):
    async def fake_parse(images, kinds, hint=""):
        return {
            "kind": "location", "subtype": "", "name": "The Neon Bazaar",
            "summary": "A sprawling night market.", "body": "Stalls lit by neon signs.",
            "tags": "market", "folder": "", "visible_to_players": True,
        }
    monkeypatch.setattr(ai_module, "parse_entity_from_images", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-images", files=[("files", _png())])
    draft = r.json()

    r2 = client.post("/api/import/execute", json={"json_text": json.dumps(draft), "kind": "entity_single"})
    assert r2.status_code == 200, r2.text

    from app.database import SessionLocal
    from app.models import Entity
    db = SessionLocal()
    try:
        e = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.name == "The Neon Bazaar").first()
        assert e is not None
        assert e.kind == "location"
    finally:
        db.close()


# ── /import page renders the new section ───────────────────────────────────

def test_import_page_includes_photo_import_section(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/import")
    assert r.status_code == 200
    assert "Import from Photo" in r.text
    assert "/api/ai/character-from-images" in r.text
    assert "/api/ai/entity-from-images" in r.text
