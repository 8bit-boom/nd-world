"""Tests for POST /api/ai/entity-from-text — AI Chat's "📥 Save as entity"
button (app/templates/ai_chat.html's draftEntityFromMessage/renderEntityDraft).
Drafts a structured entity from a passage of text via app.ai.parse_entity_from_text
(mocked here — no real Ollama needed), same draft-then-review-then-write shape
as the Facts page's recap parser, except the actual write reuses the existing
POST /api/import/execute route (already tested by tests/test_import.py) rather
than a new write path — these tests only cover the new draft route itself.
"""
from app import ai as ai_module
from app.constants import KINDS

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_entity_from_text_returns_draft_without_saving(client, seed, monkeypatch):
    async def fake_parse(raw_text, kinds, model="", kind_hint=""):
        assert "merchant" in raw_text
        assert kinds == KINDS
        return {
            "kind": "character", "subtype": "NPC", "name": "Elena the Merchant",
            "summary": "Runs a bazaar stall.", "body": "# Elena\nSells trinkets.",
            "tags": "bazaar, npc", "folder": "NPCs", "visible_to_players": True,
        }
    monkeypatch.setattr(ai_module, "parse_entity_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-text", json={"text": "There's a merchant named Elena at the bazaar."})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Elena the Merchant"
    assert data["kind"] == "character"
    assert data["visible_to_players"] is True

    from app.database import SessionLocal
    from app.models import Entity
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()


def test_entity_from_text_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(raw_text, kinds, model="", kind_hint=""):
        raise ValueError("Could not turn that reply into an entity — try rephrasing or picking a shorter passage.")
    monkeypatch.setattr(ai_module, "parse_entity_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-text", json={"text": "gibberish"})
    assert r.status_code == 502


def test_entity_from_text_rejects_blank_text(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-text", json={"text": "   "})
    assert r.status_code == 400


def test_player_cannot_call_entity_from_text(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/entity-from-text", json={"text": "a merchant"})
    assert r.status_code == 403


def test_entity_from_text_draft_round_trips_through_import_execute(client, seed, monkeypatch):
    """The full flow end to end: draft -> GM reviews (unchanged here) ->
    POST /api/import/execute creates the real entity, exactly like
    ai_chat.html's renderEntityDraft() Create button does."""
    async def fake_parse(raw_text, kinds, model="", kind_hint=""):
        return {
            "kind": "location", "subtype": "", "name": "The Neon Bazaar",
            "summary": "A sprawling night market.", "body": "Stalls lit by neon signs.",
            "tags": "market", "folder": "", "visible_to_players": True,
        }
    monkeypatch.setattr(ai_module, "parse_entity_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-text", json={"text": "Describe the bazaar."})
    draft = r.json()

    r2 = client.post("/api/import/execute", json={
        "json_text": __import__("json").dumps(draft), "kind": "entity_single",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True

    from app.database import SessionLocal
    from app.models import Entity
    db = SessionLocal()
    try:
        e = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.name == "The Neon Bazaar").first()
        assert e is not None
        assert e.kind == "location"
        assert e.summary == "A sprawling night market."
    finally:
        db.close()


# ── kind steering (the new-entity form's ✨ Draft button) ────────────────────

def test_kind_hint_is_passed_through_and_bad_kinds_dropped(client, seed, monkeypatch):
    """The ✨ Draft button sends the form's currently-selected kind; the
    route passes it through as kind_hint for generative framing — but an
    unknown value is silently dropped to extraction rather than 400ing
    (the model still classifies the text on its own)."""
    seen = {}

    async def fake_parse(raw_text, kinds, model="", kind_hint=""):
        seen["kind_hint"] = kind_hint
        return {"kind": kind_hint or "item", "name": "X", "summary": "", "body": "",
                "tags": "", "folder": "", "visible_to_players": True}
    monkeypatch.setattr(ai_module, "parse_entity_from_text", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/ai/entity-from-text",
                    json={"text": "a retired smuggler turned harbor inspector", "kind": "character"})
    assert r.status_code == 200
    assert seen["kind_hint"] == "character"

    r = client.post("/api/ai/entity-from-text",
                    json={"text": "whatever", "kind": "not-a-kind"})
    assert r.status_code == 200
    assert seen["kind_hint"] == ""


def test_parse_entity_from_text_generative_framing_when_kind_hint_set(monkeypatch):
    """Pin the framing switch at the source: kind_hint set → the system
    prompt carries the creative-BRIEF steering and names the kind;
    without it, the original extraction wording is untouched."""
    import asyncio
    captured = {}

    class _Msg:
        content = '{"kind": "character", "name": "Rust", "summary": "", "body": "", "tags": "", "folder": "", "visible_to_players": true}'
    class _Resp:
        message = _Msg()

    class _FakeClient:
        async def chat(self, model, messages, format=None, **kw):
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return _Resp()

    class _FakeOld:
        pass

    monkeypatch.setattr(ai_module, "_client", lambda: _FakeClient())

    # With hint: generative wording + the kind named.
    out = asyncio.run(ai_module.parse_entity_from_text(
        "a retired smuggler", ["character", "item"], kind_hint="character"))
    assert out["kind"] == "character" and out["name"] == "Rust"
    assert "creative BRIEF" in captured["system"]
    assert '"character"' in captured["system"]

    # Without: the original extraction contract, no steering appended.
    captured.clear()
    asyncio.run(ai_module.parse_entity_from_text("a merchant named Elena", ["character", "item"]))
    assert "creative BRIEF" not in captured["system"]
    assert "Extract only" in captured["system"]
