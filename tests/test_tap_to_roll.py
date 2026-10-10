"""Tap-to-roll on the character sheet: stat boxes open a roll sheet, the roll (Stat + d10 + boost) goes into the shared
dice log with a label saying what it was for (app/routers/dice.py `label`)."""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import PLAYER_PASSWORD, login
from .test_character_hub import _as, _pc


def _stats(pc_id, stats):
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, pc_id).stats_json = json.dumps(stats)
        db.commit()
    finally:
        db.close()


def test_a_labelled_roll_is_logged_for_the_table(client, seed):
    _as(client, seed.player_a)
    r = client.post("/api/dice/roll", json={"notation": "1d10+8+2", "label": "Intellect check — Mirabel"})
    assert r.status_code == 200
    d = r.json()
    assert d["label"] == "Intellect check — Mirabel" and d["breakdown"][0]["rolls"][0] in range(1, 11)
    assert d["total"] == d["breakdown"][0]["rolls"][0] + 10
    hist = client.get("/api/dice/history").json()["rolls"]
    assert hist[0]["label"] == "Intellect check — Mirabel"
    assert "Intellect check" in client.get("/dice").text


def test_label_is_cleaned_and_optional(client, seed):
    _as(client, seed.player_a)
    d = client.post("/api/dice/roll", json={"notation": "1d6", "label": "a\x00b\n" + "x" * 200}).json()
    assert "\x00" not in d["label"] and "\n" not in d["label"] and len(d["label"]) <= 80
    assert client.post("/api/dice/roll", json={"notation": "1d6"}).json()["label"] == ""


def test_the_owner_gets_tappable_stats_named_even_when_saved_bare(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _stats(pc, [{"id": "str", "value": 7}, {"id": "int", "value": 4}])    # {id, value} only, like an AI-drafted sheet
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert 'data-roll-label="Strength"' in html and 'data-roll-val="7"' in html
    assert 'data-roll-label="Intellect"' in html
    assert 'id="rs"' in html and 'id="rs-go"' in html


def test_someone_who_cannot_manage_the_sheet_cannot_roll_from_it(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _stats(pc, [{"id": "str", "value": 7}])
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc}")
    assert 'data-roll-label' not in r.text


# ── inventory: use one, weights, carry limit, coin purse ─────────────────────

def _gear(pc_id, equipment=None, currency=None):
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc_id)
        if equipment is not None:
            row.equipment_json = json.dumps(equipment)
        if currency is not None:
            row.currency_json = json.dumps(currency)
        db.commit()
    finally:
        db.close()


def test_use_one_and_set_weight_and_the_carry_bar(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _stats(pc, [{"id": "str", "value": 3}, {"id": "bod", "value": 2}])           # default limit 5 x (3+2) = 25
    _gear(pc, equipment=[{"name": "Stim", "qty": 2, "weight": 0.5}, {"name": "Rifle", "qty": 1, "weight": 0}])
    _as(client, seed.player_a)
    r = client.post(f"/api/characters/{pc}/equipment", json={"action": "qty", "index": 0, "delta": -1})
    assert r.status_code == 200 and r.json()["equipment"][0]["qty"] == 1
    assert client.post(f"/api/characters/{pc}/equipment", json={"action": "qty", "index": 0, "delta": -9}).json()["equipment"][0]["qty"] == 0
    r = client.post(f"/api/characters/{pc}/equipment", json={"action": "weight", "index": 1, "value": 30}).json()
    assert r["carry"] == {"used": 30.0, "limit": 25, "default": True, "over": True}
    r = client.post(f"/api/characters/{pc}/carry-limit", json={"value": 40}).json()
    assert r["carry"]["limit"] == 40 and r["carry"]["default"] is False and r["carry"]["over"] is False
    assert client.post(f"/api/characters/{pc}/carry-limit", json={"value": 0}).json()["carry"]["default"] is True
    assert client.post(f"/api/characters/{pc}/equipment", json={"action": "qty", "index": 7, "delta": 1}).status_code == 400


def test_coin_purse_delta_set_and_never_negative(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _gear(pc, currency=[{"abbr": "CR", "label": "Credits", "value": 100}, {"abbr": "EB", "label": "Eurobucks", "value": 5}])
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{pc}/currency", json={"abbr": "cr", "action": "delta", "value": -30}).json()["value"] == 70
    assert client.post(f"/api/characters/{pc}/currency", json={"abbr": "Credits", "action": "delta", "value": -500}).json()["value"] == 0
    assert client.post(f"/api/characters/{pc}/currency", json={"abbr": "EB", "action": "set", "value": 42}).json()["value"] == 42
    assert client.post(f"/api/characters/{pc}/currency", json={"abbr": "XX", "action": "delta", "value": 1}).status_code == 404
    client.cookies.clear()
    _as(client, seed.player_b)
    assert client.post(f"/api/characters/{pc}/currency", json={"abbr": "CR", "action": "delta", "value": 1}).status_code == 403
    assert client.post(f"/api/characters/{pc}/carry-limit", json={"value": 9}).status_code == 403


def test_the_gear_page_has_the_steppers_and_the_bar(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _gear(pc, currency=[{"abbr": "CR", "label": "Credits", "value": 7}])
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert 'id="carry-box"' in html and "coinDelta(this, 1)" in html and "function eqQty" in html
