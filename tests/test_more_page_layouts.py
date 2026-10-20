"""Phase 6 page layouts (maps list, map viewer side list, boards, parties, party page dashboard, calendar Week, AI chat "Chat only"): the
registry sections, the Week helper in calendar-week.js under Node, and each page's wiring to nd-pagelayout.js."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs

from .conftest import GM_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")

WANT = {"maps": ["grid", "list", "large"], "map_viewer": ["mapfull", "panel", "minimap"], "boards": ["grid", "list"], "parties": ["grid", "table"],
        "party_page": ["classic", "dashboard"], "calendar_view": ["month", "week"], "ai_chat": ["withside", "focus"]}


def test_the_sections_offer_their_modes_and_today_stays_the_default():
    for sec, ids in WANT.items():
        sp = view_prefs.spec(sec)
        assert [m["id"] for m in sp["modes"]] == ids and sp["default"] == ids[0], sec
        assert view_prefs.clean(sec, {"mode": ids[1]})["mode"] == ids[1] and view_prefs.clean(sec, {"mode": "x"})["mode"] == ids[0]
        assert sec in view_prefs.SECTION_TITLES


def _run(expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/calendar-week.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_week_view_opens_on_todays_row_else_the_first_real_day():
    assert _run("[c.rowCount(33, 7), c.rowCount(35, 7), c.rowCount(0, 7), c.rowCount(5, 0)]") == [5, 5, 0, 0]
    assert _run("[c.startRow(16, 3, 7), c.startRow(-1, 3, 7), c.startRow(-1, -1, 7), c.startRow(9, 0, 0)]") == [2, 0, 0, 0]
    assert _run("[c.inRow(13, 1, 7), c.inRow(14, 1, 7)]") == [True, False]


@pytest.mark.parametrize("tpl,sec", [("maps.html", "maps"), ("map_viewer.html", "map_viewer"), ("boards.html", "boards"), ("parties/list.html", "parties"),
                                     ("parties/detail.html", "party_page"), ("calendar/month.html", "calendar_view"), ("ai_chat.html", "ai_chat")])
def test_each_page_has_its_bar_and_layout_element(tpl, sec):
    src = open(f"app/templates/{tpl}").read()
    assert f'pl_bar("{sec}")' in src and f'pl_attrs("{sec}", request)' in src


def test_maps_and_calendar_render_the_saved_choice(client, seed):
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/view-prefs/calendar_view", json={"mode": "week"}).status_code == 200
    page = client.get("/calendar").text
    if 'id="cal-grid"' in page:                                                    # a world with a calendar configured
        assert 'data-pl-saved="week"' in page and "calendar-week.js" in page
    assert client.post("/api/view-prefs/maps", json={"mode": "large"}).status_code == 200
    page = client.get("/maps").text
    assert 'data-pl="maps"' in page and 'data-pl-saved="large"' in page
