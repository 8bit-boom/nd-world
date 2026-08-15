"""Tests for app/uploads.py's unique_upload_filename: every image-upload
route (see app/main.py's save_upload, characters.py/gallery.py/
home_content.py/professions.py/races.py's per-router upload helpers)
generates its stored filename through this one function so the uploader's
original filename survives — as a slugified suffix on the storage filename,
not the whole name (the random hex prefix is still what guarantees no two
uploads can collide) — instead of being discarded for a bare UUID. Format
conversion (app/imaging.py's convert_image_to, exercised in
test_bulk_image_convert.py) only ever swaps the extension on that stored
filename, so this is also what makes a converted image keep its
recognizable name rather than degrading into a hex string."""
import io

from app.gallery import image_display_name
from app.uploads import unique_upload_filename

from .conftest import GM_PASSWORD, login


def _png_file(name):
    return {"file": (name, io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000), "image/png")}


# ── unique_upload_filename ──────────────────────────────────────────────────

def test_preserves_a_slugified_version_of_the_original_name():
    fname = unique_upload_filename("Goblin Portrait.PNG", ".png")
    assert fname.endswith("-goblin-portrait.png")


def test_still_collision_safe_via_a_random_hex_prefix():
    a = unique_upload_filename("same-name.png", ".png")
    b = unique_upload_filename("same-name.png", ".png")
    assert a != b  # same original name, different upload -> different stored file
    assert a.endswith("-same-name.png")
    assert b.endswith("-same-name.png")


def test_strips_punctuation_and_spaces_into_dashes():
    fname = unique_upload_filename("My Cool Map!! (final) v2.jpg", ".jpg")
    assert fname.endswith("-my-cool-map-final-v2.jpg")


def test_long_original_name_is_truncated_not_dropped():
    long_name = "a" * 200
    fname = unique_upload_filename(f"{long_name}.png", ".png")
    stem = fname.split("-", 1)[1][:-len(".png")]
    assert 0 < len(stem) <= 60


def test_falls_back_to_bare_hex_when_nothing_slugifiable():
    # No alphanumeric characters at all to build a name from.
    fname = unique_upload_filename("😀🎉.png", ".png")
    assert fname.endswith(".png")
    assert "-" not in fname  # pure hex fallback, no readable suffix to append


def test_falls_back_to_bare_hex_for_blank_or_missing_filename():
    assert unique_upload_filename("", ".png").endswith(".png")
    assert unique_upload_filename(None, ".png").endswith(".png")


# ── image_display_name strips the stored-filename prefix back off ──────────

def test_display_name_strips_hex_prefix_from_unattached_upload():
    fname = unique_upload_filename("Goblin Portrait.png", ".png")
    assert image_display_name(f"/uploads/gallery/{fname}") == "goblin-portrait.png"


def test_display_name_leaves_a_manually_placed_filename_untouched():
    # A filename that doesn't match the hex-prefix pattern (e.g. seeded test
    # data, or a pre-existing install's images from before this feature)
    # passes through unchanged rather than being mangled.
    assert image_display_name("/uploads/gallery/album-only.png") == "album-only.png"


def test_display_name_prefers_the_use_label_when_attached():
    fname = unique_upload_filename("Goblin Portrait.png", ".png")
    name = image_display_name(f"/uploads/gallery/{fname}", uses=[{"label": "Goblin Scout"}])
    assert name == "Goblin Scout"


# ── End-to-end: a real upload's stored filename carries the original name ──

def test_entity_image_upload_preserves_original_filename(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-image", files=_png_file("Ancient Ruins.png"))
    assert r.status_code == 200
    url = r.json()["url"]
    assert "ancient-ruins" in url


def test_gallery_album_upload_preserves_original_filename(client, seed):
    from app.database import SessionLocal
    from app.models import ImageAlbum

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        album = ImageAlbum(world_id=seed.world_a.id, name="Test Album", image_urls_json="[]")
        db.add(album)
        db.commit()
        db.refresh(album)
        album_id = album.id
    finally:
        db.close()

    r = client.post(f"/images/albums/{album_id}/upload", files=_png_file("Crumbling Tower.png"))
    assert r.status_code in (200, 303)

    r = client.get(f"/images/albums/{album_id}")
    assert "crumbling-tower" in r.text
