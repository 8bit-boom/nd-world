"""Tests for the race catalog (app/routers/races.py): the bundled markdown
race library, one-click add into a world as Entity(kind="race") rows, and
that races are otherwise just a normal entity kind (GM-only writes, player-safe
reads, no cross-world reach).
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_races_page_lists_builtin_catalog(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/races")
    assert r.status_code == 200
    assert "Dwarf" in r.text
    assert "Dragonblooded" in r.text


def test_races_page_is_player_safe(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/races")
    assert r.status_code == 200


def test_add_builtin_race_creates_entity_in_active_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race", Entity.name.ilike("%dwarf%")
        ).first()
        assert ent is not None
        assert ent.subtype == "standard"
    finally:
        db.close()


def test_add_builtin_race_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})
    assert r.status_code == 403


def test_add_all_builtin_adds_every_race_once(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-all-builtin", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        count_first = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race"
        ).count()
        assert count_first >= 17
    finally:
        db.close()

    # Re-running must not create duplicates.
    client.post("/races/add-all-builtin", follow_redirects=False)
    db = SessionLocal()
    try:
        count_second = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race"
        ).count()
        assert count_second == count_first
    finally:
        db.close()


def test_race_kind_appears_in_generic_kind_browsing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})

    r = client.get("/kind/race")
    assert r.status_code == 200
    assert "Dwarf" in r.text


def test_race_delete_rejects_entity_from_other_world(client, seed):
    """Mirrors this session's established cross-world-leak hardening: a race
    id from a different world must not be deletable via the active world's
    session, even though the original NeonDragonsWorld implementation this
    was ported from didn't check world ownership at all."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    other_world_race = Entity(world_id=seed.world_b.id, kind="race", subtype="standard", name="Foreign Race")
    db = SessionLocal()
    try:
        db.add(other_world_race)
        db.commit()
        db.refresh(other_world_race)
        race_id = other_world_race.id
    finally:
        db.close()

    r = client.post(f"/races/{race_id}/delete")
    assert r.status_code == 404

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == race_id).first() is not None
    finally:
        db.close()
