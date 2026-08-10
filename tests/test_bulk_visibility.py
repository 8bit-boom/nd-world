"""Regression tests for the Settings > Visibility bulk-apply action —
POST /api/entities/bulk-visibility."""
from app.database import SessionLocal
from app.models import Entity, entity_player_access

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entities(world, n=3, visible=True):
    db = SessionLocal()
    try:
        ents = [Entity(world_id=world.id, kind="character", name=f"NPC {i}", visible_to_players=visible)
                for i in range(n)]
        db.add_all(ents)
        db.commit()
        for e in ents:
            db.refresh(e)
        return ents
    finally:
        db.close()


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_gm_can_bulk_hide_entities(client, seed):
    ents = _make_entities(seed.world_a, n=3, visible=True)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-visibility", json={
        "entity_ids": [e.id for e in ents],
        "visibility_mode": "gm",
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 3

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id.in_([e.id for e in ents])).all()
        assert all(not e.visible_to_players for e in refreshed)
    finally:
        db.close()


def test_gm_can_bulk_set_specific_players(client, seed):
    ents = _make_entities(seed.world_a, n=2, visible=True)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-visibility", json={
        "entity_ids": [e.id for e in ents],
        "visibility_mode": "players",
        "allowed_player_ids": [seed.player_a.id],
    })
    assert r.status_code == 200

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id.in_([e.id for e in ents])).all()
        assert all(not e.visible_to_players for e in refreshed)
        for e in ents:
            allowed = {r[0] for r in db.query(entity_player_access.c.user_id)
                       .filter(entity_player_access.c.entity_id == e.id).all()}
            assert allowed == {seed.player_a.id}
    finally:
        db.close()


def test_bulk_visibility_scoped_to_active_world(client, seed):
    """Entity ids from a world the GM didn't select as active are ignored,
    not silently applied — matches the same world-scoping every other
    bulk/write route in this app enforces."""
    ents_b = _make_entities(seed.world_b, n=2, visible=True)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-visibility", json={
        "entity_ids": [e.id for e in ents_b],
        "visibility_mode": "gm",
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 0

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id.in_([e.id for e in ents_b])).all()
        assert all(e.visible_to_players for e in refreshed)
    finally:
        db.close()


def test_player_cannot_bulk_change_visibility(client, seed):
    ents = _make_entities(seed.world_a, n=1, visible=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/entities/bulk-visibility", json={
        "entity_ids": [e.id for e in ents],
        "visibility_mode": "gm",
    })
    assert r.status_code in (303, 403)


def test_invalid_visibility_mode_rejected(client, seed):
    ents = _make_entities(seed.world_a, n=1, visible=True)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-visibility", json={
        "entity_ids": [e.id for e in ents],
        "visibility_mode": "bogus",
    })
    assert r.status_code == 400


def test_settings_visibility_tab_renders(client, seed):
    _make_entities(seed.world_a, n=2, visible=True)
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/settings?tab=visibility")
    assert r.status_code == 200
    assert "NPC 0" in r.text
    assert "vis-apply-btn" in r.text
