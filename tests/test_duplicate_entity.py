"""Tests for POST /entity/{id}/duplicate — cloning an entity (common for
stat-blocked mooks) copies every Entity column except id/created_at/
updated_at, appends "(copy)" to the name, and deliberately does NOT copy
notes, links, or player-access grants."""
from app.database import SessionLocal
from app.models import Entity, EntityNote, entity_player_access

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "character"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_duplicate_copies_fields_and_appends_copy_to_name(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Goblin", subtype="minion", folder="Monsters", tags="green,weak",
        summary="A goblin.", body="**HP**: 5", visible_to_players=False,
        custom_fields_json='{"age": "12"}',
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/entity/{eid}/duplicate", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        clones = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.name == "Goblin (copy)").all()
        assert len(clones) == 1
        clone = clones[0]
        assert clone.id != eid
        assert clone.subtype == "minion"
        assert clone.folder == "Monsters"
        assert clone.tags == "green,weak"
        assert clone.summary == "A goblin."
        assert clone.body == "**HP**: 5"
        assert clone.visible_to_players is False
        assert clone.custom_fields_json == '{"age": "12"}'
    finally:
        db.close()


def test_duplicate_does_not_copy_notes_or_player_access(client, seed):
    eid = _make_entity(seed.world_a.id, name="Vault", visible_to_players=False)
    db = SessionLocal()
    try:
        db.add(EntityNote(entity_id=eid, content="secret note", visible_to_players=False))
        db.execute(entity_player_access.insert().values(entity_id=eid, user_id=seed.player_a.id))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/entity/{eid}/duplicate")

    db = SessionLocal()
    try:
        clone = db.query(Entity).filter(Entity.name == "Vault (copy)").first()
        assert clone is not None
        assert db.query(EntityNote).filter(EntityNote.entity_id == clone.id).count() == 0
        access = db.execute(entity_player_access.select().where(entity_player_access.c.entity_id == clone.id)).fetchall()
        assert access == []
    finally:
        db.close()


def test_duplicate_requires_gm(client, seed):
    eid = _make_entity(seed.world_a.id, name="Goblin", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/entity/{eid}/duplicate")
    assert r.status_code == 403


def test_duplicate_404_for_unknown_entity(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/entity/999999/duplicate")
    assert r.status_code == 404
