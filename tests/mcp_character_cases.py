"""MCP character & party tools: system-aware (native N&D, Hunt in the Moonlight, Asterion),
and bound by the same GM / owner / section rules as the web UI.

NOT collected on its own (the name has no test_ prefix): tests/test_mcp.py imports these cases at
its end. The MCP session manager's task group is tied to the first event loop that touches it, so
every test that drives the MCP client has to share that module's loop — a second module with its own
loop fails with "Task group is not initialized". Run them with: pytest tests/test_mcp.py"""
import json

import pytest

from app import auth as auth_module
from app import database as dbmod
from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate, User, World, WorldMembership

from .conftest import PLAYER_PASSWORD
from .test_mcp import _call, _issue_token, _reset_db, _result, _seed


def _scene():
    """GM + player + player2 in World A; Anders (HitM, owned by player), Vex (native, owned by
    player2), Zeus (Asterion, unowned), all in one party. Built-in templates are created from the
    real field constants (the MCP harness doesn't run the app's seeding)."""
    _reset_db()
    ids = _seed()
    db = SessionLocal()
    try:
        p2 = User(email="mcp-player2@test.local", password_hash=auth_module.hash_password(PLAYER_PASSWORD),
                  display_name="Player Two", is_gm=False)
        db.add(p2)
        db.commit()
        db.add(WorldMembership(world_id=ids["world_a_id"], user_id=p2.id))
        tpls = {}
        for slug, name, fields in (("hunt-in-the-moonlight", "Hunt in the Moonlight", dbmod._HITM_FIELDS),
                                   ("asterion", "Asterion", dbmod._ASTERION_FIELDS)):
            t = SheetTemplate(name=name, slug=slug, is_builtin=True, sheet_mode="custom", fields_json=json.dumps(fields))
            db.add(t)
            tpls[slug] = t
        db.commit()
        anders = PlayerCharacter(world_id=ids["world_a_id"], owner_user_id=ids["player_id"], name="Anders", max_hp=0,
                                 sheet_template_id=tpls["hunt-in-the-moonlight"].id, conditions_json=json.dumps(["Stunned"]),
                                 custom_fields_json=json.dumps({"health_current": 3, "health_max": 5, "stamina_current": 2,
                                                                "stamina_max": 5, "strain": "2", "xpCurrent": 4, "xpLifetime": 10}))
        vex = PlayerCharacter(world_id=ids["world_a_id"], owner_user_id=p2.id, name="Vex", max_hp=20, current_hp=9,
                              stats_json=json.dumps([{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]),
                              shock_current=2, pp_current=1, mp_current=1, xp=5, backstory="Public. [gmonly]Secret.[/gmonly]")
        zeus = PlayerCharacter(world_id=ids["world_a_id"], name="Zeus", max_hp=0, sheet_template_id=tpls["asterion"].id,
                               custom_fields_json=json.dumps({"flesh_current": 2, "flesh_max": 5, "glory": 3}))
        db.add_all([anders, vex, zeus])
        db.commit()
        party = Party(world_id=ids["world_a_id"], name="Crew", member_pc_ids_json=json.dumps([anders.id, vex.id, zeus.id]),
                      loot_json=json.dumps([{"lid": "aaaaaaaa", "name": "Rope", "qty": 1, "notes": "", "claimed_by": [anders.id]}]))
        db.add(party)
        db.commit()
        ids.update(p2_id=p2.id, anders=anders.id, vex=vex.id, zeus=zeus.id, party=party.id)
        return ids
    finally:
        db.close()


def _world(ids, **fields):
    db = SessionLocal()
    try:
        w = db.get(World, ids["world_a_id"])
        for k, v in fields.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)


def _pc(pc_id):
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        db.expunge(pc)
        return pc
    finally:
        db.close()


# ── reads ────────────────────────────────────────────────────────────────────

async def test_gm_lists_every_character_with_its_own_systems_state():
    ids = _scene()
    rows = _result(await _call(_issue_token(ids["gm_id"]), "list_characters", {"world_id": ids["world_a_id"]}))
    assert [r["name"] for r in rows] == ["Anders", "Vex", "Zeus"]
    a, v, z = _row(rows, "Anders"), _row(rows, "Vex"), _row(rows, "Zeus")
    assert a["system"] == "Hunt in the Moonlight" and a["native"] is False
    assert a["vital"] == {"label": "Health", "current": 3, "max": 5, "down": False}
    assert {"label": "Stamina", "current": 2, "max": 5} in a["resources"] and a["conditions"] == ["Stunned"]
    assert v["system"] == "Neon & Dragons" and v["vital"]["label"] == "HP" and (v["vital"]["current"], v["vital"]["max"]) == (9, 20)
    assert {r["label"] for r in v["resources"]} == {"Shock", "PP", "MP"}
    assert z["system"] == "Asterion" and z["vital"]["label"] == "Flesh" and z["vital"]["current"] == 2


async def test_player_token_sees_only_own_characters_when_parties_are_private():
    ids = _scene()
    _world(ids, players_see_party=False)
    rows = _result(await _call(_issue_token(ids["player_id"]), "list_characters", {"world_id": ids["world_a_id"]}))
    assert [r["name"] for r in rows] == ["Anders"] and rows[0]["mine"] is True


async def test_players_see_owned_party_members_when_the_world_allows_it():
    ids = _scene()
    _world(ids, players_see_party=True)
    rows = _result(await _call(_issue_token(ids["player_id"]), "list_characters", {"world_id": ids["world_a_id"]}))
    assert [r["name"] for r in rows] == ["Anders", "Vex"], "unowned Zeus stays GM-managed"


async def test_get_character_returns_the_systems_own_sheet_markdown():
    ids = _scene()
    got = _result(await _call(_issue_token(ids["gm_id"]), "get_character", {"character_id": ids["anders"]}))
    assert got["system"] == "Hunt in the Moonlight"
    assert "3 / 5" in got["sheet"] and "Stunned" in got["sheet"] and "Hunt in the Moonlight" in got["sheet"]


async def test_gmonly_blocks_are_stripped_for_a_player_but_not_the_gm():
    ids = _scene()
    _world(ids, players_see_party=True)
    player = _result(await _call(_issue_token(ids["player_id"]), "get_character", {"character_id": ids["vex"]}))
    assert "Public." in player["sheet"] and "Secret." not in player["sheet"]
    gm = _result(await _call(_issue_token(ids["gm_id"]), "get_character", {"character_id": ids["vex"]}))
    assert "Secret." in gm["sheet"]


async def test_a_player_cannot_read_someone_elses_sheet_when_parties_are_private():
    ids = _scene()
    _world(ids, players_see_party=False)
    res = await _call(_issue_token(ids["player_id"]), "get_character", {"character_id": ids["vex"]})
    assert res.isError and "not accessible" in res.content[0].text


# ── writes ───────────────────────────────────────────────────────────────────

async def test_adjust_a_custom_resource_by_name_and_clamp():
    ids = _scene()
    tok = _issue_token(ids["gm_id"])
    r = _result(await _call(tok, "adjust_character_resource", {"character_id": ids["anders"], "resource": "Stamina", "delta": -1}))
    assert {"label": "Stamina", "current": 1, "max": 5} in r["resources"]
    r = _result(await _call(tok, "adjust_character_resource", {"character_id": ids["anders"], "resource": "health", "delta": -99}))
    assert r["vital"]["current"] == 0 and r["vital"]["down"] is True
    r = _result(await _call(tok, "adjust_character_resource", {"character_id": ids["anders"], "resource": "Health", "value": 99}))
    assert r["vital"]["current"] == 5
    assert json.loads(_pc(ids["anders"]).custom_fields_json)["health_current"] == 5


async def test_adjust_a_native_resource():
    ids = _scene()
    r = _result(await _call(_issue_token(ids["gm_id"]), "adjust_character_resource", {"character_id": ids["vex"], "resource": "HP", "delta": 4}))
    assert (r["vital"]["current"], r["vital"]["max"]) == (13, 20)
    assert _pc(ids["vex"]).current_hp == 13


async def test_unknown_resource_names_the_available_ones():
    ids = _scene()
    res = await _call(_issue_token(ids["gm_id"]), "adjust_character_resource", {"character_id": ids["anders"], "resource": "mana", "delta": 1})
    assert res.isError and "Stamina" in res.content[0].text
    res = await _call(_issue_token(ids["gm_id"]), "adjust_character_resource", {"character_id": ids["vex"], "resource": "mana", "delta": 1})
    assert res.isError and "hp, shock, pp, mp" in res.content[0].text


async def test_delta_and_value_are_exclusive():
    ids = _scene()
    res = await _call(_issue_token(ids["gm_id"]), "adjust_character_resource",
                      {"character_id": ids["anders"], "resource": "health", "delta": 1, "value": 2})
    assert res.isError and "exactly one" in res.content[0].text


async def test_an_owner_may_adjust_their_own_character_but_not_others():
    ids = _scene()
    tok = _issue_token(ids["player_id"])
    ok = _result(await _call(tok, "adjust_character_resource", {"character_id": ids["anders"], "resource": "stamina", "delta": 1}))
    assert {"label": "Stamina", "current": 3, "max": 5} in ok["resources"]
    res = await _call(tok, "adjust_character_resource", {"character_id": ids["vex"], "resource": "hp", "delta": -9})
    assert res.isError and _pc(ids["vex"]).current_hp == 9


async def test_set_conditions():
    ids = _scene()
    r = _result(await _call(_issue_token(ids["gm_id"]), "set_character_conditions",
                            {"character_id": ids["anders"], "conditions": ["Burning", "burning", "Marked"]}))
    assert r["conditions"] == ["Burning", "Marked"]
    res = await _call(_issue_token(ids["player_id"]), "set_character_conditions",
                      {"character_id": ids["vex"], "conditions": ["Stunned"]})
    assert res.isError


async def test_award_xp_goes_to_each_systems_own_fields_and_is_gm_only():
    ids = _scene()
    tok = _issue_token(ids["gm_id"])
    _result(await _call(tok, "award_character_xp", {"character_id": ids["anders"], "amount": 3}))
    _result(await _call(tok, "award_character_xp", {"character_id": ids["zeus"], "amount": 2}))
    _result(await _call(tok, "award_character_xp", {"character_id": ids["vex"], "amount": 7}))
    cf = json.loads(_pc(ids["anders"]).custom_fields_json)
    assert (cf["xpCurrent"], cf["xpLifetime"]) == (7, 13)
    assert json.loads(_pc(ids["zeus"]).custom_fields_json)["glory"] == 5
    assert _pc(ids["vex"]).xp == 12
    res = await _call(_issue_token(ids["player_id"]), "award_character_xp", {"character_id": ids["anders"], "amount": 99})
    assert res.isError and "GM" in res.content[0].text


# ── parties ──────────────────────────────────────────────────────────────────

async def test_party_tools():
    ids = _scene()
    tok = _issue_token(ids["gm_id"])
    parties = _result(await _call(tok, "list_parties", {"world_id": ids["world_a_id"]}))
    assert parties == [{"id": ids["party"], "name": "Crew", "members": ["Anders", "Vex", "Zeus"]}]
    p = _result(await _call(tok, "get_party", {"party_id": ids["party"]}))
    assert {m["name"]: m["system"] for m in p["members"]} == {"Anders": "Hunt in the Moonlight", "Vex": "Neon & Dragons", "Zeus": "Asterion"}
    assert p["loot"] == [{"name": "Rope", "qty": 1, "notes": "", "claimed_by": ["Anders"]}]


async def test_a_player_sees_only_their_own_party_unless_the_world_shows_all():
    ids = _scene()
    _world(ids, players_see_party=False, section_access_json=json.dumps({"parties": {"player": "read"}}))
    db = SessionLocal()
    try:
        other = Party(world_id=ids["world_a_id"], name="Strangers", member_pc_ids_json="[]")
        db.add(other)
        db.commit()
        other_id = other.id
    finally:
        db.close()
    tok = _issue_token(ids["player_id"])
    assert [p["name"] for p in _result(await _call(tok, "list_parties", {"world_id": ids["world_a_id"]}))] == ["Crew"]
    res = await _call(tok, "get_party", {"party_id": other_id})
    assert res.isError


async def test_rest_party_applies_each_systems_rest_and_is_gm_only():
    ids = _scene()
    r = _result(await _call(_issue_token(ids["gm_id"]), "rest_party", {"party_id": ids["party"], "kind": "long"}))
    assert set(r["rested"]) == {"Anders", "Vex", "Zeus"}
    cf = json.loads(_pc(ids["anders"]).custom_fields_json)
    assert cf["stamina_current"] == 5 and cf["strain"] == "0", "Hunt in the Moonlight rest: Stamina full, Strain cleared"
    assert json.loads(_pc(ids["zeus"]).custom_fields_json)["flesh_current"] == 5, "Asterion long rest: all Flesh"
    assert _pc(ids["vex"]).shock_current == 12, "N&D rest: all Shock"
    res = await _call(_issue_token(ids["player_id"]), "rest_party", {"party_id": ids["party"], "kind": "long"})
    assert res.isError and "GM" in res.content[0].text
    res = await _call(_issue_token(ids["gm_id"]), "rest_party", {"party_id": ids["party"], "kind": "weekly"})
    assert res.isError


async def test_a_closed_characters_section_blocks_a_player_token():
    ids = _scene()
    db = SessionLocal()
    try:
        db.get(World, ids["world_a_id"]).section_access_json = json.dumps({"characters": {"player": "none"}})
        db.commit()
    finally:
        db.close()
    res = await _call(_issue_token(ids["player_id"]), "list_characters", {"world_id": ids["world_a_id"]})
    assert res.isError and "characters" in res.content[0].text
    assert not (await _call(_issue_token(ids["gm_id"]), "list_characters", {"world_id": ids["world_a_id"]})).isError
