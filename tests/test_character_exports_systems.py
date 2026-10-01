"""Exports (and so the AI's view of a sheet, which is built from the same text) must
carry a custom-sheet character's REAL state: resource tracks as saved (cur / max, not the
template default), the active conditions, and the system's name — not N&D columns."""
import json

import pytest

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _tpl(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _hunter(seed, **kw):
    return _add(PlayerCharacter(
        world_id=seed.world_a.id, name="Anders", player_name="Archie", max_hp=0, sheet_template_id=_tpl("hunt-in-the-moonlight"),
        conditions_json=json.dumps(["Stunned", "Burning"]),
        custom_fields_json=json.dumps({"health_current": 3, "health_max": 5, "stamina_current": 1, "stamina_max": 6,
                                       "race": "Human", "abilities": [{"source": "Tracker", "effect": "Follow any trail"}]}),
        **kw))


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_markdown_carries_the_saved_resource_values_conditions_and_system(client, seed):
    pc = _hunter(seed)
    _gm(client, seed)
    md = client.get(f"/characters/{pc}/export.md").text
    assert "Hunt in the Moonlight" in md
    assert "3 / 5" in md and "1 / 6" in md, "saved current/max, not the template default"
    assert "Stunned" in md and "Burning" in md
    assert "Follow any trail" in md and "Human" in md
    assert "Level" not in md, "Level is an N&D concept"


def test_unsaved_resources_fall_back_to_the_template_default(client, seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Fresh", max_hp=0, sheet_template_id=_tpl("hunt-in-the-moonlight"),
                              custom_fields_json="{}"))
    _gm(client, seed)
    md = client.get(f"/characters/{pc}/export.md").text
    assert "Health" in md and "5 / 5" in md


def test_foundry_journal_for_a_custom_sheet_is_the_real_sheet(client, seed):
    pc = _hunter(seed)
    _gm(client, seed)
    data = client.get(f"/characters/{pc}/export.foundry.json").json()
    text = json.dumps(data)
    assert "3 / 5" in text and "Stunned" in text and "Hunt in the Moonlight" in text
    assert "HP:" not in text and "Currency" not in text, "no N&D HP/currency lines on a Hunt in the Moonlight character"


def test_nd_foundry_journal_is_unchanged(client, seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Vex", stats_json=json.dumps([{"id": "str", "value": 3}])))
    _gm(client, seed)
    text = json.dumps(client.get(f"/characters/{pc}/export.foundry.json").json())
    assert "HP:" in text


def test_ndc_is_refused_for_a_custom_sheet(client, seed):
    pc = _hunter(seed)
    _gm(client, seed)
    r = client.get(f"/characters/{pc}/export.ndc")
    assert r.status_code == 400 and "Neon & Dragons" in r.text


def test_party_ndc_bundle_skips_custom_sheet_members(client, seed):
    hunter = _hunter(seed)
    vex = _add(PlayerCharacter(world_id=seed.world_a.id, name="Vex", stats_json=json.dumps([{"id": "str", "value": 3}])))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([hunter, vex])))
    _gm(client, seed)
    r = client.get(f"/parties/{party}/export.ndc")
    assert r.status_code == 200
    names = [c.get("name") for c in r.json()]
    assert names == ["Vex"]
    assert "Anders" in r.headers.get("x-skipped-custom-sheets", "")
