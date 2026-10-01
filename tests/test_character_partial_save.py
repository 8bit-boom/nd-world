"""Editing a character must only change the fields the form actually sent.

POST /characters/{id}/edit used to run the whole form through a helper that
writes a DEFAULT for every key that is absent. The custom-sheet form posts
only name / player_name / portrait / custom_fields_json / sheet_template_id,
and the native form never posts conditions_json or app_extra_json — so every
save reset level to 1, XP and HP to 0, and wiped backstory, equipment,
conditions and the Android app's passthrough JSON. Edit is now a partial
update; create and the importer keep full-default semantics.
"""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _custom_template_id():
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first().id
    finally:
        db.close()


def _rich_pc(seed, **extra):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(
            world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Veteran", player_name="Pia",
            race="Human", char_class="Rogue", level=5, xp=700, backstory="Raised on the docks.",
            notes="Owes the Guild.", max_hp=12, current_hp=7, shock_max=9, shock_current=4,
            pp_current=3, mp_current=2, minor_edge="Lucky", major_edge="Veteran",
            conditions_json=json.dumps(["Stunned"]), app_extra_json=json.dumps({"app": {"v": 2}}),
            equipment_json=json.dumps([{"name": "Sword", "qty": 1}]),
            feats_json=json.dumps([{"name": "Parry"}]), stats_json=json.dumps([{"id": "str", "value": 6}]),
            **extra,
        )
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _row(pc_id):
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        db.expunge(pc)
        return pc
    finally:
        db.close()


def _as_owner(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)


def test_custom_sheet_save_keeps_everything_it_did_not_send(client, seed):
    pc = _rich_pc(seed, sheet_template_id=_custom_template_id())
    tpl = _row(pc).sheet_template_id
    _as_owner(client, seed)
    # exactly what custom_sheet.html posts
    r = client.post(f"/characters/{pc}/edit", data={
        "name": "Veteran Renamed", "player_name": "Pia",
        "custom_fields_json": json.dumps({"spark": "Fire"}), "sheet_template_id": str(tpl),
    }, follow_redirects=False)
    assert r.status_code == 303
    row = _row(pc)
    assert row.name == "Veteran Renamed" and json.loads(row.custom_fields_json) == {"spark": "Fire"}
    assert (row.level, row.xp) == (5, 700), "level/XP were reset"
    assert (row.max_hp, row.current_hp, row.shock_max, row.shock_current) == (12, 7, 9, 4)
    assert (row.pp_current, row.mp_current) == (3, 2)
    assert (row.race, row.char_class, row.backstory, row.notes) == ("Human", "Rogue", "Raised on the docks.", "Owes the Guild.")
    assert (row.minor_edge, row.major_edge) == ("Lucky", "Veteran")
    assert json.loads(row.conditions_json) == ["Stunned"]
    assert json.loads(row.app_extra_json) == {"app": {"v": 2}}, "Android passthrough JSON was wiped"
    assert json.loads(row.equipment_json) == [{"name": "Sword", "qty": 1}]
    assert json.loads(row.feats_json) == [{"name": "Parry"}]
    assert row.sheet_template_id == tpl


def test_native_sheet_edit_keeps_conditions_and_app_extra(client, seed):
    pc = _rich_pc(seed)
    _as_owner(client, seed)
    # what form.html posts (it has no conditions_json / app_extra_json inputs)
    r = client.post(f"/characters/{pc}/edit", data={
        "name": "Veteran", "player_name": "Pia", "race": "Human", "char_class": "Rogue", "level": "6", "xp": "900",
        "backstory": "Raised on the docks.", "notes": "Updated note.", "max_hp": "14", "current_hp": "14",
        "shock_max": "9", "shock_current": "9", "pp_current": "3", "mp_current": "2",
        "minor_edge": "Lucky", "major_edge": "Veteran", "sheet_template_id": "",
        "stats_json": json.dumps([{"id": "str", "value": 7}]), "equipment_json": "[]", "feats_json": "[]",
        "currency_json": "[]", "cyberware_json": "[]", "custom_fields_json": "{}",
    }, follow_redirects=False)
    assert r.status_code == 303
    row = _row(pc)
    assert (row.level, row.xp, row.max_hp, row.notes) == (6, 900, 14, "Updated note.")
    assert json.loads(row.conditions_json) == ["Stunned"], "conditions were wiped by an unrelated edit"
    assert json.loads(row.app_extra_json) == {"app": {"v": 2}}
    assert json.loads(row.equipment_json) == [], "a field the form DID send must still be applied"
    assert row.sheet_template_id is None, "an explicit empty template selection means 'no template'"


def test_a_field_sent_empty_is_still_cleared(client, seed):
    pc = _rich_pc(seed)
    _as_owner(client, seed)
    client.post(f"/characters/{pc}/edit", data={"name": "Veteran", "backstory": "", "notes": ""}, follow_redirects=False)
    row = _row(pc)
    assert row.backstory == "" and row.notes == ""
    assert (row.level, row.xp) == (5, 700)


def test_gm_edit_is_partial_too(client, seed):
    pc = _rich_pc(seed, sheet_template_id=_custom_template_id())
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/characters/{pc}/edit", data={"name": "GM Edit", "custom_fields_json": "{}"}, follow_redirects=False)
    row = _row(pc)
    assert row.name == "GM Edit" and (row.level, row.xp) == (5, 700)
    assert row.sheet_template_id is not None, "an absent sheet_template_id must keep the template"


def test_create_still_applies_full_defaults(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/characters/new", data={"name": "Newbie"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    db = SessionLocal()
    try:
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.name == "Newbie").first()
        assert (pc.level, pc.xp) == (1, 0)
        assert json.loads(pc.conditions_json or "[]") == []
    finally:
        db.close()


def test_edit_and_delete_notify_live_sync(client, seed, monkeypatch):
    from app.routers import characters as chars
    touched = []
    monkeypatch.setattr(chars.live, "touch", lambda world_id: touched.append(world_id))
    pc = _rich_pc(seed)
    _as_owner(client, seed)
    client.post(f"/characters/{pc}/edit", data={"name": "Touch"}, follow_redirects=False)
    assert touched == [seed.world_a.id], "an edit must refresh open party/cockpit views"
    client.post(f"/characters/{pc}/delete", follow_redirects=False)
    assert touched == [seed.world_a.id, seed.world_a.id]
