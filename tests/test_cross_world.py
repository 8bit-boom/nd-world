"""Regression tests for the cross-world authorization holes: a player could
previously view any character sheet or player-visible entity in *any* world,
not just worlds they'd been invited to, by walking sequential IDs — because
_can_view_character and GET /entity/{id} checked visibility flags but never
checked that the viewer actually belonged to the entity's/character's world.
The two routes deny in different ways (403 vs 404) — both are covered as-is.
"""
from app.database import SessionLocal
from app.models import Entity, PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_pc_in(world, name="Some Hero", owner_id=None):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world.id, name=name, owner_user_id=owner_id)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def _make_entity_in(world, name="Some NPC", visible=True):
    db = SessionLocal()
    try:
        ent = Entity(world_id=world.id, kind="character", name=name, visible_to_players=visible)
        db.add(ent)
        db.commit()
        db.refresh(ent)
        return ent
    finally:
        db.close()


def test_player_cannot_view_character_in_other_world(client, seed):
    pc = _make_pc_in(seed.world_b, owner_id=seed.player_b.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/characters/{pc.id}")
    # character_sheet() denies with 403 (not 404) once the PC row is found but
    # _can_view_character rejects the viewer — unlike the entity route below,
    # which 404s instead. Both are safe; this locks in this route's actual
    # (403) convention rather than assuming it matches the other one.
    assert r.status_code == 403


def test_player_can_view_character_in_own_world(client, seed):
    pc = _make_pc_in(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/characters/{pc.id}")
    assert r.status_code == 200


def test_player_cannot_view_entity_in_other_world(client, seed):
    ent = _make_entity_in(seed.world_b, visible=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/entity/{ent.id}")
    assert r.status_code == 404


def test_player_can_view_visible_entity_in_own_world(client, seed):
    ent = _make_entity_in(seed.world_a, visible=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/entity/{ent.id}")
    assert r.status_code == 200


def test_gm_can_view_characters_and_entities_in_any_world(client, seed):
    pc = _make_pc_in(seed.world_b, owner_id=seed.player_b.id)
    ent = _make_entity_in(seed.world_a, visible=True)
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get(f"/characters/{pc.id}").status_code == 200
    assert client.get(f"/entity/{ent.id}").status_code == 200
