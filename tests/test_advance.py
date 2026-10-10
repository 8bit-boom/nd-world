"""Spending XP by the Player's Guide's costs (app/advance.py, /api/characters/{id}/advance): stat = new rank x 2, Race /
Profession feat = rank x 4, Common = rank x 3, only unlocked Ranks, only your own race / profession; undoable from the log."""
import json

from app import advance
from app.database import SessionLocal
from app.models import PlayerCharacter

from .test_character_hub import _as, _pc


def test_costs_and_rank_unlocking():
    assert advance.stat_cost(0) == 2 and advance.stat_cost(5) == 12
    assert advance.feat_cost("Race", 2) == 8 and advance.feat_cost("Profession", 1) == 4 and advance.feat_cost("Common", 3) == 9
    assert advance.unlocked_rank([]) == 1
    assert advance.unlocked_rank([{"rank": "Rank 1"}]) == 1
    assert advance.unlocked_rank([{"rank": "Rank 1"}, {"rank": "Rank 1"}]) == 2
    assert advance.unlocked_rank([{"rank": "Rank 1"}] * 2 + [{"rank": "Rank 2"}] * 2) == 3
    assert advance.unlocked_rank([{"rank": "Rank 1"}] * 9) == 2            # one Rank at a time
    assert advance.unlocked_rank([{"rank": "Rank 3"}] * 4) == 1


def _hero(seed, xp=20, race_id="elf", prof_id="charlatan", feats=None):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    db = SessionLocal()
    try:
        r = db.get(PlayerCharacter, pc)
        r.xp, r.race_id, r.profession_id = xp, race_id, prof_id
        r.stats_json = json.dumps([{"id": "str", "label": "Strength", "abbr": "STR", "value": 4}])
        r.feats_json = json.dumps(feats or [])
        db.commit()
    finally:
        db.close()
    return pc


def test_buying_a_stat_and_what_it_leaves(client, seed):
    pc = _hero(seed, xp=20)
    _as(client, seed.player_a)
    st = client.get(f"/api/characters/{pc}/advance").json()
    assert st["available"] == 20 and {r["id"]: r["cost"] for r in st["stats"]}["str"] == 10
    r = client.post(f"/api/characters/{pc}/advance", json={"kind": "stat", "stat": "str"})
    assert r.status_code == 200 and r.json()["spent"] == 10 and r.json()["available"] == 10
    db = SessionLocal()
    try:
        stats = {s["id"]: s["value"] for s in json.loads(db.get(PlayerCharacter, pc).stats_json)}
    finally:
        db.close()
    assert stats["str"] == 5
    assert client.post(f"/api/characters/{pc}/advance", json={"kind": "stat", "stat": "str"}).status_code == 400    # 12 > 10 left
    assert client.post(f"/api/characters/{pc}/advance", json={"kind": "stat", "stat": "zzz"}).status_code == 400


def test_feats_are_own_race_and_profession_ranked_and_priced(client, seed):
    pc = _hero(seed, xp=100)
    _as(client, seed.player_a)
    feats = client.get(f"/api/characters/{pc}/advance").json()["feats"]
    assert feats and {f["category"] for f in feats} <= {"Race", "Profession", "Common"}
    from app import game_catalog
    cat = {f["id"]: f for f in game_catalog.catalog_payload()["feats"]}
    for f in feats:
        assert (cat[f["id"]].get("associatedRace") in ("", "elf")) if f["category"] == "Race" else True
        assert f["cost"] == advance.feat_cost(f["category"], f["rank"])
    rank1 = next(f for f in feats if f["category"] == "Race" and f["rank"] == 1)
    rank2 = next(f for f in feats if f["rank"] == 2)
    assert rank2["can"] is False and "unlocks" in rank2["why"]
    assert client.post(f"/api/characters/{pc}/advance", json={"kind": "feat", "id": rank2["id"]}).status_code == 400
    r = client.post(f"/api/characters/{pc}/advance", json={"kind": "feat", "id": rank1["id"]})
    assert r.status_code == 200 and r.json()["spent"] == 4
    assert all(f["id"] != rank1["id"] for f in r.json()["feats"])                     # already owned: gone from the list
    second = next(f for f in r.json()["feats"] if f["category"] == "Race" and f["rank"] == 1)
    r = client.post(f"/api/characters/{pc}/advance", json={"kind": "feat", "id": second["id"]}).json()
    assert r["unlocked_rank"] == 2                                                     # two Rank 1 feats unlock Rank 2


def test_undo_from_the_log_and_the_spent_override(client, seed):
    pc = _hero(seed, xp=20)
    _as(client, seed.player_a)
    client.post(f"/api/characters/{pc}/advance", json={"kind": "stat", "stat": "str"})
    e = client.get(f"/api/characters/{pc}/hub/log").json()["entries"][0]
    assert e["text"].startswith("Bought Strength 4") and e["can_undo"]
    assert client.post(f"/api/characters/{pc}/hub/log/{e['id']}/undo").status_code == 200
    st = client.get(f"/api/characters/{pc}/advance").json()
    assert st["spent"] == 0 and {r["id"]: r["value"] for r in st["stats"]}["str"] == 4
    assert client.post(f"/api/characters/{pc}/advance", json={"kind": "spent", "value": 6}).json()["available"] == 14


def test_advancement_is_the_managers_and_native_only(client, seed):
    pc = _hero(seed)
    _as(client, seed.player_b)
    assert client.get(f"/api/characters/{pc}/advance").status_code == 403
    assert client.post(f"/api/characters/{pc}/advance", json={"kind": "stat", "stat": "str"}).status_code == 403
    client.cookies.clear()
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert "ndOpenAdvance()" in html and 'id="adv"' in html
