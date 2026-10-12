"""The cockpit's keyboard shortcuts (static/js/cockpit-keys.js, pure) and the quick switcher it plugs into
(static/js/command-palette.js), driven under Node; plus the wiring that makes the keys work inside windows.

Driven in Chromium against the running app too: Alt+N / Alt+W / Alt+1..9 / Alt+U / Alt+M / F1 / Ctrl+K pressed while the focus
is INSIDE a window's page (an iframe never lets its keys reach the page around it), on a Russian layout's key codes, and the
palette opening a page / an entity as a window."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _node(script: str, **env_extra):
    env = {"PATH": os.environ["PATH"], "KEYS": str(ROOT / "static/js/cockpit-keys.js"),
           "PALETTE": str(ROOT / "static/js/command-palette.js"), **env_extra}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


KEYS_HARNESS = r"""
global.window = global;
require(process.env.KEYS);
const K = global.ndCockpitKeys;
const ev = (o) => Object.assign({key: '', code: '', ctrl: false, alt: false, meta: false, shift: false, typing: false, mac: false}, o);
const out = {};
"""


def _keys(body: str):
    return _node(KEYS_HARNESS + body + "\nconsole.log(JSON.stringify(out));")


@needs_node
def test_every_documented_shortcut_does_what_the_table_says():
    out = _keys("""
    out.add = K.shortcutFor(ev({code: 'KeyN', alt: true}));
    out.windows = K.shortcutFor(ev({code: 'KeyW', alt: true}));
    out.maximize = K.shortcutFor(ev({code: 'KeyM', alt: true}));
    out.undo = K.shortcutFor(ev({code: 'KeyU', alt: true}));
    out.help = K.shortcutFor(ev({code: 'F1'}));
    out.layouts = K.shortcutFor(ev({code: 'KeyL', alt: true}));
    out.lock = K.shortcutFor(ev({code: 'KeyK', alt: true}));
    out.show = [1, 2, 3, 4, 5, 6, 7, 8, 9].map(n => K.shortcutFor(ev({code: 'Digit' + n, alt: true})));
    """)
    assert out["add"] == {"action": "add"}
    assert out["windows"] == {"action": "windows"}
    assert out["maximize"] == {"action": "maximize"}
    assert out["undo"] == {"action": "undo"}
    assert out["help"] == {"action": "help"}
    assert out["layouts"] == {"action": "layouts"} and out["lock"] == {"action": "lock"}
    assert out["show"] == [{"action": "show", "n": n} for n in range(1, 10)]


@needs_node
def test_the_keys_are_matched_by_the_physical_key_so_other_layouts_work():
    # a Russian layout types other letters on the same keys: Alt+N is still the key that is N on a US keyboard
    out = _keys("""
    out.ru = K.shortcutFor(ev({key: 'т', code: 'KeyN', alt: true}));
    out.hebrew = K.shortcutFor(ev({key: 'מ', code: 'KeyN', alt: true}));
    // the character alone (no physical key) is not enough
    out.char_only = K.shortcutFor(ev({key: 'n', code: 'KeyB', alt: true}));
    """)
    assert out["ru"] == {"action": "add"} and out["hebrew"] == {"action": "add"}
    assert out["char_only"] is None


@needs_node
def test_altgr_and_browser_keys_are_left_alone():
    out = _keys("""
    // Ctrl+Alt is AltGr on many European keyboards: it types characters, so it never triggers a shortcut
    out.altgr = K.shortcutFor(ev({code: 'KeyN', alt: true, ctrl: true}));
    out.altgr_digit = K.shortcutFor(ev({code: 'Digit3', alt: true, ctrl: true}));
    out.alt_shift = K.shortcutFor(ev({code: 'KeyN', alt: true, shift: true}));
    out.meta = K.shortcutFor(ev({code: 'KeyN', alt: true, meta: true}));
    out.plain_n = K.shortcutFor(ev({code: 'KeyN'}));
    out.ctrl_n = K.shortcutFor(ev({code: 'KeyN', ctrl: true}));           // new browser window
    out.ctrl_w = K.shortcutFor(ev({code: 'KeyW', ctrl: true}));           // close the tab
    out.alt_left = K.shortcutFor(ev({code: 'ArrowLeft', alt: true}));     // browser back
    out.numpad = K.shortcutFor(ev({code: 'Numpad1', alt: true}));         // Alt + numpad types a character on Windows
    out.numpad_zero = K.shortcutFor(ev({code: 'Numpad0', alt: true}));    // ... and so does the Alt-code that starts with 0
    """)
    assert all(v is None for v in out.values()), out


@needs_node
def test_option_letters_type_characters_on_a_mac_so_they_wait_while_typing():
    out = _keys("""
    out.mac_typing = K.shortcutFor(ev({code: 'KeyN', alt: true, typing: true, mac: true}));
    out.mac_not_typing = K.shortcutFor(ev({code: 'KeyN', alt: true, typing: false, mac: true}));
    out.win_typing = K.shortcutFor(ev({code: 'KeyN', alt: true, typing: true, mac: false}));
    out.f1_typing_mac = K.shortcutFor(ev({code: 'F1', typing: true, mac: true}));
    """)
    assert out["mac_typing"] is None
    assert out["mac_not_typing"] == {"action": "add"}
    assert out["win_typing"] == {"action": "add"}          # Alt+N types nothing on Windows
    assert out["f1_typing_mac"] == {"action": "help"}


@needs_node
def test_a_question_mark_opens_the_help_but_never_while_typing():
    out = _keys("""
    out.q = K.shortcutFor(ev({key: '?', code: 'Slash', shift: true}));
    out.q_other_layout = K.shortcutFor(ev({key: '?', code: 'Digit7', shift: true}));
    out.q_typing = K.shortcutFor(ev({key: '?', code: 'Slash', shift: true, typing: true}));
    out.q_ctrl = K.shortcutFor(ev({key: '?', code: 'Slash', shift: true, ctrl: true}));
    out.slash = K.shortcutFor(ev({key: '/', code: 'Slash'}));
    """)
    assert out["q"] == {"action": "help"} and out["q_other_layout"] == {"action": "help"}
    assert out["q_typing"] is None and out["q_ctrl"] is None and out["slash"] is None


@needs_node
def test_the_old_undo_close_key_still_works_and_ctrl_k_is_the_palettes_not_ours():
    out = _keys("""
    out.legacy = K.shortcutFor(ev({key: 'T', code: 'KeyT', ctrl: true, shift: true}));
    out.legacy_no_shift = K.shortcutFor(ev({code: 'KeyT', ctrl: true}));
    out.ctrl_k = K.shortcutFor(ev({key: 'k', code: 'KeyK', ctrl: true}));      // command-palette.js handles it
    """)
    assert out["legacy"] == {"action": "undo"}
    assert out["legacy_no_shift"] is None
    assert out["ctrl_k"] is None


@needs_node
def test_the_help_lists_what_the_table_handles_and_leaves_out_the_unlisted():
    out = _keys("""
    out.rows = K.helpRows();
    out.actions = K.SHORTCUTS.filter(s => s.action && !s.external).map(s => s.action);
    out.unlisted = K.SHORTCUTS.filter(s => s.hidden).map(s => s.keys);
    """)
    shown = " | ".join(r["keys"] for r in out["rows"])
    for needle in ("Ctrl + K", "Alt + N", "Alt + W", "Alt + 1 … 9", "Alt + M", "Alt + U", "F1", "Esc"):
        assert needle in shown, needle
    assert all(r["what"] for r in out["rows"]), "every listed key says what it does"
    assert "Ctrl + Shift + T" not in shown and "Ctrl + Shift + T" in out["unlisted"]
    # every action the handler can return is one cockpit.js runs
    js = (ROOT / "static/js/cockpit.js").read_text()
    for action in set(out["actions"]):
        assert f"k.action === '{action}'" in js, action


@needs_node
def test_typing_targets_are_text_fields_and_not_buttons_or_checkboxes():
    out = _keys("""
    const el = (tag, type, ce) => ({tagName: tag, type: type, isContentEditable: !!ce});
    out.textarea = K.isTypingTarget(el('TEXTAREA'));
    out.select = K.isTypingTarget(el('SELECT'));
    out.text = K.isTypingTarget(el('INPUT', 'text'));
    out.search = K.isTypingTarget(el('INPUT', 'search'));
    out.no_type = K.isTypingTarget({tagName: 'INPUT'});
    out.number = K.isTypingTarget(el('INPUT', 'number'));
    out.checkbox = K.isTypingTarget(el('INPUT', 'checkbox'));
    out.button_input = K.isTypingTarget(el('INPUT', 'button'));
    out.range = K.isTypingTarget(el('INPUT', 'range'));
    out.editable = K.isTypingTarget(el('DIV', '', true));
    out.div = K.isTypingTarget(el('DIV'));
    out.button = K.isTypingTarget(el('BUTTON'));
    out.nothing = K.isTypingTarget(null);
    """)
    for yes in ("textarea", "select", "text", "search", "no_type", "number", "editable"):
        assert out[yes] is True, yes
    for no in ("checkbox", "button_input", "range", "div", "button", "nothing"):
        assert out[no] is False, no


# ── the quick switcher ─────────────────────────────────────────────────────────────────────────────────────────────

PALETTE_HARNESS = r"""
global.window = global;
const listeners = {};
global.document = {
  addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
  getElementById: () => null,
  body: { dataset: {} },
  querySelectorAll: () => [],
};
global.location = { pathname: '/', search: '' };
const P = require(process.env.PALETTE);
const key = (o) => { const e = Object.assign({ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, key: '', code: '', defaultPrevented: false}, o);
  e.preventDefault = () => { e.defaultPrevented = true; }; return e; };
const press = (e) => (listeners.keydown || []).forEach(fn => fn(e));
const out = {};
"""


def _palette(body: str):
    return _node(PALETTE_HARNESS + body + "\nconsole.log(JSON.stringify(out));")


@needs_node
def test_the_palette_ranks_the_start_of_a_label_above_a_word_above_the_middle_of_a_word():
    out = _palette("""
    const s = (q, label, extra) => P.score(q, Object.assign({label: label}, extra || {}));
    out.start = s('silt', 'Silt Docks');
    out.word = s('docks', 'Silt Docks');
    out.middle = s('ilt', 'Silt Docks');
    out.sub_only = s('npc', 'Mira', {sub: 'npc'});
    out.fuzzy = s('sdk', 'Silt Docks');
    out.none = s('zzz', 'Silt Docks');
    out.two_words = s('silt dock', 'Silt Docks');
    out.second_word_missing = s('silt xyz', 'Silt Docks');
    out.after_emoji = s('silt', 'Show: 🗺 Silt Docks');
    out.kw = s('combat', '⚔ The Siege', {kw: 'combat window'});
    """)
    assert out["start"] > out["word"] > out["middle"] > out["sub_only"] > out["fuzzy"] > 0
    assert out["none"] == 0 and out["second_word_missing"] == 0
    assert out["two_words"] > out["start"]                    # every word counts
    assert out["after_emoji"] == out["word"]                   # a word after "Show: 🗺 " is a word start
    assert out["kw"] > 0, "keywords find an item whose label does not say it"


@needs_node
def test_a_window_inside_a_frame_hands_ctrl_k_to_the_page_that_has_commands():
    out = _palette("""
    let toggled = 0, asked = 0;
    // this page has no commands of its own; its parent (the cockpit) does
    global.parent = { ndPalette: { toggle: () => { toggled++; }, hasProviders: () => { asked++; return true; } } };
    const e = key({ctrlKey: true, code: 'KeyK', key: 'k'});
    press(e);
    out.forwarded = toggled; out.prevented = e.defaultPrevented;
    // a Russian layout: the character is 'л', the physical key is still K
    toggled = 0;
    press(key({ctrlKey: true, code: 'KeyK', key: 'л'}));
    out.russian = toggled;
    // not Ctrl+K: left alone
    toggled = 0;
    press(key({ctrlKey: true, code: 'KeyJ', key: 'j'}));
    press(key({code: 'KeyK', key: 'k'}));
    press(key({ctrlKey: true, shiftKey: true, code: 'KeyK', key: 'K'}));
    out.others = toggled;
    """)
    assert out["forwarded"] == 1 and out["prevented"] is True
    assert out["russian"] == 1
    assert out["others"] == 0


@needs_node
def test_a_page_with_providers_keeps_ctrl_k_for_itself():
    out = _palette("""
    let toggled = 0;
    global.parent = { ndPalette: { toggle: () => { toggled++; }, hasProviders: () => true } };
    P.addProvider(() => [{label: 'x', run: () => {}}]);
    out.has = P.hasProviders();
    try { press(key({ctrlKey: true, code: 'KeyK', key: 'k'})); } catch (e) { out.local_dom_missing = true; }   // opens its own (no DOM here)
    out.forwarded = toggled;
    """)
    assert out["has"] is True
    assert out["forwarded"] == 0, "the cockpit's own palette must not bounce up to a page around it"


@needs_node
def test_the_palette_walks_up_to_the_nearest_page_with_commands_not_the_top_one():
    out = _palette("""
    const calls = [];
    const top = { ndPalette: { toggle: () => calls.push('top'), hasProviders: () => false } };
    const cockpit = { parent: top, ndPalette: { toggle: () => calls.push('cockpit'), hasProviders: () => true } };
    global.parent = cockpit;                       // we are a window's page inside the cockpit, inside another page
    press(key({ctrlKey: true, code: 'KeyK', key: 'k'}));
    out.calls = calls;
    """)
    assert out["calls"] == ["cockpit"]


@needs_node
def test_a_cross_origin_parent_is_not_an_error():
    out = _palette("""
    const hostile = {}; Object.defineProperty(hostile, 'ndPalette', { get() { throw new Error('SecurityError'); } });
    global.parent = hostile;
    try { press(key({ctrlKey: true, code: 'KeyK', key: 'k'})); } catch (e) { out.local_dom_missing = true; }   // fell back to its own
    out.survived = true;
    """)
    assert out["survived"] is True


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_cockpit_loads_the_key_table_before_its_own_script_and_has_the_help_overlay(client, seed):
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    assert html.index("cockpit-keys.js") < html.index("/static/js/cockpit.js")
    for needle in ('id="ck-help"', 'id="ck-help-body"', 'id="ck-help-close"', 'id="ck-help-btn"', "Keyboard &amp; mouse"):
        assert needle in html, needle


def test_keys_pressed_inside_a_windows_page_reach_the_cockpit():
    js = (ROOT / "static/js/cockpit.js").read_text()
    relay = js.split("function relayFrameEvents(f) {", 1)[1].split("function activateFrameWindow", 1)[0]
    assert "w.addEventListener('keydown'" in relay and "onKey(e)" in relay
    assert "e.stopPropagation()" in relay, "the page itself must not also act on a key the cockpit took"
    # a dialog that is up owns the keys, a held key does not repeat the action, mobile has no shortcuts
    on_key = js.split("function onKey(e) {", 1)[1].split("\n  }\n", 1)[0]
    assert "modalOpen()" in on_key and "e.repeat" in on_key and "MOBILE()" in on_key


def test_the_cockpit_extends_the_quick_switcher_once_it_exists():
    js = (ROOT / "static/js/cockpit.js").read_text()
    # command-palette.js is `defer`red, so it is not defined yet when cockpit.js runs
    assert "if (window.ndPalette) registerPalette(); else document.addEventListener('DOMContentLoaded', registerPalette);" in js
    for needle in ("pal.addProvider(paletteItems)", "pal.setEntityOpener(openEntityWindow)", "pal.setPageOpener(openPageWindow)"):
        assert needle in js, needle
    base = (ROOT / "app/templates/base.html").read_text()
    assert "command-palette.js" in base and "defer" in base.split("command-palette.js", 1)[1].split(">", 1)[0]


def test_the_palette_sits_above_every_cockpit_layer():
    palette = (ROOT / "static/js/command-palette.js").read_text()
    assert "z-index:100020" in palette
    for layer in ("100000", "100001", "100002", "100003", "100010", "100011"):
        assert int(layer) < 100020


def test_escape_closes_one_thing_at_a_time():
    js = (ROOT / "static/js/cockpit.js").read_text()
    esc = js.split("if (e.key === 'Escape') {", 1)[1].split("addSearch.addEventListener", 1)[0]
    assert esc.index("ndPalette.isOpen()") < esc.index("helpOpen()") < esc.index("closeAdd()")
    assert "const addWasOpen" in esc and "!addWasOpen" in esc, "Esc on the Add dialog must not also give a maximised window back"


@needs_node
def test_zoom_keys_work_with_or_without_shift_on_any_layout_and_alt_zero_resets():
    out = _keys("""
    const act = (o) => { const r = K.shortcutFor(ev(o)); return r && r.action; };
    out.in_equal = act({code: 'Equal', alt: true});
    out.in_plus = act({code: 'Equal', alt: true, shift: true, key: '+'});         // the + sign needs Shift on most keyboards
    out.in_numpad = act({code: 'NumpadAdd', alt: true});
    out.out_minus = act({code: 'Minus', alt: true});
    out.out_shift = act({code: 'Minus', alt: true, shift: true});
    out.out_numpad = act({code: 'NumpadSubtract', alt: true});
    out.reset = act({code: 'Digit0', alt: true});
    out.numpad_zero = act({code: 'Numpad0', alt: true});                           // Alt-codes start with it: left alone
    out.ctrl_plus = act({code: 'Equal', ctrl: true});                              // the browser's own page zoom
    out.ctrl_alt_plus = act({code: 'Equal', ctrl: true, alt: true});
    out.mac_typing = act({code: 'Minus', alt: true, typing: true, mac: true});     // Option + - types an en dash on a Mac
    out.win_typing = act({code: 'Minus', alt: true, typing: true});
    """)
    assert (out["in_equal"], out["in_plus"], out["in_numpad"]) == ("zoomin", "zoomin", "zoomin")
    assert (out["out_minus"], out["out_shift"], out["out_numpad"]) == ("zoomout", "zoomout", "zoomout")
    assert out["reset"] == "zoomreset"
    assert out["numpad_zero"] is None and out["ctrl_plus"] is None and out["ctrl_alt_plus"] is None
    assert out["mac_typing"] is None and out["win_typing"] == "zoomout"


@needs_node
def test_the_help_lists_the_zoom_and_lock_keys_once_each():
    out = _keys("""
    out.rows = K.helpRows().map(r => r.keys);
    """)
    rows = out["rows"]
    assert "Alt + K" in rows and "Alt + L" in rows and "Alt + +  /  Alt + −  /  Alt + 0" in rows
    assert len(rows) == len(set(rows)), "no key is listed twice"
