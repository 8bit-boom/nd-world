"""Folders on the Maps page: maps (a "folder" key in their JSON) and schematics (a column) are browsed one folder at a time."""
import json

import pytest

from app.database import SessionLocal
from app.main import _MAPS_DIR
from app.models import Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean_maps_dir():
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()
    yield
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()


def _map(world_id, slug, name, folder=None):
    d = {"name": name, "world_id": world_id, "width": 100, "height": 100, "markers": []}
    if folder is not None:
        d["folder"] = folder
    (_MAPS_DIR / f"{slug}.json").write_text(json.dumps(d), encoding="utf-8")


def _schem(world_id, slug, name, folder=None):
    db = SessionLocal()
    try:
        db.add(Schematic(world_id=world_id, name=name, slug=slug, is_html=False, canvas_width=400, canvas_height=300,
                         canvas_bg="dark", elements_json="[]", folder=folder))
        db.commit()
    finally:
        db.close()


def _folder_of_schem(slug):
    db = SessionLocal()
    try:
        return db.query(Schematic).filter(Schematic.slug == slug).first().folder
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_page_shows_one_folder_at_a_time(client, seed):
    w = seed.world_a.id
    _map(w, "top-map", "Top Map")
    _map(w, "hue-map", "Hue Map", "Cities/Hue")
    _map(w, "dock-map", "Dock Map", "Cities")
    _schem(w, "hue-schem", "Hue Schem", "Cities/Hue")
    _gm(client, seed)
    r = client.get("/maps")
    assert "Top Map" in r.text and "Hue Map" not in r.text and "Dock Map" not in r.text and "Hue Schem" not in r.text
    assert "📂 Cities (3)" in r.text
    r = client.get("/maps?folder=Cities")
    assert "Dock Map" in r.text and "Top Map" not in r.text and "Hue Map" not in r.text
    assert "📂 Hue (2)" in r.text
    r = client.get("/maps?folder=Cities/Hue")
    assert "Hue Map" in r.text and "Hue Schem" in r.text and "Dock Map" not in r.text


def test_players_browse_folders_but_have_no_controls(client, seed):
    _map(seed.world_a.id, "hue-map", "Hue Map", "Cities")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps?folder=Cities")
    assert r.status_code == 200 and "Hue Map" in r.text
    assert "📁 Move" not in r.text and "Rename folder" not in r.text
    assert client.post("/maps/hue-map/folder", data={"folder": "X"}, follow_redirects=False).status_code in (401, 403)


def test_create_in_folder_and_move(client, seed):
    _gm(client, seed)
    r = client.post("/maps/new", data={"name": "Folder Map", "folder": " Cities\\ Hue/./.. "}, follow_redirects=False)
    assert r.status_code == 303
    data = json.loads((_MAPS_DIR / "folder-map.json").read_text())
    assert data["folder"] == "Cities/ Hue" or data["folder"] == "Cities/Hue"
    r = client.post("/maps/schematic/new", data={"name": "Folder Schem", "folder": "A/B"}, follow_redirects=False)
    assert r.status_code == 303
    assert _folder_of_schem("folder-schem") == "A/B"
    r = client.post("/maps/folder-map/folder", data={"folder": "Moved", "back": "Cities"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maps?folder=Cities"
    assert json.loads((_MAPS_DIR / "folder-map.json").read_text())["folder"] == "Moved"
    r = client.post("/maps/folder-map/folder", data={"folder": "", "back": ""}, follow_redirects=False)
    assert r.headers["location"] == "/maps"
    assert "folder" not in json.loads((_MAPS_DIR / "folder-map.json").read_text())
    r = client.post("/maps/schematic/folder-schem/folder", data={"folder": "", "back": "A B"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maps?folder=A%20B"
    assert _folder_of_schem("folder-schem") is None


def test_rename_folder_moves_everything_under_it(client, seed):
    w = seed.world_a.id
    _map(w, "m1", "M1", "Old")
    _map(w, "m2", "M2", "Old/Sub")
    _map(w, "m3", "M3", "Older")
    _schem(w, "s1", "S1", "Old/Sub")
    _gm(client, seed)
    r = client.post("/map-folders/rename", data={"old": "Old", "new": "New/Place"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maps?folder=New/Place"
    f = lambda s: json.loads((_MAPS_DIR / f"{s}.json").read_text()).get("folder")
    assert (f("m1"), f("m2"), f("m3")) == ("New/Place", "New/Place/Sub", "Older")
    assert _folder_of_schem("s1") == "New/Place/Sub"
    assert client.post("/map-folders/rename", data={"old": "New", "new": "New/In"}).status_code == 400
    assert client.post("/map-folders/rename", data={"old": "", "new": "X"}).status_code == 400
    r = client.post("/map-folders/rename", data={"old": "New/Place", "new": ""}, follow_redirects=False)
    assert (f("m1"), f("m2")) == (None, "Sub")


def test_toolbar_pastes_a_copied_markdown_file(client, seed):
    """Ctrl+V of a copied .md/.txt file into a note/entity field inserts its text (the field had only image paste before)."""
    js = client.get("/static/js/text-format-toolbar.js").text
    assert "\".markdown\"" in js and "setRangeText(text" in js


def test_plain_text_fields_take_a_dropped_or_pasted_md_file(client, seed):
    """nd-clipboard.js (on every page) inserts a dropped/pasted .md/.txt file into textareas without the formatting toolbar."""
    js = client.get("/static/js/nd-clipboard.js").text
    assert "plainTextField" in js and "'.markdown'" in js and "addEventListener('drop'" in js
