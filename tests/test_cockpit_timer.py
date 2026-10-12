"""The cockpit's timer window: its wall-clock state machine (static/js/cockpit-timer.js, pure) under Node, what the server keeps of it,
and the wiring around it (presets never carry a running timer, the alarm is visible as well as audible).

A timer that counted ticks lost time whenever the tab was in the background (browsers throttle hidden timers to once a second, then
once a minute), the laptop slept, or the page reloaded. This one stores an END TIME, so every one of those shows the true time."""
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
require(process.env.TIMER);
const T = global.ndCockpitTimer;
const MIN = 60000, T0 = 1700000000000;
const fresh = (o) => T.normalize(Object.assign({}, o || {}));
const out = {};
"""


def _timer(body: str):
    script = HARNESS + body + "\nconsole.log(JSON.stringify(out));"
    env = {"PATH": os.environ["PATH"], "TIMER": str(ROOT / "static/js/cockpit-timer.js")}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_a_countdown_shows_the_true_time_left_however_long_nobody_looked():
    out = _timer("""
    const t = fresh({minutes: 5});
    out.started = T.toggle(t, T0);
    out.after_2 = T.left(t, T0 + 2 * MIN);
    // a background tab throttled to one tick a minute, or a laptop that slept for an hour:
    out.expired_at_5 = T.expired(t, T0 + 5 * MIN);
    out.expired_before = T.expired(t, T0 + 5 * MIN - 1);
    out.left_after_hour = T.left(t, T0 + 60 * MIN);
    out.wake_late = T.wake(t, T0 + 60 * MIN);
    out.after = {running: t.running, done: t.done, remainMs: t.remainMs};
    """)
    assert out["started"] == "started" and out["after_2"] == 3 * 60000
    assert out["expired_at_5"] is True and out["expired_before"] is False
    assert out["left_after_hour"] == 0 and out["wake_late"] == "expired"
    assert out["after"] == {"running": False, "done": True, "remainMs": 0}


@needs_node
def test_pause_keeps_what_was_left_and_resume_counts_from_the_moment_it_resumes():
    out = _timer("""
    const t = fresh({minutes: 10});
    T.toggle(t, T0);
    out.paused = T.toggle(t, T0 + 4 * MIN);               // 6 minutes left
    out.left_paused = T.left(t, T0 + 3 * 3600000);         // however long it sits paused
    out.label_paused = T.startLabel(t);
    out.resumed = T.toggle(t, T0 + 3 * 3600000);
    out.left_resumed = T.left(t, T0 + 3 * 3600000 + 2 * MIN);
    out.label_running = T.startLabel(t);
    """)
    assert out["paused"] == "paused" and out["left_paused"] == 6 * 60000
    assert out["label_paused"] == "Resume" and out["label_running"] == "Pause"
    assert out["resumed"] == "started" and out["left_resumed"] == 4 * 60000


@needs_node
def test_a_stopwatch_adds_up_across_pauses():
    out = _timer("""
    const t = fresh({mode: 'up'});
    T.toggle(t, T0);
    out.at_90s = T.elapsed(t, T0 + 90000);
    T.toggle(t, T0 + 90000);                                // pause at 1:30
    out.paused = T.elapsed(t, T0 + 10 * MIN);
    T.toggle(t, T0 + 10 * MIN);                             // resume
    out.after_resume = T.elapsed(t, T0 + 10 * MIN + 30000);
    out.never_expires = T.expired(t, T0 + 1e9);
    """)
    assert out["at_90s"] == 90000 and out["paused"] == 90000
    assert out["after_resume"] == 120000 and out["never_expires"] is False


@needs_node
def test_the_clock_shows_whole_seconds_rounded_so_zero_means_over():
    out = _timer("""
    out.countdown = [T.fmt(59200, false), T.fmt(1, false), T.fmt(0, false), T.fmt(61000, false), T.fmt(3600000, false), T.fmt(3 * 3600000 + 5 * MIN + 7000, false), T.fmt(-5, false)];
    out.stopwatch = [T.fmt(59999, true), T.fmt(0, true), T.fmt(61999, true), T.fmt(3600000, true)];
    """)
    assert out["countdown"] == ["01:00", "00:01", "00:00", "01:01", "1:00:00", "3:05:07", "00:00"]
    assert out["stopwatch"] == ["00:59", "00:00", "01:01", "1:00:00"]


@needs_node
def test_plus_and_minus_a_minute_move_the_end_time_and_never_below_a_second():
    out = _timer("""
    const run = fresh({minutes: 5}); T.toggle(run, T0);
    T.adjust(run, MIN, T0 + 1000);
    out.running_plus = T.left(run, T0 + 1000);
    T.adjust(run, -10 * MIN, T0 + 1000);                     // far more than is left
    out.running_floor = T.left(run, T0 + 1000);
    const idle = fresh({minutes: 5});
    T.adjust(idle, MIN, T0); out.idle_plus = idle.remainMs;
    T.adjust(idle, -100 * MIN, T0); out.idle_floor = idle.remainMs;
    T.adjust(idle, 10000 * MIN, T0); out.idle_cap = idle.remainMs;
    const watch = fresh({mode: 'up'}); T.adjust(watch, MIN, T0); out.watch = watch.remainMs === 5 * MIN && watch.elapsedMs === 0;
    const over = fresh({minutes: 2}); T.toggle(over, T0); T.expire(over); T.adjust(over, MIN, T0 + 3 * MIN);
    out.over = {done: over.done, left: over.remainMs};
    """)
    assert out["running_plus"] == 6 * 60000 - 1000
    assert out["running_floor"] == 1000
    assert out["idle_plus"] == 6 * 60000 and out["idle_floor"] == 1000 and out["idle_cap"] == 600 * 60000
    assert out["watch"] is True
    assert out["over"] == {"done": False, "left": 3 * 60000}, "a finished timer starts over from its length, then takes the minute"


@needs_node
def test_a_saved_timer_that_ran_out_while_nobody_looked_comes_back_done_without_a_surprise_state():
    out = _timer("""
    const t = fresh({minutes: 5, mode: 'down', running: true, endAt: T0 + 5 * MIN, startAt: 0, remainMs: 5 * MIN});
    out.still = T.wake(JSON.parse(JSON.stringify(t)), T0 + 2 * MIN);
    const late = JSON.parse(JSON.stringify(t));
    out.late = T.wake(late, T0 + 9 * MIN);
    out.late_state = {running: late.running, done: late.done, remainMs: late.remainMs};
    out.again = T.wake(late, T0 + 10 * MIN);                 // already over
    const idle = fresh({});
    out.idle = T.wake(idle, T0);
    """)
    assert out["still"] == "running" and out["late"] == "expired"
    assert out["late_state"] == {"running": False, "done": True, "remainMs": 0}
    assert out["again"] == "done" and out["idle"] == "idle"


@needs_node
def test_whatever_was_saved_or_tampered_with_becomes_a_valid_timer():
    out = _timer("""
    out.empty = T.normalize(undefined);
    out.junk = T.normalize({mode: 'sideways', minutes: 'abc', running: 'yes', done: 0, endAt: 'x', startAt: -9, remainMs: NaN, elapsedMs: Infinity});
    out.huge = T.normalize({minutes: 10 ** 9, endAt: 10 ** 18, remainMs: 10 ** 12, elapsedMs: 10 ** 15});
    out.negative = T.normalize({minutes: -4, remainMs: -1, elapsedMs: -50});
    out.string_numbers = T.normalize({minutes: '12', remainMs: '60000'});
    out.array = T.normalize([1, 2]);
    """)
    assert out["empty"] == {"mode": "down", "minutes": 5, "running": False, "done": False, "endAt": 0, "startAt": 0, "remainMs": 300000, "elapsedMs": 0}
    junk = out["junk"]
    assert junk["mode"] == "down" and junk["minutes"] == 5 and junk["running"] is True and junk["done"] is False
    assert junk["endAt"] == 0 and junk["startAt"] == 0 and junk["remainMs"] == 300000 and junk["elapsedMs"] == 0
    huge = out["huge"]
    assert huge["minutes"] == 600 and huge["endAt"] == 4102444800000 and huge["remainMs"] == 600 * 60000 and huge["elapsedMs"] == 100 * 3600000
    neg = out["negative"]
    assert neg["minutes"] == 1 and neg["remainMs"] == 0 and neg["elapsedMs"] == 0
    assert out["string_numbers"]["minutes"] == 12 and out["string_numbers"]["remainMs"] == 60000
    assert out["array"]["minutes"] == 5


@needs_node
def test_a_tampered_running_timer_with_no_end_time_ends_at_once_instead_of_running_forever():
    out = _timer("""
    const t = T.normalize({mode: 'down', running: true, endAt: 0});
    out.wake = T.wake(t, T0);
    out.done = t.done;
    """)
    assert out["wake"] == "expired" and out["done"] is True


@needs_node
def test_reset_and_a_change_of_mode_go_back_to_the_full_length_and_the_start_label_follows_the_state():
    out = _timer("""
    const t = fresh({minutes: 7});
    out.fresh = T.startLabel(t);
    T.toggle(t, T0); T.toggle(t, T0 + MIN);
    out.touched = T.startLabel(t);
    T.reset(t);
    out.after_reset = [T.startLabel(t), t.remainMs, t.elapsedMs, t.running, t.done];
    T.toggle(t, T0); T.expire(t);
    out.done_label = T.startLabel(t);
    T.setMode(t, 'up');
    out.after_mode = [t.mode, t.running, t.done, t.minutes, T.startLabel(t)];
    T.toggle(t, T0); T.toggle(t, T0 + 5000);
    out.watch_touched = T.startLabel(t);
    T.setMinutes(t, 9999); out.minutes_cap = t.minutes;
    T.setMinutes(t, 0); out.minutes_floor = t.minutes;
    """)
    assert out["fresh"] == "Start" and out["touched"] == "Resume"
    assert out["after_reset"] == ["Start", 7 * 60000, 0, False, False]
    assert out["done_label"] == "Again"
    assert out["after_mode"] == ["up", False, False, 7, "Start"]
    assert out["watch_touched"] == "Resume"
    assert out["minutes_cap"] == 600 and out["minutes_floor"] == 1


# ── the server keeps a timer's state, and only on a timer ─────────────────────────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _panel(type_, data, i=1):
    return {"id": f"p{i}", "type": type_, "ref": "", "title": "x", "x": 8, "y": 8, "w": 340, "h": 260, "z": 1, "data": data}


def test_a_timers_state_is_saved_with_the_layout_and_clamped(client, seed):
    _login_gm(client, seed)
    state = {"mode": "up", "minutes": 7, "running": True, "done": False, "endAt": 1700000300000, "startAt": 1700000000000,
             "remainMs": 420000, "elapsedMs": 1500, "junk": "dropped"}
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [_panel("timer", {"timer": state, "text": "t", "other": 1})]}, "presets": {}}).status_code == 200
    saved = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"][0]["data"]
    assert saved["text"] == "t" and "other" not in saved
    assert saved["timer"] == {k: v for k, v in state.items() if k != "junk"}


def test_nonsense_timer_state_is_made_safe_never_stored_raw_and_never_a_500(client, seed):
    _login_gm(client, seed)
    junk = {"mode": "x", "minutes": 10 ** 9, "running": "yes", "endAt": -5, "startAt": 10 ** 30, "remainMs": "abc", "elapsedMs": [1]}
    # a JSON Infinity / NaN is a float that is not an int: it must not blow up the sanitiser
    body = '{"current": {"panels": [{"id": "p1", "type": "timer", "data": {"timer": {"endAt": Infinity, "remainMs": NaN, "minutes": -Infinity}}}]}, "presets": {}}'
    r = client.post("/api/cockpit/workspace", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [_panel("timer", {"timer": junk})]}, "presets": {}}).status_code == 200
    t = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"][0]["data"]["timer"]
    assert t == {"mode": "down", "minutes": 600, "running": True, "done": False, "endAt": 0, "startAt": 4102444800000,
                 "remainMs": 600 * 60000, "elapsedMs": 0}


def test_only_a_timer_window_keeps_timer_state(client, seed):
    _login_gm(client, seed)
    panels = [_panel("notes", {"text": "n", "timer": {"running": True}}, 1), _panel("dice", {"timer": {"running": True}}, 2)]
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": panels}, "presets": {}}).status_code == 200
    saved = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"]
    assert saved[0]["data"] == {"text": "n"} and saved[1]["data"] == {"text": ""}


# ── wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_timer_script_loads_before_the_cockpit_and_the_timer_window_is_built_on_it(client, seed):
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    assert html.index("cockpit-timer.js") < html.index("/static/js/cockpit.js")
    js = (ROOT / "static/js/cockpit.js").read_text()
    body = js.split("function buildTimer(p, body) {", 1)[1].split("// ── AI find", 1)[0]
    for needle in ("TIMER.normalize(p.data.timer)", "TIMER.toggle(t, Date.now())", "TIMER.expired(t, Date.now())", "TIMER.wake(t, Date.now())"):
        assert needle in body, needle
    # the old tick counter is gone: no `remain -= 1`, no elapsed += 1
    assert "remain -= 1" not in body and "elapsed += 1" not in body
    # a rebuilt window stops the previous incarnation's interval before starting its own
    assert body.index("panelCleanups.get(p.id)") < body.index("document.createElement('div')")


def test_a_saved_layout_never_carries_a_running_timer():
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "function stripTimers(layout)" in js
    assert "stripTimers(clone(savedLayout()))" in js, "saving a layout"
    assert "stripTimers(clone(pr)).panels" in js, "applying a saved layout"


def test_the_alarm_is_visible_as_well_as_audible(client, seed):
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "function setAlarmUi(p, on)" in js and "'ck-alarm'" in js and "document.title = (alarms.size" in js
    assert "' alarm' : ''" in js, "the dock chip of a window that has gone off pulses too"
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    for needle in (".ck-win.ck-alarm", ".ck-chip.alarm", "@keyframes ck-alarm", "container-type: size", "cqw"):
        assert needle in html, needle
    # alarms is declared with the other per-panel state, before anything that draws the dock can run
    assert js.index("const alarms = new Set()") < js.index("function renderDock()")


def test_a_short_timer_window_keeps_the_time_then_the_controls_and_drops_the_presets_first(client, seed):
    import re
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    base = html.index(".ck-timer-row { display: flex;")
    drops = {m.group(2): (int(m.group(1)), m.start()) for m in re.finditer(r"@container \(max-height: (\d+)px\) \{ (\.ck-timer-presets|\.ck-timer-row) \{ display: none; \} \}", html)}
    assert set(drops) == {".ck-timer-presets", ".ck-timer-row"}, drops
    # the presets (a convenience) go while there is still room for the controls, and the controls go only when there is no room at all
    assert drops[".ck-timer-presets"][0] > drops[".ck-timer-row"][0] > 0
    # both are the same specificity as the rule that lays the rows out, so they only win when they come after it
    assert all(at > base for _, at in drops.values())
    # the time stays and is itself a big button: it starts and pauses the timer, which is all that is left in a very short window
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "if (silenced) { silenced = false; return; } start();" in js and "cursor: pointer; user-select: none;" in html
    # a touch on a FLASHING timer silences it (pointerdown resets it); the click that follows on the same time must not start it again
    assert "silenced = t.done; if (t.done) dismiss();" in js


def test_pages_in_windows_are_compact_and_the_dice_window_keeps_its_buttons_in_view(client, seed):
    css = (ROOT / "static/style.css").read_text()
    block = css.split("Pages inside a window (body.nd-embed", 1)[1].split("/* ── Phone layout", 1)[0]
    for rule in ("body.nd-embed .detail-meta h1 { font-size: 1.25rem", "body.nd-embed .list-header h1", "body.nd-embed .dice-head { display: none !important; }",
                 "body.nd-embed #dice-tray {", "body.nd-embed .dice-page { padding"):
        assert rule in block, rule
    # the dice page's own inline styles would out-rank these rules: every override of one needs !important
    for line in block.splitlines():
        if line.startswith("body.nd-embed .dice-") or line.startswith("body.nd-embed #dice-tray"):
            assert "!important" in line, line
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/dice?embed=1").text
    for hook in ('class="dice-page"', 'class="dice-head"', 'id="dice-form" class="dice-card"', 'id="dice-tray"'):
        assert hook in html, hook
