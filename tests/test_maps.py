"""Filesystem-backed maps (JSON files under _MAPS_DIR) previously had no
delete or rename at all; Schematic battle-maps had delete but no rename.
Covers both new code paths, plus the world-ownership check that keeps a GM
from renaming/deleting a map that belongs to a different world.
"""
import json

import pytest

from app.database import SessionLocal
from app.main import _MAPS_DIR
from app.models import MapOverlay, Schematic

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean_maps_dir():
    """_MAPS_DIR isn't reset by the `client` fixture (only the DB and uploads
    dir are), so a map file written by one test would otherwise leak into the
    next test's fresh (but same-slug-space) DB."""
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


def test_map_rename(client, seed):
    _make_map(seed.world_a.id, "old-cave")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/old-cave/rename", data={"name": "New Cave"}, follow_redirects=False)
    assert r.status_code == 303
    data = json.loads((_MAPS_DIR / "old-cave.json").read_text())
    assert data["name"] == "New Cave"
    # The slug/URL is unchanged — rename only touches the display name.
    assert (_MAPS_DIR / "old-cave.json").exists()


def test_map_rename_blank_rejected(client, seed):
    _make_map(seed.world_a.id, "old-cave")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/old-cave/rename", data={"name": "   "})
    assert r.status_code == 400
    data = json.loads((_MAPS_DIR / "old-cave.json").read_text())
    assert data["name"] == "Test Map"


def test_map_rename_wrong_world_404s(client, seed):
    _make_map(seed.world_b.id, "b-only-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/b-only-map/rename", data={"name": "Hijacked"})
    assert r.status_code == 404
    data = json.loads((_MAPS_DIR / "b-only-map.json").read_text())
    assert data["name"] == "Test Map"


def test_map_delete_removes_file_and_overlay(client, seed):
    _make_map(seed.world_a.id, "doomed-map")
    db = SessionLocal()
    try:
        db.add(MapOverlay(slug="doomed-map", custom_markers_json="[]", custom_regions_json="[]"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/doomed-map/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (_MAPS_DIR / "doomed-map.json").exists()

    db = SessionLocal()
    try:
        assert db.query(MapOverlay).filter(MapOverlay.slug == "doomed-map").first() is None
    finally:
        db.close()


def test_map_delete_wrong_world_404s(client, seed):
    _make_map(seed.world_b.id, "b-only-map-2")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/b-only-map-2/delete")
    assert r.status_code == 404
    assert (_MAPS_DIR / "b-only-map-2.json").exists()


def test_map_delete_missing_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/does-not-exist/delete")
    assert r.status_code == 404


def _make_schematic(db, world_id, slug, name="Test Schematic"):
    s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False,
                   canvas_width=2000, canvas_height=1500, canvas_bg="dark", elements_json="[]")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_schematic_rename(client, seed):
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "old-lair")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/old-lair/rename", data={"name": "New Lair"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        s = db.query(Schematic).filter(Schematic.slug == "old-lair").first()
        assert s.name == "New Lair"
        assert s.slug == "old-lair"  # slug/URL stays stable
    finally:
        db.close()


def test_schematic_rename_blank_rejected(client, seed):
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "old-lair")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/old-lair/rename", data={"name": ""})
    assert r.status_code == 400


def test_schematic_editor_shows_background_image(client, seed):
    """Regression test: image_url used to be saved on the Schematic row (via
    the upload endpoint) but the editor's SVG never actually referenced it —
    an uploaded background silently never appeared. Now it must render as an
    <image> tied to schematic.image_url."""
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "with-bg")
        s.image_url = "/uploads/schematics/with-bg.webp"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/maps/schematic/with-bg")
    assert r.status_code == 200
    assert 'id="bg-image"' in r.text
    assert "/uploads/schematics/with-bg.webp" in r.text


def test_schematic_editor_no_background_image_element_when_unset(client, seed):
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "no-bg")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/maps/schematic/no-bg")
    assert r.status_code == 200
    assert 'id="bg-image"' not in r.text
