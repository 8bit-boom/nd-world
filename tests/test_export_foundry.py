"""Tests for GET /export/foundry.json (app/main.py) — a whole-world export
as Foundry VTT JournalEntry documents (system-agnostic, same v10+
page-based schema as characters.py's existing per-character
export.foundry.json). GM-only, like everything else under /export."""
import json

from app.database import SessionLocal
from app.models import Entity, EntityNote, PlayerCharacter, World

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


def _add_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "character"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _add_note(entity_id, content):
    db = SessionLocal()
    try:
        db.add(EntityNote(entity_id=entity_id, content=content))
        db.commit()
    finally:
        db.close()


def _add_pc(world_id, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, name=kw.pop("name", "Hero"), **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def test_export_foundry_gm_allowed(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/foundry.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert 'attachment; filename="world-a-foundry.json"' in r.headers["content-disposition"]


def test_export_foundry_player_forbidden(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/foundry.json")
    assert r.status_code == 403


def test_export_foundry_includes_rules_entities_and_characters(client, seed):
    _set_world(seed.world_a.id, rules_md="# Custom Rules\n\nHouse rules here.")
    eid = _add_entity(seed.world_a.id, name="Vex the Informant", kind="character",
                       subtype="NPC", summary="A nervous fixer.", body="Knows everyone in the Hollow.")
    _add_note(eid, "Secretly a corp plant.")
    _add_pc(seed.world_a.id, name="Kestrel")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/foundry.json")
    assert r.status_code == 200
    docs = json.loads(r.text)
    assert isinstance(docs, list)

    names = [d["name"] for d in docs]
    assert "Rules" in names
    assert "[Character — NPC] Vex the Informant" in names
    assert "Kestrel" in names

    rules_doc = next(d for d in docs if d["name"] == "Rules")
    assert "Custom Rules" in rules_doc["pages"][0]["text"]["content"]

    entity_doc = next(d for d in docs if d["name"] == "[Character — NPC] Vex the Informant")
    assert entity_doc["flags"]["nd-world"]["entity_id"] == eid
    overview = entity_doc["pages"][0]["text"]["content"]
    assert "nervous fixer" in overview
    assert "Knows everyone in the Hollow" in overview
    notes_page = next(p for p in entity_doc["pages"] if p["name"] == "Notes")
    assert "Secretly a corp plant." in notes_page["text"]["content"]


def test_export_foundry_document_shape_is_valid_journal_entries(client, seed):
    _add_entity(seed.world_a.id, name="Plain Entity", kind="location")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/foundry.json")
    docs = json.loads(r.text)
    for doc in docs:
        assert "name" in doc
        assert doc["folder"] is None
        assert isinstance(doc["pages"], list) and doc["pages"]
        for page in doc["pages"]:
            assert page["type"] == "text"
            assert "content" in page["text"]
        assert doc["flags"]["nd-world"]["source"] == "nd-world"


def test_export_foundry_cross_world_isolation(client, seed):
    _add_entity(seed.world_a.id, name="World A Entity", kind="location")
    _add_entity(seed.world_b.id, name="World B Entity", kind="location")
    _add_pc(seed.world_b.id, name="World B Hero")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/foundry.json")
    text = r.text
    assert "World A Entity" in text
    assert "World B Entity" not in text
    assert "World B Hero" not in text
