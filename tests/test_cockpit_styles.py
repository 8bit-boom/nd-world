"""The cockpit's layout styles: Free windows (today's) or an arrangement kept for you - Stage + rail, Tabs, Tiled grid. The planner is pure
(static/js/cockpit-layout.js `arrangeStyle`, driven under Node); the page wiring and the saved choice (the account's own, per person) are
checked through the app."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import view_prefs

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = r"""
global.window = global; require(process.env.LAYOUT); const L = window.ndCockpitLayout;
const mk = (id, type, z, extra) => Object.assign({ id, type, x: 0, y: 0, w: 300, h: 300, z, collapsed: false }, extra || {});
const geo = ps => ps.map(p => ({ id: p.id, x: p.x, y: p.y, w: p.w, h: p.h, parked: !!p.parked }));
const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const anyOverlap = ps => ps.filter(p => !p.parked).some((a, i, v) => v.slice(i + 1).some(b => overlap(a, b)));
const opts = o => Object.assign({ klass: 'wide', vp: { w: 1600, h: 900 }, slots: Infinity, pref: () => 300 }, o || {});
"""


def _run(body: str):
    import os
    r = subprocess.run(["node", "-e", HARNESS + body], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "LAYOUT": str(ROOT / "static/js/cockpit-layout.js")})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_cockpit_section_offers_four_styles():
    sp = view_prefs.spec("cockpit")
    assert [m["id"] for m in sp["modes"]] == ["free", "rail", "tabs", "grid"] and sp["default"] == "free"
    assert {m["id"]: m["label"] for m in sp["modes"]} == {"free": "Free windows", "rail": "Stage + rail", "tabs": "Tabs", "grid": "Tiled grid"}
    assert view_prefs.clean("cockpit", {"mode": "rail"})["mode"] == "rail" and view_prefs.clean("cockpit", {"mode": "web"})["mode"] == "free"


@needs_node
def test_stage_plus_rail_puts_the_map_on_the_stage_and_stacks_the_rest_beside_it():
    out = _run("""
      const ps = [mk('a', 'dice', 1), mk('b', 'map', 2), mk('c', 'party', 3), mk('d', 'quests', 4)];
      const moved = L.arrangeStyle(ps, 'rail', opts());
      const again = L.arrangeStyle(ps, 'rail', opts());
      console.log(JSON.stringify({ moved, again, g: geo(ps), overlap: anyOverlap(ps) }));""")
    g = {p["id"]: p for p in out["g"]}
    assert out["moved"] is True and out["again"] is False and out["overlap"] is False          # arranged once, then stable
    assert g["b"]["x"] == 8 and g["b"]["w"] > g["a"]["w"] * 2 and g["b"]["h"] > 800            # the map: the big window on the left
    assert g["a"]["x"] == g["c"]["x"] == g["d"]["x"] > g["b"]["x"] + g["b"]["w"]               # the others: one column on the right
    assert g["a"]["y"] < g["c"]["y"] < g["d"]["y"] and all(p["x"] + p["w"] <= 1600 and p["y"] + p["h"] <= 900 for p in out["g"])


@needs_node
def test_the_stage_can_be_named_and_falls_back_to_the_last_used_window_without_a_map():
    out = _run("""
      const ps = [mk('a', 'dice', 5), mk('b', 'quests', 9), mk('c', 'party', 1)];
      L.arrangeStyle(ps, 'rail', opts());
      const last = geo(ps).find(p => p.w > 700).id;
      L.arrangeStyle(ps, 'rail', opts({ stage: 'c' }));
      const named = geo(ps).find(p => p.w > 700).id;
      const narrow = [mk('a', 'map', 2), mk('b', 'dice', 1)];
      L.arrangeStyle(narrow, 'rail', opts({ vp: { w: 700, h: 900 } }));
      console.log(JSON.stringify({ last, named, narrow: geo(narrow) }));""")
    assert out["last"] == "b" and out["named"] == "c"                                           # most recently used, unless one is named
    n = {p["id"]: p for p in out["narrow"]}
    assert n["a"]["y"] < n["b"]["y"] and n["a"]["w"] == n["b"]["w"]                             # a narrow screen: the stage on top, the rail below


@needs_node
def test_tiled_grid_keeps_the_order_added_and_never_overlaps():
    out = _run("""
      const ps = [mk('a', 'dice', 1), mk('b', 'map', 2), mk('c', 'party', 3), mk('d', 'quests', 4), mk('e', 'notes', 5)];
      L.arrangeStyle(ps, 'grid', opts());
      const g = geo(ps);
      console.log(JSON.stringify({ g, overlap: anyOverlap(ps), bounds: g.every(p => p.x >= 0 && p.y >= 0 && p.x + p.w <= 1600) }));""")
    assert out["overlap"] is False and out["bounds"] is True
    pos = [(p["y"], p["x"]) for p in out["g"]]
    assert pos == sorted(pos)                                                                    # a, b, c, d, e read left to right, top to bottom
    assert out["g"][1]["w"] == out["g"][0]["w"]                                                  # no stage: the map is no bigger than the dice


@needs_node
def test_tabs_show_one_window_full_size_and_park_the_rest():
    out = _run("""
      const ps = [mk('a', 'dice', 1), mk('b', 'map', 2), mk('c', 'party', 3)];
      L.arrangeStyle(ps, 'tabs', opts({ active: 'c' }));
      const first = geo(ps);
      L.arrangeStyle(ps, 'tabs', opts({ active: 'a' }));
      console.log(JSON.stringify({ first, second: geo(ps) }));""")
    shown = [p for p in out["first"] if not p["parked"]]
    assert [p["id"] for p in shown] == ["c"] and shown[0]["w"] == 1584 and shown[0]["h"] == 884
    assert [p["id"] for p in out["second"] if not p["parked"]] == ["a"]                          # another tab brought in: the others go to the dock


@needs_node
def test_a_laptop_shows_only_as_many_as_its_slots_and_free_or_unknown_styles_change_nothing():
    out = _run("""
      const ps = [mk('a', 'dice', 1), mk('b', 'map', 2), mk('c', 'party', 3), mk('d', 'quests', 4)];
      L.arrangeStyle(ps, 'grid', opts({ klass: 'laptop', slots: 2, vp: { w: 1280, h: 620 } }));
      const shown = geo(ps).filter(p => !p.parked).map(p => p.id);
      const free = [mk('a', 'dice', 1)], before = JSON.stringify(free);
      const r1 = L.arrangeStyle(free, 'free', opts()), r2 = L.arrangeStyle(free, 'nonsense', opts()), r3 = L.arrangeStyle(free, 'rail', opts({ vp: { w: 10, h: 10 } }));
      console.log(JSON.stringify({ shown, r1, r2, r3, same: JSON.stringify(free) === before, none: L.arrangeStyle([], 'rail', opts()) }));""")
    assert len(out["shown"]) == 2 and "d" in out["shown"]                                        # the most recent stay
    assert out["r1"] is False and out["r2"] is False and out["r3"] is False and out["same"] is True and out["none"] is False


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_page_has_the_style_buttons_and_remembers_each_persons_choice(client, seed):
    _as(client, seed, True)
    page = client.get("/cockpit").text
    assert 'id="ck-stylebar"' in page and "const CK_STYLE_SAVED = {};" in page
    assert client.post("/api/view-prefs/cockpit", json={"mode": "tabs"}).json()["mode"] == "tabs"
    assert '"mode": "tabs"' in client.get("/cockpit").text.replace("&#34;", '"')
    _as(client, seed, False)                                                                     # a player has their own - and the same buttons
    pl = client.get("/player-cockpit").text
    assert 'id="ck-stylebar"' in pl and "const CK_STYLE_SAVED = {};" in pl
