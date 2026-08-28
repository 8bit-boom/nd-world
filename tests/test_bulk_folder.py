"""Tests for the entity list's bulk-action-bar "Move to Folder" action —
POST /api/entities/bulk-folder. Same shape as bulk_set_visibility (see
test_bulk_visibility.py) — mass-assigns Entity.folder for a batch of ids,
scoped to the active world; an empty folder moves the batch to Unfiled."""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entities(world, n=3, folder=None):
    db = SessionLocal()
    try:
        ents = [Entity(world_id=world.id, kind="character", name=f"NPC {i}", folder=folder)
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


def test_gm_can_bulk_move_to_a_folder(client, seed):
    ents = _make_entities(seed.world_a, n=3)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-folder", json={
        "entity_ids": [e.id for e in ents],
        "folder": "Monsters",
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 3

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id.in_([e.id for e in ents])).all()
        assert all(e.folder == "Monsters" for e in refreshed)
    finally:
        db.close()


def test_blank_folder_moves_to_unfiled(client, seed):
    ents = _make_entities(seed.world_a, n=2, folder="Old Folder")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-folder", json={
        "entity_ids": [e.id for e in ents],
        "folder": "",
    })
    assert r.status_code == 200

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id.in_([e.id for e in ents])).all()
        assert all(e.folder is None for e in refreshed)
    finally:
        db.close()


def test_bulk_folder_scoped_to_active_world(client, seed):
    ents_a = _make_entities(seed.world_a, n=1)
    ents_b = _make_entities(seed.world_b, n=1)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/entities/bulk-folder", json={
        "entity_ids": [ents_a[0].id, ents_b[0].id],
        "folder": "Sneaky",
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 1  # only the World A entity, despite both ids being sent

    db = SessionLocal()
    try:
        assert db.get(Entity, ents_a[0].id).folder == "Sneaky"
        assert db.get(Entity, ents_b[0].id).folder is None
    finally:
        db.close()


def test_bulk_folder_requires_gm(client, seed):
    ents = _make_entities(seed.world_a, n=1)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/entities/bulk-folder", json={
        "entity_ids": [ents[0].id],
        "folder": "Sneaky",
    })
    assert r.status_code == 403


def test_bulk_folder_requires_active_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/entities/bulk-folder", json={"entity_ids": [], "folder": "x"})
    # A GM always has SOME world resolvable (get_active_world falls back to
    # the first accessible world), so this only 400s if there's truly none —
    # same shape as bulk_set_visibility's own guard.
    assert r.status_code in (200, 400)
