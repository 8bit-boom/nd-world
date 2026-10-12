"""The cockpit's window manager: resizing from any of eight handles, magnetic snapping to the workspace and to other windows.

The maths is in static/js/cockpit-layout.js (pure functions) and is driven here under Node; the wiring (handles, guides, the
active-window highlight, shortcuts that work inside windows) is checked against the shipped sources and was driven in
Chromium against the running app."""
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
const vp = {w: 1600, h: 900};
const out = {};
const box = (x, y, w, h) => ({x, y, w, h});
"""


def _run(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"LAYOUT": str(ROOT / "static/js/cockpit-layout.js"), "PATH": __import__("os").environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── resizing from any handle ────────────────────────────────────────────────────────────────────

@needs_node
def test_each_handle_moves_only_its_own_sides():
    out = _run("""
const s = box(400, 300, 500, 400);
out.e = L.resizeBox(s, 60, 99, 'e', vp);          // dy is ignored by a horizontal handle
out.w = L.resizeBox(s, -60, 99, 'w', vp);
out.n = L.resizeBox(s, 99, -40, 'n', vp);
out.s = L.resizeBox(s, 99, 40, 's', vp);
out.se = L.resizeBox(s, 30, 20, 'se', vp);
out.nw = L.resizeBox(s, -30, -20, 'nw', vp);
out.ne = L.resizeBox(s, 30, -20, 'ne', vp);
out.sw = L.resizeBox(s, -30, 20, 'sw', vp);
""")
    assert out["e"] == {"x": 400, "y": 300, "w": 560, "h": 400}
    assert out["w"] == {"x": 340, "y": 300, "w": 560, "h": 400}          # the right edge stays at 900
    assert out["n"] == {"x": 400, "y": 260, "w": 500, "h": 440}          # the bottom edge stays at 700
    assert out["s"] == {"x": 400, "y": 300, "w": 500, "h": 440}
    assert out["se"] == {"x": 400, "y": 300, "w": 530, "h": 420}
    assert out["nw"] == {"x": 370, "y": 280, "w": 530, "h": 420}
    assert out["ne"] == {"x": 400, "y": 280, "w": 530, "h": 420}
    assert out["sw"] == {"x": 370, "y": 300, "w": 530, "h": 420}


@needs_node
def test_a_window_never_gets_smaller_than_the_minimum_and_the_far_edge_stays_put():
    out = _run("""
const s = box(400, 300, 500, 400);
const w = L.resizeBox(s, 5000, 0, 'w', vp);        // dragged far past the right edge
const n = L.resizeBox(s, 0, 5000, 'n', vp);
const e = L.resizeBox(s, -5000, 0, 'e', vp);
const so = L.resizeBox(s, 0, -5000, 's', vp);
out.w = [w.x + w.w, w.w]; out.n = [n.y + n.h, n.h]; out.e = [e.x, e.w]; out.s = [so.y, so.h];
out.custom = L.resizeBox(s, -5000, -5000, 'se', vp, 300, 200);
""")
    assert out["w"] == [900, 240] and out["n"] == [700, 140]               # the right / bottom edge did not move
    assert out["e"] == [400, 240] and out["s"] == [300, 140]               # the left / top edge did not move
    assert out["custom"]["w"] == 300 and out["custom"]["h"] == 200


@needs_node
def test_a_window_cannot_be_dragged_out_of_the_workspace_sideways_or_off_the_top():
    out = _run("""
const s = box(100, 60, 500, 400);
const w = L.resizeBox(s, -5000, 0, 'w', vp);
const n = L.resizeBox(s, 0, -5000, 'n', vp);
const e = L.resizeBox(s, 5000, 0, 'e', vp);
const sd = L.resizeBox(s, 0, 100000, 's', vp);
out.w = [w.x, w.x + w.w]; out.n = [n.y, n.y + n.h]; out.e = e.x + e.w; out.s = sd.h;
""")
    assert out["w"] == [0, 600] and out["n"] == [0, 460] and out["e"] == 1600
    assert out["s"] == 8000                                                  # the workspace scrolls: down is allowed, up to a cap


@needs_node
def test_whatever_the_drag_the_opposite_edges_do_not_move_and_the_box_stays_valid():
    out = _run("""
let seed = 3; const rnd = n => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
const bad = [];
const dirs = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];
for (let i = 0; i < 2000; i++) {
  const s = box(rnd(900), rnd(500), 240 + rnd(600), 140 + rnd(500));
  if (s.x + s.w > vp.w) s.w = vp.w - s.x < 240 ? 240 : vp.w - s.x;
  const d = dirs[rnd(8)], dx = rnd(1400) - 700, dy = rnd(1000) - 500;
  const r = L.resizeBox(s, dx, dy, d, vp);
  const sides = L.EDGE_SIDES[d];
  if (r.w < 240 || r.h < 140) bad.push(['small', s, d, dx, dy, r]);
  if (r.x < 0 || r.y < 0) bad.push(['negative', s, d, dx, dy, r]);
  if (!sides.left && !sides.right && (r.x !== s.x || r.w !== s.w)) bad.push(['horizontal moved', s, d, dx, dy, r]);
  if (!sides.top && !sides.bottom && (r.y !== s.y || r.h !== s.h)) bad.push(['vertical moved', s, d, dx, dy, r]);
  if (sides.left && !sides.right && r.x + r.w !== s.x + s.w) bad.push(['right edge moved', s, d, dx, dy, r]);
  if (sides.top && !sides.bottom && r.y + r.h !== s.y + s.h) bad.push(['bottom edge moved', s, d, dx, dy, r]);
  if (sides.right && !sides.left && r.x !== s.x) bad.push(['left edge moved', s, d, dx, dy, r]);
  if (sides.bottom && !sides.top && r.y !== s.y) bad.push(['top edge moved', s, d, dx, dy, r]);
}
out.bad = bad.slice(0, 3);
""")
    assert out["bad"] == []


@needs_node
def test_an_unknown_handle_name_behaves_like_the_corner_handle():
    out = _run("out.r = L.resizeBox(box(10, 10, 300, 300), 20, 30, 'nonsense', vp);")
    assert out["r"] == {"x": 10, "y": 10, "w": 320, "h": 330}


# ── snapping ────────────────────────────────────────────────────────────────────────────────────

@needs_node
def test_a_moved_window_snaps_next_to_another_with_the_tiled_layouts_gap_or_flush():
    out = _run("""
const other = box(100, 100, 400, 300);                         // right edge at 500
out.gap = L.snapBox(box(511, 100, 300, 200), 'move', [other], vp);   // 3px off "right edge + 8px gap"
out.flush = L.snapBox(box(503, 100, 300, 200), 'move', [other], vp); // 3px off flush
out.align = L.snapBox(box(103, 450, 300, 200), 'move', [other], vp); // left edges line up
out.far = L.snapBox(box(530, 700, 300, 150), 'move', [other], vp);   // 30px away sideways, nowhere near vertically: untouched
""")
    assert out["gap"]["x"] == 508 and out["gap"]["guides"]["x"] == [508]
    assert out["flush"]["x"] == 500 and out["flush"]["guides"]["x"] == [500]
    assert out["align"]["x"] == 100
    assert out["far"]["x"] == 530 and out["far"]["guides"] == {"x": [], "y": []}


@needs_node
def test_snapping_to_the_workspace_edges_and_its_margin_keeps_the_size():
    out = _run("""
out.left = L.snapBox(box(5, 200, 300, 200), 'move', [], vp);           // within 8px of the 8px margin
out.right = L.snapBox(box(1286, 200, 300, 200), 'move', [], vp);       // right edge 1586 -> 1592 (= 1600 - 8)
out.top = L.snapBox(box(300, 6, 300, 200), 'move', [], vp);
out.flushRight = L.snapBox(box(1296, 200, 300, 200), 'move', [], vp);  // right edge 1596: the wall itself (1600) is 4 away
""")
    assert out["left"]["x"] == 8 and out["left"]["w"] == 300
    assert out["right"]["x"] == 1292 and out["right"]["w"] == 300
    assert out["top"]["y"] == 8 and out["top"]["h"] == 200
    assert out["flushRight"]["x"] in (1292, 1300)                          # either the margin (1292) or the wall (1300): nearest wins


@needs_node
def test_the_closer_of_the_two_sides_wins_when_both_could_snap():
    out = _run("""
const a = box(100, 100, 400, 300), b = box(800, 100, 400, 300);        // a gap of 300 between them
out.r = L.snapBox(box(a.x + a.w + 2, 100, 295, 200), 'move', [a, b], vp);   // left 502 (2 from 500), right 797 (5 from 800 - 8 = 792)
""")
    assert out["r"]["x"] == 500 and out["r"]["w"] == 295


@needs_node
def test_resizing_snaps_only_the_edges_being_dragged():
    out = _run("""
const other = box(900, 100, 400, 300);
const e = L.snapBox(box(400, 100, 497, 300), 'e', [other], vp);        // right edge 897 -> 892 (other.x - 8) or 900: nearest is 900
const w = L.snapBox(box(1203, 500, 300, 200), 'w', [other], vp);       // left edge 1203 -> 1200 (other.x + 300 is 1200? no: 900+400 = 1300) -> nothing near
const n = L.snapBox(box(300, 405, 300, 200), 'n', [other], vp);        // top 405 -> 408 (other.y + h + GAP) wait: 100 + 300 + 8 = 408
out.e = [e.x, e.w, e.guides.x]; out.n = [n.y, n.h, n.guides.y];
out.w_changed = w.x !== 1203 || w.w !== 300;
""")
    assert out["e"][0] == 400 and out["e"][1] == 500 and out["e"][2] == [900]             # the left edge stayed, the width grew to the line
    assert out["n"][0] == 408 and out["n"][1] == 197                                     # the bottom edge (605) stayed where it was
    assert out["w_changed"] is False


@needs_node
def test_moving_never_changes_the_size_and_snapping_never_leaves_the_workspace_or_shrinks_a_window():
    out = _run("""
let seed = 11; const rnd = n => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
const bad = [];
const dirs = ['move', 'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];
for (let i = 0; i < 3000; i++) {
  const others = Array.from({length: rnd(6)}, () => box(rnd(1200), rnd(700), 240 + rnd(500), 140 + rnd(400)));
  const b = box(rnd(1300), rnd(700), 240 + rnd(500), 140 + rnd(400));
  const d = dirs[rnd(9)];
  const r = L.snapBox(b, d, others, vp);
  if (d === 'move' && (r.w !== b.w || r.h !== b.h)) bad.push(['size changed on move', b, r]);
  if (r.x < 0 || r.y < 0 || r.w < 240 || r.h < 140) bad.push(['invalid', b, d, r]);
  if (![r.x, r.y, r.w, r.h].every(Number.isFinite)) bad.push(['non-finite', b, d, r]);
  if (Math.abs(r.x - b.x) > 8 + 1 && d === 'move') bad.push(['moved too far', b, r]);
}
out.bad = bad.slice(0, 3);
""")
    assert out["bad"] == []


@needs_node
def test_a_zero_threshold_turns_snapping_off():
    out = _run("out.r = L.snapBox(box(503, 100, 300, 200), 'move', [box(100, 100, 400, 300)], vp, 0);")
    assert out["r"]["x"] == 503 and out["r"]["guides"]["x"] == []              # nothing within 0px sideways (an exactly aligned top does not move)


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_cockpit_script_resizes_from_every_edge_and_snaps_through_the_layout_maths():
    js = (ROOT / "static/js/cockpit.js").read_text()
    for needle in ("LAYOUT.resizeBox", "LAYOUT.snapBox", "ck-rz-", "showGuides", "hideGuides", "setActive", "ck-active"):
        assert needle in js, needle
    # the eight handles exist and the old single-corner math is gone
    assert "['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']" in js
    assert "Math.max(240, Math.min(viewport.clientWidth, ev.clientX - sx))" not in js


def test_the_cockpit_page_styles_the_handles_the_active_window_and_the_guides(client, seed):
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    for needle in (".ck-rz-e", ".ck-rz-nw", "cursor: ew-resize", "cursor: nesw-resize", ".ck-win.ck-active", "#ck-guides", ".ck-guide"):
        assert needle in html, needle
    assert "prefers-reduced-motion" in html


def test_a_press_inside_a_windows_page_raises_and_highlights_its_window():
    """Each page in a window is a document of its own, so a click inside it never reaches the cockpit: the window listens in on the
    same-origin page each time it loads (and a focus move into the frame - Tab - does the same through the window's blur)."""
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "f.addEventListener('load', function () { relayFrameEvents(f); });" in js
    assert "w.addEventListener('pointerdown', function () { activateFrameWindow(f); }, true);" in js
    assert "if (q && !q.parked) { bringToFront(q); setActive(q); }" in js
    assert "window.addEventListener('blur'" in js and "activateFrameWindow(a)" in js


def test_pressing_a_resize_handle_raises_the_window_like_any_press_on_it():
    js = (ROOT / "static/js/cockpit.js").read_text()
    body = js.split("function wireResize(handle, win, p, dir) {", 1)[1].split("function up()", 1)[0]
    assert "bringToFront(p);" in body and "setActive(p);" in body
    # raising twice in a row is a no-op rather than a z-index that grows with every click
    assert "if (el && p.z === zTop && el.style.zIndex === String(zTop)) return;" in js


def test_ctrl_while_dragging_is_free_placement_without_snapping_or_the_grid():
    js = (ROOT / "static/js/cockpit.js").read_text()
    drag = js.split("function wireDrag(handle, win, p) {", 1)[1].split("function wireResize", 1)[0]
    assert "free = ev.ctrlKey || ev.metaKey;" in drag
    assert "free ? null : LAYOUT.snapBox(" in drag
    assert "else if (!snapped && !free)" in drag
    resize = js.split("function wireResize(handle, win, p, dir) {", 1)[1].split("// ── the active window", 1)[0]
    assert "free = ev.ctrlKey || ev.metaKey;" in resize and "if (!snapped && !free)" in resize


def test_every_window_page_has_a_name_for_a_screen_reader_and_keeps_it_when_renamed():
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "function titleFrames(p, root)" in js
    assert "f.title = fullTitle(p)" in js
    # built with the window, and re-applied by both rename paths (the title's double-click and the window menu)
    assert js.count("titleFrames(p, ") >= 3


def test_the_ui_scale_picker_goes_up_to_two_hundred_percent(client, seed):
    _login_gm(client, seed)
    html = client.get("/").text
    assert '<option value="175">175%</option>' in html and '<option value="200">200%</option>' in html
    settings = client.get("/settings").text
    assert '<option value="175">175%</option>' in settings and '<option value="200">200%</option>' in settings
    css = (ROOT / "static/style.css").read_text()
    assert 'html[data-scale="200"] { font-size: calc(var(--font-size-base) * 2); }' in css


def test_the_dock_chip_row_shows_the_main_window_scrolls_and_fades():
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "' main' : ''" in js and "aria-pressed" in js
    assert "function fadeChips()" in js and "function revealChip(p)" in js
    # the row's own scroll position moves, never the page's
    assert "dockChips.scrollLeft" in js and "scrollIntoView" not in js.split("function revealChip(p)", 1)[1].split("}\n", 1)[0]
