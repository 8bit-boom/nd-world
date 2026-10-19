"""The character page's layouts (Tabs / Play) and the tabs pinned to the phone's bottom bar: the registry's hub section, the pure helpers in
static/js/hub-layout.js under Node, and the page's wiring for a player - whose choice is their own, kept on their account."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import PlayerCharacter, UserViewPref

from .conftest import PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_hub_section():
    sp = view_prefs.spec("hub")
    assert [m["id"] for m in sp["modes"]] == ["tabs", "play", "dashboard", "split"] and sp["default"] == "tabs"
    assert {f["id"]: f["label"] for f in sp["filters"]}["keepsakes"] == "Kept" and len(sp["filters"]) == 13
    # pinned tabs: only known ones, no repeats, at most four
    out = view_prefs.clean("hub", {"mode": "play", "filters": ["quests", "quests", "log", "nope", "world", "notes", "sheet"]})
    assert out["mode"] == "play" and out["filters"] == ["quests", "log", "world", "notes"]
    assert view_prefs.clean("hub", {"mode": "pads"})["mode"] == "tabs" and view_prefs.clean("hub", {})["filters"] == []
    assert view_prefs.clean("hub", {"mode": "dashboard"})["mode"] == "dashboard" and view_prefs.clean("hub", {"mode": "split"})["mode"] == "split"


def _run(expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/hub-layout.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_pins_swipes_and_scroll_hints():
    tabs = ["sheet", "cockpit", "journey", "quests", "notes", "world", "log"]
    assert _run(f"c.pinsFor([], {json.dumps(tabs)}, 4)") == ["sheet", "journey", "quests", "notes"]                       # the default four
    assert _run(f"c.pinsFor([], {json.dumps(tabs[:4] + ['world'])}, 4)") == ["sheet", "journey", "quests"]                # a tab the viewer does not have is skipped
    assert _run(f"c.pinsFor(['log', 'world', 'sheet', 'quests', 'journey'], {json.dumps(tabs)}, 4)") == ["sheet", "log", "world", "quests"]   # Sheet always, then the first three
    assert _run("c.swipePage(-90, 20, 300)") == 1 and _run("c.swipePage(90, -10, 300)") == -1       # left = next page, right = previous
    assert _run("c.swipePage(-90, 70, 300)") == 0 and _run("c.swipePage(-30, 0, 100)") == 0 and _run("c.swipePage(-90, 0, 2000)") == 0
    assert _run("c.stepPage(['a', 'b', 'all'], 'b', 1)") == "b" and _run("c.stepPage(['a', 'b', 'all'], 'a', 1)") == "b" and _run("c.stepPage(['a', 'b'], 'a', -1)") == "a"
    # Dashboard / Split put the pinned tabs beside the sheet - not the sheet itself, nor the heavy Cockpit / Pages
    assert _run("c.railTabs(['sheet', 'journey', 'cockpit', 'quests', 'places', 'quests', 'nope'], ['sheet', 'journey', 'cockpit', 'quests', 'places'])") == ["journey", "quests"]
    assert _run("c.railTabs([], ['sheet'])") == [] and _run("c.railTabs(undefined, ['sheet'])") == []
    assert _run("c.scrollHints(0, 300, 700)") == {"left": False, "right": True} and _run("c.scrollHints(400, 300, 700)") == {"left": True, "right": False}


def test_a_players_layout_is_theirs_and_the_page_uses_it(client, seed):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Aria")
        db.add(pc)
        db.commit()
        pid = pc.id
    finally:
        db.close()
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/characters/{pid}").text
    assert 'data-layout="tabs"' in page and 'data-account=""' in page and "/static/js/hub-layout.js" in page
    assert 'id="pch-layout-btn"' in page and "Layout &amp; tabs" in page.replace("&", "&amp;") or "Layout & tabs" in page
    assert 'id="pch-rail"' in page and "Pinned tabs" in page
    r = client.post("/api/view-prefs/hub", json={"mode": "play", "filters": ["world", "log", "bogus"]})
    assert r.status_code == 200 and r.json()["mode"] == "play" and r.json()["filters"] == ["world", "log"]
    page = client.get(f"/characters/{pid}").text
    assert 'data-layout="play"' in page and 'data-account="1"' in page and '["world", "log"]' in page.replace("&#34;", '"')
    # it is this player's own row: nobody else's page changes
    db = SessionLocal()
    try:
        assert db.query(UserViewPref).filter(UserViewPref.section == "hub").count() == 1
    finally:
        db.close()


def test_dashboard_and_split_are_saved_and_rendered_for_the_page(client, seed):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Vex")
        db.add(pc)
        db.commit()
        pid = pc.id
    finally:
        db.close()
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for mode in ("dashboard", "split"):
        assert client.post("/api/view-prefs/hub", json={"mode": mode, "filters": ["quests", "journey"]}).json()["mode"] == mode
        page = client.get(f"/characters/{pid}").text
        assert f'data-layout="{mode}"' in page and "maybeMountSide" in page
