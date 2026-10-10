"""Ability cards: feats with a cost read from their text, a Use button, and once-per-Rest / session memory."""
import json

from app import feat_use
from app.database import SessionLocal
from app.models import PlayerCharacter

from .test_character_hub import _as, _pc


def test_costs_and_limits_are_read_from_the_text():
    assert feat_use.parse_cost("Cost: 2 MP — Action\nYou do a thing.") == {"mp": 2}
    assert feat_use.parse_cost("**Cost:** MP. **Action.** more") == {"mp": 1}
    assert feat_use.parse_cost("Cost: 3 MP + 3 Shock") == {"mp": 3, "shock": 3}
    assert feat_use.parse_cost("Cost: PP, Health — Action") == {"pp": 1, "hp": 1}
    assert feat_use.parse_cost("No cost here, but it uses 5 PP in the text") == {}
    assert feat_use.parse_cost("Cost: none") == {}
    assert feat_use.parse_limit("Once per session, you can do it") == "session"
    assert feat_use.parse_limit("once per Rest") == "rest"
    assert feat_use.parse_limit("whenever") is None


def _hero(seed, feats, pp=3, mp=3):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    db = SessionLocal()
    try:
        r = db.get(PlayerCharacter, pc)
        r.feats_json = json.dumps(feats)
        r.stats_json = json.dumps([{"id": k, "value": 4} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")])
        r.pp_current, r.mp_current, r.shock_current, r.current_hp, r.max_hp = pp, mp, 4, 9, 12
        db.commit()
    finally:
        db.close()
    return pc


def _first_costly_catalog_feat(limit=None):
    from app import game_catalog
    for f in game_catalog.catalog_payload()["feats"]:
        c = feat_use.parse_cost(f["description"])
        if c and "hp" not in c and (feat_use.parse_limit(f["description"]) == limit):
            return f, c
    raise AssertionError("no such feat in the catalog")


def test_use_spends_logs_and_can_be_undone(client, seed):
    feat, cost = _first_costly_catalog_feat()
    pc = _hero(seed, [{"name": feat["name"], "id": feat["id"], "type": "Race", "rank": "Rank 1"}])
    _as(client, seed.player_a)
    cards = client.get(f"/api/characters/{pc}/ability-cards").json()
    card = cards["cards"][0]
    assert card["name"] == feat["name"] and card["cost"] == cost and card["description"]
    pay = {"index": 0, "pp": cost.get("pp", 0), "mp": cost.get("mp", 0), "shock": cost.get("shock", 0)}
    r = client.post(f"/api/characters/{pc}/ability-use", json=pay).json()
    assert r["pools"]["pp"] == 3 - pay["pp"] and r["pools"]["mp"] == 3 - pay["mp"]
    e = client.get(f"/api/characters/{pc}/hub/log").json()["entries"][0]
    assert e["text"].startswith("Used " + feat["name"]) and e["can_undo"]
    assert client.post(f"/api/characters/{pc}/hub/log/{e['id']}/undo").status_code == 200
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc)
        assert (row.pp_current, row.mp_current) == (3, 3)
    finally:
        db.close()


def test_not_enough_points_is_refused_and_nothing_is_spent(client, seed):
    feat, _cost = _first_costly_catalog_feat()
    pc = _hero(seed, [{"name": feat["name"], "id": feat["id"]}], pp=1, mp=1)
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{pc}/ability-use", json={"index": 0, "pp": 1, "mp": 5}).status_code == 400
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc)
        assert (row.pp_current, row.mp_current) == (1, 1)
    finally:
        db.close()
    assert client.post(f"/api/characters/{pc}/ability-use", json={"index": 7}).status_code == 404


def test_once_per_rest_is_used_up_until_a_rest(client, seed):
    feat = {"name": "Home-made trick", "type": "Common", "rank": "Rank 1", "notes": "Cost: 1 PP — once per rest, you can do it"}
    pc = _hero(seed, [feat])
    _as(client, seed.player_a)
    card = client.get(f"/api/characters/{pc}/ability-cards").json()["cards"][0]
    assert card["limit"] == "rest" and card["cost"] == {"pp": 1}
    assert client.post(f"/api/characters/{pc}/ability-use", json={"index": 0, "pp": 1}).status_code == 200
    assert client.post(f"/api/characters/{pc}/ability-use", json={"index": 0, "pp": 1}).status_code == 409
    assert client.post(f"/api/characters/{pc}/hub/rest", json={"kind": "long"}).status_code == 200
    assert client.get(f"/api/characters/{pc}/ability-cards").json()["cards"][0]["used"] is None
    assert client.post(f"/api/characters/{pc}/ability-use", json={"index": 0, "pp": 1}).status_code == 200
    r = client.post(f"/api/characters/{pc}/ability-ready", json={"index": 0}).json()
    assert r["cards"][0]["used"] is None


def test_cards_are_the_managers_alone(client, seed):
    pc = _hero(seed, [{"name": "X"}])
    _as(client, seed.player_b)
    assert client.get(f"/api/characters/{pc}/ability-cards").status_code == 403
    assert client.post(f"/api/characters/{pc}/ability-use", json={"index": 0}).status_code == 403
