"""Folder layouts of an entity list (section "folders": tree, chips, tiles, columns, drawer): the registry, the pure folder model under Node, and the
page's wiring to nd-pagelayout.js / nd-folders.js."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs

from .conftest import GM_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_folders_section_offers_five_layouts_and_today_stays_the_default():
    sp = view_prefs.spec("folders")
    assert [m["id"] for m in sp["modes"]] == ["tree", "chips", "tiles", "columns", "drawer"] and sp["default"] == "tree"
    assert sp["modes"][0]["label"] == "Tree sidebar"
    assert view_prefs.clean("folders", {"mode": "tiles"})["mode"] == "tiles" and view_prefs.clean("folders", {"mode": "grid"})["mode"] == "tree"
    assert "folders" in view_prefs.SECTION_TITLES


def _run(expr):
    folders = json.dumps(["Characters", "NPCs", "NPCs/Court", "NPCs/Court/Kitchen", "Regions/North"])
    counts = json.dumps({"NPCs": 90, "NPCs/Court": 4, "Characters": 1, "Regions/North": 2, "": 3})
    js = "const c = require('./static/js/nd-folders.js'); const m = c.model(" + folders + ", " + counts + "); console.log(JSON.stringify(" + expr + "))"
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_the_folder_model_lists_children_counts_and_trails():
    # a path that exists only as the parent of another ("Regions") still gets its own folder
    assert _run("m.children('').map(c => [c.name, c.count])") == [["Characters", 1], ["NPCs", 94], ["Regions", 2]]
    assert _run("m.children('NPCs').map(c => c.path)") == ["NPCs/Court"]
    assert _run("m.children('NPCs/Court/Kitchen')") == []
    assert _run("[m.trail('NPCs/Court'), m.trail(''), m.parentOf('NPCs/Court'), m.parentOf('NPCs')]") == [["NPCs", "NPCs/Court"], [], "NPCs", ""]
    # a folder's count includes everything below it, and "Unfiled" ("") is not part of any folder
    assert _run("[m.count('NPCs'), m.count('NPCs/Court'), m.count('Regions')]") == [94, 4, 2]
    assert _run("[m.has('Regions'), m.has('Nowhere')]") == [True, False]


def test_the_entity_list_has_the_bar_the_containers_and_the_script(client, seed):
    from app.database import SessionLocal
    from app.models import Entity
    db = SessionLocal()
    try:
        for name, folder in (("Aldric", "NPCs/Court"), ("Brenna", "NPCs"), ("Cade", "")):
            db.add(Entity(world_id=seed.world_a.id, kind="character", name=f"{name} F", folder=folder, body="x", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/kind/character?folder=NPCs").text
    assert 'data-pl="folders"' in html and 'data-pl-bar="folders"' in html
    for needle in ('id="folder-nav"', 'id="fn-chips"', 'id="fn-tiles"', 'id="fn-columns"', 'id="fn-drawer-btn"', 'id="fn-backdrop"', "/static/js/nd-folders.js"):
        assert needle in html, needle
    assert 'data-pl-default="tree"' in html and 'class="folder-sidebar"' in html            # today's tree stays the default
    # a list with no folders gets none of it
    other = client.get("/kind/location").text
    assert 'id="folder-nav"' not in other and 'data-pl-bar="folders"' not in other
    # the saved choice comes back from the account
    assert client.post("/api/view-prefs/folders", json={"mode": "columns"}).status_code == 200
    assert 'data-pl-saved="columns"' in client.get("/kind/character?folder=NPCs").text


def test_the_stylesheet_shows_one_folder_layout_at_a_time():
    css = open("static/style.css").read()
    for mode in ("chips", "tiles", "columns", "drawer"):
        assert f'.list-layout[data-view="{mode}"]' in css, mode
    assert '.list-layout[data-pl="folders"]:not([data-view="tree"]):not([data-view="drawer"]) .folder-sidebar { display: none; }' in css
