"""The always-visible strip and the in-hub launcher (app/routers/character_hub.py `hub/now`, `hub/places`) and the
custom-system quick adjust (`/api/characters/{id}/resource`): they show a player what they could already reach, and
nothing a GM-run combatant's name could spoil."""
import json

import pytest

from app.database import SessionLocal
from app.models import CombatSession, PlayerCharacter, SheetTemplate, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login
from .test_character_hub import _add, _as, _party, _pc, _set_world


def _combat(world, combatants, active_idx=0, round_num=2):
    return _add(CombatSession(world_id=world.id, name="Ambush", combatants_json=json.dumps(combatants),
                              active_idx=active_idx, round_num=round_num))


def _fields():
    return [
        {"id": "health", "label": "Health", "type": "resource", "section": "Vitals", "default_value": "5/5", "vital": "hp"},
        {"id": "hunger", "label": "Hunger", "type": "resource", "section": "Vitals", "default_value": "0/10"},
    ]


def _custom_pc(seed, owner=None):
    tpl = _add(SheetTemplate(world_id=seed.world_a.id, name="Homebrew", slug="homebrew-now", sheet_mode="custom",
                             fields_json=json.dumps(_fields())))
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=(owner or seed.player_a).id, name="Aria",
                             sheet_template_id=tpl)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


# ── hub/now ──────────────────────────────────────────────────────────────────

def test_now_shows_the_owners_vitals_and_that_they_can_edit(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc)
        row.current_hp, row.max_hp, row.conditions_json = 7, 12, json.dumps(["Burning"])
        db.commit()
    finally:
        db.close()
    _as(client, seed.player_a)
    d = client.get(f"/api/characters/{pc}/hub/now").json()
    assert d["can_edit"] is True
    assert d["me"]["hp"] == 7 and d["me"]["max_hp"] == 12 and d["me"]["native"] is True
    assert d["me"]["conditions"] == ["Burning"]
    assert d["combat"] is None


def test_a_gm_looking_in_gets_it_read_only_and_a_stranger_gets_nothing(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/characters/{pc}/hub/now").json()["can_edit"] is False
    client.cookies.clear()
    _as(client, seed.player_b)
    assert client.get(f"/api/characters/{pc}/hub/now").status_code == 404


def test_combat_turn_never_names_a_gm_run_combatant(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    friend = _pc(seed.player_a, seed.world_a, "Friend")
    mine = {"id": "a", "name": "Hero", "source": "pc", "pc_id": pc}
    pal = {"id": "b", "name": "Friend", "source": "pc", "pc_id": friend}
    boss = {"id": "c", "name": "Secret Cult Leader", "source": "entity", "entity_id": 5}
    _as(client, seed.player_a)

    def now():
        return client.get(f"/api/characters/{pc}/hub/now").json()["combat"]

    cid = _combat(seed.world_a, [mine, boss, pal], active_idx=0)
    c = now()
    assert c["turn"] == "me" and c["round"] == 2
    _set_combat(cid, active_idx=1)
    c = now()
    assert c["turn"] == "enemy" and c["name"] is None and "Secret" not in json.dumps(c)
    assert c["next_is_me"] is False
    _set_combat(cid, active_idx=2)
    c = now()
    assert c["turn"] == "pc" and c["name"] == "Friend" and c["next_is_me"] is True


def _set_combat(cid, **fields):
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cid)
        for k, v in fields.items():
            setattr(cs, k, v)
        db.commit()
    finally:
        db.close()


def test_an_encounter_without_this_character_is_not_shown(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _combat(seed.world_a, [{"id": "x", "name": "Other", "source": "pc", "pc_id": pc + 999}])
    _as(client, seed.player_a)
    assert client.get(f"/api/characters/{pc}/hub/now").json()["combat"] is None


def test_custom_system_strip_has_its_vital_and_other_tracks(client, seed):
    pc = _custom_pc(seed)
    _as(client, seed.player_a)
    me = client.get(f"/api/characters/{pc}/hub/now").json()["me"]
    assert me["native"] is False and me["hp_id"] == "health" and me["hp"] == 5 and me["max_hp"] == 5
    assert [r["id"] for r in me["resources"]] == ["hunger"]            # the vital is not listed twice


# ── the quick adjust for custom systems ─────────────────────────────────────

def test_resource_delta_is_clamped_and_saved(client, seed):
    pc = _custom_pc(seed)
    _as(client, seed.player_a)
    r = client.post(f"/api/characters/{pc}/resource", json={"field_id": "health", "action": "delta", "value": -2})
    assert r.status_code == 200 and r.json()["current"] == 3 and r.json()["max"] == 5
    assert client.post(f"/api/characters/{pc}/resource", json={"field_id": "health", "action": "delta", "value": -99}).json()["current"] == 0
    assert client.post(f"/api/characters/{pc}/resource", json={"field_id": "health", "action": "delta", "value": 99}).json()["current"] == 5
    assert client.post(f"/api/characters/{pc}/resource", json={"field_id": "hunger", "action": "set", "value": 4}).json()["current"] == 4
    db = SessionLocal()
    try:
        saved = json.loads(db.get(PlayerCharacter, pc).custom_fields_json)
    finally:
        db.close()
    assert saved["health_current"] == 5 and saved["hunger_current"] == 4
    assert client.get(f"/api/characters/{pc}/hub/now").json()["me"]["resources"][0]["current"] == 4


def test_resource_is_owner_only_and_needs_a_custom_sheet(client, seed):
    pc = _custom_pc(seed)
    native = _pc(seed.player_a, seed.world_a, "Native")
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{pc}/resource", json={"field_id": "nope", "action": "delta", "value": 1}).status_code == 404
    assert client.post(f"/api/characters/{native}/resource", json={"field_id": "health", "action": "delta", "value": 1}).status_code == 400
    client.cookies.clear()
    _as(client, seed.player_b)
    assert client.post(f"/api/characters/{pc}/resource", json={"field_id": "health", "action": "delta", "value": 1}).status_code in (403, 404)


# ── hub/places ───────────────────────────────────────────────────────────────

def test_places_list_only_what_players_can_open(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _set_world(seed.world_a.id, section_access_json=json.dumps({
        "maps": {"player": "read", "assistant": "edit"}, "calendar": {"player": "none", "assistant": "edit"},
        "combat": {"player": "none", "assistant": "edit"}}))
    _as(client, seed.player_a)
    d = client.get(f"/api/characters/{pc}/hub/places").json()
    items = {i["id"]: i for g in d["groups"] for i in g["items"]}
    assert "maps" in items and items["maps"]["href"].endswith("w=" + seed.world_a.slug)
    assert "calendar" not in items and "combat" not in items          # closed to players
    assert not {"cockpit", "boards", "import", "export", "background_jobs", "system_monitor", "characters"} & set(items)


def test_places_is_not_open_to_a_stranger(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_b)
    assert client.get(f"/api/characters/{pc}/hub/places").status_code == 404


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_character_page_carries_the_strip_the_viewer_and_the_phone_bar(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert 'id="pch-now"' in html and 'id="pch-tags"' in html and 'id="pch-more"' in html
    assert "/static/js/pc-viewer.js" in html
    assert 'data-tab="places"' in html and "pch-tab--minor" in html


def test_the_viewer_script_parses():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", "static/js/pc-viewer.js"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# ── initiative + end turn, and the turn order itself ────────────────────────

def test_turn_order_follows_initiative_not_the_order_added(client, seed):
    """active_idx is a position in the initiative-sorted order (the tracker page's own rule)."""
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    slow = {"id": "a", "name": "Slowpoke", "source": "pc", "pc_id": pc, "initiative": 3}
    fast = {"id": "b", "name": "Boss", "source": "entity", "entity_id": 9, "initiative": 20}
    _combat(seed.world_a, [slow, fast], active_idx=0)          # position 0 = the highest initiative = Boss
    _as(client, seed.player_a)
    c = client.get(f"/api/characters/{pc}/hub/now").json()["combat"]
    assert c["turn"] == "enemy" and c["next_is_me"] is True


def _stats(pc_id, **vals):
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, pc_id).stats_json = json.dumps([{"id": k, "value": v} for k, v in vals.items()])
        db.commit()
    finally:
        db.close()


def test_roll_initiative_once_into_the_encounter_and_the_log(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _stats(pc, dex=4, itu=3)
    boss = {"id": "b", "name": "Boss", "source": "entity", "entity_id": 9, "initiative": 9}
    mine = {"id": "a", "name": "Hero", "source": "pc", "pc_id": pc, "initiative": 0}
    cid = _combat(seed.world_a, [mine, boss], active_idx=0)     # Boss (9) has the turn
    _as(client, seed.player_a)
    assert client.get(f"/api/characters/{pc}/hub/now").json()["combat"]["can_roll_initiative"] is True
    r = client.post(f"/api/characters/{pc}/hub/initiative")
    assert r.status_code == 200 and 8 <= r.json()["initiative"] <= 17            # d10 + (4 + 3)
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cid)
        saved = json.loads(cs.combatants_json)
        assert saved[0]["initiative"] == r.json()["initiative"]
        from app import combat_turns
        assert saved[combat_turns.current_index(cs, saved)]["name"] == "Boss"     # the Boss keeps the turn after the re-sort
    finally:
        db.close()
    assert client.post(f"/api/characters/{pc}/hub/initiative").status_code == 409
    assert client.get("/api/dice/history").json()["rolls"][0]["label"].startswith("Initiative")


def test_end_turn_only_on_your_turn_and_wraps_the_round(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    mine = {"id": "a", "name": "Hero", "source": "pc", "pc_id": pc, "initiative": 12}
    boss = {"id": "b", "name": "Boss", "source": "entity", "entity_id": 9, "initiative": 5}
    cid = _combat(seed.world_a, [boss, mine], active_idx=1, round_num=4)   # Hero (12) first, so position 1 is the Boss
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{pc}/hub/end-turn").status_code == 409      # the Boss has the turn
    _set_combat(cid, active_idx=0)
    r = client.post(f"/api/characters/{pc}/hub/end-turn")
    assert r.status_code == 200 and r.json()["combat"]["turn"] == "enemy"
    _set_combat(cid, active_idx=1)
    boss_turn = client.post(f"/api/characters/{pc}/hub/end-turn")
    assert boss_turn.status_code == 409                                              # still not Hero's turn
    db = SessionLocal()
    try:
        assert db.get(CombatSession, cid).round_num == 4
    finally:
        db.close()


def test_initiative_and_end_turn_are_owner_only(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _combat(seed.world_a, [{"id": "a", "name": "H", "source": "pc", "pc_id": pc, "initiative": 0}])
    _as(client, seed.player_b)
    assert client.post(f"/api/characters/{pc}/hub/initiative").status_code == 404
    assert client.post(f"/api/characters/{pc}/hub/end-turn").status_code == 404


def test_advance_wraps_to_a_new_round():
    from types import SimpleNamespace
    from app import combat_turns
    cs = SimpleNamespace(active_idx=1, round_num=2)
    combat_turns.advance(cs, [{"initiative": 1}, {"initiative": 2}])
    assert (cs.active_idx, cs.round_num) == (0, 3)
    combat_turns.advance(cs, [{"initiative": 1}, {"initiative": 2}])
    assert (cs.active_idx, cs.round_num) == (1, 3)
