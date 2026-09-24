"""Regression tests: deleting an Entity must also delete any EntityRelation
row referencing it as source or target — app/main.py's single-entity and
bulk-delete routes, plus app/routers/races.py and app/routers/professions.py
(app/mcp_server.py's delete_entity gets the identical fix, tested in
tests/test_mcp.py).

Entity.id is a plain INTEGER PRIMARY KEY (no AUTOINCREMENT), so SQLite can
reuse a deleted entity's id for the next entity created in that world — a
stale EntityRelation row left behind would silently reattach a GM's
confirmed graph edge to that unrelated future entity. See
test_id_reuse_does_not_resurrect_a_stale_relation below for the concrete
scenario this guards against.
"""
from app.database import SessionLocal
from app.models import Entity, EntityRelation

from .conftest import GM_PASSWORD, login


def _make_entity(world_id, kind="character", name="Bob"):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, name=name)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _make_relation(world_id, source_id, target_id, relation="member_of"):
    db = SessionLocal()
    try:
        r = EntityRelation(world_id=world_id, source_id=source_id, target_id=target_id, relation=relation)
        db.add(r)
        db.commit()
        db.refresh(r)
        return r.id
    finally:
        db.close()


def _relation_count(entity_id):
    db = SessionLocal()
    try:
        return db.query(EntityRelation).filter(
            (EntityRelation.source_id == entity_id) | (EntityRelation.target_id == entity_id)
        ).count()
    finally:
        db.close()


# ── POST /entity/{id}/delete (single) ────────────────────────────────────────

def test_single_delete_removes_relations_as_source_and_target(client, seed):
    bob = _make_entity(seed.world_a.id, name="Bob")
    guild = _make_entity(seed.world_a.id, kind="organization", name="Guild")
    dockside = _make_entity(seed.world_a.id, kind="location", name="Dockside")
    _make_relation(seed.world_a.id, bob, guild, "member_of")
    _make_relation(seed.world_a.id, dockside, bob, "home_of")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/entity/{bob}/delete", follow_redirects=False)
    assert r.status_code == 303

    assert _relation_count(bob) == 0
    # The unrelated edge (guild/dockside not involving bob) must survive.
    db = SessionLocal()
    try:
        assert db.query(EntityRelation).count() == 0  # both rows involved bob
    finally:
        db.close()


def test_single_delete_leaves_unrelated_relations_alone(client, seed):
    bob = _make_entity(seed.world_a.id, name="Bob")
    guild = _make_entity(seed.world_a.id, kind="organization", name="Guild")
    other_a = _make_entity(seed.world_a.id, name="Other A")
    other_b = _make_entity(seed.world_a.id, kind="organization", name="Other B")
    _make_relation(seed.world_a.id, bob, guild, "member_of")
    unrelated_id = _make_relation(seed.world_a.id, other_a, other_b, "allied_with")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/entity/{bob}/delete", follow_redirects=False)

    db = SessionLocal()
    try:
        assert db.get(EntityRelation, unrelated_id) is not None
    finally:
        db.close()


def test_id_reuse_does_not_resurrect_a_stale_relation(client, seed):
    """The concrete scenario the fix guards against: delete the newest
    entity (SQLite is likely to reuse its rowid next), create an unrelated
    new entity, and confirm no stale edge attaches to it."""
    bob = _make_entity(seed.world_a.id, name="Bob")
    guild = _make_entity(seed.world_a.id, kind="organization", name="Guild")
    _make_relation(seed.world_a.id, bob, guild, "member_of")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/entity/{bob}/delete", follow_redirects=False)

    new_id = _make_entity(seed.world_a.id, name="Someone Else")
    assert _relation_count(new_id) == 0


# ── POST /kind/{kind}/bulk-delete ────────────────────────────────────────────

def test_bulk_delete_removes_relations_for_every_matched_entity(client, seed):
    bob = _make_entity(seed.world_a.id, name="Bob")
    alice = _make_entity(seed.world_a.id, name="Alice")
    guild = _make_entity(seed.world_a.id, kind="organization", name="Guild")
    _make_relation(seed.world_a.id, bob, guild, "member_of")
    _make_relation(seed.world_a.id, alice, guild, "member_of")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/kind/character/bulk-delete", data={"entity_ids": [str(bob), str(alice)]}, follow_redirects=False)
    assert r.status_code == 303

    assert _relation_count(bob) == 0
    assert _relation_count(alice) == 0


# ── POST /races/{id}/delete, /professions/{id}/delete ───────────────────────

def test_race_delete_removes_relations(client, seed):
    race = _make_entity(seed.world_a.id, kind="race", name="Chrome Elves")
    org = _make_entity(seed.world_a.id, kind="organization", name="Elf Council")
    _make_relation(seed.world_a.id, race, org, "governed_by")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/races/{race}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _relation_count(race) == 0


def test_profession_delete_removes_relations(client, seed):
    prof = _make_entity(seed.world_a.id, kind="profession", name="Netrunner")
    org = _make_entity(seed.world_a.id, kind="organization", name="Hacker Collective")
    _make_relation(seed.world_a.id, prof, org, "associated_with")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/professions/{prof}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _relation_count(prof) == 0
