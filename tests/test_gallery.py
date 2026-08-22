"""Tests for the /images gallery tab: app/gallery.py's discover_world_images
(finds every image already referenced in a world's entities/PCs) and
app/routers/gallery.py's ImageAlbum CRUD (a GM-curated named collection of
image URLs, on top of the discovered set)."""
import io
import json

from app.database import SessionLocal
from app.gallery import all_world_image_urls
from app.models import Entity, EntityNote, ImageAlbum, PlayerCharacter, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000


def _png_file(name="pic.png"):
    return {"file": (name, io.BytesIO(_PNG_BYTES), "image/png")}


def _make_album(world_id, name="Album", urls=None, parent_id=None):
    db = SessionLocal()
    try:
        a = ImageAlbum(world_id=world_id, name=name, image_urls_json=json.dumps(urls or []), parent_id=parent_id)
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


# ── Full-resolution lightbox on thumbnails ──────────────────────────────────

def test_album_thumbnail_opens_lightbox(client, seed):
    album_id = _make_album(seed.world_a.id, "Album", urls=["/uploads/pic.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/images/albums/{album_id}")
    assert 'onclick="openLightbox(this.src, this.alt)"' in r.text


def test_index_thumbnail_has_expand_button_that_opens_lightbox_without_toggling_checkbox(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="A", image_url="/uploads/shared.png")
        db.add(e)
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert "gallery-img-expand" in r.text
    assert "closest('.gallery-img-cell').querySelector('img')" in r.text
    assert "event.preventDefault(); event.stopPropagation();" in r.text  # doesn't also toggle the select checkbox


# ── all_world_image_urls (entity form's "choose from gallery" picker) ──────

def test_all_world_image_urls_unions_discovered_and_album_only(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="A", image_url="/uploads/discovered.png")
        db.add(e)
        db.commit()
        world = db.get(World, seed.world_a.id)
        # discovered.png is referenced by an entity; album-only.png sits only
        # in an album (e.g. a fresh upload nobody has attached anywhere yet).
        db.add(ImageAlbum(world_id=seed.world_a.id, name="A",
                           image_urls_json=json.dumps(["/uploads/discovered.png", "/uploads/album-only.png"])))
        db.commit()
        entries = all_world_image_urls(db, world)
    finally:
        db.close()
    assert [e["url"] for e in entries] == ["/uploads/album-only.png", "/uploads/discovered.png"]  # deduped + sorted
    by_url = {e["url"]: e["name"] for e in entries}
    assert by_url["/uploads/discovered.png"] == "A"  # used by entity "A" -> named after it
    assert by_url["/uploads/album-only.png"] == "album-only.png"  # no use yet -> falls back to filename


def test_all_world_image_urls_is_world_scoped(client, seed):
    db = SessionLocal()
    try:
        db.add(ImageAlbum(world_id=seed.world_b.id, name="B", image_urls_json=json.dumps(["/uploads/other.png"])))
        db.commit()
        world_a = db.get(World, seed.world_a.id)
        entries = all_world_image_urls(db, world_a)
    finally:
        db.close()
    assert entries == []


def test_album_view_shows_image_names(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="Named NPC", image_url="/uploads/npc.png")
        db.add(e)
        db.commit()
    finally:
        db.close()
    album_id = _make_album(seed.world_a.id, "Album", urls=["/uploads/npc.png", "/uploads/orphan.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/images/albums/{album_id}")
    assert "Named NPC" in r.text  # discovered use's label
    assert "orphan.png" in r.text  # no use -> filename fallback


def test_entity_new_form_includes_gallery_picker(client, seed):
    _make_album(seed.world_a.id, "Album", urls=["/uploads/pickme.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/new")
    assert r.status_code == 200
    assert "Choose from Gallery" in r.text
    assert "/uploads/pickme.png" in r.text
    assert "gallery-picker-overlay" in r.text


def test_entity_edit_form_includes_gallery_picker_scoped_to_entitys_own_world(client, seed):
    """Uses entity.world_id, not whatever world happens to be active in the
    cookie — the GM could be editing an entity while a different world is
    active elsewhere in the UI."""
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="Edit Me")
        db.add(e)
        db.commit()
        db.refresh(e)
        eid = e.id
    finally:
        db.close()
    _make_album(seed.world_a.id, "Album", urls=["/uploads/pickme-a.png"])
    _make_album(seed.world_b.id, "Album", urls=["/uploads/pickme-b.png"])

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)  # different world active than the entity's own
    r = client.get(f"/entity/{eid}/edit")
    assert r.status_code == 200
    assert "/uploads/pickme-a.png" in r.text
    assert "/uploads/pickme-b.png" not in r.text


def test_entity_form_gallery_picker_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/new").status_code == 403


# ── Nested albums (folders inside albums) ───────────────────────────────────

def test_create_sub_album_nests_under_parent(client, seed):
    parent_id = _make_album(seed.world_a.id, "Parent")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/albums/new", data={"name": "Child", "parent_id": str(parent_id)}, follow_redirects=False)
    assert r.status_code == 303
    child_id = int(r.headers["location"].rsplit("/", 1)[-1])

    db = SessionLocal()
    try:
        child = db.get(ImageAlbum, child_id)
        assert child.parent_id == parent_id
    finally:
        db.close()

    r = client.get(f"/images/albums/{parent_id}")
    assert "Child" in r.text


def test_index_page_only_lists_top_level_albums(client, seed):
    parent_id = _make_album(seed.world_a.id, "Parent")
    _make_album(seed.world_a.id, "Sub Album Q", parent_id=parent_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert "Parent" in r.text
    assert "Sub Album Q" not in r.text
    assert "1 sub-album" in r.text


def test_breadcrumb_shows_full_ancestor_chain(client, seed):
    grandparent_id = _make_album(seed.world_a.id, "Grandparent")
    parent_id = _make_album(seed.world_a.id, "Parent", parent_id=grandparent_id)
    child_id = _make_album(seed.world_a.id, "Child", parent_id=parent_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/images/albums/{child_id}")
    assert f'href="/images/albums/{grandparent_id}"' in r.text
    assert f'href="/images/albums/{parent_id}"' in r.text
    assert "Grandparent" in r.text and "Parent" in r.text


def test_sub_album_parent_must_be_in_same_world(client, seed):
    other_world_album_id = _make_album(seed.world_b.id, "Foreign")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/albums/new", data={"name": "Child", "parent_id": str(other_world_album_id)})
    assert r.status_code == 404


def test_deleting_parent_album_cascades_to_sub_albums(client, seed):
    parent_id = _make_album(seed.world_a.id, "Parent")
    child_id = _make_album(seed.world_a.id, "Child", parent_id=parent_id)
    grandchild_id = _make_album(seed.world_a.id, "Grandchild", parent_id=child_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/images/albums/{parent_id}/delete", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        assert db.get(ImageAlbum, parent_id) is None
        assert db.get(ImageAlbum, child_id) is None
        assert db.get(ImageAlbum, grandchild_id) is None
    finally:
        db.close()


def test_deleting_sub_album_leaves_parent_intact(client, seed):
    parent_id = _make_album(seed.world_a.id, "Parent")
    child_id = _make_album(seed.world_a.id, "Child", parent_id=parent_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/images/albums/{child_id}/delete")

    db = SessionLocal()
    try:
        assert db.get(ImageAlbum, parent_id) is not None
        assert db.get(ImageAlbum, child_id) is None
    finally:
        db.close()


def test_all_world_image_urls_includes_images_in_nested_sub_albums(client, seed):
    parent_id = _make_album(seed.world_a.id, "Parent")
    _make_album(seed.world_a.id, "Child", urls=["/uploads/nested-only.png"], parent_id=parent_id)
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        entries = all_world_image_urls(db, world)
    finally:
        db.close()
    assert "/uploads/nested-only.png" in [e["url"] for e in entries]


# ── Permanent image delete ──────────────────────────────────────────────────
# The album ✕ (galleryRemoveFromAlbum) only unlinks an image from that one
# album — it stays on disk and (if it's in other albums too) still shows up
# there. These cover the actual file-delete endpoint, which the reported bug
# was that no such thing existed for images sitting unused in a sub-album.

def test_delete_unused_album_image_removes_file_and_album_entry(client, seed):
    from app.main import UPLOADS_DIR
    album_id = _make_album(seed.world_a.id, "Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/images/albums/{album_id}/upload", files=_png_file())
    url = _album_urls(album_id)[0]
    fname = url.rsplit("/", 1)[-1]
    path = UPLOADS_DIR / "gallery" / fname
    assert path.exists()

    r = client.post("/images/delete", json={"url": url})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert not path.exists()
    assert _album_urls(album_id) == []


def test_delete_removes_image_from_every_album_that_references_it(client, seed):
    album1 = _make_album(seed.world_a.id, "One", urls=["/uploads/shared-orphan.png"])
    album2 = _make_album(seed.world_a.id, "Two", urls=["/uploads/shared-orphan.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/delete", json={"url": "/uploads/shared-orphan.png"})
    assert r.status_code == 200
    assert _album_urls(album1) == []
    assert _album_urls(album2) == []


def test_delete_blocked_while_image_still_in_use(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="In Use NPC",
                    image_url="/uploads/in-use.png")
        db.add(e)
        db.commit()
    finally:
        db.close()
    album_id = _make_album(seed.world_a.id, "Album", urls=["/uploads/in-use.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/images/delete", json={"url": "/uploads/in-use.png"})
    assert r.status_code == 400
    assert "In Use NPC" in r.json()["detail"]
    assert _album_urls(album_id) == ["/uploads/in-use.png"]  # untouched — nothing removed on a blocked delete


def test_delete_is_world_scoped(client, seed):
    _make_album(seed.world_b.id, "World B Album", urls=["/uploads/foreign-orphan.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/delete", json={"url": "/uploads/foreign-orphan.png"})
    assert r.status_code == 404


def test_delete_unknown_image_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/delete", json={"url": "/uploads/never-existed.png"})
    assert r.status_code == 404


def test_album_page_has_delete_button(client, seed):
    album_id = _make_album(seed.world_a.id, "Album", urls=["/uploads/pic.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/images/albums/{album_id}")
    assert "galleryDeleteImage(" in r.text
    assert "/images/delete" in r.text


def test_image_delete_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/delete", json={"url": "/uploads/whatever.png"})
    assert r.status_code == 403


# ── Spotlight (send an image to players as a popup) ─────────────────────────

def _world(world_id):
    db = SessionLocal()
    try:
        return db.get(World, world_id)
    finally:
        db.close()


def test_gm_send_spotlight_then_player_poll_sees_it(client, seed):
    _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/spotlight", json={"url": "/uploads/scene.png"})
    assert r.status_code == 200
    assert _world(seed.world_a.id).spotlight_image_url == "/uploads/scene.png"

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/spotlight")
    assert r.status_code == 200
    data = r.json()
    assert data["image_url"] == "/uploads/scene.png"
    assert data["version"] >= 1


def test_gm_clear_spotlight(client, seed):
    _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/images/spotlight", json={"url": "/uploads/scene.png"})
    version_after_send = _world(seed.world_a.id).spotlight_version

    r = client.post("/images/spotlight/clear")
    assert r.status_code == 200
    w = _world(seed.world_a.id)
    assert w.spotlight_image_url is None
    assert w.spotlight_version == version_after_send + 1  # bumped so pollers actively close it


def test_send_spotlight_rejects_url_not_in_this_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/spotlight", json={"url": "/uploads/never-existed.png"})
    assert r.status_code == 404
    assert _world(seed.world_a.id).spotlight_image_url is None


def test_spotlight_is_world_scoped(client, seed):
    _make_album(seed.world_a.id, "Album A", urls=["/uploads/scene-a.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/images/spotlight", json={"url": "/uploads/scene-a.png"})

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/spotlight")
    assert r.json()["image_url"] is None


def test_spotlight_version_unchanged_across_repeated_polls(client, seed):
    """The player poller only re-opens the popup when the version changes —
    two polls with nothing new sent in between must return the same
    version."""
    _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/images/spotlight", json={"url": "/uploads/scene.png"})

    v1 = client.get("/api/spotlight").json()["version"]
    v2 = client.get("/api/spotlight").json()["version"]
    assert v1 == v2


def test_send_spotlight_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/images/spotlight", json={"url": "/uploads/whatever.png"})
    assert r.status_code == 403
    r = client.post("/images/spotlight/clear")
    assert r.status_code == 403


def test_api_spotlight_is_player_safe(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/spotlight")
    assert r.status_code == 200


def test_api_spotlight_default_shape_before_anything_sent(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/spotlight")
    assert r.status_code == 200
    assert r.json() == {"version": 0, "image_url": None, "label": None}


def test_album_page_has_send_to_players_button(client, seed):
    album_id = _make_album(seed.world_a.id, "Album", urls=["/uploads/pic.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/images/albums/{album_id}")
    assert "ndSendSpotlight(" in r.text
    assert "/images/spotlight" in r.text


def test_all_images_page_has_send_to_players_button(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="A", image_url="/uploads/shared.png")
        db.add(e)
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert "ndSendSpotlight(" in r.text
    assert "gallery-img-send" in r.text


def test_gm_only_status_banner_shown_when_spotlight_active(client, seed):
    _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/images/spotlight", json={"url": "/uploads/scene.png"})

    r = client.get("/images")
    assert "spotlight-banner" in r.text
    assert "ndSpotlightClear()" in r.text


def test_sender_suppresses_own_reopened_popup(client, seed):
    """Regression test found via live Playwright verification: the sender's
    own page also runs the poller (so they see their broadcast as
    confirmation), and ndSendSpotlight/ndSpotlightClear reload the page to
    refresh the status banner afterward — without suppression, that reload's
    first poll tick would immediately reopen the just-sent popup on top of
    the banner's own Stop button, making it unclickable. Both action
    functions must arm the one-shot suppression before reloading, and
    base.html's poller must consume it."""
    album_id = _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/images/albums/{album_id}")
    assert "ndSpotlightSuppressNextPoll();" in r.text  # from ndSendSpotlight

    r = client.get("/")
    assert "function ndSpotlightSuppressNextPoll()" in r.text
    assert "nd_spotlight_suppress_once" in r.text
    assert "if (suppressOnce) { suppressOnce = false; return; }" in r.text
    assert "ndSpotlightSuppressNextPoll();\n  location.reload();" in r.text  # from ndSpotlightClear


def test_status_banner_hidden_from_players_and_when_inactive(client, seed):
    _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/images")
    assert "spotlight-banner" not in r.text  # nothing active yet

    client.post("/images/spotlight", json={"url": "/uploads/scene.png"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert "spotlight-banner" not in r.text  # GM-only, even while active


def test_base_html_poller_present_for_gm_and_player(client, seed):
    for email, password in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        login(client, email, password)
        client.cookies.set("active_world", seed.world_a.slug)
        r = client.get("/")
        assert "pollSpotlight" in r.text
        assert "openLightbox(data.image_url, data.label" in r.text


# ── GET /api/gallery/browse — lazy album browsing for the shared picker ────

def test_gallery_browse_root_lists_top_level_albums_only(client, seed):
    top_id = _make_album(seed.world_a.id, "Top Album", urls=["/uploads/a.png", "/uploads/b.png"])
    _make_album(seed.world_a.id, "Sub Album", urls=["/uploads/c.png"], parent_id=top_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/api/gallery/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["breadcrumb"] == []
    assert body["images"] is None
    assert [a["name"] for a in body["albums"]] == ["Top Album"]  # sub-album not shown at root
    top = body["albums"][0]
    assert top["id"] == top_id
    assert top["image_count"] == 2
    assert top["sub_album_count"] == 1
    assert top["cover_url"] == "/uploads/a.png"


def test_gallery_browse_into_album_lists_its_images_and_sub_albums(client, seed):
    top_id = _make_album(seed.world_a.id, "Top Album", urls=["/uploads/a.png"])
    sub_id = _make_album(seed.world_a.id, "Sub Album", urls=["/uploads/c.png"], parent_id=top_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/api/gallery/browse?album_id={top_id}")
    assert r.status_code == 200
    body = r.json()
    assert [b["name"] for b in body["breadcrumb"]] == ["Top Album"]
    assert [a["name"] for a in body["albums"]] == ["Sub Album"]
    assert body["albums"][0]["id"] == sub_id
    assert [i["url"] for i in body["images"]] == ["/uploads/a.png"]


def test_gallery_browse_breadcrumb_reflects_full_nesting_depth(client, seed):
    root_id = _make_album(seed.world_a.id, "Root")
    mid_id = _make_album(seed.world_a.id, "Middle", parent_id=root_id)
    leaf_id = _make_album(seed.world_a.id, "Leaf", urls=["/uploads/leaf.png"], parent_id=mid_id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/api/gallery/browse?album_id={leaf_id}")
    assert r.status_code == 200
    body = r.json()
    assert [b["name"] for b in body["breadcrumb"]] == ["Root", "Middle", "Leaf"]
    assert body["images"] == [{"url": "/uploads/leaf.png", "name": "leaf.png"}]


def test_gallery_browse_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/gallery/browse").status_code == 403


def test_gallery_browse_unknown_album_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/gallery/browse?album_id=999999").status_code == 404


def test_gallery_browse_cross_world_album_404s(client, seed):
    other_id = _make_album(seed.world_b.id, "Other World Album")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/gallery/browse?album_id={other_id}").status_code == 404


def test_gallery_picker_modal_includes_breadcrumb_container(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/new")
    assert 'id="gallery-picker-breadcrumb"' in r.text
