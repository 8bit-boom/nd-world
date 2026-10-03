"""The cockpit on monitors of different sizes and resolutions.

A cockpit layout is a set of windows in pixels, and the same layout follows the GM across devices (and a player's across
the browser, the character page's embedded Cockpit tab and a phone). Pixels alone meant: a layout arranged on a 4K
monitor opened on a laptop with windows hanging off the right edge, a default layout that stretched one column across an
ultrawide, and windows that never adapted when the browser went fullscreen.

The maths lives in static/js/cockpit-layout.js (pure functions, no DOM) and is driven here under Node across a matrix of
screens: tiling (default layout / auto-arrange), scaling a layout arranged for one screen to another, pulling stray
windows back in, and sizing a newly added window. The server keeps the screen a layout was arranged for."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = r"""
global.window = global;
require(process.env.LAYOUT);
const L = global.ndCockpitLayout;
const SIZES = [[1024, 600], [1180, 740], [1280, 620], [1366, 650], [1440, 780], [1536, 760], [1920, 950],
               [2560, 1280], [3440, 1280], [3840, 2000], [5120, 1300]].map(([w, h]) => ({w, h}));
const PREF = {map: 560, wmap: 560, combat: 560, dice: 480, party: 380, quests: 340, pc: 380, notes: 300, timer: 260,
              tables: 280, calendar: 480, ai_chat: 640, gallery: 640, audio: 640, video: 560, entity: 620, ecard: 400, find: 540};
const pref = p => PREF[p.type] || 300;
const SETS = {
  gm: ['map', 'party', 'quests', 'dice'], gmNoMap: ['party', 'quests', 'dice'],
  player: ['map', 'pc', 'party', 'quests', 'dice'], playerNoMap: ['pc', 'party', 'quests', 'dice'],
  one: ['map'], two: ['dice', 'notes'], many: ['map', 'party', 'quests', 'dice', 'notes', 'timer', 'tables', 'calendar',
    'ai_chat', 'gallery', 'audio', 'video'],
  max: Array.from({length: 24}, (_, i) => ['dice', 'notes', 'timer', 'quests'][i % 4]),
};
const mk = types => types.map((t, i) => ({id: 'p' + (i + 1), type: t, ref: '', x: 0, y: 0, w: 300, h: 300, z: i + 1, collapsed: false}));
const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const bad = [];
function note(msg) { bad.push(msg); }
const tag = (set, vp) => set + '@' + vp.w + 'x' + vp.h;
const out = {};
"""


def _run(body: str):
    script = HARNESS + body + "\nout.bad = bad; console.log(JSON.stringify(out));"
    env = {"LAYOUT": str(ROOT / "static/js/cockpit-layout.js"), "PATH": __import__("os").environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── tiling: the default layout and 📌 Auto-arrange ──────────────────────────────────────────────

@needs_node
def test_tiled_windows_stay_inside_the_screen_and_never_overlap_on_any_monitor():
    out = _run("""
for (const vp of SIZES) for (const [name, types] of Object.entries(SETS)) {
  const t = L.tilePanels(mk(types), vp, {pref});
  if (t.length !== types.length) note(tag(name, vp) + ' lost panels');
  t.forEach((p, i) => {
    if (![p.x, p.y, p.w, p.h].every(Number.isFinite)) note(tag(name, vp) + ' non-finite ' + p.id);
    if (p.x < 0 || p.x + p.w > vp.w) note(tag(name, vp) + ' ' + p.id + ' leaves the screen horizontally: x=' + p.x + ' w=' + p.w);
    if (p.w < 240) note(tag(name, vp) + ' ' + p.id + ' too narrow ' + p.w);
    if (p.h < 140) note(tag(name, vp) + ' ' + p.id + ' too short ' + p.h);
    for (let j = i + 1; j < t.length; j++) if (overlap(p, t[j])) note(tag(name, vp) + ' ' + p.id + ' overlaps ' + t[j].id);
  });
}
""")
    assert out["bad"] == []


@needs_node
def test_a_tiled_layout_uses_the_whole_width_even_on_an_ultrawide():
    out = _run("""
for (const vp of SIZES) for (const name of ['gm', 'gmNoMap', 'player', 'two', 'many']) {
  const t = L.tilePanels(mk(SETS[name]), vp, {pref});
  const right = Math.max(...t.map(p => p.x + p.w)), left = Math.min(...t.map(p => p.x));
  if (left > 16) note(tag(name, vp) + ' starts at ' + left);
  if (right < vp.w - 24) note(tag(name, vp) + ' stops at ' + right + ' of ' + vp.w);
}
""")
    assert out["bad"] == []


@needs_node
def test_the_map_is_the_stage_the_biggest_window_and_a_real_share_of_the_screen():
    out = _run("""
for (const vp of SIZES) for (const name of ['gm', 'player', 'many']) {
  const t = L.tilePanels(mk(SETS[name]), vp, {pref});
  const area = p => p.w * p.h, map = t.find(p => p.type === 'map');
  if (t.some(p => area(p) > area(map))) note(tag(name, vp) + ' something is bigger than the map');
  if (area(map) < 0.3 * vp.w * vp.h) note(tag(name, vp) + ' map only ' + Math.round(100 * area(map) / (vp.w * vp.h)) + '%');
}
""")
    assert out["bad"] == []


@needs_node
def test_a_handful_of_windows_fit_without_scrolling_on_a_normal_height_screen():
    out = _run("""
for (const vp of SIZES.filter(v => v.h >= 900)) for (const name of ['gm', 'gmNoMap', 'player', 'playerNoMap', 'two']) {
  const t = L.tilePanels(mk(SETS[name]), vp, {pref});
  const bottom = Math.max(...t.map(p => p.y + p.h));
  if (bottom > vp.h) note(tag(name, vp) + ' needs scrolling: bottom=' + bottom);
}
""")
    assert out["bad"] == []


@needs_node
def test_no_tile_is_a_tall_thin_sliver_on_a_tall_or_ultrawide_screen():
    out = _run("""
for (const vp of SIZES) for (const name of ['gm', 'gmNoMap', 'player', 'playerNoMap', 'two', 'many']) {
  const t = L.tilePanels(mk(SETS[name]), vp, {pref});
  t.filter(p => p.type !== 'map').forEach(p => {
    if (p.h / p.w > 1.8) note(tag(name, vp) + ' ' + p.id + ' is ' + p.w + 'x' + p.h);
  });
}
""")
    assert out["bad"] == []


@needs_node
def test_no_tile_is_a_flat_bar_either_where_there_is_height_to_spare():
    out = _run("""
// (not the 12-window set: there every window is held at its minimum readable height and the workspace scrolls)
for (const vp of SIZES.filter(v => v.h >= 900)) for (const name of ['gm', 'gmNoMap', 'player', 'playerNoMap', 'two']) {
  const t = L.tilePanels(mk(SETS[name]), vp, {pref});
  t.filter(p => p.type !== 'map').forEach(p => {
    if (p.w / p.h > 3.6) note(tag(name, vp) + ' ' + p.id + ' is a bar ' + p.w + 'x' + p.h);
  });
}
""")
    assert out["bad"] == []


@needs_node
def test_tall_windows_get_more_room_than_small_ones_in_the_same_column():
    out = _run("""
const t = L.tilePanels(mk(['map', 'timer', 'ai_chat']), {w: 1366, h: 900}, {pref});
const timer = t.find(p => p.type === 'timer'), chat = t.find(p => p.type === 'ai_chat');
out.timer = timer.h; out.chat = chat.h; out.sameColumn = timer.x === chat.x;
""")
    assert out["sameColumn"] and out["chat"] > out["timer"]


@needs_node
def test_tiling_does_not_touch_the_input_and_handles_nothing_and_tiny_screens():
    out = _run("""
const src = mk(SETS.gm), before = JSON.stringify(src);
L.tilePanels(src, {w: 1920, h: 950}, {pref});
out.untouched = JSON.stringify(src) === before;
out.empty = L.tilePanels([], {w: 1920, h: 950}).length;
const tiny = L.tilePanels(mk(SETS.gm), {w: 400, h: 300}, {pref});
out.tinyFinite = tiny.every(p => [p.x, p.y, p.w, p.h].every(Number.isFinite) && p.w > 0 && p.h > 0);
""")
    assert out["untouched"] and out["empty"] == 0 and out["tinyFinite"]


@needs_node
def test_auto_arrange_expands_a_collapsed_window():
    out = _run("""
const src = mk(SETS.gm); src[1].collapsed = true;
out.collapsed = L.tilePanels(src, {w: 1920, h: 950}, {pref}).map(p => p.collapsed);
""")
    assert out["collapsed"] == [False, False, False, False]


# ── a layout arranged for one screen, opened on another ────────────────────────────────────────

@needs_node
def test_a_layout_scaled_to_any_other_screen_stays_inside_it():
    out = _run("""
const home = {w: 1920, h: 950};
const base = L.tilePanels(mk(SETS.player), home, {pref});
for (const vp of SIZES) {
  const s = L.scalePanels(base, home, vp);
  s.forEach(p => {
    if (p.x < 0 || p.x + p.w > vp.w) note(tag('scaled', vp) + ' ' + p.id + ' x=' + p.x + ' w=' + p.w);
    if (p.w < Math.min(240, vp.w) || p.h < 140) note(tag('scaled', vp) + ' ' + p.id + ' tiny ' + p.w + 'x' + p.h);
  });
}
""")
    assert out["bad"] == []


@needs_node
def test_scaling_keeps_the_arrangement_and_does_not_make_windows_overlap():
    out = _run("""
const home = {w: 1920, h: 950};
const base = L.tilePanels(mk(SETS.gm), home, {pref});
for (const vp of SIZES.filter(v => v.w >= 1280)) {
  const s = L.scalePanels(base, home, vp);
  for (let i = 0; i < s.length; i++) for (let j = i + 1; j < s.length; j++) if (overlap(s[i], s[j])) note(tag('scaled', vp) + ' ' + s[i].id + ' overlaps ' + s[j].id);
  const map = s.find(p => p.type === 'map'), dice = s.find(p => p.type === 'dice');
  if (!(map.x < dice.x)) note(tag('scaled', vp) + ' the map is no longer left of the dice');
}
""")
    assert out["bad"] == []


@needs_node
def test_scaling_up_and_back_returns_to_the_original():
    out = _run("""
const home = {w: 1920, h: 950}, big = {w: 2560, h: 1280};
const base = L.tilePanels(mk(SETS.gm), home, {pref});
const back = L.scalePanels(L.scalePanels(base, home, big), big, home);
base.forEach((p, i) => {
  for (const k of ['x', 'y', 'w', 'h']) if (Math.abs(p[k] - back[i][k]) > 16) note(p.id + ' ' + k + ' drifted ' + p[k] + ' -> ' + back[i][k]);
});
""")
    assert out["bad"] == []


@needs_node
def test_a_window_the_user_enlarged_stays_proportionally_bigger_after_scaling():
    out = _run("""
const home = {w: 1920, h: 950};
const src = mk(['dice', 'notes']);
src[0].x = 100; src[0].y = 100; src[0].w = 400; src[0].h = 300;
src[1].x = 600; src[1].y = 100; src[1].w = 800; src[1].h = 300;
const s = L.scalePanels(src, home, {w: 2560, h: 1280});
out.ratioBefore = 800 / 400; out.ratioAfter = s[1].w / s[0].w;
""")
    assert abs(out["ratioAfter"] - out["ratioBefore"]) < 0.15


@needs_node
def test_only_a_real_change_of_screen_triggers_scaling():
    out = _run("""
const a = {w: 1920, h: 950};
out.same = L.needsScale(a, a);
out.scrollbar = L.needsScale(a, {w: 1905, h: 950});          // a scrollbar appearing
out.bookmarks = L.needsScale(a, {w: 1920, h: 925});          // a bar toggled
out.laptop = L.needsScale(a, {w: 1366, h: 650});
out.fullscreen = L.needsScale({w: 1366, h: 650}, {w: 1366, h: 768});
out.heightOnly = L.needsScale(a, {w: 1920, h: 700});
out.junk = [L.needsScale(null, a), L.needsScale(a, null), L.needsScale({w: 0, h: 0}, a)];
""")
    assert out["same"] is False and out["scrollbar"] is False and out["bookmarks"] is False
    assert out["laptop"] is True and out["fullscreen"] is True and out["heightOnly"] is True
    assert out["junk"] == [False, False, False]


@needs_node
def test_fitting_pulls_back_windows_that_hang_off_the_screen_without_touching_the_rest():
    out = _run("""
const src = mk(['dice', 'notes', 'quests']);
src[0].x = 1800; src[0].w = 600;                   // hangs off a 1366 screen
src[1].x = 10; src[1].w = 3000;                    // wider than the screen
src[2].x = 100; src[2].w = 300; src[2].y = 5000;   // far below: the workspace scrolls, that is fine
const f = L.fitPanels(src, {w: 1366, h: 650});
out.f = f.map(p => [p.x, p.w, p.y]);
""")
    (x0, w0, _), (x1, w1, _), (x2, w2, y2) = out["f"]
    assert x0 + w0 <= 1366 and x0 >= 0
    assert w1 <= 1366 and x1 + w1 <= 1366
    assert (x2, w2, y2) == (100, 300, 5000)


# ── a window added later ───────────────────────────────────────────────────────────────────────

@needs_node
def test_new_windows_are_sized_for_the_screen_and_always_fit_it():
    out = _run("""
const specs = [{w: 780, h: 560}, {w: 420, h: 480}, {w: 720, h: 720}, {w: 340, h: 260}];
for (const vp of SIZES) for (const s of specs) {
  const z = L.sizeFor(s, vp);
  if (z.w > vp.w - 16 || z.h > vp.h - 16) note(tag('size', vp) + ' ' + s.w + 'x' + s.h + ' -> ' + z.w + 'x' + z.h + ' does not fit');
  if (z.w < 240 && vp.w - 16 >= 240) note(tag('size', vp) + ' too narrow ' + z.w);
  if (z.h < 140) note(tag('size', vp) + ' too short ' + z.h);
}
const small = L.sizeFor({w: 780, h: 560}, {w: 1366, h: 650}), big = L.sizeFor({w: 780, h: 560}, {w: 3840, h: 2000});
out.grows = big.w > small.w && big.h > small.h;
""")
    assert out["bad"] == [] and out["grows"]


@needs_node
def test_new_windows_cascade_inside_the_screen():
    out = _run("""
for (const vp of SIZES) for (let n = 0; n < 30; n++) {
  const z = L.sizeFor({w: 780, h: 560}, vp), at = L.cascade(n, z, vp);
  if (at.x < 0 || at.x + z.w > vp.w) note(tag('cascade', vp) + ' n=' + n + ' x=' + at.x + ' w=' + z.w);
  if (at.y < 0) note(tag('cascade', vp) + ' n=' + n + ' y=' + at.y);
}
""")
    assert out["bad"] == []


# ── the server keeps the screen a layout was arranged for ──────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


PANEL = {"id": "p1", "type": "dice", "ref": "", "title": "🎲", "x": 5, "y": 5, "w": 420, "h": 480, "z": 1}


def test_the_workspace_remembers_the_screen_it_was_arranged_for(client, seed):
    _login_gm(client, seed)
    ws = {"current": {"panels": [PANEL], "vp": {"w": 2560, "h": 1280}},
          "presets": {"Combat": {"panels": [PANEL], "vp": {"w": 1366, "h": 650}}}}
    assert client.post("/api/cockpit/workspace", json=ws).status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]
    assert back["current"]["vp"] == {"w": 2560, "h": 1280}
    assert back["presets"]["Combat"]["vp"] == {"w": 1366, "h": 650}


@pytest.mark.parametrize("junk", [None, "wide", [1, 2], {"w": "x", "h": 5}, {"w": -5, "h": 600}, {"w": 10, "h": 10},
                                  {"w": 10 ** 9, "h": 10 ** 9}, {"w": 1920}])
def test_a_missing_or_nonsense_screen_is_dropped_not_stored(client, seed, junk):
    _login_gm(client, seed)
    ws = {"current": {"panels": [PANEL], "vp": junk}, "presets": {"A": {"panels": [PANEL], "vp": junk}}}
    assert client.post("/api/cockpit/workspace", json=ws).status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]
    assert "vp" not in back["current"] and "vp" not in back["presets"]["A"]


def test_a_layout_saved_before_this_change_still_loads(client, seed):
    _login_gm(client, seed)
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [PANEL]}, "presets": {}}).status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]
    assert back["current"]["panels"][0]["w"] == 420 and "vp" not in back["current"]


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────

def test_the_cockpit_page_loads_the_layout_maths_before_the_cockpit(client, seed):
    _login_gm(client, seed)
    for path in ("/cockpit", "/player-cockpit"):
        html = client.get(path).text
        assert "/static/js/cockpit-layout.js" in html, path
        assert html.index("cockpit-layout.js") < html.index("/static/js/cockpit.js"), path


def test_the_cockpit_script_uses_the_layout_maths_instead_of_fixed_pixels():
    js = (ROOT / "static/js/cockpit.js").read_text()
    for needle in ("ndCockpitLayout", "tilePanels", "scalePanels", "needsScale", "fitPanels", "sizeFor"):
        assert needle in js, needle
    assert "vp:" in js                                   # the screen is saved with the layout
    assert "function clampX" not in js                   # the old right-edge-only clamp is gone
    assert "Math.round(vw / 640)" not in js               # auto-arrange no longer has its own column guess
