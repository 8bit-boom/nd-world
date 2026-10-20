"""The home page's layouts (Classic / Session night / Focus / My widgets): the registry section with its id lists, the pure helpers in
static/js/home-layouts.js under Node, and the page's wiring - every block is tagged and the saved choice is the person's own."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_home_section_keeps_validated_id_lists():
    sp = view_prefs.spec("home")
    assert [m["id"] for m in sp["modes"]] == ["classic", "night", "focus", "widgets"] and sp["default"] == "classic"
    out = view_prefs.clean("home", {"mode": "widgets", "order": ["recent", "links", "recent", "Bad Id", "x" * 50], "hidden": ["maps", 5, "tags"]})
    assert out["mode"] == "widgets" and out["order"] == ["recent", "links"] and out["hidden"] == ["maps", "tags"]
    assert view_prefs.clean("home", {})["order"] == [] and view_prefs.clean("home", {"mode": "web"})["mode"] == "classic"
    assert len(view_prefs.clean("home", {"order": [f"b{i}" for i in range(40)]})["order"]) == 16                       # capped
    assert "order" not in view_prefs.clean("entities", {"order": ["a"]})                                                # only sections that declare lists


def _run(expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/home-layouts.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_block_order_moves_hiding_and_what_each_layout_shows():
    present = ["hero", "links", "kinds", "recent"]
    assert _run(f"c.orderBlocks({json.dumps(present)}, ['recent', 'gone', 'links', 'recent'])") == ["recent", "links", "hero", "kinds"]       # saved first, the rest as the page has them
    assert _run("c.moveBlock(['a', 'b', 'c'], 'b', -1)") == ["b", "a", "c"] and _run("c.moveBlock(['a', 'b', 'c'], 'a', -1)") == ["a", "b", "c"]
    assert _run("c.moveBlock(['a', 'b', 'c'], 'c', 1)") == ["a", "b", "c"] and _run("c.moveBlock(['a', 'b'], 'zz', 1)") == ["a", "b"]
    assert _run("c.toggleHidden(['a'], 'b')") == ["a", "b"] and _run("c.toggleHidden(['a', 'b'], 'a')") == ["b"]
    assert _run("c.showsBlock('classic', 'maps', [])") is True and _run("c.showsBlock('focus', 'links', [])") is True and _run("c.showsBlock('focus', 'maps', [])") is False
    assert _run("c.showsBlock('widgets', 'maps', ['maps'])") is False and _run("c.showsBlock('widgets', 'recent', ['maps'])") is True
    assert _run("c.showsBlock('night', 'recent', [])") is False and _run("c.showsBlock('night', 'links', [])") is True


@needs_node
def test_the_next_game_night_is_the_next_confirmed_one_still_ahead_else_an_open_poll():
    plans = [{"status": "confirmed", "past": True, "confirmed_slot_id": 1, "title": "Old", "location": "", "slots": [{"id": 1, "starts_at": "2026-01-01T18:00:00Z"}]},
             {"status": "confirmed", "past": False, "confirmed_slot_id": 3, "title": "Soon", "location": "Pub", "slots": [{"id": 3, "starts_at": "2026-11-02T18:00:00Z"}]},
             {"status": "confirmed", "past": False, "confirmed_slot_id": 4, "title": "Later", "location": "", "slots": [{"id": 4, "starts_at": "2026-12-02T18:00:00Z"}]},
             {"status": "open", "title": "Poll", "slots": []}]
    got = _run(f"c.nextNight({json.dumps(plans)}, '2026-10-20T10:00:00Z')")
    assert got == {"kind": "confirmed", "title": "Soon", "at": "2026-11-02T18:00:00Z", "location": "Pub"}
    got = _run(f"c.nextNight({json.dumps(plans[:1] + plans[3:])}, '2026-10-20T10:00:00Z')")
    assert got["kind"] == "poll" and got["title"] == "Poll"
    assert _run("c.nextNight([], '')") is None


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_home_page_tags_its_blocks_and_remembers_each_persons_choice(client, seed):
    _as(client, seed, True)
    page = client.get("/").text
    assert 'id="home-stack"' in page and 'id="home-layoutbar"' in page and 'id="home-night"' in page and "/static/js/home-layouts.js" in page
    for block in ("hero", "links", "kinds", "recent"):
        assert f'data-hb="{block}"' in page or block in ("hero", "recent"), block
    assert 'data-ep-view=""' in page
    r = client.post("/api/view-prefs/home", json={"mode": "widgets", "order": ["recent", "links"], "hidden": ["maps"]})
    assert r.status_code == 200 and r.json()["order"] == ["recent", "links"]
    page = client.get("/").text
    assert 'data-ep-view="widgets"' in page and 'data-ep-order="recent,links"' in page and 'data-ep-hidden="maps"' in page
    _as(client, seed, False)                                                                      # a player: their own page, on the default
    pl = client.get("/").text
    assert 'data-ep-view=""' in pl and 'data-ep-order=""' in pl
