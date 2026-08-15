"""Tests for the "Choose from Gallery" picker wired into maps and
schematics (map creation, the maps-list replace controls, and the
schematic editor's embed-image tool) — the same shared component already
used on the entity form (app/templates/_gallery_picker_modal.html,
static/js/gallery-picker.js), reused here instead of duplicated.

The one thing that's genuinely new risk here (not present for the entity
form's picker, which only GM-only routes ever reach): GET /maps is
player-safe. all_world_image_urls() returns every image used anywhere in
the world, including on GM-only/hidden entities, so gallery_images must
stay empty for a non-GM viewer of /maps rather than leaking that list.
"""
import json

import pytest

from app.database import SessionLocal
from app.main import _MAPS_DIR
from app.models import Entity, Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean_maps_dir():
    """_MAPS_DIR isn't reset by the `client` fixture (only the DB and
    uploads dir are) — same rationale/pattern as test_maps.py."""
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()
    yield
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()


def _make_map(world_id, slug, name="Test Map"):
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / f"{slug}.json").write_text(json.dumps({
        "name": name, "world_id": world_id, "width": 3072, "height": 3072, "markers": [],
    }), encoding="utf-8")


def _make_schematic(db, world_id, slug, name="Test Schematic"):
    s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False,
                   canvas_width=2000, canvas_height=1500, canvas_bg="dark", elements_json="[]")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_map_new_form_has_gallery_picker_button_and_data(client, seed):
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="location", name="Secret Vault",
                     image_url="/uploads/vault.png", visible_to_players=False)
        db.add(ent)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/new")
    assert r.status_code == 200
    assert "ndGalleryPickerOpen(onMapGalleryPick)" in r.text
    assert "/uploads/vault.png" in r.text  # GM-only route, safe to include GM-hidden images
    assert 'id="gallery-picker-overlay"' in r.text


def test_maps_page_gm_sees_gallery_picker_with_full_image_list(client, seed):
    _make_map(seed.world_a.id, "gm-map")
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="location", name="Secret Vault",
                     image_url="/uploads/vault.png", visible_to_players=False)
        db.add(ent)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    assert 'id="gallery-picker-overlay"' in r.text
    assert "ndMapGalleryPick" in r.text
    assert "/uploads/vault.png" in r.text


def test_maps_page_player_does_not_see_gallery_picker_or_image_list(client, seed):
    """The security-relevant regression guard: /maps is player-safe, so a
    player must never receive the full all_world_image_urls() payload
    (which is not visibility-filtered) even though the GM's picker on this
    same page does show it."""
    _make_map(seed.world_a.id, "player-map")
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="location", name="Secret Vault",
                     image_url="/uploads/vault.png", visible_to_players=False)
        db.add(ent)
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    assert 'id="gallery-picker-overlay"' not in r.text
    # The actual clickable button (not just the word "Gallery", which also
    # appears in a code comment inside the always-present shared <script>
    # block describing the GM-only trigger's behavior).
    assert 'onclick="ndMapGalleryPick(this)"' not in r.text
    assert "/uploads/vault.png" not in r.text
    assert "window.NDGalleryImages" not in r.text


def test_schematic_editor_has_gallery_picker_button_and_data(client, seed):
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "gallery-sch")
        slug = s.slug
        ent = Entity(world_id=seed.world_a.id, kind="location", name="Secret Vault",
                     image_url="/uploads/vault.png", visible_to_players=False)
        db.add(ent)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/maps/schematic/{slug}")
    assert r.status_code == 200
    assert "pickImageFromGallery()" in r.text
    assert 'id="gallery-picker-overlay"' in r.text
    assert "/uploads/vault.png" in r.text  # GM-only editor route, safe


def test_gallery_picker_js_shares_the_upload_form_reuse_trick(client, seed):
    """ndGalleryUrlToFile is what lets a picked gallery image ride the
    existing (already-tested) multipart upload routes for maps/schematics
    without a new backend endpoint — confirm the shared module actually
    ships it."""
    r = client.get("/static/js/gallery-picker.js")
    assert r.status_code == 200
    js = r.text
    assert "async function ndGalleryUrlToFile(url)" in js
    assert "function ndGalleryPickerOpen(onPick)" in js
    assert "function ndGalleryPickerClose()" in js


def test_map_form_gallery_pick_populates_the_existing_file_input(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/new")
    js_inline = r.text
    assert "async function onMapGalleryPick(entry)" in js_inline
    assert "ndGalleryUrlToFile(entry.url)" in js_inline
    assert "input.files = dt.files" in js_inline


def test_maps_list_gallery_pick_submits_the_replace_form(client, seed):
    _make_map(seed.world_a.id, "some-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    js_inline = r.text
    assert "function ndMapGalleryPick(btn)" in js_inline
    assert "form.submit()" in js_inline
