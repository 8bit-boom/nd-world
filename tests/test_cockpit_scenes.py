"""The cockpit's dock on the BIG screen, and scenes (a view over the windows): the pure maths under Node
(static/js/cockpit-layout.js: showWide / parkWide / applyScene and the saved shape's `parked`), what the server keeps, and the
Layouts menu that replaced the preset select, delete, save, auto-arrange and reset buttons of the header.

Driven in Chromium against the running app too: every scene on the big screen and on a laptop, windows reused rather than
duplicated, the dock chips on the big screen, parking and bringing back, the menu by keyboard."""
import json
import os
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
const pref = (p) => ({map: 560, combat: 560, dice: 660, party: 380, quests: 340, notes: 300, ai_chat: 640, wmap: 560, entity: 620, find: 540, tables: 280, calendar: 480}[p.type] || 300);
const win = (id, type, extra) => Object.assign({id: id, type: type, x: 10, y: 10, w: 400, h: 300, z: 1, collapsed: false}, extra || {});
const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const shown = (ps) => ps.filter(p => !p.parked);
"""


def _run(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"PATH": os.environ["PATH"], "LAYOUT": str(ROOT / "static/js/cockpit-layout.js")}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── the big screen's dock ───────────────────────────────────────────────────────────────────────

@needs_node
def test_a_window_in_the_dock_comes_back_and_to_the_front_and_anything_may_wait_there():
    out = _run("""
    const ps = [win('a', 'map', {z: 3}), win('b', 'dice', {z: 5, parked: true, collapsed: true}), win('c', 'notes', {z: 4})];
    out.front = L.showWide(ps, 'a');                       // already showing: just raised
    out.front_z = ps[0].z;
    out.shown = L.showWide(ps, 'b');
    out.b = {parked: ps[1].parked, collapsed: ps[1].collapsed, z: ps[1].z};
    out.none = L.showWide(ps, 'zzz');
    out.park_c = L.parkWide(ps, 'c');
    out.park_c_again = L.parkWide(ps, 'c');
    out.park_unknown = L.parkWide(ps, 'zzz');
    // there is no "one has to stay": every window may wait in the dock on the big screen
    L.parkWide(ps, 'a'); L.parkWide(ps, 'b');
    out.all_parked = ps.every(p => p.parked);
    out.untouched_box = ps.every(p => p.x === 10 && p.w === 400);       // parking never moves or resizes anything
    """)
    assert out["front"] == "front" and out["front_z"] == 6
    assert out["shown"] == "shown" and out["b"] == {"parked": False, "collapsed": False, "z": 7}
    assert out["none"] == "none"
    assert out["park_c"] is True and out["park_c_again"] is False and out["park_unknown"] is False
    assert out["all_parked"] is True and out["untouched_box"] is True


@needs_node
def test_the_big_screens_dock_is_saved_at_the_top_level_and_the_laptops_stays_in_lap():
    out = _run("""
    const saved = [win('a', 'map', {parked: true}), win('b', 'dice', {lap: {x: 8, y: 8, w: 900, h: 500, z: 2, collapsed: false, parked: true}}), win('c', 'notes')];
    // opened on the big screen: a waits in the dock there, b and c show; the laptop's flags are kept for later
    const wide = L.toWorking(saved, {klass: 'wide'}).panels;
    out.wide_flags = wide.map(p => !!p.parked);
    out.lap_kept = wide[1]._lap && wide[1]._lap.parked;
    const back = L.toSaved(wide, 'wide');
    out.back_parked = back.map(p => p.parked === true);
    out.back_lap = back[1].lap.parked;
    out.back_has_no_false = back.every(p => p.parked === undefined || p.parked === true);
    // opened on a laptop, then saved from there: the big screen's flag survives untouched
    const lap = L.toWorking(saved, {klass: 'laptop', vp: {w: 1280, h: 600}, slots: 2, pref: pref}).panels;
    out.lap_has_own = lap.map(p => !!p.parked);
    const fromLap = L.toSaved(lap, 'laptop');
    out.wide_survived = fromLap.map(p => p.parked === true);
    """)
    assert out["wide_flags"] == [True, False, False] and out["lap_kept"] is True
    assert out["back_parked"] == [True, False, False] and out["back_lap"] is True and out["back_has_no_false"] is True
    assert out["wide_survived"] == [True, False, False], "a trip through the laptop must not change who waits in the big screen's dock"


@needs_node
def test_a_layout_saved_before_the_big_screen_had_a_dock_shows_every_window():
    out = _run("""
    const saved = [win('a', 'map'), win('b', 'dice'), win('c', 'notes')];
    const wide = L.toWorking(saved, {klass: 'wide'}).panels;
    out.parked = wide.map(p => !!p.parked);
    out.saved_keys = L.toSaved(wide, 'wide').map(p => Object.keys(p).filter(k => k === 'parked' || k.startsWith('_')));
    """)
    assert out["parked"] == [False, False, False]
    assert out["saved_keys"] == [[], [], []], "nothing private leaks into what is saved, and no parked: false is written"


@needs_node
def test_switching_class_back_and_forth_never_loses_either_screens_dock():
    out = _run("""
    let saved = [win('a', 'map'), win('b', 'dice'), win('c', 'notes'), win('d', 'combat')];
    // big screen: park b
    let w = L.toWorking(saved, {klass: 'wide'}).panels;
    L.parkWide(w, 'b');
    saved = L.toSaved(w, 'wide');
    // laptop: derives its own arrangement, then the user brings d in and parks a
    let l = L.toWorking(saved, {klass: 'laptop', vp: {w: 1280, h: 600}, slots: 2, pref: pref}).panels;
    L.showLaptop(l, 'd', {w: 1280, h: 600}, 2, pref);
    saved = L.toSaved(l, 'laptop');
    // back on the big screen: b is still waiting in ITS dock, nothing else is
    w = L.toWorking(saved, {klass: 'wide'}).panels;
    out.wide_again = w.map(p => !!p.parked);
    // and the laptop still has its own arrangement
    l = L.toWorking(L.toSaved(w, 'wide'), {klass: 'laptop', vp: {w: 1280, h: 600}, slots: 2, pref: pref}).panels;
    out.lap_again_shown = shown(l).map(p => p.id).sort();
    """)
    assert out["wide_again"] == [False, True, False, False]
    assert len(out["lap_again_shown"]) == 2 and "d" in out["lap_again_shown"]


# ── scenes ────────────────────────────────────────────────────────────────────────────────────────

SIZES = [("laptop14", 1280, 560), ("laptop15", 1536, 700), ("fhd", 1920, 960), ("qhd", 2560, 1300), ("4k", 3824, 1880)]


@needs_node
@pytest.mark.parametrize("name,w,h", SIZES)
def test_a_scene_on_the_big_screen_shows_exactly_its_windows_tiled_and_parks_the_rest(name, w, h):
    out = _run(f"""
    const vp = {{w: {w}, h: {h}}};
    const ps = [win('p1', 'notes', {{z: 9}}), win('p2', 'map'), win('p3', 'combat'), win('p4', 'party'), win('p5', 'dice'), win('p6', 'entity'), win('p7', 'quests')];
    const before = ps.map(p => [p.x, p.y, p.w, p.h]);
    L.applyScene(ps, ['p2', 'p3', 'p4', 'p5'], {{klass: 'wide', vp: vp, slots: Infinity, pref: pref, stage: 'p2'}});
    const sh = shown(ps);
    out.shown = sh.map(p => p.id).sort();
    out.parked = ps.filter(p => p.parked).map(p => p.id).sort();
    out.inside = sh.every(p => p.x >= 0 && p.y >= 0 && p.x + p.w <= vp.w + 1);
    out.overlaps = sh.some((a, i) => sh.some((b, j) => i < j && overlaps(a, b)));
    const stage = ps.find(p => p.id === 'p2');
    out.stage_is_largest = sh.every(p => p === stage || stage.w * stage.h >= p.w * p.h);
    out.parked_boxes_kept = ps.filter(p => p.parked).every(p => {{ const i = ps.indexOf(p); return before[i][0] === p.x && before[i][1] === p.y && before[i][2] === p.w && before[i][3] === p.h; }});
    out.z = ['p2', 'p3', 'p4', 'p5'].map(id => ps.find(p => p.id === id).z);
    out.min = Math.min(...sh.map(p => Math.min(p.w, p.h)));
    """)
    assert out["shown"] == ["p2", "p3", "p4", "p5"]
    assert out["parked"] == ["p1", "p6", "p7"]
    assert out["inside"] is True and out["overlaps"] is False
    assert out["stage_is_largest"] is True
    assert out["parked_boxes_kept"] is True, "on the big screen the dock's windows keep the place they had"
    assert out["z"] == sorted(out["z"], reverse=True), "recency follows the scene's order: the stage is the most recent"
    assert out["min"] >= 140


@needs_node
@pytest.mark.parametrize("slots,expect", [(1, 1), (2, 2), (3, 3)])
def test_a_scene_on_a_laptop_shows_only_as_many_windows_as_the_screen_takes_and_the_stage_is_main(slots, expect):
    out = _run(f"""
    const vp = {{w: 1280, h: 560}};
    const ps = [win('p1', 'notes'), win('p2', 'map'), win('p3', 'combat'), win('p4', 'party'), win('p5', 'dice')];
    L.applyScene(ps, ['p2', 'p3', 'p4', 'p5'], {{klass: 'laptop', vp: vp, slots: {slots}, pref: pref, stage: 'p2'}});
    const sh = shown(ps);
    out.shown = sh.map(p => p.id).sort();
    out.parked = ps.filter(p => p.parked).map(p => p.id).sort();
    out.main = L.mainOf(sh).id;
    out.overlaps = sh.some((a, i) => sh.some((b, j) => i < j && overlaps(a, b)));
    const m = L.mainOf(sh);
    out.parked_at_main = ps.filter(p => p.parked).every(p => p.x === m.x && p.y === m.y && p.w === m.w && p.h === m.h);
    """)
    assert len(out["shown"]) == expect and out["shown"] == ["p2", "p3", "p4"][:expect]
    assert out["main"] == "p2", "the scene's first window is the stage / main window"
    assert out["overlaps"] is False
    assert out["parked_at_main"] is True, "what waits in the laptop's dock sits where a swap will find a sane box"
    assert "p1" in out["parked"]


@needs_node
def test_a_scene_ignores_ids_that_do_not_exist_and_duplicates_and_does_nothing_for_an_empty_one():
    out = _run("""
    const vp = {w: 1920, h: 960};
    const ps = [win('a', 'map'), win('b', 'dice'), win('c', 'notes')];
    L.applyScene(ps, ['ghost', 'a', 'a', 'b', 'nope'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref, stage: 'a'});
    out.shown = shown(ps).map(p => p.id).sort();
    const same = ps.map(p => JSON.stringify(p));
    L.applyScene(ps, [], {klass: 'wide', vp: vp, slots: Infinity, pref: pref});
    L.applyScene(ps, ['ghost'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref});
    out.unchanged = ps.map(p => JSON.stringify(p)).join() === same.join();
    // no stage named: the ordinary tiler picks its own (a map beats the rest)
    L.applyScene(ps, ['b', 'a', 'c'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref});
    out.no_stage = shown(ps).length;
    // a stage that is not among the windows shown falls back to no stage
    L.applyScene(ps, ['b', 'c'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref, stage: 'a'});
    out.stage_missing = shown(ps).map(p => p.id).sort();
    """)
    assert out["shown"] == ["a", "b"] and out["unchanged"] is True
    assert out["no_stage"] == 3 and out["stage_missing"] == ["b", "c"]


@needs_node
def test_applying_scenes_one_after_another_keeps_every_window_and_each_scene_brings_its_own_back():
    out = _run("""
    const vp = {w: 1920, h: 960};
    const ps = [win('map1', 'map'), win('cmb', 'combat'), win('pty', 'party'), win('dice1', 'dice'), win('npc', 'entity'), win('nts', 'notes'), win('ai', 'ai_chat')];
    const ids = (arr) => shown(ps).map(p => p.id).sort();
    L.applyScene(ps, ['map1', 'cmb', 'pty', 'dice1'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref, stage: 'map1'});
    out.combat = ids();
    L.applyScene(ps, ['npc', 'pty', 'nts', 'ai'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref, stage: 'npc'});
    out.roleplay = ids();
    out.count = ps.length;
    // the combat windows were only parked: back to combat brings the same ones, nothing was closed or recreated
    L.applyScene(ps, ['map1', 'cmb', 'pty', 'dice1'], {klass: 'wide', vp: vp, slots: Infinity, pref: pref, stage: 'map1'});
    out.combat_again = ids();
    out.count_again = ps.length;
    """)
    assert out["combat"] == ["cmb", "dice1", "map1", "pty"]
    assert out["roleplay"] == ["ai", "npc", "nts", "pty"]
    assert out["combat_again"] == out["combat"] and out["count"] == out["count_again"] == 7


# ── what the server keeps ───────────────────────────────────────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _panel(i, **extra):
    return {"id": f"p{i}", "type": "dice", "ref": "", "title": "x", "x": 8, "y": 8, "w": 400, "h": 300, "z": 1, **extra}


def test_the_big_screens_dock_flag_is_saved_and_only_a_real_true_counts(client, seed):
    _login_gm(client, seed)
    panels = [_panel(1, parked=True), _panel(2, parked=False), _panel(3, parked="yes"), _panel(4, parked=1), _panel(5),
              _panel(6, parked=True, lap={"x": 8, "y": 8, "w": 900, "h": 500, "z": 1, "collapsed": False, "parked": False})]
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": panels}, "presets": {"Combat": {"panels": panels[:2]}}}).status_code == 200
    ws = client.get("/api/cockpit/workspace").json()["workspace"]
    got = [p.get("parked") for p in ws["current"]["panels"]]
    assert got == [True, None, None, None, None, True]
    assert ws["current"]["panels"][5]["lap"]["parked"] is False, "the laptop's flag is its own"
    assert [p.get("parked") for p in ws["presets"]["Combat"]["panels"]] == [True, None]


# ── wiring: the Layouts menu ──────────────────────────────────────────────────────────────────────

def test_the_header_has_one_layouts_menu_instead_of_five_controls_and_keeps_the_ids_the_tests_know(client, seed):
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    head = html.split('<header id="ck-head">', 1)[1].split("</header>", 1)[0]
    for gone in ('id="ck-preset-select"', 'id="ck-delete-preset"', 'id="ck-save-preset"', 'id="ck-pin-btn"', 'id="ck-reset"'):
        assert gone not in head, gone
    assert 'id="ck-layouts-btn"' in head and 'aria-haspopup="menu"' in head
    menu = html.split('id="ck-layouts-pop"', 1)[1].split("<style>", 1)[0]
    for needle in ('id="ck-scenes"', 'id="ck-saved"', 'id="ck-pin-btn"', 'id="ck-save-preset"', 'id="ck-reset"'):
        assert needle in menu, needle
    assert "const CK_MAX_PRESETS = 12;" in html


def test_players_get_the_layouts_menu_without_scenes(client, seed):
    from .conftest import PLAYER_PASSWORD
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/player-cockpit")
    assert r.status_code == 200
    menu = r.text.split('id="ck-layouts-pop"', 1)[1].split("<style>", 1)[0]
    assert 'id="ck-saved"' in menu and 'id="ck-scenes"' not in menu


def test_scenes_reuse_windows_add_what_is_missing_and_are_a_gm_feature():
    js = (ROOT / "static/js/cockpit.js").read_text()
    for needle in ("const SCENES = [", "async function applyScene(sc, opts)", "async function sceneWindow(type)", "LAYOUT.applyScene(panels, ids,",
                   "{ scene: true }", "if (MOBILE() || PLAYER) return;"):
        assert needle in js, needle
    for sid in ("combat", "explore", "roleplay", "prep"):
        assert f"id: '{sid}'" in js, sid
    scene_fn = js.split("async function sceneWindow(type) {", 1)[1].split("async function applyScene", 1)[0]
    # an existing window is used before one is made; an entity is never invented (it needs a choice)
    assert scene_fn.index("const have = panels.filter") < scene_fn.index("addPanel(type, ref, title")
    assert "type === 'entity') return null" in scene_fn
    # a scene saves ONCE, after arranging what it added - not once per window it made
    assert "if (!(opts && opts.scene)) { revealWin(p); save(); }" in js


def test_saved_layouts_are_applied_deleted_and_saved_through_one_set_of_functions():
    js = (ROOT / "static/js/cockpit.js").read_text()
    for needle in ("function applyPreset(name)", "function deletePreset(name)", "function saveLayoutAs()", "function renderLayoutsMenu()",
                   "CK_MAX_PRESETS"):
        assert needle in js, needle
    for gone in ("presetSelect", "deletePresetBtn", "renderPresetSelect"):
        assert gone not in js, gone
    # replacing a layout is asked about, and the twelfth-plus layout the server would refuse whole is stopped here
    save_fn = js.split("function saveLayoutAs() {", 1)[1].split("// The window a scene wants", 1)[0]
    assert "confirm('Replace the saved layout" in save_fn and "toast('At most '" in save_fn


def test_the_dock_is_the_taskbar_of_both_layouts_and_the_slot_control_is_the_laptops():
    js = (ROOT / "static/js/cockpit.js").read_text()
    render = js.split("function renderDock() {", 1)[1].split("function revealChip", 1)[0]
    assert "dockEl.hidden = MOBILE() || !panels.length;" in render
    assert "!lap && !panels.some(function (p) { return p.parked; })" in render, "'Keep loaded' only matters on the big screen once something waits"
    assert "document.body.classList.toggle('ck-keep', !MOBILE() && keepLoaded());" in js
    show = js.split("function showPanel(p) {", 1)[1].split("function parkPanel", 1)[0]
    assert "LAYOUT.showWide(panels, p.id)" in show and "LAYOUT.showLaptop(" in show
    park = js.split("function parkPanel(p) {", 1)[1].split("function makeMainPanel", 1)[0]
    assert "LAYOUT.parkWide(panels, p.id)" in park and "One window has to stay on screen" in park
    # auto-arrange tidies what shows and leaves the dock alone
    assert "LAYOUT.visibleOf(panels).sort(" in js


def test_alt_l_opens_the_layouts_menu_and_the_palette_offers_the_scenes(client, seed):
    keys = (ROOT / "static/js/cockpit-keys.js").read_text()
    assert "action: 'layouts'" in keys and "KeyL" in keys
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "k.action === 'layouts'" in js and "layoutsBtn.click()" in js
    assert "'Scene: ' + sc.label" in js and "applyPreset(name)" in js
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    for needle in ("#ck-layouts-pop", ".ck-pop-h", "body.ck-laptop #ck-dock-slots { display: flex; }"):
        assert needle in html, needle
    assert "body.ck-laptop #ck-dock { display: flex" not in html, "the dock is no longer laptop-only"
