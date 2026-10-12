"""Scrolling the cockpit's workspace: windows can be made taller than the screen and more of them kept open than fit.

The numbers (how fast to scroll while a window is carried or stretched near the edge, where to scroll to bring a window into
sight, which screenful is showing, a wheel's movement in pixels) are pure functions in static/js/cockpit-layout.js, driven here
under Node; the wiring (autoscroll while dragging, Alt + wheel over a window, the status bar's ▲ 1/2 ▼, Alt + ↑ / ↓, a window brought
into sight when it is shown, a maximised window kept in sight) is checked against the shipped sources and was driven in Chromium
against the running app, with real scrollbars, at desktop and laptop sizes."""
import json
import os
import re
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
const out = {};
"""


def _run(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"LAYOUT": str(ROOT / "static/js/cockpit-layout.js"), "PATH": os.environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── autoscroll while a window is carried or stretched ───────────────────────────────────────────────────────────────

@needs_node
def test_the_workspace_scrolls_only_near_its_top_and_bottom_edges_and_faster_the_nearer():
    out = _run("""
const top = 100, h = 800;                                    // a workspace spanning y = 100..900
out.middle = [150 + 60, 400, 500, 843].map(y => L.autoScrollStep(y, top, h));
out.up = [155, 140, 120, 100].map(y => L.autoScrollStep(y, top, h));
out.down = [845, 860, 880, 900].map(y => L.autoScrollStep(y, top, h));
out.beyond = [50, -400, 950, 5000].map(y => L.autoScrollStep(y, top, h));
""")
    assert out["middle"] == [0, 0, 0, 0], "nothing scrolls while the pointer is in the middle"
    assert all(v < 0 for v in out["up"]) and all(v > 0 for v in out["down"])
    # (the pointers are listed on their way TOWARDS each edge)
    up, down = [abs(v) for v in out["up"]], out["down"]
    assert up == sorted(up) and down == sorted(down) and len(set(up)) == len(up) and len(set(down)) == len(down), "the nearer the edge the faster"
    assert out["up"][-1] == -out["down"][-1] == -L_MAX() and out["down"][-1] == L_MAX(), "the top speed at the edge ..."
    assert out["beyond"] == [-L_MAX(), -L_MAX(), L_MAX(), L_MAX()], "... and beyond it: a held button keeps going outside the workspace"


def L_MAX():
    return int(re.search(r"var SCROLL_MAX = (\d+);", (ROOT / "static/js/cockpit-layout.js").read_text()).group(1))


@needs_node
def test_a_very_short_workspace_keeps_a_middle_that_does_not_scroll_and_nonsense_does_nothing():
    out = _run("""
out.short = [0, 20, 30, 45, 60, 75, 90].map(y => L.autoScrollStep(y, 0, 90));      // 90px: bands of 30
out.empty = [L.autoScrollStep(10, 0, 0), L.autoScrollStep(NaN, 0, 800), L.autoScrollStep(10, NaN, 800), L.autoScrollStep(Infinity, 0, 800)];
out.custom = [L.autoScrollStep(5, 0, 800, {edge: 20, max: 5}), L.autoScrollStep(795, 0, 800, {edge: 20, max: 5}), L.autoScrollStep(400, 0, 800, {edge: 20, max: 5})];
""")
    assert out["short"][3] == 0, "the middle third stays still"
    assert out["short"][0] < 0 < out["short"][-1]
    assert out["empty"] == [0, 0, 0, 0]
    assert out["custom"][0] < 0 < out["custom"][1] and out["custom"][2] == 0 and abs(out["custom"][0]) <= 5


@needs_node
def test_the_scroll_a_window_gets_is_never_zero_inside_a_band():
    # the first pixel into a band must already move: a speed that rounds to 0 would look like a stuck window
    out = _run("""
out.steps = []; for (let y = 0; y <= 900; y++) out.steps.push(L.autoScrollStep(y, 0, 900));
""")
    steps = out["steps"]
    edge = 56
    assert all(steps[y] != 0 for y in range(0, edge)) and all(steps[900 - y] != 0 for y in range(0, edge))
    assert all(v == 0 for v in steps[edge:900 - edge + 1])


# ── bringing a window into sight ─────────────────────────────────────────────────────────────────────────────────────

@needs_node
def test_a_window_already_in_sight_is_left_alone_and_one_out_of_sight_is_scrolled_the_least_that_shows_it():
    out = _run("""
const R = (st, vh, top, h, o) => L.revealScroll(st, vh, top, h, 8, o);
out.inside = R(0, 800, 100, 300);
out.exact = R(0, 800, 8, 784);                                  // fills the view with the margin to spare
out.below = R(0, 800, 1000, 300, {max: 5000});                  // bottom edge + margin brought to the bottom of the view
out.above = R(2000, 800, 500, 300, {max: 5000});                // top edge - margin brought to the top
out.partlyBelow = R(0, 800, 600, 300, {max: 5000});
out.partlyAbove = R(400, 800, 350, 200, {max: 5000});
out.clamped = R(0, 800, 4000, 600, {max: 1000});                // cannot scroll further than the workspace goes
out.noRoom = R(0, 800, 1000, 300, {max: 0});
""")
    assert out["inside"] is None and out["exact"] is None
    assert out["below"] == 1000 + 300 + 8 - 800 == 508
    assert out["above"] == 500 - 8 == 492
    assert out["partlyBelow"] == 600 + 300 + 8 - 800 == 108
    assert out["partlyAbove"] == 350 - 8 == 342
    assert out["clamped"] == 1000
    assert out["noRoom"] is None, "the workspace does not scroll at all"


@needs_node
def test_a_window_taller_than_the_view_only_needs_its_title_bar_in_sight():
    out = _run("""
const R = (st, vh, top, h, o) => L.revealScroll(st, vh, top, h, 8, o);
out.titleVisible = R(300, 800, 400, 2000, {max: 5000});         // 3000-high window whose title bar is on screen: leave it
out.titleAbove = R(900, 800, 400, 2000, {max: 5000});           // scrolled past its top: back to its top
out.titleBelow = R(0, 800, 1500, 2000, {max: 5000});           // not reached yet: its top is brought to the top of the view
out.titleHidden = R(300, 800, 295, 2000, {max: 5000});          // the title bar is under the top edge
""")
    assert out["titleVisible"] is None
    assert out["titleAbove"] == 400 - 8
    assert out["titleBelow"] == 1500 - 8
    assert out["titleHidden"] == 295 - 8


@needs_node
def test_a_collapsed_window_counts_by_its_title_bar_height_and_nonsense_never_scrolls():
    out = _run("""
out.collapsed = L.revealScroll(0, 800, 1500, 34, 8, {max: 5000});    // a collapsed window is just its title bar
out.junk = [L.revealScroll(NaN, 800, 100, 300, 8), L.revealScroll(0, 0, 100, 300, 8), L.revealScroll(0, 800, undefined, 300, 8), L.revealScroll(0, 800, 100, NaN, 8)];
""")
    assert out["collapsed"] == 1500 + 34 + 8 - 800
    assert out["junk"] == [None, None, None, None]


# ── "screen 2 of 3" ──────────────────────────────────────────────────────────────────────────────────────────────────

@needs_node
def test_the_workspace_says_which_screenful_it_is_showing():
    out = _run("""
out.fits = L.scrollPages(0, 878, 878);
out.almost = L.scrollPages(0, 878, 881);                       // a few px more than fits is not "scrollable"
out.two = [0, 150, 383].map(st => L.scrollPages(st, 878, 1261));            // 150 of 383 is not yet half way
out.three = [0, 250, 561, 900, 1122].map(st => L.scrollPages(st, 878, 2000));  // the screenful nearest to where you are
out.hair = L.scrollPages(0, 878, 890);
out.junk = [L.scrollPages(NaN, 878, 2000), L.scrollPages(0, 0, 2000), L.scrollPages(0, undefined, undefined)];
""")
    assert out["fits"]["scrollable"] is False and out["almost"]["scrollable"] is False
    assert [(p["page"], p["pages"]) for p in out["two"]] == [(1, 2), (1, 2), (2, 2)]
    assert [p["page"] for p in out["three"]] == [1, 1, 2, 3, 3] and all(p["pages"] == 3 for p in out["three"])
    assert (out["two"][0]["atTop"], out["two"][0]["atBottom"]) == (True, False)
    assert (out["two"][2]["atTop"], out["two"][2]["atBottom"]) == (False, True)
    assert out["hair"]["scrollable"] is True and out["hair"]["pages"] == 2, "a workspace that scrolls at all is at least two screens"
    assert all(j["scrollable"] is False or j["page"] >= 1 for j in out["junk"])


@needs_node
def test_a_step_is_most_of_a_screen_and_a_wheel_is_counted_in_pixels():
    out = _run("""
out.steps = [L.pageStep(878), L.pageStep(600), L.pageStep(50), L.pageStep(undefined)];
out.wheel = [L.wheelPixels(100, 0, 800), L.wheelPixels(3, 1, 800), L.wheelPixels(1, 2, 800), L.wheelPixels(-1, 2, 800), L.wheelPixels(NaN, 0, 800), L.wheelPixels(-120, 0)];
""")
    assert out["steps"] == [746, 510, 80, 80], "85% of the view, never less than 80px"
    assert out["wheel"] == [100, 120, 680, -680, 0, -120]


# ── a window added while the workspace is scrolled ────────────────────────────────────────────────────────────────────

@needs_node
def test_a_window_added_while_scrolled_opens_in_the_part_you_are_looking_at():
    out = _run("""
const vp = {w: 1200, h: 700}, size = {w: 400, h: 300};
out.top = L.cascade(0, size, vp);
out.scrolled = L.cascade(0, size, vp, 900);
out.third = L.cascade(2, size, vp, 900);
out.neg = L.cascade(0, size, vp, -50);
out.nan = L.cascade(0, size, vp, NaN);
""")
    assert out["top"] == {"x": 48, "y": 40}, "unchanged for a workspace that is not scrolled"
    assert out["scrolled"] == {"x": 48, "y": 940}
    assert out["third"]["y"] == 900 + 40 + 56 and out["third"]["x"] == 48 + 56
    assert out["neg"] == out["top"] and out["nan"] == out["top"]


def test_a_drag_stops_where_the_server_stops_keeping_a_windows_place():
    src = (ROOT / "app/routers/cockpit.py").read_text()
    server_y = int(re.search(r'"y": _clamp_int\(p\.get\("y"\), 0, (\d+)\)', src).group(1))
    server_h = int(re.search(r'"h": _clamp_int\(p\.get\("h"\), 120, (\d+), default=300\)', src).group(1))
    layout = (ROOT / "static/js/cockpit-layout.js").read_text()
    assert int(re.search(r"var MAX_Y = (\d+);", layout).group(1)) == server_y, "a window dragged lower than this would jump back when saved"
    assert f"clamp(start.h + dy, minH, {server_h})" in layout, "so would one stretched taller"


# ── the keys ──────────────────────────────────────────────────────────────────────────────────────────────────────────

KEYS_HARNESS = r"""
global.window = global;
require(process.env.KEYS);
const K = global.ndCockpitKeys;
const ev = (o) => Object.assign({key: '', code: '', ctrl: false, alt: false, meta: false, shift: false, typing: false, mac: false}, o);
const act = (o) => { const r = K.shortcutFor(ev(o)); return r && r.action; };
const out = {};
"""


def _keys(body: str):
    script = KEYS_HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"KEYS": str(ROOT / "static/js/cockpit-keys.js"), "PATH": os.environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_alt_and_the_arrows_scroll_the_workspace_a_screen_or_to_the_ends_and_leave_text_fields_alone():
    out = _keys("""
out.up = act({code: 'ArrowUp', alt: true});
out.down = act({code: 'ArrowDown', alt: true});
out.top = act({code: 'ArrowUp', alt: true, shift: true});
out.end = act({code: 'ArrowDown', alt: true, shift: true});
out.plain = [act({code: 'ArrowDown'}), act({code: 'ArrowUp'}), act({code: 'ArrowDown', shift: true})];       // the page's own arrows
out.ctrl_alt = [act({code: 'ArrowDown', ctrl: true, alt: true}), act({code: 'ArrowUp', ctrl: true, alt: true})];   // AltGr on many keyboards
out.meta = act({code: 'ArrowDown', alt: true, meta: true});
out.typing = [act({code: 'ArrowDown', alt: true, typing: true}), act({code: 'ArrowUp', alt: true, typing: true})];  // a select / text field: Alt+Down opens a dropdown
out.mac_typing = act({code: 'ArrowDown', alt: true, typing: true, mac: true});
out.help = K.helpRows().filter(r => /\\u2191/.test(r.keys)).map(r => r.keys);
""")
    assert (out["up"], out["down"], out["top"], out["end"]) == ("scrollup", "scrolldown", "scrolltop", "scrollend")
    assert out["plain"] == [None, None, None], "a page's own arrow keys are never ours"
    assert out["ctrl_alt"] == [None, None] and out["meta"] is None
    assert out["typing"] == [None, None] and out["mac_typing"] is None
    assert out["help"] == ["Alt + ↑  /  Alt + ↓"], "listed once, the Shift variants are the unlisted ones"


# ── the wiring ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _js():
    return (ROOT / "static/js/cockpit.js").read_text()


def test_carrying_or_stretching_a_window_to_the_top_or_bottom_edge_scrolls_the_workspace():
    js = _js()
    drag = js.split("function wireDrag(handle, win, p) {", 1)[1].split("function wireResize", 1)[0]
    resize = js.split("function wireResize(handle, win, p, dir) {", 1)[1].split("// ── the active window", 1)[0]
    for name, body in (("drag", drag), ("resize", resize)):
        assert "autoScroller(apply, p)" in body, name
        assert "auto.update(ev)" in body and "auto.stop();" in body, name
        assert "scheduleScrollUi();" in body, name
        # the window follows the content: the position is worked out from the pointer and the CURRENT scroll, in apply(), which the
        # autoscroller replays for the last pointer event
        assert "viewport.scrollTop" in body and "function apply(ev)" in body, name
        # a click that wobbled a few pixels must not start the workspace running
        assert "> 8) auto.update(ev)" in body, name
        # and a capture that ends without a pointerup (focus moved, the window went away) must never leave it scrolling by itself:
        # the end of a drag runs once however it ends
        assert "handle.addEventListener('lostpointercapture', up);" in body and "if (ended) return;" in body, name
    assert "Math.min(LAYOUT.MAX_Y, Math.max(0, ev.clientY - sy + viewport.scrollTop))" in drag, "a drag stops where the server stops keeping the place"
    scroller = js.split("function autoScroller(apply, p) {", 1)[1].split("// The status bar's", 1)[0]
    assert "LAYOUT.autoScrollStep(ev.clientY, r.top, r.height)" in scroller
    assert "now - last" in scroller and "Math.min(64, now - last)" in scroller, "by time, not by frame: 144 Hz is not faster and a stalled tab does not jump"
    assert "setSpacer(null)" in scroller, "the room made below the window goes away when the button is released"


def test_a_workspace_that_fits_gets_room_below_a_window_while_it_is_carried_so_it_can_be_stretched_past_the_screen():
    js = _js()
    assert "function setSpacer(bottom)" in js and "spacer.id = 'ck-spacer'" in js
    assert "setSpacer(p.y + p.h + Math.round(viewport.clientHeight * 0.5))" in js
    # ... only once a drag is scrolling DOWN: an ordinary drag never makes the scrollbar flicker in and out under the windows
    frame = js.split("function frame(now) {", 1)[1].split("return {", 1)[0]
    assert "if (d > 0 || spacer) makeRoom();" in frame
    assert frame.index("LAYOUT.autoScrollStep") < frame.index("makeRoom()")
    html = (ROOT / "app/templates/cockpit.html").read_text()
    spacer_css = html.split("#ck-spacer {", 1)[1].split("}", 1)[0]
    # (an element that is not displayed adds nothing to the scrollable area; a hidden one with a box does)
    assert "position: absolute" in spacer_css and "visibility: hidden" in spacer_css and "pointer-events: none" in spacer_css
    assert "display: none" not in spacer_css
    # the ▲ 1/2 ▼ does not flicker with it
    assert "if (!scrollBox || spacer) return;" in js


def test_alt_and_the_wheel_scroll_the_workspace_over_any_window_not_the_page_inside_it():
    js = _js()
    fn = js.split("function workspaceWheel(e) {", 1)[1].split("// Cancelling a page's own scroll", 1)[0]
    assert "if (e.ctrlKey || e.metaKey || MOBILE()" in fn, "Ctrl + wheel is the browser's zoom"
    assert "if (e.altKey === (inPage && wheelOnWorkspace())) return false;" in fn, "Alt scrolls the workspace - or, with the option ticked, over a PAGE, gives the wheel back"
    assert "LAYOUT.wheelPixels(e.deltaY, e.deltaMode, viewport.clientHeight)" in fn
    assert "scrollRoom() < 1) return false" in fn and fn.index("scrollRoom() < 1") < fn.index("e.preventDefault()"), "nothing to scroll: the wheel is left alone"
    # the cockpit's own DOM (a notes window's textarea scrolls too) ...
    assert "watchWheel(window);" in js
    # ... and every page in a window, each time one loads (a page is a document of its own: its wheel never reaches this one)
    relay = js.split("function relayFrameEvents(f) {", 1)[1].split("function activateFrameWindow", 1)[0]
    assert "watchWheel(w);" in relay
    # a NON-passive wheel listener makes every scroll over its page wait for the main thread, which the cockpit and all its windows share:
    # it exists only while Alt is held (armed by Alt going down, taken off by Alt going up or a wheel without it)
    watch = js.split("function armWheel(on) {", 1)[1].split("watchWheel(window);", 1)[0]
    assert "w.addEventListener('wheel', workspaceWheel, { capture: true, passive: false })" in watch
    assert "w.removeEventListener('wheel', workspaceWheel, { capture: true })" in watch
    assert "e.key === 'Alt' && !altDown) armWheel(true)" in watch and "e.key === 'Alt') armWheel(false)" in watch
    assert "{ capture: true, passive: true }" in watch, "the always-on observer must be passive"
    assert js.count("workspaceWheel, { capture: true, passive: false }") == 1, "the only place the blocking listener is added is the one that arms it"


def test_a_window_is_brought_into_sight_when_it_is_shown_added_or_reopened_and_a_new_arrangement_starts_at_the_top():
    js = _js()
    assert "LAYOUT.revealScroll(viewport.scrollTop, viewport.clientHeight, p.y, h, LAYOUT.GAP, { max: scrollRoom() })" in js
    show = js.split("function showPanel(p) {", 1)[1].split("function parkPanel", 1)[0]
    assert "revealWin(p);" in show and show.index("applyDockState();") < show.index("revealWin(p);"), "after its geometry is applied"
    add = js.split("function addPanel(type, ref, title, opts) {", 1)[1].split("// ── layouts: scenes", 1)[0]
    assert "LAYOUT.cascade(panels.length, size, vp, MOBILE() ? 0 : viewport.scrollTop)" in add, "opens where you are looking"
    assert "{ revealWin(p); save(); }" in add
    assert "revealWin(item.panel);" in js.split("function undoClose() {", 1)[1].split("document.addEventListener('keydown'", 1)[0]
    for name, after in (("function applyPreset(name) {", "function deletePreset"), ("async function applyScene(sc) {", "layoutsBtn.addEventListener"),
                        ("function autoArrange() {", "pinBtn.addEventListener"), ("function setSlots(v) {", "if (dockSlots)")):
        body = js.split(name, 1)[1].split(after, 1)[0]
        assert "scrollWorkspaceTo(0," in body, name
    reset = js.split("document.getElementById('ck-reset').addEventListener('click', function () {", 1)[1].split("});", 1)[0]
    assert "scrollWorkspaceTo(0, false);" in reset, "the default windows are read from the top"
    dock = js.split("function dockPanel(win, p, right) {", 1)[1].split("}", 1)[0]
    assert "p.y = Math.round(viewport.scrollTop);" in dock, "the half docked is the half you are looking at"


def test_a_maximised_window_is_scrolled_to_and_the_workspace_is_locked_until_it_is_given_back():
    js = _js()
    fn = js.split("function syncMaxLock() {", 1)[1].split("new MutationObserver", 1)[0]
    assert "maxedAt = viewport.scrollTop; viewport.scrollTop = 0;" in fn and "viewport.scrollTop = back;" in fn
    assert "new MutationObserver(syncMaxLock).observe(viewport, { attributes: true, attributeFilter: ['class'], subtree: true, childList: true });" in js
    html = (ROOT / "app/templates/cockpit.html").read_text()
    assert "#ck-viewport.ck-maxed { overflow-y: hidden; }" in html
    # the key and the three buttons all just toggle the class, so ONE observer covers them (and a maximised window that is closed)
    assert js.count("classList.toggle('maxed')") >= 3


def test_the_scroll_keys_buttons_and_wheel_are_explained_in_the_help_and_the_status_bar_and_the_scrollbar_is_visible(client, seed):
    js = _js()
    tips = js.split("const MOUSE_TIPS = [", 1)[1].split("];", 1)[0]
    assert "Alt + mouse wheel" in tips and "Drag or resize at the top / bottom edge" in tips
    for action in ("scrollup", "scrolldown", "scrolltop", "scrollend"):
        assert f"k.action === '{action}'" in js, action
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    status = html.split('<footer id="ck-status">', 1)[1].split("</footer>", 1)[0]
    assert 'id="ck-stat-scroll"' in status and " hidden" in status.split('id="ck-stat-scroll"', 1)[1].split(">", 1)[0], "only shown while the workspace scrolls"
    for ident in ("ck-scroll-up", "ck-scroll-down", "ck-scroll-pos"):
        assert f'id="{ident}"' in status, ident
    assert status.index('id="ck-stat-scroll"') < status.index('id="ck-help-btn"')
    for needle in ("scrollbar-color:", "#ck-viewport::-webkit-scrollbar { width: 14px; }", "#ck-stat-scroll[hidden] { display: none; }"):
        assert needle in html, needle
    # the scrollbar's place is NOT reserved: a layout that fits the screen (arranged before there was one) keeps every pixel of its width,
    # where reserving ~15px would clip the windows at the right edge and half hide the scrollbar of the page inside them
    assert "scrollbar-gutter" not in html.split("<style>", 1)[1]


def test_the_wheel_can_be_made_to_scroll_the_workspace_over_windows_in_this_browser(client, seed):
    js = _js()
    assert "const WHEEL_KEY = 'nd_cockpit_wheel';" in js
    # the pages in the windows are armed all the time with the option on, the cockpit's own document only while Alt is down: its dock
    # chips, menus and a notes window's text keep their plain wheel
    sync = js.split("function syncWheel() {", 1)[1].split("function watchWheel", 1)[0]
    assert "altDown || (w !== window && wheelOnWorkspace())" in sync
    fn = js.split("function workspaceWheel(e) {", 1)[1].split("// Cancelling a page's own scroll", 1)[0]
    assert "const inPage = !!e.view && e.view !== window;" in fn
    toggle = js.split("function toggleWheelMode() {", 1)[1].split("\n  }\n", 1)[0]
    assert "setPref(WHEEL_KEY, wheelOnWorkspace() ? '' : '1');" in toggle and "syncWheel();" in toggle
    assert "applyWheelRow();" in js.split("applyLock();", 1)[1].split("fitViewport();", 1)[0], "the menu row shows the saved choice at start"
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    row = html.split('id="ck-wheel-row"', 1)[1].split("</button>", 1)[0]
    assert 'role="menuitemcheckbox"' in row and 'aria-pressed="false"' in row and "Wheel scrolls the workspace" in row
    assert html.index('id="ck-lock-row"') < html.index('id="ck-wheel-row"') < html.index('id="ck-reset"')
