"""The last layouts of docs/SITE_LAYOUTS_PLAN.md: rules Search first, recap Cards, board Outline / Timeline, calendar agenda Eras timeline, encounter
list and background-jobs list layouts, GM session page Tabs / Run mode, map Mini-map. Registry, the pure helpers under Node, and page wiring."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")

WANT = {"rules": ["scroll", "book", "wide", "searchfirst"], "recap": ["full", "sections", "cards"], "board_view": ["canvas", "outline", "timeline"],
        "calendar_agenda": ["table", "timeline"], "combat_list": ["grid", "table", "grouped"], "jobs_list": ["cards", "compact", "table"],
        "session_gm": ["long", "tabs", "run"], "map_viewer": ["mapfull", "panel", "minimap"]}


def test_every_new_mode_is_registered_and_the_first_stays_the_default():
    for sec, ids in WANT.items():
        sp = view_prefs.spec(sec)
        assert [m["id"] for m in sp["modes"]] == ids and sp["default"] == ids[0], sec
        assert view_prefs.clean(sec, {"mode": ids[-1]})["mode"] == ids[-1] and view_prefs.clean(sec, {"mode": "zz"})["mode"] == ids[0]


def _run(mod, expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/{mod}.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_outline_follows_connections_from_the_best_connected_node():
    nodes = json.dumps([{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}, {"id": "z"}])
    edges = json.dumps([{"from": "a", "to": "b"}, {"from": "a", "to": "c"}, {"from": "c", "to": "d"}, {"from": "a", "to": "ghost"}, {"from": "b", "to": "b"}])
    got = _run("board-layouts", f"c.outlineTree({nodes}, {edges})")
    assert [(o["id"], o["depth"]) for o in got] == [("a", 0), ("c", 1), ("d", 2), ("b", 1), ("z", 0)]
    assert got[1]["via"] == "a" and got[0]["via"] is None
    assert _run("board-layouts", "c.outlineTree([], [])") == []


@needs_node
def test_timeline_orders_dated_events_then_undated_by_canvas_position():
    assert _run("board-layouts", "[c.dateKey('Fire 2026-03-02'), c.dateKey('Flood 1888'), c.dateKey('no date'), c.dateKey('room 12')]") == [20260302, 18880000, None, None]
    nodes = json.dumps([{"id": 1, "type": "event", "title": "Later 2026-05-01", "x": 0}, {"id": 2, "type": "event", "title": "Undated right", "x": 900},
                        {"id": 3, "type": "event", "title": "Early 1888", "x": 5}, {"id": 4, "type": "event", "title": "Undated left", "x": 10},
                        {"id": 5, "type": "clue", "title": "Not an event 1900"}])
    assert [o["id"] for o in _run("board-layouts", f"c.timelineOrder({nodes})")] == [3, 1, 4, 2]


@needs_node
def test_agenda_year_headings_and_session_tabs_helpers():
    assert _run("agenda-eras", "c.yearStarts(['Year 1', 'Year 1', 'Year 2', 'Year 1', 'Year 1'])") == [0, 2, 3]
    assert _run("agenda-eras", "c.yearStarts([])") == []
    assert _run("session-gm-layouts", "c.tabsFor(['recap', 'facts', 'loot'])") == [{"id": "recap", "label": "Recap"}, {"id": "facts", "label": "Facts"}, {"id": "prep", "label": "Prep & rewards"}]
    assert _run("session-gm-layouts", "c.tabsFor([])") == []
    assert _run("session-gm-layouts", "[c.blocksOf('prep'), c.blocksOf('nope')]") == [["prep", "loot", "xp", "combats"], []]
    assert _run("session-gm-layouts", "[c.pickTab([{id:'a'},{id:'b'}], 'b'), c.pickTab([{id:'a'},{id:'b'}], 'x'), c.pickTab([], 'a')]") == ["b", "a", ""]


@pytest.mark.parametrize("tpl,sec", [("board.html", "board_view"), ("calendar/agenda.html", "calendar_agenda"), ("combat/list.html", "combat_list"),
                                     ("background_jobs.html", "jobs_list"), ("sessions/detail.html", "session_gm"), ("map_viewer.html", "map_viewer")])
def test_each_page_has_its_layout_bar_and_element(tpl, sec):
    src = open(f"app/templates/{tpl}").read()
    assert f'pl_bar("{sec}")' in src and f'pl_attrs("{sec}", request)' in src


def test_session_page_tags_every_block_and_the_rules_mode_has_a_bar():
    src = open("app/templates/sessions/detail.html").read()
    for name in ("recap", "transcript", "facts", "prep", "loot", "xp", "combats"):
        assert f'data-sp="{name}"' in src, name
    assert "searchfirst" in open("static/js/rules-book.js").read() + open("static/style.css").read()
