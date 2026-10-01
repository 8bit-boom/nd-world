"""Party loot: stable item ids, validated input, and "give to a character".

Loot actions used to address an item by its list INDEX, so two people acting on
stale lists (one claims while the GM removes) hit the wrong item. Every item now
carries a stable `lid`; index is still accepted for old clients. Junk numbers
no longer 500, and a new `give` action moves an item (or part of a stack) into a
member's equipment instead of leaving it in the stash forever.
"""
import json
import re

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _scene(seed, loot=None, **party_kw):
    mine = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Mine"))
    mate = _add(PlayerCharacter(world_id=seed.world_a.id, name="Mate"))
    outsider = _add(PlayerCharacter(world_id=seed.world_a.id, name="Outsider"))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([mine, mate]),
                       loot_json=json.dumps(loot or []), **party_kw))
    return mine, mate, outsider, party


def _set_loot(party, loot):
    db = SessionLocal()
    try:
        db.get(Party, party).loot_json = json.dumps(loot)
        db.commit()
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _open_member_edit(world):
    from app.models import World
    db = SessionLocal()
    try:
        db.get(World, world.id).section_access_json = json.dumps({"parties": {"player": "edit"}})
        db.commit()
    finally:
        db.close()


def _post(client, party, **body):
    return client.post(f"/api/parties/{party}/loot", json=body)


def _loot(party):
    db = SessionLocal()
    try:
        return json.loads(db.get(Party, party).loot_json)
    finally:
        db.close()


def _equipment(pc):
    db = SessionLocal()
    try:
        return json.loads(db.get(PlayerCharacter, pc).equipment_json or "[]")
    finally:
        db.close()


# ── stable ids ───────────────────────────────────────────────────────────────

def test_added_items_get_a_stable_lid(client, seed):
    _, _, _, party = _scene(seed)
    _gm(client, seed)
    loot = _post(client, party, action="add", name="Gem", qty=2, notes="shiny").json()["loot"]
    assert len(loot) == 1 and re.fullmatch(r"[0-9a-f]{8}", loot[0]["lid"])
    again = _post(client, party, action="add", name="Gem", qty=1).json()["loot"]
    assert len({i["lid"] for i in again}) == 2, "ids must be unique"


def test_legacy_items_without_lid_are_given_one_and_it_sticks(client, seed):
    _, _, _, party = _scene(seed, loot=[{"name": "Old", "qty": 1}, {"name": "Older", "qty": 3, "claimed_by": []}])
    _gm(client, seed)
    first = _post(client, party, action="add", name="New").json()["loot"]
    assert all(i.get("lid") for i in first)
    stored = {i["name"]: i["lid"] for i in _loot(party)}
    second = _post(client, party, action="add", name="Newer").json()["loot"]
    assert {i["name"]: i["lid"] for i in second if i["name"] in stored} == stored, "ids must not change between writes"


def test_remove_by_lid_hits_the_right_item_even_after_the_list_shifted(client, seed):
    _, _, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "A", "qty": 1, "claimed_by": []},
                                         {"lid": "bbbbbbbb", "name": "B", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    assert _post(client, party, action="remove", lid="aaaaaaaa").status_code == 200
    # a second client still holding the OLD list (B at index 1) removes B by id
    assert _post(client, party, action="remove", lid="bbbbbbbb").status_code == 200
    assert _loot(party) == []


def test_stale_lid_is_a_409_and_changes_nothing(client, seed):
    _, _, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "A", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    r = _post(client, party, action="remove", lid="deadbeef")
    assert r.status_code == 409
    assert [i["name"] for i in _loot(party)] == ["A"]


def test_index_still_works_for_old_clients(client, seed):
    mine, _, _, party = _scene(seed, loot=[{"name": "A", "qty": 1}, {"name": "B", "qty": 1}])
    _gm(client, seed)
    assert _post(client, party, action="claim", index=1, pc_id=mine).status_code == 200
    names = {i["name"]: i["claimed_by"] for i in _loot(party)}
    assert names == {"A": [], "B": [mine]}
    assert _post(client, party, action="remove", index=0).status_code == 200
    assert [i["name"] for i in _loot(party)] == ["B"]


def test_claim_and_unclaim_by_lid(client, seed):
    mine, mate, outsider, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "A", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    assert _post(client, party, action="claim", lid="aaaaaaaa", pc_id=mine).json()["loot"][0]["claimed_by"] == [mine]
    assert _post(client, party, action="claim", lid="aaaaaaaa", pc_id=mine).json()["loot"][0]["claimed_by"] == [mine]
    assert _post(client, party, action="unclaim", lid="aaaaaaaa", pc_id=mine).json()["loot"][0]["claimed_by"] == []
    assert _post(client, party, action="claim", lid="aaaaaaaa", pc_id=outsider).status_code == 400, "non-members can't claim"


# ── validation ───────────────────────────────────────────────────────────────

def test_junk_input_is_a_400_not_a_500(client, seed):
    mine, _, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "A", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    for body in ({"action": "add", "name": "X", "qty": "lots"}, {"action": "add", "name": "   "},
                 {"action": "add", "name": ["x"]}, {"action": "add", "name": "X", "qty": 0},
                 {"action": "add", "name": "X", "qty": 10 ** 9},
                 {"action": "remove", "index": "zero"}, {"action": "claim", "lid": "aaaaaaaa", "pc_id": "me"},
                 {"action": "nonsense"}, {"action": "give", "lid": "aaaaaaaa", "pc_id": mine, "qty": "two"}):
        r = _post(client, party, **body)
        assert r.status_code == 400, f"{body} -> {r.status_code}"
    assert client.post(f"/api/parties/{party}/loot", content=b"not json").status_code == 400
    assert [i["name"] for i in _loot(party)] == ["A"], "bad requests must not change the stash"


def test_name_and_notes_are_capped(client, seed):
    _, _, _, party = _scene(seed)
    _gm(client, seed)
    item = _post(client, party, action="add", name="N" * 500, notes="x" * 5000).json()["loot"][0]
    assert len(item["name"]) <= 120 and len(item["notes"]) <= 500


# ── give ─────────────────────────────────────────────────────────────────────

def test_give_moves_the_item_into_equipment(client, seed):
    mine, mate, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "Rope", "qty": 2, "notes": "50ft",
                                               "claimed_by": []},
                                              {"lid": "bbbbbbbb", "name": "Gem", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    r = _post(client, party, action="give", lid="aaaaaaaa", pc_id=mate)
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["loot"]] == ["Gem"], "a fully given item leaves the stash"
    eq = _equipment(mate)
    assert len(eq) == 1 and (eq[0]["name"], eq[0]["qty"], eq[0]["notes"]) == ("Rope", 2, "50ft")
    assert _equipment(mine) == []


def test_give_part_of_a_stack_and_merge_with_existing_gear(client, seed):
    mine, mate, _, party = _scene(seed)
    _set_loot(party, [{"lid": "aaaaaaaa", "name": "Rations", "qty": 5, "claimed_by": [mate]}])
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, mate).equipment_json = json.dumps([{"name": "rations", "qty": 1, "weight": 0.5}])
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    assert _post(client, party, action="give", lid="aaaaaaaa", pc_id=mate, qty=2).status_code == 200
    stash = _loot(party)
    assert stash[0]["qty"] == 3, "the remainder stays in the stash"
    eq = _equipment(mate)
    assert len(eq) == 1 and eq[0]["qty"] == 3 and eq[0]["weight"] == 0.5, "merged into the matching entry"
    assert _post(client, party, action="give", lid="aaaaaaaa", pc_id=mate, qty=99).status_code == 400, "can't give more than exists"
    assert _loot(party)[0]["qty"] == 3


def test_give_clears_claims_when_the_item_is_gone(client, seed):
    mine, mate, _, party = _scene(seed)
    _set_loot(party, [{"lid": "aaaaaaaa", "name": "Gem", "qty": 1, "claimed_by": [mine, mate]}])
    _gm(client, seed)
    _post(client, party, action="give", lid="aaaaaaaa", pc_id=mine)
    assert _loot(party) == [] and len(_equipment(mine)) == 1


def test_give_rejects_non_members_and_custom_sheets(client, seed):
    mine, mate, outsider, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "Gem", "qty": 1, "claimed_by": []}])
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first().id
        db.get(PlayerCharacter, mate).sheet_template_id = tpl
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    assert _post(client, party, action="give", lid="aaaaaaaa", pc_id=outsider).status_code == 400
    r = _post(client, party, action="give", lid="aaaaaaaa", pc_id=mate)
    assert r.status_code == 400 and "custom" in r.json()["detail"].lower()
    assert _loot(party)[0]["name"] == "Gem" and _equipment(outsider) == [] and _equipment(mate) == []


def test_member_can_take_for_their_own_character_only(client, seed):
    _open_member_edit(seed.world_a)
    mine, mate, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "Gem", "qty": 1, "claimed_by": []},
                                              {"lid": "bbbbbbbb", "name": "Map", "qty": 1, "claimed_by": []}])
    _player(client, seed)
    assert _post(client, party, action="give", lid="aaaaaaaa", pc_id=mate).status_code == 403
    assert _post(client, party, action="give", lid="aaaaaaaa", pc_id=mine).status_code == 200
    assert [i["name"] for i in _loot(party)] == ["Map"] and _equipment(mine)[0]["name"] == "Gem"
    assert _equipment(mate) == []


def test_give_notifies_live_sync(client, seed, monkeypatch):
    from app.routers import parties as parties_mod
    touched = []
    monkeypatch.setattr(parties_mod.live, "touch", lambda world_id: touched.append(world_id))
    mine, _, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "Gem", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    _post(client, party, action="give", lid="aaaaaaaa", pc_id=mine)
    assert touched == [seed.world_a.id]


# ── what the page ships ──────────────────────────────────────────────────────

def test_page_uses_lids_and_offers_give(client, seed):
    mine, _, _, party = _scene(seed, loot=[{"lid": "aaaaaaaa", "name": "Gem", "qty": 1, "claimed_by": []}])
    _gm(client, seed)
    html = client.get(f"/parties/{party}").text
    assert "aaaaaaaa" in html, "the loot JSON the page boots with must carry lids"
    assert "action: 'give'" in html or '"give"' in html
