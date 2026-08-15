"""Tests for POST /api/import/convert-images — retroactive bulk conversion
of every image already referenced by a world (entity/character art,
schematic backgrounds and embedded images, investigation board cards,
gallery albums, and uploaded maps) to a single GM-chosen format/quality.
The counterpart to app/imaging.py's convert_image, which only ever runs
once at upload time.
"""
import io
import json

from PIL import Image

from app.database import SessionLocal
from app.main import UPLOADS_DIR
from app.models import Entity, ImageAlbum, InvestBoard, PlayerCharacter, Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _real_png_bytes(color=(200, 50, 50), size=(16, 16)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _write_upload(subdir, filename, data=None):
    d = UPLOADS_DIR / subdir if subdir else UPLOADS_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_bytes(data if data is not None else _real_png_bytes())
    return p


def test_convert_images_updates_entity_image_url(client, seed):
    _write_upload("", "ent1.png")
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="character", name="Aria", image_url="/uploads/ent1.png")
        db.add(ent)
        db.commit()
        db.refresh(ent)
        ent_id = ent.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 200
    body = r.json()
    assert body["converted"] == 1
    result = next(res for res in body["results"] if res["scope"] == "entity")
    assert result["status"] == "ok"
    assert result["new_url"] == "/uploads/ent1.webp"

    db = SessionLocal()
    try:
        refreshed = db.query(Entity).filter(Entity.id == ent_id).first()
        assert refreshed.image_url == "/uploads/ent1.webp"
    finally:
        db.close()
    assert (UPLOADS_DIR / "ent1.webp").is_file()
    assert not (UPLOADS_DIR / "ent1.png").exists()


def test_convert_images_updates_character_portrait(client, seed):
    _write_upload("", "pc1.png")
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Boro", portrait_url="/uploads/pc1.png")
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "jpg", "quality": 80})
    assert r.status_code == 200
    assert r.json()["converted"] == 1

    db = SessionLocal()
    try:
        refreshed = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
        assert refreshed.portrait_url == "/uploads/pc1.jpg"
    finally:
        db.close()


def test_convert_images_updates_schematic_background_and_embed(client, seed):
    _write_upload("schematics", "sc1.png")
    _write_upload("schematics/embeds", "embed1.png")
    elements = [{"id": "e1", "type": "image", "href": "/uploads/schematics/embeds/embed1.png", "x": 0, "y": 0}]
    db = SessionLocal()
    try:
        s = Schematic(
            world_id=seed.world_a.id, name="Dungeon", slug="dungeon",
            image_url="/uploads/schematics/sc1.png", elements_json=json.dumps(elements),
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = s.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "avif", "quality": 70})
    assert r.status_code == 200
    body = r.json()
    assert body["converted"] == 2

    db = SessionLocal()
    try:
        refreshed = db.query(Schematic).filter(Schematic.id == sid).first()
        assert refreshed.image_url == "/uploads/schematics/sc1.avif"
        els = json.loads(refreshed.elements_json)
        assert els[0]["href"] == "/uploads/schematics/embeds/embed1.avif"
    finally:
        db.close()


def test_convert_images_updates_board_node_and_skips_external_url(client, seed):
    _write_upload("", "card1.png")
    nodes = [
        {"id": "n1", "title": "Clue", "image_url": "/uploads/card1.png"},
        {"id": "n2", "title": "External", "image_url": "https://example.com/pic.png"},
        {"id": "n3", "title": "No image", "image_url": ""},
    ]
    db = SessionLocal()
    try:
        b = InvestBoard(world_id=seed.world_a.id, name="Case File", slug="case-file", nodes_json=json.dumps(nodes))
        db.add(b)
        db.commit()
        db.refresh(b)
        bid = b.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 200
    body = r.json()
    board_results = {res["label"]: res for res in body["results"] if res["scope"].startswith("board:")}
    assert board_results["Clue"]["status"] == "ok"
    assert board_results["External"]["status"] == "skipped"  # not a local upload — left alone
    assert "No image" not in board_results  # blank image_url isn't even attempted

    db = SessionLocal()
    try:
        refreshed = db.query(InvestBoard).filter(InvestBoard.id == bid).first()
        node_map = {n["id"]: n for n in json.loads(refreshed.nodes_json)}
        assert node_map["n1"]["image_url"] == "/uploads/card1.webp"
        assert node_map["n2"]["image_url"] == "https://example.com/pic.png"  # untouched
        assert node_map["n3"]["image_url"] == ""
    finally:
        db.close()


def test_convert_images_updates_map_image(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/maps/new",
        data={"name": "Old Town", "width": 1000, "height": 1000},
        files={"image_file": ("oldtown.png", io.BytesIO(_real_png_bytes()), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    slug = r.headers["location"].rsplit("/", 1)[-1]
    assert (UPLOADS_DIR / "maps" / f"{slug}.png").is_file()

    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 200
    body = r.json()
    map_results = [res for res in body["results"] if res["scope"] == "map"]
    assert len(map_results) == 1
    assert map_results[0]["status"] == "ok"
    assert (UPLOADS_DIR / "maps" / f"{slug}.webp").is_file()
    assert not (UPLOADS_DIR / "maps" / f"{slug}.png").exists()


def test_convert_images_scoped_to_active_world(client, seed):
    _write_upload("", "other.png")
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_b.id, kind="character", name="Sauron", image_url="/uploads/other.png")
        db.add(ent)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 200
    assert r.json()["converted"] == 0
    assert (UPLOADS_DIR / "other.png").is_file()  # untouched, belongs to world B


def test_convert_images_idempotent_skip(client, seed):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 10, 10)).save(buf, format="AVIF", quality=80)
    _write_upload("", "already.avif", data=buf.getvalue())
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="item", name="Idol", image_url="/uploads/already.avif")
        db.add(ent)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "avif", "quality": 80})
    assert r.status_code == 200
    body = r.json()
    assert body["converted"] == 0
    assert body["results"][0]["status"] == "skipped"


def test_convert_images_updates_album_only_image(client, seed):
    """An image sitting only in a gallery album (never attached to any
    entity/PC/schematic/board) must still get converted — not just the
    images convert_world_images already knew how to find via other tables."""
    _write_upload("gallery", "orphan-upload.png")
    db = SessionLocal()
    try:
        album = ImageAlbum(world_id=seed.world_a.id, name="Loose Uploads",
                            image_urls_json=json.dumps(["/uploads/gallery/orphan-upload.png"]))
        db.add(album)
        db.commit()
        db.refresh(album)
        album_id = album.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 200
    body = r.json()
    album_result = next(res for res in body["results"] if res["scope"].startswith("album:"))
    assert album_result["status"] == "ok"
    assert album_result["new_url"] == "/uploads/gallery/orphan-upload.webp"

    db = SessionLocal()
    try:
        refreshed = db.query(ImageAlbum).filter(ImageAlbum.id == album_id).first()
        assert json.loads(refreshed.image_urls_json) == ["/uploads/gallery/orphan-upload.webp"]
    finally:
        db.close()
    assert (UPLOADS_DIR / "gallery" / "orphan-upload.webp").is_file()
    assert not (UPLOADS_DIR / "gallery" / "orphan-upload.png").exists()


def test_convert_images_keeps_shared_image_in_sync_across_references(client, seed):
    """The same image can be both an entity's image_url AND filed into a
    gallery album. Converting must update BOTH references to the one new
    filename — not convert (and rename) the file once for the entity, then
    fail to find it again for the album and leave a stale/broken URL there."""
    _write_upload("gallery", "shared.png")
    shared_url = "/uploads/gallery/shared.png"
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="character", name="Shared Art", image_url=shared_url)
        db.add(ent)
        album = ImageAlbum(world_id=seed.world_a.id, name="Album", image_urls_json=json.dumps([shared_url]))
        db.add(album)
        db.commit()
        db.refresh(ent)
        db.refresh(album)
        ent_id, album_id = ent.id, album.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 200
    body = r.json()
    entity_result = next(res for res in body["results"] if res["scope"] == "entity")
    album_result = next(res for res in body["results"] if res["scope"].startswith("album:"))
    assert entity_result["status"] == "ok"
    assert album_result["status"] == "ok"  # NOT "skipped" — this is the bug being guarded against
    assert entity_result["new_url"] == album_result["new_url"] == "/uploads/gallery/shared.webp"

    db = SessionLocal()
    try:
        refreshed_ent = db.query(Entity).filter(Entity.id == ent_id).first()
        refreshed_album = db.query(ImageAlbum).filter(ImageAlbum.id == album_id).first()
        assert refreshed_ent.image_url == "/uploads/gallery/shared.webp"
        assert json.loads(refreshed_album.image_urls_json) == ["/uploads/gallery/shared.webp"]
    finally:
        db.close()
    # Only one physical re-encode happened — the file was renamed once, not
    # converted-then-orphaned-then-left-broken.
    assert (UPLOADS_DIR / "gallery" / "shared.webp").is_file()
    assert not (UPLOADS_DIR / "gallery" / "shared.png").exists()


def test_convert_images_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 80})
    assert r.status_code == 403


def test_convert_images_rejects_invalid_format(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "bmp", "quality": 80})
    assert r.status_code == 400


def test_convert_images_rejects_invalid_quality(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/convert-images", json={"format": "webp", "quality": 500})
    assert r.status_code == 400
