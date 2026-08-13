"""Phase 15 regression tests: world_delete previously just did db.delete(w)
and committed. SQLite's foreign_keys pragma is never turned on (see
database.py) and World has no ORM cascade configured for most of its child
tables (only Entity does, via World.entities), so deleting a world silently
orphaned every PlayerCharacter, Schematic, WorldMembership, InviteCode,
CombatSession, Party, Quest, GameSession, PrivateNote, InvestBoard,
RandomTable, WorldCalendar/CalendarEvent row for it, plus every
filesystem-backed Map JSON file, its MapOverlay row, and every uploaded map/
schematic image, forever. Also covers the paired schematic_delete cleanup fix
(background image / HTML file / embedded images previously leaked on every
schematic delete, independent of world_delete).
"""
import json

import pytest

from app.database import SessionLocal
from app.main import _MAPS_DIR
from app.models import (
    CombatSession, Entity, EntityNote, GameSession, ImageAlbum, InvestBoard, InviteCode,
    MapOverlay, Party, PlayerCharacter, PrivateNote, Quest, RandomTable,
    Schematic, WorldCalendar, WorldMembership, entity_links, entity_player_access,
)

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean_maps_dir():
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()
    yield
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()


def test_world_delete_cascades_every_child_table(client, seed):
    world_id = seed.world_a.id
    db = SessionLocal()
    try:
        e1 = Entity(world_id=world_id, kind="location", name="Doomed City")
        e2 = Entity(world_id=world_id, kind="character", name="Doomed NPC")
        db.add_all([e1, e2]); db.commit(); db.refresh(e1); db.refresh(e2)
        db.add(EntityNote(entity_id=e1.id, content="secret"))
        db.execute(entity_links.insert().values(source_id=e1.id, target_id=e2.id))
        db.execute(entity_player_access.insert().values(entity_id=e1.id, user_id=seed.player_a.id))
        db.add(PlayerCharacter(world_id=world_id, owner_user_id=seed.player_a.id, name="Hero"))
        db.add(Schematic(world_id=world_id, name="Battle Map", slug="cascade-schem",
                          is_html=False, canvas_width=2000, canvas_height=1500,
                          canvas_bg="dark", elements_json="[]"))
        db.add(CombatSession(world_id=world_id, name="Fight"))
        db.add(Party(world_id=world_id, name="The Party"))
        db.add(Quest(world_id=world_id, title="Find the thing"))
        db.add(GameSession(world_id=world_id, title="Session 1"))
        db.add(InvestBoard(world_id=world_id, name="Board", slug="cascade-board"))
        db.add(RandomTable(world_id=world_id, name="Loot Table", slug="cascade-loot-table"))
        db.add(WorldCalendar(world_id=world_id))
        db.add(PrivateNote(world_id=world_id, player_user_id=seed.player_a.id, title="Note"))
        db.add(InviteCode(world_id=world_id, code="INVITE1"))
        db.add(ImageAlbum(world_id=world_id, name="Doomed Album", image_urls_json="[]"))
        db.commit()

        membership_before = db.query(WorldMembership).filter(WorldMembership.world_id == world_id).count()
        assert membership_before > 0  # seed fixture already made player_a a member
        e1_id, e2_id = e1.id, e2.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{world_id}/delete", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.world_id == world_id).count() == 0
        assert db.query(EntityNote).filter(EntityNote.entity_id == e1_id).count() == 0
        assert db.execute(entity_links.select().where(entity_links.c.source_id == e1_id)).fetchall() == []
        assert db.execute(entity_player_access.select().where(entity_player_access.c.entity_id == e1_id)).fetchall() == []
        assert db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).count() == 0
        assert db.query(Schematic).filter(Schematic.world_id == world_id).count() == 0
        assert db.query(CombatSession).filter(CombatSession.world_id == world_id).count() == 0
        assert db.query(Party).filter(Party.world_id == world_id).count() == 0
        assert db.query(Quest).filter(Quest.world_id == world_id).count() == 0
        assert db.query(GameSession).filter(GameSession.world_id == world_id).count() == 0
        assert db.query(InvestBoard).filter(InvestBoard.world_id == world_id).count() == 0
        assert db.query(RandomTable).filter(RandomTable.world_id == world_id).count() == 0
        assert db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).count() == 0
        assert db.query(PrivateNote).filter(PrivateNote.world_id == world_id).count() == 0
        assert db.query(InviteCode).filter(InviteCode.world_id == world_id).count() == 0
        assert db.query(WorldMembership).filter(WorldMembership.world_id == world_id).count() == 0
        assert db.query(ImageAlbum).filter(ImageAlbum.world_id == world_id).count() == 0
    finally:
        db.close()


def test_world_delete_cleans_up_maps_and_schematic_files(client, seed):
    from app.main import UPLOADS_DIR
    world_id = seed.world_a.id

    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / "cascade-map.json").write_text(json.dumps({
        "name": "Cascade Map", "world_id": world_id, "width": 100, "height": 100, "markers": [],
    }), encoding="utf-8")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    (maps_upload_dir / "cascade-map.png").write_bytes(b"fake-png")

    db = SessionLocal()
    try:
        db.add(MapOverlay(slug="cascade-map", custom_markers_json="[]", custom_regions_json="[]"))
        s = Schematic(world_id=world_id, name="Cascade Schem", slug="cascade-schem-file",
                      is_html=False, canvas_width=2000, canvas_height=1500,
                      canvas_bg="dark", elements_json="[]", image_url="/uploads/schematics/cascade-schem-file.png")
        db.add(s); db.commit()
    finally:
        db.close()
    sch_upload_dir = UPLOADS_DIR / "schematics"
    sch_upload_dir.mkdir(parents=True, exist_ok=True)
    (sch_upload_dir / "cascade-schem-file.png").write_bytes(b"fake-png")

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{world_id}/delete", follow_redirects=False)
    assert r.status_code == 303

    assert not (_MAPS_DIR / "cascade-map.json").exists()
    assert not (maps_upload_dir / "cascade-map.png").exists()
    assert not (sch_upload_dir / "cascade-schem-file.png").exists()

    db = SessionLocal()
    try:
        assert db.query(MapOverlay).filter(MapOverlay.slug == "cascade-map").first() is None
        assert db.query(Schematic).filter(Schematic.slug == "cascade-schem-file").first() is None
    finally:
        db.close()


def test_world_delete_leaves_other_world_untouched(client, seed):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_b.id, kind="location", name="Safe City"))
        db.add(PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Safe Hero"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/delete", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.world_id == seed.world_b.id).count() == 1
        assert db.query(PlayerCharacter).filter(PlayerCharacter.world_id == seed.world_b.id).count() == 1
        assert db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_b.id).count() == 1
    finally:
        db.close()


def _make_schematic(db, world_id, slug, **kw):
    fields = dict(world_id=world_id, name="Test Schematic", slug=slug, is_html=False,
                  canvas_width=2000, canvas_height=1500, canvas_bg="dark",
                  elements_json=json.dumps(kw.pop("elements", [])))
    fields.update(kw)
    s = Schematic(**fields)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_schematic_delete_removes_background_image(client, seed):
    from app.main import UPLOADS_DIR
    sch_dir = UPLOADS_DIR / "schematics"
    sch_dir.mkdir(parents=True, exist_ok=True)
    (sch_dir / "del-bg.png").write_bytes(b"fake-png")
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "del-bg", image_url="/uploads/schematics/del-bg.png")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/del-bg/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (sch_dir / "del-bg.png").exists()


def test_schematic_delete_removes_embedded_images(client, seed):
    from app.main import UPLOADS_DIR
    embeds_dir = UPLOADS_DIR / "schematics" / "embeds"
    embeds_dir.mkdir(parents=True, exist_ok=True)
    (embeds_dir / "abc123.png").write_bytes(b"fake-png")
    elements = [{"id": "img1", "type": "image", "href": "/uploads/schematics/embeds/abc123.png",
                 "x": 0, "y": 0, "w": 100, "h": 100}]
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "del-embed", elements=elements)
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/del-embed/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (embeds_dir / "abc123.png").exists()


def test_schematic_delete_removes_html_file(client, seed):
    from app.main import SCHEMATICS_STATIC_DIR
    SCHEMATICS_STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (SCHEMATICS_STATIC_DIR / "del-html.html").write_text("<html></html>", encoding="utf-8")
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "del-html-schem", is_html=True, html_file="del-html.html")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/del-html-schem/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (SCHEMATICS_STATIC_DIR / "del-html.html").exists()
