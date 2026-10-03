"""Clicking an image inside a cockpit window opens it full screen in the MAIN window, where the GM can send it to the
second monitor or to the players.

Every cockpit (and floated) window is a whole page in an iframe, and an iframe's lightbox is confined to that little
window — and the GM's controls (second screen, send to players) are deliberately not loaded in iframes. So an embedded
page's openLightbox() hands the image up to the window above it, which shows it in the real lightbox. The hand-over
(static/js/nd-lightbox-bridge.js) is plain JS driven here under Node with a fake window and document."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = r"""
function makeWindow(opts) {
  const listeners = {}, docListeners = {};
  const opened = [], posted = [];
  const w = {
    location: {origin: 'https://nd.example', href: 'https://nd.example/gallery?embed=1'},
    opened, posted, listeners, docListeners,
    openLightbox(src, alt) { opened.push([src, alt]); },
    addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    focus() { w.focused = true; },
  };
  w.parent = opts.parent === 'self' ? w : (opts.parent || null);
  if (opts.parent === 'self') w.parent = w;
  w.document = {
    body: {classList: {contains: c => c === 'nd-embed' && !!opts.embed}},
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
  };
  w.URL = URL;
  return w;
}
function load(w) {
  global.window = w; global.document = w.document; global.URL = URL; global.Date = Date;
  delete require.cache[require.resolve(process.env.BRIDGE)];
  require(process.env.BRIDGE);
}
function img(attrs, ancestors) {
  const a = attrs || {};
  return {tagName: 'IMG', alt: a.alt || '', getAttribute: k => a[k] == null ? null : a[k], hasAttribute: k => a[k] != null,
    currentSrc: a.src || '', src: a.src || '',
    // Element.closest() starts at the element itself, then walks up
    closest: sel => {
      const wanted = sel.split(',').map(s => s.trim());
      if (wanted.includes('[onclick]') && a.onclick != null) return {};
      return (ancestors || []).find(x => wanted.includes(x)) ? {} : null;
    }};
}
function click(w, target, extra) { (w.docListeners.click || []).forEach(fn => fn(Object.assign({target, defaultPrevented: false}, extra || {}))); }
function message(w, data, origin) { (w.listeners.message || []).forEach(fn => fn({data, origin: origin || w.location.origin})); }
const out = {};
"""


def _run(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"BRIDGE": str(ROOT / "static/js/nd-lightbox-bridge.js"), "PATH": __import__("os").environ["PATH"]}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── inside a window (an embedded page) ─────────────────────────────────────────────────────────

@needs_node
def test_an_embedded_page_hands_its_image_to_the_window_above_instead_of_opening_it_in_the_small_frame():
    out = _run("""
const w = makeWindow({embed: true});
w.parent = {postMessage: (m, o) => w.posted.push([m, o])};
load(w);
w.openLightbox('/uploads/dragon.png', 'Dragon');
out.opened = w.opened; out.posted = w.posted;
""")
    assert out["opened"] == [], "the little frame must not open its own lightbox"
    (msg, origin), = out["posted"]
    assert msg == {"type": "nd-lightbox", "src": "https://nd.example/uploads/dragon.png", "alt": "Dragon"}
    assert origin == "https://nd.example", "only the same site may receive it"


@needs_node
def test_without_a_window_above_the_page_still_opens_its_own_lightbox():
    out = _run("""
const w = makeWindow({embed: true, parent: 'self'});     // opened on its own: the parent is the window itself
load(w);
w.openLightbox('/uploads/dragon.png', 'Dragon');
out.opened = w.opened;
""")
    assert out["opened"] == [["/uploads/dragon.png", "Dragon"]]


@needs_node
def test_a_normal_page_keeps_its_own_lightbox():
    out = _run("""
const w = makeWindow({embed: false, parent: 'self'});
load(w);
w.openLightbox('/uploads/dragon.png', 'Dragon');
out.opened = w.opened; out.clickHandlers = (w.docListeners.click || []).length;
""")
    assert out["opened"] == [["/uploads/dragon.png", "Dragon"]] and out["clickHandlers"] == 0


@needs_node
def test_a_failing_hand_over_falls_back_to_the_local_lightbox():
    out = _run("""
const w = makeWindow({embed: true, parent: {postMessage() { throw new Error('detached'); }}});
load(w);
w.openLightbox('/uploads/dragon.png', 'Dragon');
out.opened = w.opened;
""")
    assert out["opened"] == [["/uploads/dragon.png", "Dragon"]]


@needs_node
def test_the_same_image_opened_twice_in_a_blink_is_handed_over_once():
    """A page that opens the lightbox itself AND is caught by the data-full fallback must not pop it up twice."""
    out = _run("""
const posted = [];
const w = makeWindow({embed: true, parent: {postMessage: (m, o) => posted.push(m)}});
load(w);
w.openLightbox('/uploads/a.png', 'A'); w.openLightbox('/uploads/a.png', 'A'); w.openLightbox('/uploads/b.png', 'B');
out.srcs = posted.map(m => m.src);
""")
    assert out["srcs"] == ["https://nd.example/uploads/a.png", "https://nd.example/uploads/b.png"]


@needs_node
def test_a_thumbnail_with_a_full_size_but_no_handler_opens_full_screen_on_click():
    out = _run("""
const posted = [];
const w = makeWindow({embed: true, parent: {postMessage: (m, o) => posted.push(m)}});
load(w);
click(w, img({'data-full': '/uploads/full.png', src: '/uploads/thumb.png', alt: 'Dragon'}));
out.srcs = posted.map(m => [m.src, m.alt]);
""")
    assert out["srcs"] == [["https://nd.example/uploads/full.png", "Dragon"]]


@needs_node
def test_clicks_that_mean_something_else_are_left_alone():
    out = _run("""
const posted = [];
const w = makeWindow({embed: true, parent: {postMessage: (m, o) => posted.push(m)}});
load(w);
const full = {'data-full': '/uploads/full.png', src: '/uploads/t.png'};
click(w, img({src: '/uploads/plain.png'}));                                  // no full size: not ours
click(w, img(full, ['a[href]']));                                            // inside a link: it navigates
click(w, img(full, ['button']));                                             // inside a button: it is a control
click(w, img(Object.assign({onclick: 'pick()'}, full)), {});                 // has its own handler
click(w, img(full, ['[onclick]']));                                          // inside something with a handler (a picker)
click(w, img(full, ['[data-nd-lightbox="off"]']));                           // opted out
click(w, img(full), {defaultPrevented: true});                               // someone already handled the click
click(w, {tagName: 'DIV', closest: () => null});                             // not an image at all
out.posted = posted.length;
""")
    assert out["posted"] == 0


# ── the window above (the main page) ───────────────────────────────────────────────────────────

@needs_node
def test_the_main_window_opens_what_a_window_hands_up_and_takes_focus_for_escape():
    out = _run("""
const w = makeWindow({embed: false, parent: 'self'});
load(w);
message(w, {type: 'nd-lightbox', src: 'https://nd.example/uploads/dragon.png', alt: 'Dragon'});
out.opened = w.opened; out.focused = !!w.focused;
""")
    assert out["opened"] == [["https://nd.example/uploads/dragon.png", "Dragon"]] and out["focused"]


@needs_node
def test_the_main_window_ignores_anything_that_is_not_from_its_own_site_or_not_an_image_address():
    out = _run("""
const w = makeWindow({embed: false, parent: 'self'});
load(w);
message(w, {type: 'nd-lightbox', src: '/uploads/a.png', alt: 'x'}, 'https://evil.example');       // another site
message(w, {type: 'nd-lightbox', src: 'javascript:alert(1)', alt: 'x'});
message(w, {type: 'nd-lightbox', src: 'data:text/html,<script>1</script>', alt: 'x'});
message(w, {type: 'nd-lightbox', src: '//evil.example/a.png', alt: 'x'});
message(w, {type: 'nd-lightbox', src: 42, alt: 'x'});
message(w, {type: 'something-else', src: '/uploads/a.png'});
message(w, 'nd-lightbox'); message(w, null);
out.opened = w.opened.length;
message(w, {type: 'nd-lightbox', src: '/uploads/ok.png', alt: 'y'.repeat(500)});
out.alt = w.opened[0][1].length;
""")
    assert out["opened"] == 0 and out["alt"] <= 200


@needs_node
def test_a_window_inside_a_window_passes_it_up_again():
    """The character page's Cockpit tab is a cockpit inside an iframe, whose windows are iframes: three levels."""
    out = _run("""
const hub = [];
const mid = makeWindow({embed: true, parent: {postMessage: (m, o) => hub.push(m)}});
load(mid);
message(mid, {type: 'nd-lightbox', src: 'https://nd.example/uploads/a.png', alt: 'A'});     // from a window of the cockpit
out.localOpened = mid.opened.length; out.passedUp = hub.map(m => m.src);
""")
    assert out["localOpened"] == 0 and out["passedUp"] == ["https://nd.example/uploads/a.png"]


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────

def test_every_page_loads_the_bridge_right_after_the_lightbox(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/", "/cockpit", "/?embed=1"):
        html = client.get(path).text
        assert "/static/js/nd-lightbox-bridge.js" in html, path
        assert html.index("function openLightbox") < html.index("nd-lightbox-bridge.js"), path


def test_players_get_it_too(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert "/static/js/nd-lightbox-bridge.js" in client.get("/player-cockpit").text


def test_the_cockpits_own_cards_open_their_images_full_screen():
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert ".ck-card img" in js and "openLightbox" in js


# ── "send to players" in the lightbox ──────────────────────────────────────────────────────────

STAGE = (ROOT / "static/js/nd-stage.js").read_text()


def test_the_lightbox_has_both_send_buttons_in_one_bar():
    assert 'id: "nd-lightbox-actions"' in STAGE.replace("'", '"') or "nd-lightbox-actions" in STAGE
    for ident in ("nd-stage-lightbtn", "nd-spot-lightbtn", "Send to players", "Send to screen"):
        assert ident in STAGE, ident
    assert "/images/spotlight" in STAGE and "/images/spotlight/clear" in STAGE and "/api/spotlight" in STAGE


def test_sending_to_players_does_not_pop_the_image_up_again_on_the_gms_own_screen():
    assert "ndSpotlightSeen" in STAGE


def test_sending_and_clearing_the_spotlight_say_which_version_they_made(client, seed):
    from .test_gallery import _make_album
    _make_album(seed.world_a.id, "Album", urls=["/uploads/scene.png"])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    sent = client.post("/images/spotlight", json={"url": "/uploads/scene.png"}).json()
    assert sent["ok"] is True and isinstance(sent["version"], int) and sent["version"] >= 1
    cleared = client.post("/images/spotlight/clear").json()
    assert cleared["ok"] is True and cleared["version"] == sent["version"] + 1
    assert client.get("/api/spotlight").json()["version"] == cleared["version"]


def test_a_picture_from_outside_the_world_is_refused_with_a_404_the_button_can_explain(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/images/spotlight", json={"url": "/uploads/not-ours.png"}).status_code == 404
