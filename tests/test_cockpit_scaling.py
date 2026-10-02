"""Cockpit windows are whole pages (iframes). Opening many must not stampede the server or the browser:
  - a window's page loads only once the window is actually showing, a few at a time (not 48 at once);
  - a window hidden by the layout for a minute (collapsed, an inactive phone tab, the character page's hidden
    Cockpit tab) gives its page back, and loads it again when shown;
  - a window scrolled out of view is NOT unloaded (its state would be lost for nothing);
  - the page refuses a window count the server would refuse to save, and a failed save no longer says "saved".
The loader (static/js/cockpit-frames.js) is plain JS driven here under Node with a fake observer, fake timers and
fake frames; the wiring is checked against the shipped sources."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routers.cockpit import MAX_PANELS

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = r"""
global.window = global;
require(process.env.LOADER);
const timers = []; let now = 0;
function setT(fn, ms) { const t = {fn, at: now + ms, live: true}; timers.push(t); return t; }
function clearT(t) { if (t) t.live = false; }
function advance(ms) { now += ms; timers.filter(t => t.live && t.at <= now).sort((a, b) => a.at - b.at).forEach(t => { t.live = false; t.fn(); }); }
class FakeIO {
  constructor(cb) { this.cb = cb; this.targets = new Set(); FakeIO.last = this; }
  observe(t) { this.targets.add(t); } unobserve(t) { this.targets.delete(t); } disconnect() { this.targets.clear(); }
  set(t, intersecting, hiddenByLayout) {
    const rect = hiddenByLayout ? {width: 0, height: 0} : {width: 500, height: 300};
    this.cb([{target: t, isIntersecting: intersecting, boundingClientRect: rect}]);
  }
}
function frame(name) {
  const ls = {};
  return {name, dataset: {}, src: '',
    addEventListener(e, fn) { (ls[e] = ls[e] || []).push(fn); }, removeEventListener(e, fn) { ls[e] = (ls[e] || []).filter(x => x !== fn); },
    fire(e) { (ls[e] || []).slice().forEach(fn => fn()); }};
}
function make(opts) {
  return ndCreateFrameLoader(Object.assign({concurrency: 3, loadTimeoutMs: 6000, hiddenGraceMs: 60000, setTimeout: setT, clearTimeout: clearT, IntersectionObserver: FakeIO}, opts || {}));
}
const out = {};
"""


def _run(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"LOADER": str(ROOT / "static/js/cockpit-frames.js"), "PATH": __import__("os").environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_visible_windows_load_a_few_at_a_time_in_order():
    out = _run("""
const L = make(); const fs = Array.from({length: 8}, (_, i) => frame('f' + i));
fs.forEach((f, i) => L.mount(f, '/page/' + i));
fs.forEach(f => FakeIO.last.set(f, true, false));
out.first = fs.map(f => f.src);
fs[0].fire('load');
out.afterOneLoad = fs.map(f => f.src).filter(Boolean).length;
fs[1].fire('error');
fs[2].fire('load');
out.afterThree = fs.map(f => f.src).filter(Boolean).length;
out.order = fs.map(f => f.src).filter(Boolean);
out.stats = L.stats();
""")
    assert [s for s in out["first"] if s] == ["/page/0", "/page/1", "/page/2"], "only the first 3 start, in order"
    assert out["afterOneLoad"] == 4 and out["afterThree"] == 6, "each finished load (or error) frees a slot for the next"
    assert out["order"] == [f"/page/{i}" for i in range(6)]
    assert out["stats"]["queued"] == 2 and out["stats"]["loading"] == 3


@needs_node
def test_a_window_that_is_not_showing_does_not_load_until_it_is():
    out = _run("""
const L = make(); const a = frame('a'), b = frame('b');
L.mount(a, '/a'); L.mount(b, '/b');
FakeIO.last.set(a, true, false); FakeIO.last.set(b, false, true);
out.before = [a.src, b.src];
FakeIO.last.set(b, true, false);
out.after = [a.src, b.src];
""")
    assert out["before"] == ["/a", ""] and out["after"] == ["/a", "/b"]


@needs_node
def test_a_page_that_never_answers_does_not_hold_its_slot_forever():
    out = _run("""
const L = make({concurrency: 1}); const a = frame('a'), b = frame('b');
L.mount(a, '/a'); L.mount(b, '/b');
FakeIO.last.set(a, true, false); FakeIO.last.set(b, true, false);
out.t0 = [a.src, b.src];
advance(5900); out.t1 = [a.src, b.src];
advance(200); out.t2 = [a.src, b.src];
""")
    assert out["t0"] == ["/a", ""] and out["t1"] == ["/a", ""] and out["t2"] == ["/a", "/b"]


@needs_node
def test_a_window_hidden_by_the_layout_gives_its_page_back_after_a_minute_and_reloads_when_shown():
    out = _run("""
const L = make(); const a = frame('a');
L.mount(a, '/a'); FakeIO.last.set(a, true, false); a.fire('load');
FakeIO.last.set(a, false, true);          // collapsed / inactive tab: display:none
advance(30000); out.at30 = a.src;
advance(31000); out.at61 = a.src; out.state = L.stats();
FakeIO.last.set(a, true, false); out.shown = a.src;
""")
    assert out["at30"] == "/a", "a quick toggle does not throw the page away"
    assert out["at61"] == "about:blank" and out["state"]["suspended"] == 1
    assert out["shown"] == "/a", "showing it again loads it again"


@needs_node
def test_showing_a_hidden_window_again_in_time_keeps_its_page():
    out = _run("""
const L = make(); const a = frame('a');
L.mount(a, '/a'); FakeIO.last.set(a, true, false); a.fire('load');
FakeIO.last.set(a, false, true); advance(40000);
FakeIO.last.set(a, true, false); advance(100000);
out.src = a.src;
""")
    assert out["src"] == "/a"


@needs_node
def test_a_window_scrolled_out_of_view_is_not_unloaded():
    out = _run("""
const L = make(); const a = frame('a');
L.mount(a, '/a'); FakeIO.last.set(a, true, false); a.fire('load');
FakeIO.last.set(a, false, false);         // off-screen but still laid out (the workspace scrolls)
advance(600000); out.src = a.src;
""")
    assert out["src"] == "/a"


@needs_node
def test_forgetting_a_frame_frees_its_slot_and_its_place_in_the_queue():
    out = _run("""
const L = make({concurrency: 1}); const a = frame('a'), b = frame('b'), c = frame('c');
[a, b, c].forEach((f, i) => { L.mount(f, '/' + 'abc'[i]); FakeIO.last.set(f, true, false); });
L.forget(b); L.forget(a);          // b was queued, a was loading
out.srcs = [a.src, b.src, c.src];
""")
    assert out["srcs"] == ["/a", "", "/c"], "closing the loading window lets the next one start; the queued one never loads"


@needs_node
def test_reload_resets_a_loaded_frame_and_wakes_a_suspended_one():
    out = _run("""
const L = make(); const a = frame('a'), s = frame('s');
L.mount(a, '/a'); FakeIO.last.set(a, true, false); a.fire('load'); a.src = '/a?navigated-inside'; L.reload(a); out.reloaded = a.src;
L.mount(s, '/s'); FakeIO.last.set(s, true, false); s.fire('load');
FakeIO.last.set(s, false, true); advance(61000); out.asleep = s.src;
FakeIO.last.set(s, true, false); s.fire('load');
L.reload(s); out.woke = s.src;
""")
    assert out["reloaded"] == "/a" and out["asleep"] == "about:blank" and out["woke"] == "/s"


@needs_node
def test_without_an_intersection_observer_frames_still_load_staggered():
    out = _run("""
const L = ndCreateFrameLoader({concurrency: 2, setTimeout: setT, clearTimeout: clearT, IntersectionObserver: null});
const fs = [frame('a'), frame('b'), frame('c')];
fs.forEach((f, i) => L.mount(f, '/' + i));
out.srcs = fs.map(f => f.src);
""")
    assert out["srcs"] == ["/0", "/1", ""]


@needs_node
@pytest.mark.parametrize("count, live, max_, low, allow, warn", [
    (24, 3, 24, False, False, False),      # full: refused
    (30, 3, 24, False, False, False),
    (23, 3, 24, False, True, False),
    (5, 11, 24, False, True, True),        # the 12th live page: told once
    (5, 12, 24, False, True, False),       # already told
    (5, 4, 24, True, True, True),          # a phone / low-memory device: the 5th
    (5, 3, 24, True, True, False),
])
def test_the_window_policy(count, live, max_, low, allow, warn):
    out = _run(f"out.p = ndCockpitWindowPolicy({{count: {count}, liveFrames: {live}, max: {max_}, lowMemory: {str(low).lower()}}});")
    assert out["p"]["allow"] is allow and out["p"]["warn"] is warn
    if not allow:
        assert str(max_) in out["p"]["message"]
    if warn:
        assert "memory" in out["p"]["message"].lower()


# ── wiring (source) ──────────────────────────────────────────────────────────

def _cockpit_js():
    return (ROOT / "static/js/cockpit.js").read_text()


def test_the_page_loads_the_loader_before_cockpit_js_and_knows_the_servers_cap(client, seed):
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/cockpit").text
    assert html.index("cockpit-frames.js") < html.index("cockpit.js?")
    assert f"const CK_MAX_PANELS = {MAX_PANELS};" in html


def test_cockpit_js_uses_the_loader_the_policy_and_checks_the_save_response():
    js = _cockpit_js()
    assert "ndCreateFrameLoader" in js and "frames.mount(f, embed(path))" in js, "iframes go through the loader"
    assert "f.loading = 'lazy'" not in js and "f.src = embed(path)" not in js, "no direct src: the loader sets it"
    assert js.count("frame.src = frame.src") == 0 and js.count("frames.reload(") >= 3, "reload goes through the loader"
    assert "ndCockpitWindowPolicy" in js and "CK_MAX_PANELS" in js
    save = js.split("function saveNow()", 1)[1].split("function adopt", 1)[0]
    assert "r.ok" in save and "not saved" in js.lower(), "a rejected save is reported, not shown as saved"
    assert "frames.forget" in js, "closing a window releases its frame"


def test_the_character_hub_hands_its_cockpit_frame_to_the_loader():
    hub = (ROOT / "app/templates/characters/_player_hub.html").read_text()
    assert "cockpit-frames.js" in hub and "ndCreateFrameLoader" in hub
    assert "cockpitFrames.mount(f, f.dataset.src)" in hub and "f.setAttribute('src', f.dataset.src)" not in hub
    assert hub.index("cockpit-frames.js") < hub.index("ndCreateFrameLoader")
