"""Tap-to-roll for the four rulebooks (app/pool_roll.py, POST /api/characters/{id}/roll, the `dice` rules of a system):
Neon & Dragons / Chronicles of the Worm roll Stat + d10 (+ up to 2 PP/MP from the matching pool, advantage/disadvantage);
Hunt in the Moonlight and Asterion roll a d10 success pool (6+ succeeds, a 10 explodes)."""
import json
import random

import pytest

from app import pool_roll, sheet_systems
from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .test_character_hub import _add, _as, _pc


class Seq:
    """A scripted random source: hands out the given d10 faces in order."""
    def __init__(self, *faces):
        self.faces = list(faces)

    def randint(self, lo, hi):
        return self.faces.pop(0)


# ── the engines ──────────────────────────────────────────────────────────────

def test_stat_check_crit_fail_and_advantage():
    assert pool_roll.roll_check(5, 2, "normal", Seq(10)) == {"rolls": [10], "die": 10, "total": 17, "crit": True, "fail": False}
    r = pool_roll.roll_check(5, 0, "normal", Seq(1))
    assert r["fail"] is True and r["total"] == 6
    assert pool_roll.roll_check(4, 0, "adv", Seq(3, 8))["die"] == 8
    assert pool_roll.roll_check(4, 0, "dis", Seq(3, 8))["die"] == 3
    assert pool_roll.stat_pool("str") == "pp" and pool_roll.stat_pool("itu") == "mp" and pool_roll.stat_pool("xyz") is None


def test_pool_counts_6_plus_and_a_10_explodes_again():
    r = pool_roll.roll_pool(3, 6, True, Seq(3, 7, 10, 10, 2))      # the 10 adds a die (10), which adds another (2)
    assert [d["v"] for d in r["dice"]] == [3, 7, 10, 10, 2]
    assert r["successes"] == 3 and [d["extra"] for d in r["dice"]] == [False, False, False, True, True]
    assert pool_roll.roll_pool(2, 6, False, Seq(10, 6))["successes"] == 2 and len(pool_roll.roll_pool(2, 6, False, Seq(10, 6))["dice"]) == 2
    assert pool_roll.roll_pool(0, 6, True, Seq(9))["n"] == 1                     # never below one die


def test_a_runaway_chain_of_tens_is_cut():
    class Tens:
        def randint(self, lo, hi):
            return 10
    assert len(pool_roll.roll_pool(1, 6, True, Tens())["dice"]) <= 1 + pool_roll.MAX_EXTRA_DICE


# ── the rulebooks' dice rules ────────────────────────────────────────────────

def test_builtin_systems_carry_their_rulebooks_dice():
    hunt, ast = sheet_systems.BUILTIN_SYSTEMS["hunt-in-the-moonlight"]["dice"], sheet_systems.BUILTIN_SYSTEMS["asterion"]["dice"]
    assert [p["dice"] for p in hunt["pools"]] == [2, 3] and hunt["spend"]["field"] == "stamina" and hunt["spend"]["every"] == 3
    assert [p["dice"] for p in ast["pools"]] == [2, 3] and ast["spend"]["field"] == "ichor"
    _p, god = sheet_systems.pool_dice(ast, "normal", {"kind": "God (Origin)"})
    _p, mortal = sheet_systems.pool_dice(ast, "normal", {"kind": "Mortal"})
    _p, domain = sheet_systems.pool_dice(ast, "domain", {"kind": "Mortal"})
    assert (god, mortal, domain) == (2, 1, 3)


def test_a_gms_own_template_can_declare_dice_and_junk_is_dropped():
    fields = [{"id": "grit", "type": "resource"}, {"id": "mood", "type": "select", "options": ["a"]}, {"id": "n", "type": "number"}]
    spec, warns = sheet_systems.clean_system_spec({"dice": {
        "threshold": 5, "explode": False,
        "pools": [{"label": "Normal", "dice": 2}, {"label": "", "dice": 3}, {"label": "Huge", "dice": 99}],
        "spend": {"field": "grit", "label": "Grit", "per": 1, "tally": "n", "every": 3, "into": "mood"}}}, fields)
    d = spec["dice"]
    assert d["threshold"] == 5 and d["explode"] is False and [p["label"] for p in d["pools"]] == ["Normal"]
    assert d["spend"]["field"] == "grit" and d["spend"]["tally"] == "n" and d["spend"]["into"] == "mood"
    spec, warns = sheet_systems.clean_system_spec({"dice": {"pools": [{"label": "x", "dice": 2}], "spend": {"field": "mood"}}}, fields)
    assert "spend" not in spec["dice"] and any("spend" in w for w in warns)
    assert "dice" not in sheet_systems.clean_system_spec({"dice": {"pools": []}}, fields)[0]


# ── the route: stat checks ───────────────────────────────────────────────────

def _native(seed, **extra):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc)
        row.stats_json = json.dumps([{"id": k, "value": v} for k, v in
                                     dict(str=4, dex=3, bod=2, per=1, wil=5, int=6, cha=1, itu=2).items()])
        row.pp_current, row.mp_current = 3, 1
        for k, v in extra.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()
    return pc


def test_stat_check_spends_from_the_matching_pool_and_logs(client, seed, monkeypatch):
    pc = _native(seed)
    monkeypatch.setattr(pool_roll.random, "randint", lambda a, b: 7)
    _as(client, seed.player_a)
    r = client.post(f"/api/characters/{pc}/roll", json={"kind": "stat", "stat": "str", "boost": 2}).json()
    assert r["total"] == 4 + 7 + 2 and r["spent"] == {"pool": "pp", "amount": 2, "current": 1, "max": r["spent"]["max"]} and r["shared"] is True
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc)
        assert row.pp_current == 1 and row.mp_current == 1               # physical stat paid from PP only
    finally:
        db.close()
    hist = client.get("/api/dice/history").json()["rolls"][0]
    assert hist["label"].startswith("Strength check") and hist["total"] == 13
    # a mental stat is paid from MP: only 1 MP left, so +2 is refused
    assert client.post(f"/api/characters/{pc}/roll", json={"kind": "stat", "stat": "int", "boost": 2}).status_code == 400
    assert client.post(f"/api/characters/{pc}/roll", json={"kind": "stat", "stat": "int", "boost": 1}).json()["total"] == 6 + 7 + 1
    assert client.post(f"/api/characters/{pc}/roll", json={"kind": "stat", "stat": "nope"}).status_code == 400


def test_advantage_rolls_two_dice_and_a_natural_10_is_critical(client, seed, monkeypatch):
    pc = _native(seed)
    faces = iter([2, 10])
    monkeypatch.setattr(pool_roll.random, "randint", lambda a, b: next(faces))
    _as(client, seed.player_a)
    r = client.post(f"/api/characters/{pc}/roll", json={"kind": "stat", "stat": "dex", "mode": "adv"}).json()
    assert r["rolls"] == [2, 10] and r["die"] == 10 and r["crit"] is True and r["total"] == 13


def test_rolling_is_the_managers_alone(client, seed):
    pc = _native(seed)
    _as(client, seed.player_b)
    assert client.post(f"/api/characters/{pc}/roll", json={"kind": "stat", "stat": "str"}).status_code in (403, 404)


# ── the route: success pools ─────────────────────────────────────────────────

def _pool_pc(seed, **cf):
    fields = [
        {"id": "kind", "type": "select", "options": ["God (Origin)", "Mortal"], "default_value": "God (Origin)"},
        {"id": "stamina", "type": "resource", "default_value": "5/5"},
        {"id": "staminaSpent", "type": "number", "default_value": "0"},
        {"id": "strain", "type": "select", "options": ["0", "1", "2", "3"], "default_value": "0"},
    ]
    dice = {"threshold": 6, "explode": True,
            "pools": [{"label": "Normal", "dice": 2, "override": {"field": "kind", "equals": "Mortal", "dice": 1}},
                      {"label": "Ability", "dice": 3}],
            "spend": {"field": "stamina", "label": "Stamina", "per": 1, "tally": "staminaSpent", "every": 3, "into": "strain"}}
    tpl = _add(SheetTemplate(world_id=seed.world_a.id, name="Pools", slug="pools-roll-" + str(random.random())[2:8], sheet_mode="custom",
                             fields_json=json.dumps(fields),
                             system_json=json.dumps(sheet_systems.clean_system_spec({"dice": dice}, fields)[0])))   # stored cleaned, as the editor does
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Rook", sheet_template_id=tpl,
                             custom_fields_json=json.dumps(cf))
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _cf(pc_id):
    db = SessionLocal()
    try:
        return json.loads(db.get(PlayerCharacter, pc_id).custom_fields_json)
    finally:
        db.close()


def test_pool_roll_spends_stamina_tallies_and_marks_strain(client, seed, monkeypatch):
    pc = _pool_pc(seed)
    monkeypatch.setattr(pool_roll.random, "randint", lambda a, b: 8)       # every die succeeds
    _as(client, seed.player_a)
    r = client.post(f"/api/characters/{pc}/roll", json={"kind": "pool", "pool": "p0", "spend": 3, "mod": 1}).json()
    assert r["n"] == 2 + 3 + 1 and r["successes"] == 6 and r["shared"] is True
    cf = _cf(pc)
    assert cf["stamina_current"] == 2 and cf["staminaSpent"] == 3 and cf["strain"] == "1"           # 3 spent = 1 Strain
    assert r["fields"]["strain"] == "1"
    assert client.post(f"/api/characters/{pc}/roll", json={"kind": "pool", "pool": "p0", "spend": 3}).status_code == 400   # only 2 left
    r = client.post(f"/api/characters/{pc}/roll", json={"kind": "pool", "pool": "p1", "spend": 2, "need": 3}).json()
    cf = _cf(pc)
    assert cf["staminaSpent"] == 5 and cf["strain"] == "1" and r["ok"] is True and r["need"] == 3
    hist = client.get("/api/dice/history").json()["rolls"][0]
    assert hist["label"].startswith("Ability") and hist["total"] == r["successes"]


def test_a_mortal_rolls_one_die_where_a_god_rolls_two(client, seed, monkeypatch):
    monkeypatch.setattr(pool_roll.random, "randint", lambda a, b: 2)
    _as(client, seed.player_a)
    god = _pool_pc(seed, kind="God (Origin)")
    assert client.post(f"/api/characters/{god}/roll", json={"kind": "pool", "pool": "p0"}).json()["n"] == 2
    mortal = _pool_pc(seed, kind="Mortal")
    assert client.post(f"/api/characters/{mortal}/roll", json={"kind": "pool", "pool": "p0"}).json()["n"] == 1


def test_situation_dice_never_shrink_a_pool_below_one(client, seed, monkeypatch):
    pc = _pool_pc(seed, kind="Mortal")
    monkeypatch.setattr(pool_roll.random, "randint", lambda a, b: 2)
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{pc}/roll", json={"kind": "pool", "pool": "p0", "mod": -3}).json()["n"] == 1


def test_a_system_without_dice_rules_and_a_native_sheet_refuse_the_other_kind(client, seed):
    native = _native(seed)
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{native}/roll", json={"kind": "pool", "pool": "p0"}).status_code == 400
    plain = _add(SheetTemplate(world_id=seed.world_a.id, name="Plain", slug="plain-roll", sheet_mode="custom", fields_json="[]"))
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="P", sheet_template_id=plain)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
    finally:
        db.close()
    assert client.post(f"/api/characters/{pc_id}/roll", json={"kind": "pool", "pool": "p0"}).status_code == 400
    assert client.post(f"/api/characters/{pc_id}/roll", json={"kind": "stat", "stat": "str"}).status_code == 400


def test_the_roll_sheet_is_on_both_kinds_of_page(client, seed):
    native = _native(seed)
    pool = _pool_pc(seed)
    _as(client, seed.player_a)
    for pc_id in (native, pool):
        html = client.get(f"/characters/{pc_id}").text
        assert 'id="rs"' in html and "window.ndOpenRoll" in html
    assert 'onclick="ndOpenRoll()"' in client.get(f"/characters/{pool}").text
