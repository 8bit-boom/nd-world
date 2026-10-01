"""HP / Shock / PP / MP ceilings must come from ONE rule everywhere.

A stored max of 0 means "use the stat-derived value" (HP = STR+DEX+BOD+PER+10,
Shock = WIL+INT+CHA+ITU). The sheet honoured that; the party vitals strip, the
combat tracker, its character sync and the Shock route read the raw column, so
a character on auto HP showed `22/0`, was never flagged DOWN, joined combat with
max HP 0, was clamped to 0 HP by a sync and could not gain Shock. Quick-edit
routes also 500'd on junk numbers.
"""
import json
import re
from types import SimpleNamespace

from app.database import SessionLocal
from app.models import CombatSession, Party, PlayerCharacter, SheetTemplate
from app.pc_stats import int_field, pc_maxima

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

STATS = [{"id": k, "value": v} for k, v in
         (("str", 3), ("dex", 3), ("bod", 3), ("per", 3), ("wil", 2), ("int", 2), ("cha", 2), ("itu", 2))]
# phys = 12 -> HP 22, PP 12;   ment = 8 -> Shock 8, MP 8


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _auto_pc(seed, **kw):
    kw.setdefault("max_hp", 0)
    kw.setdefault("current_hp", 22)
    return _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Auto",
                                stats_json=json.dumps(STATS), shock_max=0, shock_current=0, **kw))


def _row(pc_id):
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        db.expunge(pc)
        return pc
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── the helper itself ────────────────────────────────────────────────────────

def test_zero_means_derived_and_nonzero_wins():
    pc = SimpleNamespace(stats_json=json.dumps(STATS), max_hp=0, shock_max=0, sheet_template_id=None)
    m = pc_maxima(pc)
    assert (m["hp"], m["shock"], m["pp"], m["mp"], m["native"]) == (22, 8, 12, 8, True)
    pc.max_hp, pc.shock_max = 30, 5
    m = pc_maxima(pc)
    assert (m["hp"], m["shock"]) == (30, 5), "an explicit max overrides the derived value"
    assert (m["pp"], m["mp"]) == (12, 8), "PP/MP are always stat-derived"


def test_custom_sheet_has_no_invented_maxima():
    pc = SimpleNamespace(stats_json="[]", max_hp=0, shock_max=0, sheet_template_id=7)
    m = pc_maxima(pc)
    assert (m["hp"], m["shock"], m["pp"], m["mp"], m["native"]) == (0, 0, 0, 0, False)
    pc.max_hp = 14
    assert pc_maxima(pc)["hp"] == 14, "a stored max still counts on a custom sheet"


def test_bad_data_is_tolerated():
    assert pc_maxima(SimpleNamespace(stats_json="nope", max_hp=0, shock_max=0, sheet_template_id=None))["hp"] == 10
    pc = SimpleNamespace(stats_json=json.dumps([{"id": "str", "value": "x"}, "junk", {"value": 3}, {"id": "dex", "value": "4"}]),
                         max_hp=None, shock_max=None, sheet_template_id=None)
    assert pc_maxima(pc)["pp"] == 4


def test_int_field_rejects_junk():
    assert int_field({"v": "5"}, "v") == 5 and int_field({}, "v", 3) == 3
    for bad in ("abc", None, [], {}, True):
        try:
            int_field({"v": bad}, "v")
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


# ── party vitals ─────────────────────────────────────────────────────────────

def test_party_vitals_use_the_derived_max_and_flag_down(client, seed):
    healthy = _auto_pc(seed)
    downed = _auto_pc(seed, current_hp=0)
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([healthy, downed])))
    _gm(client, seed)
    vitals = {v["id"]: v for v in client.get(f"/api/parties/{party}/vitals").json()["members"]}
    assert vitals[healthy]["max_hp"] == 22 and vitals[healthy]["down"] is False
    assert vitals[downed]["max_hp"] == 22 and vitals[downed]["down"] is True, "0 HP on auto-max must read as DOWN"


# ── combat ───────────────────────────────────────────────────────────────────

def test_combatant_and_candidate_carry_the_derived_max(client, seed):
    from app.routers.combat import pc_to_combatant
    pc = _auto_pc(seed)
    c = pc_to_combatant(_row(pc))
    assert (c["max_hp"], c["hp"], c["max_shock"]) == (22, 22, 8)
    from app.routers.combat import _candidates
    db = SessionLocal()
    try:
        payload = {p["id"]: p for p in _candidates(db, seed.world_a.id)[2]}
    finally:
        db.close()
    assert (payload[pc]["max_hp"], payload[pc]["hp"], payload[pc]["max_shock"]) == (22, 22, 8)


def test_combat_sync_does_not_clamp_auto_max_characters_to_zero(client, seed):
    from app.routers.combat import pc_to_combatant
    pc = _auto_pc(seed, current_hp=10)
    c = pc_to_combatant(_row(pc))
    c["hp"] = 15
    c["shock"] = 3
    cs = _add(CombatSession(world_id=seed.world_a.id, name="Brawl", combatants_json=json.dumps([c])))
    _gm(client, seed)
    r = client.post(f"/api/combat/{cs}/sync-characters")
    assert r.status_code == 200 and r.json()["synced"] == ["Auto"]
    row = _row(pc)
    assert row.current_hp == 15, "sync clamped an auto-max character's HP to 0"
    assert row.shock_current == 3, "sync clamped Shock to the raw (zero) column"


# ── quick-edit routes ────────────────────────────────────────────────────────

def test_shock_route_honours_the_derived_shock_max(client, seed):
    pc = _auto_pc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/characters/{pc}/shock", json={"action": "delta", "value": 5})
    assert r.status_code == 200 and r.json()["shock_current"] == 5
    r = client.post(f"/api/characters/{pc}/shock", json={"action": "delta", "value": 50})
    assert r.json()["shock_current"] == 8 and r.json()["shock_max"] == 8


def test_junk_numbers_are_400_not_500(client, seed):
    pc = _auto_pc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    for route in ("hp-async", "shock", "pp", "mp"):
        r = client.post(f"/api/characters/{pc}/{route}", json={"action": "set", "value": "abc"})
        assert r.status_code == 400, f"{route}: {r.status_code}"
    assert client.post(f"/api/characters/{pc}/xp", json={"delta": "lots"}).status_code == 400
    assert _row(pc).current_hp == 22


# ── party rest ───────────────────────────────────────────────────────────────

def test_party_rest_restores_derived_shock_and_skips_custom_sheets(client, seed):
    db = SessionLocal()
    try:
        custom = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        custom_id = custom.id
    finally:
        db.close()
    native = _auto_pc(seed, pp_current=0, mp_current=0)
    sheet = _add(PlayerCharacter(world_id=seed.world_a.id, name="Custom", sheet_template_id=custom_id,
                                 pp_current=2, mp_current=2, shock_current=1))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([native, sheet])))
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, native).shock_current = 0
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    r = client.post(f"/api/parties/{party}/rest")
    assert r.status_code == 200
    applied = {a["id"]: a for a in r.json()["applied"]}
    assert native in applied and sheet not in applied, "custom-sheet characters have no PP/MP/Shock to rest"
    row = _row(native)
    assert (row.pp_current, row.mp_current, row.shock_current) == (6, 4, 8)
    other = _row(sheet)
    assert (other.pp_current, other.mp_current, other.shock_current) == (2, 2, 1)


# ── creating a character ─────────────────────────────────────────────────────

def test_new_character_with_auto_hp_starts_at_full_health(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/characters/new", data={"name": "Fresh", "stats_json": json.dumps(STATS)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    db = SessionLocal()
    try:
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.name == "Fresh").first()
        assert pc.max_hp == 0 and pc.current_hp == 22, "auto-max character was created at 0 HP"
        assert pc.shock_current == 0
    finally:
        db.close()


# ── the character list ───────────────────────────────────────────────────────

def test_most_wounded_sort_uses_the_effective_max(client, seed):
    hurt = _add(PlayerCharacter(world_id=seed.world_a.id, name="Hurt", stats_json=json.dumps(STATS),
                                max_hp=0, current_hp=5))
    fine = _add(PlayerCharacter(world_id=seed.world_a.id, name="Fine", max_hp=20, current_hp=20))
    scratched = _add(PlayerCharacter(world_id=seed.world_a.id, name="Scratched", max_hp=20, current_hp=18))
    _gm(client, seed)
    html = client.get("/characters?sort=hp").text
    hp_boxes = re.findall(r"HP (\d+)/(\d+)", html)
    assert hp_boxes == [("5", "22"), ("18", "20"), ("20", "20")], \
        f"wounded-first order ignored the auto-derived max: {hp_boxes}"
