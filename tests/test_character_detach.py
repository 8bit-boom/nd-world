"""Deleting or retiring a character must not leave dangling references.

Party membership is a JSON id list, loot claims are ids inside loot_json and
CalendarEvent.character_id is a plain FK column — nothing cascades. Without
detach_pc a deleted character stayed in its party's roster (counted, handed
XP and rest), stayed on loot as a claimant and stayed attached to calendar
events.
"""
import json

from app.database import SessionLocal
from app.models import CalendarEvent, Party, PlayerCharacter
from app.party_refs import detach_pc, member_ids, parties_for_pc

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


def _scene(seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Leaver"))
    mate = _add(PlayerCharacter(world_id=seed.world_a.id, name="Stayer"))
    loot = [{"name": "Gem", "qty": 1, "claimed_by": [pc, mate]}, {"name": "Rope", "qty": 2, "claimed_by": [pc]},
            {"name": "Map", "qty": 1, "claimed_by": []}]
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc, mate]),
                       loot_json=json.dumps(loot)))
    # a party in ANOTHER world that happens to reuse the id must not be touched
    other = _add(Party(world_id=seed.world_b.id, name="Elsewhere", member_pc_ids_json=json.dumps([pc])))
    event = _add(CalendarEvent(world_id=seed.world_a.id, day=3, title="Duel", character_id=pc))
    return pc, mate, party, other, event


def _party(pid):
    db = SessionLocal()
    try:
        p = db.get(Party, pid)
        return json.loads(p.member_pc_ids_json), json.loads(p.loot_json)
    finally:
        db.close()


def _event_character(eid):
    db = SessionLocal()
    try:
        return db.get(CalendarEvent, eid).character_id
    finally:
        db.close()


def _assert_clean(pc, mate, party, other, event):
    members, loot = _party(party)
    assert members == [mate], "deleted character is still a party member"
    claims = {i["name"]: i["claimed_by"] for i in loot}
    assert claims == {"Gem": [mate], "Rope": [], "Map": []}, "loot kept a claim by a character that no longer exists"
    assert [i["name"] for i in loot] == ["Gem", "Rope", "Map"], "loot items themselves must survive"
    assert _party(other)[0] == [pc], "another world's party was modified"
    assert _event_character(event) is None, "calendar event still points at the deleted character"


def test_delete_detaches_everywhere(client, seed):
    ids = _scene(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/characters/{ids[0]}/delete", follow_redirects=False)
    assert r.status_code == 303
    _assert_clean(*ids)


def test_retire_to_npc_detaches_and_notifies(client, seed, monkeypatch):
    from app.routers import characters as chars
    touched = []
    monkeypatch.setattr(chars.live, "touch", lambda world_id: touched.append(world_id))
    ids = _scene(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/characters/{ids[0]}/retire-to-npc", follow_redirects=False)
    assert r.status_code == 303
    _assert_clean(*ids)
    assert touched == [seed.world_a.id]


def test_party_page_has_no_phantom_member_after_delete(client, seed):
    pc, mate, party, other, event = _scene(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/characters/{pc}/delete", follow_redirects=False)
    html = client.get(f"/parties/{party}").text
    assert "Stayer" in html and "Leaver" not in html


def test_helpers_tolerate_bad_data():
    assert member_ids("not json") == []
    assert member_ids(None) == []
    assert member_ids('{"a": 1}') == []
    assert member_ids('[1, "x", 2, null]') == [1, 2]


def test_parties_for_pc_is_world_scoped_and_name_ordered(client, seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Solo"))
    _add(Party(world_id=seed.world_a.id, name="Zeta", member_pc_ids_json=json.dumps([pc])))
    _add(Party(world_id=seed.world_a.id, name="Alpha", member_pc_ids_json=json.dumps([pc])))
    _add(Party(world_id=seed.world_a.id, name="Nope", member_pc_ids_json="[]"))
    _add(Party(world_id=seed.world_b.id, name="Other", member_pc_ids_json=json.dumps([pc])))
    db = SessionLocal()
    try:
        assert [p.name for p in parties_for_pc(db, seed.world_a.id, pc)] == ["Alpha", "Zeta"]
        assert detach_pc(db, seed.world_a.id, pc) == 2
        db.commit()
        assert parties_for_pc(db, seed.world_a.id, pc) == []
        assert [p.name for p in parties_for_pc(db, seed.world_b.id, pc)] == ["Other"]
    finally:
        db.close()
