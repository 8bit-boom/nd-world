"""Party WRITES must be scoped to the party's own world.

The party routes load a Party by bare id and used to decide access from the
caller's role in their ACTIVE world (request.state.is_assistant) evaluated
against whatever world the party lives in — so an assistant of world A could
edit, delete, loot-edit, relocate or launch combat for any party id in world B
whose matrix allows assistant edits. Reads were already scoped
(world_row_visible); writes now are too: a non-GM may only act on a party in
the world they are currently active in AND belong to. GMs keep acting
across worlds. Member ids from another world are never stored.
"""
import json

from app.database import SessionLocal
from app.models import Entity, Party, PlayerCharacter, User, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, _PLAYER_PASSWORD_HASH, login


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _open_parties(world, **levels):
    db = SessionLocal()
    try:
        db.get(World, world.id).section_access_json = json.dumps({"parties": levels})
        db.commit()
    finally:
        db.close()


class Worlds:
    """player_a: assistant in world_a, NOT a member of world_b. Both worlds let
    assistants edit parties, so the only thing standing between player_a and
    world_b's party is the scoping under test."""

    def __init__(self, seed):
        self.seed = seed
        db = SessionLocal()
        try:
            m = db.query(WorldMembership).filter(
                WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id).first()
            m.role = "assistant"
            db.commit()
        finally:
            db.close()
        _open_parties(seed.world_a, assistant="edit", player="read")
        _open_parties(seed.world_b, assistant="edit", player="read")
        self.pc_a = _add(PlayerCharacter(world_id=seed.world_a.id, name="A-Hero"))
        self.pc_b = _add(PlayerCharacter(world_id=seed.world_b.id, name="B-Hero"))
        self.npc_b = _add(Entity(world_id=seed.world_b.id, kind="character", name="B-NPC"))
        self.party_a = _add(Party(world_id=seed.world_a.id, name="Party A", member_pc_ids_json=json.dumps([self.pc_a])))
        self.party_b = _add(Party(world_id=seed.world_b.id, name="Party B", member_pc_ids_json=json.dumps([self.pc_b]),
                                  notes="original"))

    def party_b_row(self):
        db = SessionLocal()
        try:
            p = db.get(Party, self.party_b)
            return None if p is None else (p.name, p.notes, p.loot_json, p.location_json, p.member_pc_ids_json)
        finally:
            db.close()


def _assistant(client, w):
    login(client, w.seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", w.seed.world_a.slug)


def _writes(party):
    """Every party write route, as (label, callable(client) -> response)."""
    return [
        ("edit", lambda c: c.post(f"/parties/{party}/edit", data={"name": "HACKED", "notes": "pwned"}, follow_redirects=False)),
        ("delete", lambda c: c.post(f"/parties/{party}/delete", follow_redirects=False)),
        ("loot", lambda c: c.post(f"/api/parties/{party}/loot", json={"action": "add", "name": "Gold", "qty": 1})),
        ("location", lambda c: c.post(f"/api/parties/{party}/location", json={"kind": "map", "slug": "x", "lat": 1, "lng": 1})),
        ("launch-combat", lambda c: c.post(f"/api/parties/{party}/launch-combat")),
    ]


def test_assistant_cannot_write_to_another_worlds_party(client, seed):
    w = Worlds(seed)
    before = w.party_b_row()
    _assistant(client, w)
    for label, call in _writes(w.party_b):
        r = call(client)
        assert r.status_code in (403, 404), f"{label}: expected a refusal, got {r.status_code}"
    assert w.party_b_row() == before, "world B's party was modified from world A"


def test_assistant_still_manages_parties_in_their_own_world(client, seed):
    w = Worlds(seed)
    _assistant(client, w)
    r = client.post(f"/parties/{w.party_a}/edit", data={"name": "Renamed", "notes": "n", "member_pc_ids": [str(w.pc_a)]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.post(f"/api/parties/{w.party_a}/loot", json={"action": "add", "name": "Gold", "qty": 1}).status_code == 200
    db = SessionLocal()
    try:
        assert db.get(Party, w.party_a).name == "Renamed"
    finally:
        db.close()


def test_active_world_must_match_even_for_a_member_of_both(client, seed):
    """A user who is an assistant in the active world and merely a player in
    world_b must not get world_a's assistant powers over world_b's party."""
    w = Worlds(seed)
    db = SessionLocal()
    try:
        db.add(WorldMembership(world_id=seed.world_b.id, user_id=seed.player_a.id, role="player"))
        db.commit()
    finally:
        db.close()
    before = w.party_b_row()
    _assistant(client, w)
    for label, call in _writes(w.party_b):
        assert call(client).status_code in (403, 404), label
    assert w.party_b_row() == before


def test_gm_can_still_write_across_worlds(client, seed):
    w = Worlds(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{w.party_b}/edit", data={"name": "GM Renamed", "notes": "ok"}, follow_redirects=False)
    assert r.status_code == 303
    assert w.party_b_row()[0] == "GM Renamed"


def test_member_ids_from_another_world_are_never_stored(client, seed):
    w = Worlds(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{w.party_a}/edit",
                    data={"name": "Party A", "member_pc_ids": [str(w.pc_a), str(w.pc_b)],
                          "member_entity_ids": [str(w.npc_b)]}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        p = db.get(Party, w.party_a)
        assert json.loads(p.member_pc_ids_json) == [w.pc_a]
        assert json.loads(p.member_entity_ids_json) == []
    finally:
        db.close()
