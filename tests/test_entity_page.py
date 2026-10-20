"""An entity page's layouts (Long page / Tabs / Two columns / Reader): the registry section, the pure decisions in static/js/entity-page.js
under Node, and the page's wiring - the blocks are tagged, the choice is the person's own and the server only renders what the viewer may see."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import Entity, EntityNote

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_entity_page_section():
    sp = view_prefs.spec("entity_page")
    assert [m["id"] for m in sp["modes"]] == ["classic", "tabs", "cols", "reader"] and sp["default"] == "classic"
    assert {m["id"]: m["label"] for m in sp["modes"]} == {"classic": "Long page", "tabs": "Tabs", "cols": "Two columns", "reader": "Reader"}
    assert view_prefs.clean("entity_page", {"mode": "cols"})["mode"] == "cols" and view_prefs.clean("entity_page", {"mode": "web"})["mode"] == "classic"
    assert "entity_page" in view_prefs.SECTION_TITLES


def _run(expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/entity-page.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_tabs_columns_and_the_mode_that_fits_the_screen():
    allg = ["overview", "details", "notes", "links", "media", "ai"]
    assert _run("c.tabsFor(['ai', 'notes', 'overview'])") == ["overview", "notes", "ai"]                          # always in the same order, only what exists
    assert _run("c.pickTab('notes', 'media', ['overview', 'notes'])") == "notes"                                  # the address wins
    assert _run("c.pickTab('', 'media', ['overview', 'media'])") == "media" and _run("c.pickTab('nope', 'gone', ['overview'])") == "overview"
    assert _run("c.pickTab('', '', [])") == ""
    assert _run("c.groupOfHash('#tab-links')") == "links" and _run("c.groupOfHash('#other')") == ""
    assert _run(f"c.columnsOf({json.dumps(allg)})") == {"main": ["overview", "notes", "ai"], "side": ["details", "links", "media"]}
    assert _run("c.effectiveMode('cols', 1280)") == "cols" and _run("c.effectiveMode('cols', 600)") == "classic"        # two columns need room
    assert _run("c.effectiveMode('tabs', 390)") == "tabs" and _run("c.effectiveMode('reader', 390)") == "reader" and _run("c.effectiveMode('x', 1280)") == "classic"


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_page_tags_its_blocks_and_only_renders_what_the_viewer_may_see(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, name="Hub City", kind="location", visible_to_players=True, body="## History\nLong ago.\n[gmonly]a secret door[/gmonly]")
        db.add(e)
        db.commit()
        db.add(EntityNote(entity_id=e.id, content="GM scheme", visible_to_players=False))
        db.add(EntityNote(entity_id=e.id, content="A rumour", visible_to_players=True))
        db.commit()
        eid = e.id
    finally:
        db.close()
    _as(client, seed, True)
    gm = client.get(f"/entity/{eid}").text
    for g in ("overview", "notes", "links", "ai"):
        assert f'data-ep="{g}"' in gm, g
    assert 'id="ep-layoutbar"' in gm and "/static/js/entity-page.js" in gm and 'data-ep-saved=""' in gm and "GM scheme" in gm
    _as(client, seed, False)
    pl = client.get(f"/entity/{eid}").text
    assert 'data-ep="overview"' in pl and 'data-ep="notes"' in pl and 'data-ep="ai"' not in pl                       # the layouts only arrange what is there
    assert "GM scheme" not in pl and "a secret door" not in pl and "A rumour" in pl
    assert client.post("/api/view-prefs/entity_page", json={"mode": "tabs"}).json()["mode"] == "tabs"
    assert 'data-ep-saved="tabs"' in client.get(f"/entity/{eid}").text
    _as(client, seed, True)
    assert 'data-ep-saved=""' in client.get(f"/entity/{eid}").text                                                    # the GM's own page is on its default
