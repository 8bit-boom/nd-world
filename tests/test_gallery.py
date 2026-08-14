"""Tests for the /images gallery tab: app/gallery.py's discover_world_images
(finds every image already referenced in a world's entities/PCs) and
app/routers/gallery.py's ImageAlbum CRUD (a GM-curated named collection of
image URLs, on top of the discovered set)."""
import io
import json

from app.database import SessionLocal
from app.models import Entity, EntityNote, ImageAlbum, PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000


def _png_file(name="pic.png"):
    return {"file": (name, io.BytesIO(_PNG_BYTES), "image/png")}


def _make_album(world_id, name="Album", urls=None):
    db = SessionLocal()
    try:
        a = ImageAlbum(world_id=world_id, name=name, image_urls_json=json.dumps(urls or []))
        db.add(a)
        db.commit()
        db.refresh(a)
        return a.id
    finally:
        db.close()


def _album_urls(album_id):
    db = SessionLocal()
    try:
        a = db.get(ImageAlbum, album_id)
        return json.loads(a.image_urls_json or "[]") if a else None
    finally:
        db.close()


# ── Discovery ────────────────────────────────────────────────────────────────

def test_discovers_entity_portrait_and_inline_body_image(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="Portrait NPC",
                    image_url="/uploads/portrait.png",
                    body="Behold: ![vista](/uploads/vista.png)")
        db.add(e)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert r.status_code == 200
    assert "/uploads/portrait.png" in r.text
    assert "/uploads/vista.png" in r.text


def test_discovers_entity_note_and_pc_images(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="location", name="Place")
        db.add(e)
        db.commit()
        db.refresh(e)
        note = EntityNote(entity_id=e.id, content="![clue](/uploads/clue.png)", visible_to_players=False)
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Hero", portrait_url="/uploads/hero.png",
                              backstory="Home: ![hometown](/uploads/hometown.png)")
        db.add_all([note, pc])
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert r.status_code == 200
    for url in ("/uploads/clue.png", "/uploads/hero.png", "/uploads/hometown.png"):
        assert url in r.text


def test_same_image_used_twice_is_deduplicated(client, seed):
    db = SessionLocal()
    try:
        e1 = Entity(world_id=seed.world_a.id, kind="character", name="A", image_url="/uploads/shared.png")
        e2 = Entity(world_id=seed.world_a.id, kind="character", name="B", image_url="/uploads/shared.png")
        db.add_all([e1, e2])
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert r.status_code == 200
    assert r.text.count('value="/uploads/shared.png"') == 1  # one checkbox = one discovered image, deduplicated
    assert "used in 2 places" in r.text


def test_discovery_is_world_scoped(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_b.id, kind="character", name="Other World NPC",
                    image_url="/uploads/other-world.png")
        db.add(e)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert "/uploads/other-world.png" not in r.text


# ── Access control ───────────────────────────────────────────────────────────

def test_images_gallery_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/images").status_code == 403
    assert client.post("/images/albums/new", data={"name": "x"}).status_code == 403


def test_nav_shows_images_link_to_gm_only(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "data-ql-ref=\"/images\"" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "data-ql-ref=\"/images\"" not in r.text


# ── Album CRUD ───────────────────────────────────────────────────────────────

def test_gm_can_create_and_view_album(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/albums/new", data={"name": "Vista Shots"}, follow_redirects=False)
    assert r.status_code == 303
    album_url = r.headers["location"]
    r = client.get(album_url)
    assert r.status_code == 200
    assert "Vista Shots" in r.text


def test_album_add_and_remove_images(client, seed):
    album_id = _make_album(seed.world_a.id, "Test Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/images/albums/{album_id}/add", json={"urls": ["/uploads/a.png", "/uploads/b.png"]})
    assert r.status_code == 200
    assert r.json()["count"] == 2
    assert _album_urls(album_id) == ["/uploads/a.png", "/uploads/b.png"]

    r = client.post(f"/images/albums/{album_id}/add", json={"urls": ["/uploads/a.png"]})
    assert r.json()["count"] == 2  # no duplicate

    r = client.post(f"/images/albums/{album_id}/remove", json={"url": "/uploads/a.png"})
    assert r.status_code == 200
    assert _album_urls(album_id) == ["/uploads/b.png"]


def test_album_direct_upload(client, seed):
    album_id = _make_album(seed.world_a.id, "Uploads")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/images/albums/{album_id}/upload", files=_png_file(), follow_redirects=False)
    assert r.status_code == 303
    urls = _album_urls(album_id)
    assert len(urls) == 1
    assert urls[0].startswith("/uploads/gallery/")


def test_uploaded_only_image_not_in_discovered_list(client, seed):
    """An image uploaded straight into an album (not referenced by any
    entity/PC) shows up as that album's cover thumbnail on /images, but must
    not appear as a discovered/selectable image in the All Images grid."""
    album_id = _make_album(seed.world_a.id, "Uploads")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/images/albums/{album_id}/upload", files=_png_file())
    url = _album_urls(album_id)[0]

    r = client.get("/images")
    assert f'value="{url}"' not in r.text  # not a selectable discovered-image checkbox
    assert url in r.text  # still shown as the album's cover thumbnail


def test_album_rename(client, seed):
    album_id = _make_album(seed.world_a.id, "Old Name")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/images/albums/{album_id}/rename", data={"name": "New Name"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get(f"/images/albums/{album_id}")
    assert "New Name" in r.text
    assert "Old Name" not in r.text


def test_album_delete(client, seed):
    album_id = _make_album(seed.world_a.id, "Doomed")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/images/albums/{album_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/images/albums/{album_id}").status_code == 404


def test_album_access_is_world_scoped(client, seed):
    album_id = _make_album(seed.world_b.id, "World B Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/images/albums/{album_id}").status_code == 404
    r = client.post(f"/images/albums/{album_id}/add", json={"urls": ["/uploads/x.png"]})
    assert r.status_code == 404


def test_gallery_thumbnails_use_object_fit_contain(client, seed):
    """Thumbnails used to crop with object-fit:cover — per an explicit UX
    request they now show the full image, letterboxed, via
    object-fit:contain."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/images")
    assert "object-fit:contain" in r.text
    assert "object-fit:cover" not in r.text

    album_id = _make_album(seed.world_a.id, "Album")
    r = client.get(f"/images/albums/{album_id}")
    assert "object-fit:contain" in r.text
    assert "object-fit:cover" not in r.text


def test_album_page_has_drag_and_drop_upload(client, seed):
    album_id = _make_album(seed.world_a.id, "Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/images/albums/{album_id}")
    assert 'id="album-dropzone"' in r.text
    assert "function galleryHasFiles(dt)" in r.text
    assert "async function albumUploadFiles(fileList)" in r.text
    assert "multiple" in r.text  # file input accepts several files at once


def test_album_upload_rejects_bad_extension(client, seed):
    album_id = _make_album(seed.world_a.id, "Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/images/albums/{album_id}/upload",
                     files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400
