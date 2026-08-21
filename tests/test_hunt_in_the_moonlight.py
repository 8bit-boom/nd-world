"""Tests for the bundled "Hunt in the Moonlight" sheet template
(app/database.py's _HITM_FIELDS + its _seed() block) — a global,
built-in, sheet_mode="custom" SheetTemplate seeded exactly like the
existing "nd-default"/"asterion" built-ins, so every world can pick it
for a new character with no per-world setup.
"""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login


def _get_hitm_template():
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == "hunt-in-the-moonlight").first()
    finally:
        db.close()


def test_hitm_template_is_seeded_as_a_global_builtin(client, seed):
    tpl = _get_hitm_template()
    assert tpl is not None
    assert tpl.world_id is None
    assert tpl.is_builtin is True
    assert tpl.sheet_mode == "custom"
    assert tpl.name == "Hunt in the Moonlight"


def test_hitm_fields_json_is_well_formed(client, seed):
    tpl = _get_hitm_template()
    fields = json.loads(tpl.fields_json)
    assert isinstance(fields, list) and len(fields) > 0

    expected_sections = {
        "Identity", "Hunter's Oath", "Core Tracks", "Experience", "Moon Calendar",
        "Abilities", "Moon-Gifts & Occult Rites", "Hunter Tools & Loadout",
        "Body Modifications", "Session Record", "Notes",
    }
    assert expected_sections == {f["section"] for f in fields}

    ids = [f["id"] for f in fields]
    assert len(ids) == len(set(ids)), "duplicate top-level field ids"
    assert {f["type"] for f in fields} <= {"text", "textarea", "number", "list"}

    list_fields = {f["id"]: f for f in fields if f["type"] == "list"}
    assert {"xpLog", "abilities", "rites", "tools", "mods", "sessions"} == set(list_fields)
    for lf in list_fields.values():
        assert lf["item_fields"], f"{lf['id']} has no item_fields"
        for sf in lf["item_fields"]:
            # custom_sheet.html's renderer only understands text/textarea for
            # list sub-columns (no number/select sub-field support exists).
            assert sf["type"] in ("text", "textarea"), (lf["id"], sf)


def test_hitm_template_available_to_any_world(client, seed):
    """Global (world_id=None) built-ins show up on every world's
    /characters/templates browse page, not just one specific world."""
    login(client, seed.gm.email, GM_PASSWORD)
    for world in (seed.world_a, seed.world_b):
        client.cookies.set("active_world", world.slug)
        r = client.get("/characters/templates")
        assert r.status_code == 200
        assert "Hunt in the Moonlight" in r.text


def test_hitm_character_round_trips_plain_and_list_fields(client, seed):
    """A PC created against this template stores both a plain field and a
    list-type (table row) field in custom_fields_json, and both survive a
    save + reload of the rendered sheet."""
    tpl = _get_hitm_template()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    custom_fields = {
        "player": "Alex",
        "hunter": "Mara Voss",
        "healthCurrent": "4",
        "healthMax": "5",
        "moonPhase": "Gibbous",
        "abilities": [
            {"source": "Silver Trap", "tier": "1", "cost": "1",
             "rangeDamage": "Close", "effect": "Snares prey in place", "used": ""},
        ],
    }
    r = client.post("/characters/new", data={
        "name": "Mara Voss",
        "sheet_template_id": str(tpl.id),
        "custom_fields_json": json.dumps(custom_fields),
    }, follow_redirects=False)
    assert r.status_code == 303
    pc_id = int(r.headers["location"].rsplit("/", 1)[-1])

    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        assert pc.sheet_template_id == tpl.id
        stored = json.loads(pc.custom_fields_json)
    finally:
        db.close()
    assert stored["player"] == "Alex"
    assert stored["hunter"] == "Mara Voss"
    assert stored["moonPhase"] == "Gibbous"
    assert stored["abilities"][0]["source"] == "Silver Trap"

    r = client.get(f"/characters/{pc_id}")
    assert r.status_code == 200
    assert "Mara Voss" in r.text
    assert "Silver Trap" in r.text
    assert "Snares prey in place" in r.text
