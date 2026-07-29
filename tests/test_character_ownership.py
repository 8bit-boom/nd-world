"""A player must not be able to modify another player's character through the
/api/characters/* AJAX endpoints. These paths are player-safe at the
_is_player_safe allowlist level (any logged-in player can POST to them, by
design — that's how a player updates their own sheet), so the only thing
enforcing ownership is _can_manage_character inside each handler. This is
already correct today; this test locks it in against a future regression.
"""
from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_pc(world, owner_id, name="Owned Character"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world.id, name=name, owner_user_id=owner_id,
                              max_hp=20, current_hp=20)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def test_player_cannot_modify_another_players_hp(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    r = client.post(f"/api/characters/{pc.id}/hp-async", json={"action": "set", "value": 1})
    assert r.status_code == 403

    db = SessionLocal()
    try:
        refreshed = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc.id).first()
        assert refreshed.current_hp == 20, "HP was modified despite the request being rejected"
    finally:
        db.close()


def test_owner_can_modify_own_character_hp(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/characters/{pc.id}/hp-async", json={"action": "set", "value": 5})
    assert r.status_code == 200
    assert r.json()["current_hp"] == 5


def test_gm_can_modify_any_players_character_hp(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/api/characters/{pc.id}/hp-async", json={"action": "set", "value": 7})
    assert r.status_code == 200
    assert r.json()["current_hp"] == 7
