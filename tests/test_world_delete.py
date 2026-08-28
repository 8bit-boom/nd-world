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
    AudioJob, Base, ChatJob, ChatSession, CombatSession, Entity, EntityNote, Fact,
    GameSession, ImageAlbum, ImageJob, InvestBoard, InviteCode,
    MapOverlay, Party, PlayerCharacter, PrivateNote, PromptPreset, Quest, RandomTable,
    Schematic, VideoAlbum, VideoClip, WorldCalendar, WorldMembership,
    entity_links, entity_player_access,
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
        db.add(VideoAlbum(world_id=world_id, name="Doomed Video Album"))
        db.add(Fact(world_id=world_id, content="A doomed fact"))
        db.add(ChatSession(world_id=world_id, user_id=seed.gm.id, messages_json="[]"))
        db.add(PromptPreset(world_id=world_id, scope="chat", label="Doomed Preset"))
        db.add(AudioJob(world_id=world_id, purpose="attachment", filename="doomed.mp3"))
        db.add(ImageJob(world_id=world_id, prompt="a doomed image"))
        db.add(ChatJob(world_id=world_id, prompt="a doomed question"))
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
        assert db.query(VideoAlbum).filter(VideoAlbum.world_id == world_id).count() == 0
        assert db.query(Fact).filter(Fact.world_id == world_id).count() == 0
        assert db.query(ChatSession).filter(ChatSession.world_id == world_id).count() == 0
        assert db.query(PromptPreset).filter(PromptPreset.world_id == world_id).count() == 0
        assert db.query(AudioJob).filter(AudioJob.world_id == world_id).count() == 0
        assert db.query(ImageJob).filter(ImageJob.world_id == world_id).count() == 0
        assert db.query(ChatJob).filter(ChatJob.world_id == world_id).count() == 0
    finally:
        db.close()


def test_world_delete_cleans_up_video_clip_and_poster_files(client, seed):
    from app.main import UPLOADS_DIR
    world_id = seed.world_a.id
    video_dir = UPLOADS_DIR / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "cascade-clip.mp4").write_bytes(b"fake-mp4")
    (video_dir / "cascade-clip.jpg").write_bytes(b"fake-jpg")

    db = SessionLocal()
    try:
        db.add(VideoClip(
            world_id=world_id, name="Cascade Clip",
            file_url="/uploads/video/cascade-clip.mp4",
            poster_url="/uploads/video/cascade-clip.jpg",
        ))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{world_id}/delete", follow_redirects=False)
    assert r.status_code == 303

    assert not (video_dir / "cascade-clip.mp4").exists()
    assert not (video_dir / "cascade-clip.jpg").exists()
    db = SessionLocal()
    try:
        assert db.query(VideoClip).filter(VideoClip.world_id == world_id).count() == 0
    finally:
        db.close()


def test_world_delete_models_cover_every_world_scoped_table():
    """Future-proofing: hand-listing every model in world_delete's cleanup
    loop is exactly how this bug happened three times over (Video/Fact/
    ChatSession/PromptPreset/the job tables, then EntityTemplate/
    SheetTemplate, were each added to the schema without anyone
    remembering to also add them here). Rather than construct a dummy row
    per table (fragile — many tables have their own required columns with
    no default, unrelated to world_id), introspect Base.metadata for
    every table that actually has a world_id column and assert
    _WORLD_DELETE_MODELS' table names are exactly that set. A model added
    later with a world_id column but never wired into the cleanup loop
    fails this test immediately instead of silently leaking forever."""
    from app.main import _WORLD_DELETE_MODELS

    world_scoped_table_names = {
        t.name for t in Base.metadata.sorted_tables if "world_id" in t.columns
    }
    assert len(world_scoped_table_names) > 10, "sanity check: this should find most of the app's tables"

    covered_table_names = {model.__tablename__ for model in _WORLD_DELETE_MODELS}
    assert covered_table_names == world_scoped_table_names, (
        "a world_id-scoped table isn't covered by world_delete's cleanup loop (or "
        "_WORLD_DELETE_MODELS lists one that no longer has a world_id column): "
        f"missing={world_scoped_table_names - covered_table_names}, "
        f"extra={covered_table_names - world_scoped_table_names}"
    )


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
