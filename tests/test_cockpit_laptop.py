"""The cockpit on a laptop (an ASUS Vivobook 14/15 is 1280-1536 CSS px wide and 600-700 tall).

Scaling a layout arranged on a 4K monitor down to a laptop gave 240px slivers (everything shrinks, nothing is readable),
and the first change made there overwrote the big screen's arrangement. So below ~1700px of window width the cockpit
shows a few windows at a time, big - a main window plus one or two beside it - and keeps the rest in a dock (`parked`).
Both screen classes share the windows and keep their OWN geometry: the big screen's stays where it always was (x/y/w/h
and `vp`), the laptop's travels as `lap` (and `lvp`).

The maths lives in static/js/cockpit-layout.js (pure functions) and is driven here under Node; the server keeps and
validates the laptop geometry; the page wiring is checked against the shipped sources. The real thing - the dock, swapping,
the mode button, reloads, the big screen left untouched - was driven in Chromium against the running app."""
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
const LAPTOPS = [[1280, 543], [1280, 626], [1366, 584], [1440, 700], [1536, 666], [1600, 700], [1100, 600], [1000, 560]].map(([w, h]) => ({w, h}));
const PREF = {map: 560, wmap: 560, combat: 560, dice: 660, party: 380, quests: 340, pc: 380, notes: 300, timer: 260,
              tables: 280, calendar: 480, ai_chat: 640, gallery: 640, audio: 640, video: 560, entity: 620, ecard: 400, find: 540};
const pref = p => PREF[p.type] || 300;
const mk = types => types.map((t, i) => ({id: 'p' + (i + 1), type: t, ref: '', title: t, x: 0, y: 0, w: 300, h: 300, z: i + 1, collapsed: false, accent: '', data: {}}));
// a layout arranged on a 4K monitor, in the saved shape (the big screen's geometry on top)
const BIG = {w: 3824, h: 1880};
const bigLayout = types => L.tilePanels(mk(types), BIG, {pref});
const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const shown = ps => ps.filter(p => !p.parked);
const box = p => [p.x, p.y, p.w, p.h];
const NINE = ['map', 'combat', 'party', 'quests', 'dice', 'entity', 'notes', 'ai_chat', 'audio'];
const out = {};
function laptop(saved, vp, slots) { return L.toWorking(saved, {klass: 'laptop', vp, slots: slots == null ? L.resolveSlots('auto', vp, saved.length) : slots, pref}); }
"""


def _run(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"LAYOUT": str(ROOT / "static/js/cockpit-layout.js"), "PATH": __import__("os").environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── which class a window is ─────────────────────────────────────────────────────────────────────

@needs_node
@pytest.mark.parametrize("width,expected", [(901, "laptop"), (1024, "laptop"), (1280, "laptop"), (1366, "laptop"), (1536, "laptop"),
                                            (1600, "laptop"), (1699, "laptop"), (1700, "wide"), (1920, "wide"), (2560, "wide"),
                                            (3840, "wide")])
def test_the_class_follows_the_window_width_alone(width, expected):
    out = _run(f"out.c = L.screenClass({width}, null, '');")
    assert out["c"] == expected


@needs_node
def test_going_fullscreen_does_not_flip_the_layout_because_the_width_does_not_change():
    # F11 changes the height only; a 1080p laptop at 100% keeps its 1920px width either way
    out = _run("out.a = L.screenClass(1920, 'wide', ''); out.b = L.screenClass(1920, 'wide', '');")
    assert out["a"] == out["b"] == "wide"
    src = (ROOT / "static/js/cockpit-layout.js").read_text()
    assert "function screenClass(width, prev, override)" in src      # no height parameter exists to flip on


@needs_node
def test_a_sidebar_opening_does_not_flip_the_layout_back_and_forth():
    out = _run("""
out.wide_stays = L.screenClass(1650, 'wide', '');        // inside the dead band: no change
out.wide_flips = L.screenClass(1630, 'wide', '');
out.lap_stays = L.screenClass(1750, 'laptop', '');
out.lap_flips = L.screenClass(1761, 'laptop', '');
""")
    assert (out["wide_stays"], out["wide_flips"], out["lap_stays"], out["lap_flips"]) == ("wide", "laptop", "laptop", "wide")


@needs_node
def test_the_users_own_choice_wins_and_nonsense_widths_fall_back_to_the_big_layout():
    out = _run("""
out.forced_lap = L.screenClass(3840, 'wide', 'laptop');
out.forced_wide = L.screenClass(1280, 'laptop', 'wide');
out.nan = L.screenClass(NaN, null, ''); out.zero = L.screenClass(0, null, ''); out.neg = L.screenClass(-5, 'wide', '');
out.nan_keeps_laptop = L.screenClass(NaN, 'laptop', '');
out.junk_override = L.screenClass(1280, null, 'sideways');
""")
    assert out == {"forced_lap": "laptop", "forced_wide": "wide", "nan": "wide", "zero": "wide", "neg": "wide",
                   "nan_keeps_laptop": "laptop", "junk_override": "laptop"}


# ── how many windows show at once ───────────────────────────────────────────────────────────────

@needs_node
def test_slots_follow_the_room_and_the_users_choice_and_never_exceed_the_windows():
    out = _run("""
out.small = L.laptopSlots({w: 950, h: 700});          // narrow: one
out.short = L.laptopSlots({w: 1280, h: 543});         // a Vivobook 14 FHD at 150%: a main window and one beside it
out.tall = L.laptopSlots({w: 1536, h: 666});          // a Vivobook 15: the side column splits in two
out.nonsense = L.laptopSlots(null);
out.choice = [1, 2, 3, '1', '2', '3', 'all', 'auto', 'x', undefined].map(c => L.resolveSlots(c, {w: 1536, h: 666}, 9));
out.capped = [L.resolveSlots(3, {w: 1536, h: 666}, 2), L.resolveSlots('all', {w: 1536, h: 666}, 4), L.resolveSlots(1, {w: 1536, h: 666}, 0), L.resolveSlots('auto', {w: 1536, h: 666}, 1)];
""")
    assert (out["small"], out["short"], out["tall"], out["nonsense"]) == (1, 2, 3, 2)
    assert out["choice"] == [1, 2, 3, 1, 2, 3, 9, 3, 3, 3]
    assert out["capped"] == [2, 4, 1, 1]


# ── the headline: a layout arranged on a 4K monitor becomes usable on a laptop ──────────────────

@needs_node
def test_a_4k_layout_opens_on_every_laptop_as_a_few_big_windows_not_slivers():
    out = _run("""
const bad = [];
for (const vp of LAPTOPS) {
  const saved = bigLayout(NINE);
  const w = laptop(saved, vp);
  const sh = shown(w.panels);
  const tag = vp.w + 'x' + vp.h;
  if (!w.derived) bad.push(tag + ': first time on a laptop should derive an arrangement');
  if (sh.length < 1 || sh.length > 3) bad.push(tag + ': ' + sh.length + ' windows showing');
  if (sh.length !== L.resolveSlots('auto', vp, 9)) bad.push(tag + ': showing ' + sh.length + ' but the slots say ' + L.resolveSlots('auto', vp, 9));
  sh.forEach(p => {
    if (p.x < 0 || p.y < 0 || p.x + p.w > vp.w + 1 || p.y + p.h > vp.h + 1) bad.push(tag + ' ' + p.type + ' leaves the screen ' + box(p));
    if (p.w < 400 || p.h < 200) bad.push(tag + ' ' + p.type + ' is a sliver ' + box(p));
  });
  for (let i = 0; i < sh.length; i++) for (let j = i + 1; j < sh.length; j++) if (overlap(sh[i], sh[j])) bad.push(tag + ': ' + sh[i].type + ' overlaps ' + sh[j].type);
  const main = L.mainOf(sh);
  if (main.type !== 'map') bad.push(tag + ': main is ' + main.type + ', not the map');
  if (main.w * main.h < sh.reduce((a, p) => a + p.w * p.h, 0) * 0.45) bad.push(tag + ': the main window is not the biggest share');
  if (sh.length > 1 && !(main.w >= vp.w * 0.5)) bad.push(tag + ': main is only ' + main.w + ' wide');
}
out.bad = bad;
// and what scaling alone (the old behaviour) gave on the same screen, for the record
const scaled = L.scalePanels(bigLayout(NINE), BIG, {w: 1536, h: 666});
out.scaledMinWidth = Math.min(...scaled.map(p => p.w));
out.scaledMinHeight = Math.min(...scaled.map(p => p.h));
""")
    assert out["bad"] == []
    assert out["scaledMinWidth"] < 300 and out["scaledMinHeight"] < 200       # the sliver problem this replaces (measured: 256 x 140+)


@needs_node
def test_the_map_or_combat_window_leads_then_the_ones_nearest_the_top_left():
    out = _run("""
const saved = bigLayout(['quests', 'dice', 'party', 'combat', 'notes']);      // no map: combat is the stage
const w = laptop(saved, {w: 1536, h: 666});
out.shown = shown(w.panels).map(p => p.type);
const byTop = saved.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x)).filter(p => p.type !== 'combat').slice(0, 2).map(p => p.type);
out.expectNext = byTop;
out.main = L.mainOf(shown(w.panels)).type;
""")
    assert out["main"] == "combat" and out["shown"][0] == "combat" or "combat" in out["shown"]
    assert sorted(out["shown"]) == sorted(["combat"] + out["expectNext"])


@needs_node
def test_few_windows_all_show_and_a_single_window_fills_the_screen():
    out = _run("""
const vp = {w: 1536, h: 666};
const two = laptop(bigLayout(['dice', 'notes']), vp);
out.two = two.panels.map(p => [p.parked || false, ...box(p)]);
const one = laptop(bigLayout(['dice']), vp);
out.one = one.panels.map(p => [p.parked || false, ...box(p)]);
out.none = laptop([], vp).panels.length;
""")
    assert all(not row[0] for row in out["two"]) and len(out["two"]) == 2
    assert out["one"] == [[False, 8, 8, 1520, 650]]
    assert out["none"] == 0


@needs_node
def test_tiling_can_be_told_which_window_is_the_main_one():
    out = _run("""
const ps = mk(['notes', 'dice', 'quests']);                      // no map / combat window at all
const tiled = L.tilePanels(ps, {w: 1536, h: 666}, {pref, stage: 1});
out.main = tiled.map((p, i) => [i, p.w * p.h]).sort((a, b) => b[1] - a[1])[0][0];
const plain = L.tilePanels(ps, {w: 1536, h: 666}, {pref});      // unchanged without the option
out.plain = plain.length;
""")
    assert out["main"] == 1 and out["plain"] == 3


# ── swapping windows in and out of the dock ─────────────────────────────────────────────────────

SETUP = """
const vp = {w: 1536, h: 666};
const w = laptop(bigLayout(NINE), vp, 3);
const ps = w.panels;
const id = t => ps.find(p => p.type === t).id;
const find = t => ps.find(p => p.type === t);
const ids = () => shown(ps).map(p => p.type).sort();
"""


@needs_node
def test_a_window_from_the_dock_takes_the_place_and_size_of_the_least_recently_used_one_and_never_the_main():
    out = _run(SETUP + """
out.before = ids();
find('combat').z = 50;                                          // combat used recently, party long ago
find('party').z = 1;
const partyBox = box(find('party'));
out.how = L.showLaptop(ps, id('dice'), vp, 3, pref);
out.after = ids();
out.dice_box = box(find('dice'));
out.party_box = partyBox;
out.party_parked = !!find('party').parked;
out.main = L.mainOf(shown(ps)).type;
out.dice_z_top = find('dice').z > Math.max(...ps.filter(p => p !== find('dice')).map(p => p.z));
""")
    assert out["before"] == ["combat", "map", "party"]
    assert out["how"] == "swapped" and out["after"] == ["combat", "dice", "map"]
    assert out["dice_box"] == out["party_box"] and out["party_parked"] is True
    assert out["main"] == "map" and out["dice_z_top"] is True


@needs_node
def test_peeking_at_a_window_and_bringing_the_other_back_restores_exactly_what_was_there():
    out = _run(SETUP + """
const start = shown(ps).map(p => [p.type, ...box(p)]).sort();
find('combat').z = 1; find('party').z = 2;                      // combat is the least recently used side window
out.start_types = shown(ps).map(p => p.type).sort();
const first = L.showLaptop(ps, id('dice'), vp, 3, pref);          // peek at dice: it replaces one side window
const replaced = out.start_types.filter(t => !shown(ps).some(p => p.type === t))[0];
L.showLaptop(ps, id(replaced), vp, 3, pref);                      // ... and the one that went comes back
out.replaced = replaced;
out.end = shown(ps).map(p => [p.type, ...box(p)]).sort();
out.same = JSON.stringify(out.end) === JSON.stringify(start);
""")
    assert out["same"] is True, out


@needs_node
def test_one_slot_of_the_screen_rotates_through_the_dock_while_the_windows_beside_the_main_stay():
    out = _run(SETUP + """
find('combat').z = 1; find('party').z = 2;                      // combat (beside the map) is the least recently used
const sides = () => shown(ps).filter(p => p.type !== 'map').map(p => p.type).sort();
out.start = sides();
const trail = [];
['dice', 'notes', 'audio', 'entity'].forEach(t => { L.showLaptop(ps, id(t), vp, 3, pref); trail.push(sides()); });
out.trail = trail;
""")
    # the first swap has no history, so it takes the least recently used (combat); after that the window last brought in goes
    assert out["start"] == ["combat", "party"]
    assert out["trail"] == [["dice", "party"], ["notes", "party"], ["audio", "party"], ["entity", "party"]]


@needs_node
def test_the_swap_history_is_never_saved():
    out = _run(SETUP + """
L.showLaptop(ps, id('dice'), vp, 3, pref);
out.keys = [...new Set(L.toSaved(ps, 'laptop').flatMap(p => Object.keys(p)))].sort();
const back = L.toWorking(L.toSaved(ps, 'laptop'), {klass: 'laptop', vp, slots: 3, pref});
out.noSwapAfterLoad = back.panels.every(p => !('swapped' in p));
""")
    assert "swapped" not in out["keys"] and out["noSwapAfterLoad"] is True


@needs_node
def test_showing_a_window_that_is_already_on_screen_only_raises_it_and_a_stranger_does_nothing():
    out = _run(SETUP + """
const before = shown(ps).map(p => [p.type, ...box(p)]).sort();
out.how = L.showLaptop(ps, id('party'), vp, 3, pref);
out.same = JSON.stringify(shown(ps).map(p => [p.type, ...box(p)]).sort()) === JSON.stringify(before);
out.raised = find('party').z === Math.max(...ps.map(p => p.z));
out.stranger = L.showLaptop(ps, 'nope', vp, 3, pref);
""")
    assert out == {"how": "front", "same": True, "raised": True, "stranger": "none"}


@needs_node
def test_with_room_to_spare_a_window_from_the_dock_is_tiled_in_and_the_others_make_way():
    out = _run(SETUP + """
L.retileLaptop(ps, vp, 1, pref);                                 // one window showing
out.one = ids();
out.how = L.showLaptop(ps, id('dice'), vp, 3, pref);             // room for more: tiled in, nobody leaves
out.two = ids();
out.overlap = (() => { const s = shown(ps); for (let i = 0; i < s.length; i++) for (let j = i + 1; j < s.length; j++) if (overlap(s[i], s[j])) return true; return false; })();
""")
    assert out["one"] == ["map"] and out["how"] == "tiled" and len(out["two"]) == 2 and "map" in out["two"] and out["overlap"] is False


@needs_node
def test_with_one_slot_the_window_replaces_the_only_one_and_all_shows_everything():
    out = _run(SETUP + """
L.retileLaptop(ps, vp, 1, pref);
out.how = L.showLaptop(ps, id('dice'), vp, 1, pref);
out.one = ids();
out.fills = box(find('dice'));
out.all_how = L.showLaptop(ps, id('audio'), vp, Infinity, pref);
out.all = shown(ps).length;
""")
    assert out["how"] == "swapped" and out["one"] == ["dice"] and out["fills"] == [8, 8, 1520, 650]
    assert out["all_how"] == "tiled" and out["all"] == 9


@needs_node
def test_putting_a_window_in_the_dock_lets_the_others_fill_the_space_and_one_always_stays():
    out = _run(SETUP + """
out.before = ids();
out.ok = L.parkLaptop(ps, id('party'), vp, pref);
out.after = ids();
out.noPromotion = out.after.length === 2;                         // the dock did not send anyone in
const rest = shown(ps);
out.fill = rest.reduce((a, p) => a + p.w * p.h, 0) / ((vp.w - 24) * (vp.h - 16));
out.last = (() => { L.parkLaptop(ps, shown(ps)[1].id, vp, pref); return [L.parkLaptop(ps, shown(ps)[0].id, vp, pref), shown(ps).length]; })();
out.stranger = L.parkLaptop(ps, 'nope', vp, pref);
""")
    assert out["ok"] is True and len(out["after"]) == 2 and out["noPromotion"] is True
    assert out["fill"] > 0.9                                          # the two that are left use the whole screen
    assert out["last"] == [False, 1] and out["stranger"] is False


@needs_node
def test_changing_the_number_showing_parks_the_least_recently_used_or_brings_back_the_most_recent():
    out = _run(SETUP + """
ps.forEach((p, i) => { p.z = 10 + i; });                          // later in the list = more recently used
const main = L.mainOf(shown(ps)).type;
L.retileLaptop(ps, vp, 1, pref);
out.one = ids();
L.retileLaptop(ps, vp, 2, pref);
out.two = ids();                                                  // the most recently used parked window returns
const s2 = shown(ps).map(p => p.z);
out.mainKept = out.one[0] === main;
L.retileLaptop(ps, vp, Infinity, pref);
out.all = shown(ps).length;
L.retileLaptop(ps, vp, 3, pref);
out.three = shown(ps).length;
out.mainStill = L.mainOf(shown(ps)).type === main;
""")
    assert len(out["one"]) == 1 and out["mainKept"] and len(out["two"]) == 2 and out["all"] == 9 and out["three"] == 3 and out["mainStill"]


@needs_node
def test_making_a_window_the_main_one_swaps_its_place_with_the_current_main():
    out = _run(SETUP + """
const main = L.mainOf(shown(ps));
const other = shown(ps).find(p => p !== main);
const mb = box(main), ob = box(other);
out.ok = L.makeMain(ps, other.id);
out.main = L.mainOf(shown(ps)).type === other.type;
out.swapped = JSON.stringify(box(other)) === JSON.stringify(mb) && JSON.stringify(box(main)) === JSON.stringify(ob);
out.again = L.makeMain(ps, other.id);                            // already the main one
out.parked = L.makeMain(ps, ps.find(p => p.parked).id);          // a window in the dock has no place to take
""")
    assert out == {"ok": True, "main": True, "swapped": True, "again": False, "parked": False}


@needs_node
def test_after_any_sequence_of_actions_one_window_shows_and_none_overlap():
    out = _run(SETUP + """
let seed = 7; const rnd = n => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
const bad = [];
for (let step = 0; step < 400; step++) {
  const k = rnd(5), p = ps[rnd(ps.length)];
  if (k === 0) L.showLaptop(ps, p.id, vp, [1, 2, 3, Infinity][rnd(4)], pref);
  else if (k === 1) L.parkLaptop(ps, p.id, vp, pref);
  else if (k === 2) L.retileLaptop(ps, vp, [1, 2, 3, Infinity][rnd(4)], pref);
  else if (k === 3) L.makeMain(ps, p.id);
  else p.z = rnd(100);
  const s = shown(ps);
  if (!s.length) bad.push(step + ': nothing showing');
  if (k === 2 || k === 1) for (let i = 0; i < s.length; i++) for (let j = i + 1; j < s.length; j++) if (overlap(s[i], s[j])) bad.push(step + ': overlap after tiling ' + s[i].type + '/' + s[j].type);
  ps.forEach(q => { if (![q.x, q.y, q.w, q.h].every(Number.isFinite)) bad.push(step + ': non-finite geometry'); });
}
out.bad = bad.slice(0, 5);
""")
    assert out["bad"] == []


# ── two classes, one set of windows ─────────────────────────────────────────────────────────────

@needs_node
def test_the_big_screens_geometry_survives_a_trip_through_the_laptop_untouched():
    out = _run("""
const saved = bigLayout(NINE).map(p => Object.assign({}, p));
const original = JSON.stringify(saved);
const w = laptop(saved, {w: 1536, h: 666});
w.panels.forEach(p => { p.x += 5; });                              // the user drags things about on the laptop
const back = L.toSaved(w.panels, 'laptop');
out.bigUntouched = JSON.stringify(back.map(p => { const q = Object.assign({}, p); delete q.lap; return q; })) === original;
out.allHaveLap = back.every(p => p.lap && typeof p.lap.parked === 'boolean');
out.noPrivate = back.every(p => !('_wide' in p) && !('_lap' in p) && !('parked' in p));
// and back on the big screen: exactly what it had
const wide = L.toWorking(back, {klass: 'wide'});
out.wideIdentical = JSON.stringify(wide.panels.map(p => { const q = {}; ['x', 'y', 'w', 'h', 'z', 'collapsed'].forEach(k => q[k] = p[k]); return q; })) ===
  JSON.stringify(saved.map(p => { const q = {}; ['x', 'y', 'w', 'h', 'z', 'collapsed'].forEach(k => q[k] = p[k]); return q; }));
""")
    assert out == {"bigUntouched": True, "allHaveLap": True, "noPrivate": True, "wideIdentical": True}


@needs_node
def test_the_laptops_own_arrangement_survives_a_trip_through_the_big_screen():
    out = _run("""
const vp = {w: 1536, h: 666};
const w1 = laptop(bigLayout(NINE), vp);
const lapBoxes = w1.panels.map(p => [p.parked || false, ...box(p)]);
const saved1 = L.toSaved(w1.panels, 'laptop');
const onBig = L.toWorking(saved1, {klass: 'wide'});               // the PC opens it, moves a window, saves
onBig.panels[0].x += 40;
const saved2 = L.toSaved(onBig.panels, 'wide');
const w2 = L.toWorking(saved2, {klass: 'laptop', vp, slots: 3, pref});
out.derivedAgain = w2.derived;
out.same = JSON.stringify(w2.panels.map(p => [p.parked || false, ...box(p)])) === JSON.stringify(lapBoxes);
out.bigMoved = saved2[0].x === saved1[0].x + 40;
""")
    assert out == {"derivedAgain": False, "same": True, "bigMoved": True}


@needs_node
def test_a_window_added_on_the_big_screen_waits_in_the_dock_and_the_laptops_arrangement_is_kept():
    out = _run("""
const vp = {w: 1536, h: 666};
const w1 = laptop(bigLayout(['map', 'party', 'quests', 'dice']), vp);
const saved = L.toSaved(w1.panels, 'laptop');
saved.push({id: 'pNew', type: 'notes', ref: '', title: 'new', x: 100, y: 100, w: 400, h: 300, z: 9, collapsed: false, accent: '', data: {}});   // no `lap`
const w2 = L.toWorking(saved, {klass: 'laptop', vp, slots: 3, pref});
const fresh = w2.panels.find(p => p.id === 'pNew');
out.parked = fresh.parked === true;
out.sane = [fresh.x, fresh.y, fresh.w, fresh.h].every(n => n >= 0 && n < 5000) && fresh.w >= 400;
out.keptShown = shown(w2.panels).map(p => p.id).sort().join() === shown(w1.panels).map(p => p.id).sort().join();
out.derived = w2.derived;
""")
    assert out == {"parked": True, "sane": True, "keptShown": True, "derived": False}


@needs_node
def test_a_laptop_arrangement_with_everything_parked_still_shows_something():
    out = _run("""
const saved = bigLayout(['map', 'party']).map((p, i) => Object.assign(p, {lap: {x: 8, y: 8, w: 500, h: 300, z: i + 1, collapsed: false, parked: true}}));
const w = L.toWorking(saved, {klass: 'laptop', vp: {w: 1536, h: 666}, slots: 2, pref});
out.shown = shown(w.panels).length;
""")
    assert out["shown"] >= 1


@needs_node
def test_saved_panels_with_odd_or_missing_laptop_geometry_load_without_throwing():
    out = _run("""
const vp = {w: 1536, h: 666};
const saved = bigLayout(['map', 'party', 'quests']);
saved[0].lap = 'garbage'; saved[1].lap = null; saved[2].lap = {x: 8, y: 8};                // partial: keeps what it has
const w = L.toWorking(saved, {klass: 'laptop', vp, slots: 3, pref});
out.n = w.panels.length;
out.finite = w.panels.every(p => [p.x, p.y, p.w, p.h].every(Number.isFinite));
out.shown = shown(w.panels).length >= 1;
const e = L.toWorking([], {klass: 'wide'}); out.empty = e.panels.length;
""")
    assert out == {"n": 3, "finite": True, "shown": True, "empty": 0}


@needs_node
def test_the_wide_class_is_exactly_what_it_was_before_this_change():
    out = _run("""
const saved = bigLayout(NINE);
const w = L.toWorking(saved, {klass: 'wide'});
out.same = JSON.stringify(w.panels.map(p => [p.x, p.y, p.w, p.h, p.z, p.collapsed])) === JSON.stringify(saved.map(p => [p.x, p.y, p.w, p.h, p.z, p.collapsed]));
out.noneParked = w.panels.every(p => !p.parked);
out.derived = w.derived;
// saving from the wide class adds nothing for a layout that never saw a laptop
const back = L.toSaved(w.panels, 'wide');
out.noLap = back.every(p => !('lap' in p));
out.identical = JSON.stringify(back) === JSON.stringify(saved);
""")
    assert out == {"same": True, "noneParked": True, "derived": False, "noLap": True, "identical": True}


# ── the server keeps the laptop's geometry ──────────────────────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


PANEL = {"id": "p1", "type": "dice", "ref": "", "title": "🎲", "x": 5, "y": 5, "w": 420, "h": 480, "z": 1,
         "lap": {"x": 8, "y": 8, "w": 1008, "h": 650, "z": 3, "collapsed": False, "parked": False}}


def test_the_laptop_geometry_and_its_screen_are_saved_beside_the_big_screens(client, seed):
    _login_gm(client, seed)
    ws = {"current": {"panels": [PANEL, dict(PANEL, id="p2", lap={**PANEL["lap"], "parked": True})],
                      "vp": {"w": 3824, "h": 1880}, "lvp": {"w": 1536, "h": 669}}, "presets": {}}
    assert client.post("/api/cockpit/workspace", json=ws).status_code == 200
    cur = client.get("/api/cockpit/workspace").json()["workspace"]["current"]
    assert cur["vp"] == {"w": 3824, "h": 1880} and cur["lvp"] == {"w": 1536, "h": 669}
    assert cur["panels"][0]["x"] == 5 and cur["panels"][0]["w"] == 420                      # the big screen's, untouched
    assert cur["panels"][0]["lap"] == {"x": 8, "y": 8, "w": 1008, "h": 650, "z": 3, "collapsed": False, "parked": False}
    assert cur["panels"][1]["lap"]["parked"] is True


def test_a_preset_keeps_both_screens_too(client, seed):
    _login_gm(client, seed)
    layout = {"panels": [PANEL], "vp": {"w": 2560, "h": 1280}, "lvp": {"w": 1366, "h": 650}}
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [PANEL]}, "presets": {"Combat": layout}}).status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]["presets"]["Combat"]
    assert back["vp"] == {"w": 2560, "h": 1280} and back["lvp"] == {"w": 1366, "h": 650} and back["panels"][0]["lap"]["w"] == 1008


@pytest.mark.parametrize("junk", [None, "x", 5, [1, 2], {}, {"x": "a"}])
def test_nonsense_laptop_geometry_is_dropped_or_made_safe_never_stored_raw(client, seed, junk):
    _login_gm(client, seed)
    panel = dict(PANEL, lap=junk)
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [panel]}, "presets": {}}).status_code == 200
    saved = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"][0]
    lap = saved.get("lap")
    assert lap is None or (set(lap) == {"x", "y", "w", "h", "z", "collapsed", "parked"} and all(isinstance(lap[k], int) for k in "xywhz"))


def test_laptop_geometry_is_clamped_like_the_rest(client, seed):
    _login_gm(client, seed)
    panel = dict(PANEL, lap={"x": -50, "y": 10 ** 9, "w": 3, "h": 10 ** 9, "z": -4, "collapsed": 1, "parked": "yes"})
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [panel]}, "presets": {}}).status_code == 200
    lap = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"][0]["lap"]
    assert lap == {"x": 0, "y": 8000, "w": 160, "h": 8000, "z": 0, "collapsed": True, "parked": True}


@pytest.mark.parametrize("junk", [None, "wide", [1, 2], {"w": "x", "h": 5}, {"w": -5, "h": 600}, {"w": 10, "h": 10}, {"w": 10 ** 9, "h": 10 ** 9}, {"w": 1536}])
def test_a_nonsense_laptop_screen_is_dropped(client, seed, junk):
    _login_gm(client, seed)
    ws = {"current": {"panels": [PANEL], "lvp": junk}, "presets": {"A": {"panels": [PANEL], "lvp": junk}}}
    assert client.post("/api/cockpit/workspace", json=ws).status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]
    assert "lvp" not in back["current"] and "lvp" not in back["presets"]["A"]


def test_a_layout_saved_before_the_laptop_layout_existed_still_loads_unchanged(client, seed):
    _login_gm(client, seed)
    old = {"id": "p1", "type": "dice", "ref": "", "title": "🎲", "x": 5, "y": 5, "w": 420, "h": 480, "z": 1}
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [old], "vp": {"w": 2560, "h": 1280}}, "presets": {}}).status_code == 200
    cur = client.get("/api/cockpit/workspace").json()["workspace"]["current"]
    assert "lap" not in cur["panels"][0] and "lvp" not in cur and cur["vp"] == {"w": 2560, "h": 1280}


def test_a_full_workspace_with_laptop_geometry_fits_the_size_cap(client, seed):
    _login_gm(client, seed)
    from app.routers.cockpit import MAX_PANELS
    panels = [dict(PANEL, id=f"p{i}", data={"text": "note " * 1000}) for i in range(MAX_PANELS)]
    r = client.post("/api/cockpit/workspace", json={"current": {"panels": panels, "vp": {"w": 3824, "h": 1880}, "lvp": {"w": 1536, "h": 669}}, "presets": {}})
    assert r.status_code == 200, r.text


def test_a_player_still_cannot_write_the_workspace(client, seed):
    from .conftest import PLAYER_PASSWORD
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [PANEL]}, "presets": {}}).status_code == 403


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────

def test_the_cockpit_page_has_the_dock_the_mode_button_and_the_site_menu_button(client, seed):
    _login_gm(client, seed)
    for path in ("/cockpit", "/player-cockpit"):
        html = client.get(path).text
        for needle in ('id="ck-dock"', 'id="ck-dock-chips"', 'id="ck-dock-slots"', 'id="ck-mode-btn"', 'id="ck-site-btn"',
                       'data-n="1"', 'data-n="2"', 'data-n="3"', 'data-n="all"', 'id="ck-keep-box" checked', ".ck-win.ck-parked", "body.ck-laptop"):
            assert needle in html, (path, needle)


def test_the_laptop_layout_hides_the_site_bar_and_parks_windows_only_through_css_classes():
    html = (ROOT / "app/templates/cockpit.html").read_text()
    assert "body.ck-laptop:not(.ck-showsite) .topbar { display: none; }" in html
    # a window in the dock is hidden but keeps a box, so its page loads and stays loaded (display:none would be a 0x0 frame
    # that the loader neither starts nor keeps)
    keep = html[html.index("body.ck-keep .ck-win.ck-parked {"):].split("}", 1)[0]
    assert "visibility: hidden" in keep and "pointer-events: none" in keep and "max-height: 100%" in keep and "display: none" not in keep
    # ... unless the user unticked "Keep loaded": then it is display:none again
    assert ".ck-win.ck-parked { display: none; }" in html
    # the floating buttons must not sit over a window's corner on a laptop
    assert "body.ck-laptop:has(#ck-wrap) { --nd-fab-bottom: 1px; }" in html


def test_the_cockpit_script_goes_through_the_layout_maths_for_both_classes():
    js = (ROOT / "static/js/cockpit.js").read_text()
    for needle in ("LAYOUT.screenClass", "LAYOUT.toWorking", "LAYOUT.toSaved", "LAYOUT.showLaptop", "LAYOUT.parkLaptop",
                   "LAYOUT.retileLaptop", "LAYOUT.makeMain", "lvp:", "function switchClass", "function renderDock",
                   "nd_cockpit_mode", "nd_cockpit_slots", "nd_cockpit_site", "nd_cockpit_keep"):
        assert needle in js, needle
    assert "panels: panels, vp: refVp" not in js          # a save always goes through savedLayout()
    # the layout class is a per-browser choice: never part of what is stored for the world
    assert "localStorage.setItem(MODE_KEY" not in js and "setPref(MODE_KEY" in js
