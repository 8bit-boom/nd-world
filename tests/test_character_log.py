"""The character change log (app/char_extras.py, hub/log): every quick edit is recorded with who made it, and the NEWEST
change to each number can be undone."""
import json

from app.database import SessionLocal
from app.models import CharacterLog, PlayerCharacter

from .test_character_hub import _as, _pc
from .test_character_hub_now import _custom_pc


def _row(pc_id):
    db = SessionLocal()
    try:
        r = db.get(PlayerCharacter, pc_id)
        return {"hp": r.current_hp, "xp": r.xp, "level": r.level, "cond": json.loads(r.conditions_json or "[]"), "pp": r.pp_current}
    finally:
        db.close()


def test_edits_are_logged_and_the_newest_one_undoes(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    db = SessionLocal()
    try:
        r = db.get(PlayerCharacter, pc)
        r.current_hp, r.max_hp = 12, 20
        db.commit()
    finally:
        db.close()
    _as(client, seed.player_a)
    client.post(f"/api/characters/{pc}/hp-async", json={"action": "delta", "value": -4})      # 12 -> 8
    client.post(f"/api/characters/{pc}/hp-async", json={"action": "delta", "value": -3})      # 8 -> 5
    client.post(f"/api/characters/{pc}/xp", json={"delta": 100})
    client.post(f"/api/characters/{pc}/conditions", json={"action": "add", "name": "Burning"})
    d = client.get(f"/api/characters/{pc}/hub/log").json()
    texts = [e["text"] for e in d["entries"]]
    assert texts[0] == "+Burning" and "XP 0 → 100" in texts and "HP 8 → 5" in texts and "HP 12 → 8" in texts
    assert d["can_edit"] is True and all(e["actor"] for e in d["entries"])
    by_text = {e["text"]: e for e in d["entries"]}
    assert by_text["HP 8 → 5"]["can_undo"] is True and by_text["HP 12 → 8"]["can_undo"] is False   # a later HP change exists
    # an old one is refused, the newest works, then the older one becomes available
    assert client.post(f"/api/characters/{pc}/hub/log/{by_text['HP 12 → 8']['id']}/undo").status_code == 409
    assert client.post(f"/api/characters/{pc}/hub/log/{by_text['HP 8 → 5']['id']}/undo").status_code == 200
    assert _row(pc)["hp"] == 8
    d = client.get(f"/api/characters/{pc}/hub/log").json()
    assert {e["text"]: e for e in d["entries"]}["HP 12 → 8"]["can_undo"] is True
    assert client.post(f"/api/characters/{pc}/hub/log/{by_text['+Burning']['id']}/undo").status_code == 200
    assert _row(pc)["cond"] == []


def test_a_no_op_is_not_logged_and_the_log_is_the_owners_to_undo(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _as(client, seed.player_a)
    client.post(f"/api/characters/{pc}/xp", json={"delta": 0})
    assert client.get(f"/api/characters/{pc}/hub/log").json()["entries"] == []
    client.post(f"/api/characters/{pc}/xp", json={"delta": 50})
    eid = client.get(f"/api/characters/{pc}/hub/log").json()["entries"][0]["id"]
    client.cookies.clear()
    _as(client, seed.player_b)
    assert client.get(f"/api/characters/{pc}/hub/log").status_code == 404
    assert client.post(f"/api/characters/{pc}/hub/log/{eid}/undo").status_code == 404


def test_custom_tracks_coins_and_equipment_undo(client, seed):
    pc = _custom_pc(seed)
    _as(client, seed.player_a)
    client.post(f"/api/characters/{pc}/resource", json={"field_id": "health", "action": "delta", "value": -2})
    e = client.get(f"/api/characters/{pc}/hub/log").json()["entries"][0]
    assert e["text"] == "Health 5 → 3" and e["can_undo"]
    assert client.post(f"/api/characters/{pc}/hub/log/{e['id']}/undo").status_code == 200
    assert client.get(f"/api/characters/{pc}/hub/now").json()["me"]["hp"] == 5
    other = _pc(seed.player_a, seed.world_a, "Rich")
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, other).currency_json = json.dumps([{"abbr": "CR", "value": 10}])
        db.commit()
    finally:
        db.close()
    client.post(f"/api/characters/{other}/currency", json={"abbr": "CR", "action": "delta", "value": 5})
    client.post(f"/api/characters/{other}/equipment", json={"action": "add", "item": {"name": "Stim", "qty": 2}})
    log = client.get(f"/api/characters/{other}/hub/log").json()["entries"]
    assert log[0]["text"].startswith("Equipment") and log[1]["text"] == "CR 10 → 15"
    assert client.post(f"/api/characters/{other}/hub/log/{log[0]['id']}/undo").status_code == 200
    assert client.post(f"/api/characters/{other}/hub/log/{log[1]['id']}/undo").status_code == 200
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, other)
        assert json.loads(row.equipment_json or "[]") == [] and json.loads(row.currency_json)[0]["value"] == 10
    finally:
        db.close()


def test_the_log_keeps_the_newest_200_and_goes_with_the_character(client, seed):
    from app import char_extras
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    db = SessionLocal()
    try:
        row = db.get(PlayerCharacter, pc)
        for i in range(char_extras.LOG_KEEP + 15):
            char_extras.log(db, row, "x", "xp", f"XP {i}")
        db.commit()
        assert db.query(CharacterLog).filter(CharacterLog.character_id == pc).count() == char_extras.LOG_KEEP
        char_extras.delete_for_character(db, pc)
        db.commit()
        assert db.query(CharacterLog).filter(CharacterLog.character_id == pc).count() == 0
    finally:
        db.close()
