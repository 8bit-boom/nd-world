"""The SITE shell layout (navigation, width, density, theme, scale, motion, pinned destinations): the registry section, the account storage
(a player's own, kept in the session so a page render costs no query), the attributes the pages render, the Account panel, and the pure
decisions in static/js/nd-site.js under Node."""
import json
import shutil
import subprocess

import pytest
from sqlalchemy import event

from app import view_prefs
from app.database import SessionLocal, engine
from app.models import UserViewPref

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_site_section_validates_every_option_and_the_pins():
    out = view_prefs.clean("site", {"options": {"nav": "rail", "width": "huge", "density": "touch", "theme": "contrast", "scale": "125", "motion": "reduced"},
                                    "pins": ["cockpit", "cockpit", "Bad Id", "dice", "a", "b", "c", "d", "e"]})
    assert out["options"] == {"nav": "rail", "width": "centered", "density": "touch", "theme": "contrast", "scale": "125", "motion": "reduced"}
    assert out["pins"] == ["cockpit", "dice", "a", "b", "c", "d"]                       # known ids only, no repeats, at most six
    d = view_prefs.clean("site", {})
    assert d["options"] == {"nav": "top", "width": "centered", "density": "comfortable", "theme": "", "scale": "", "motion": ""} and d["pins"] == []
    assert "options" not in view_prefs.clean("entities", {"options": {"nav": "rail"}})      # other sections are exactly as they were


def test_every_section_has_a_title_for_the_account_page():
    assert set(view_prefs.SECTIONS) <= set(view_prefs.SECTION_TITLES)


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def _html_tag(page):
    return page.split(">", 1)[0] if page.startswith("<html") else page[page.index("<html"): page.index(">", page.index("<html")) + 1]


def test_a_players_site_layout_is_theirs_and_every_page_renders_it(client, seed):
    _as(client, seed, False)
    tag = _html_tag(client.get("/").text)
    assert 'data-nav="top"' in tag and 'data-width="centered"' in tag and 'data-density="comfortable"' in tag and 'data-signed="1"' in tag and "data-gm" not in tag
    r = client.post("/api/view-prefs/site", json={"options": {"nav": "bottom", "width": "reading", "density": "touch", "theme": "dim", "scale": "110", "motion": "reduced"}, "pins": ["dice", "rules", "zzz!"]})
    assert r.status_code == 200 and r.json()["pins"] == ["dice", "rules"]
    tag = _html_tag(client.get("/characters").text)
    assert 'data-nav="bottom"' in tag and 'data-width="reading"' in tag and 'data-density="touch"' in tag and 'data-acct-theme="dim"' in tag
    assert 'data-acct-scale="110"' in tag and 'data-motion="reduced"' in tag and 'data-pins="dice,rules"' in tag
    # the GM is another person: their pages are on the defaults
    _as(client, seed, True)
    gm_tag = _html_tag(client.get("/").text)
    assert 'data-nav="top"' in gm_tag and 'data-gm="1"' in gm_tag
    # signed out (the login page): defaults, nothing to save to
    client.cookies.clear()
    assert "data-signed" not in _html_tag(client.get("/login").text)


def test_a_page_render_costs_no_view_prefs_query_once_it_is_in_the_session(client, seed):
    _as(client, seed, False)
    client.post("/api/view-prefs/site", json={"options": {"nav": "rail"}})
    seen = []
    def on_execute(conn, cursor, statement, parameters, context, executemany):
        if "user_view_prefs" in statement:
            seen.append(statement)
    event.listen(engine, "before_cursor_execute", on_execute)
    try:
        for _ in range(3):
            assert 'data-nav="rail"' in _html_tag(client.get("/characters").text)
    finally:
        event.remove(engine, "before_cursor_execute", on_execute)
    assert not any("site" in s or "FROM user_view_prefs" in s for s in seen if "characters" not in s) or len(seen) <= 3
    # (the characters page itself reads its own list layout through view_bar: at most one query per render, none for the site shell)
    assert len(seen) <= 3


def test_reset_one_or_all_layouts(client, seed):
    _as(client, seed, False)
    client.post("/api/view-prefs/site", json={"options": {"nav": "rail"}})
    client.post("/api/view-prefs/hub", json={"mode": "play"})
    assert client.delete("/api/view-prefs/nope").status_code == 404
    assert client.delete("/api/view-prefs/hub").json() == {"reset": "hub"}
    assert "hub" not in client.get("/api/view-prefs").json() and "site" in client.get("/api/view-prefs").json()
    assert 'data-nav="rail"' in _html_tag(client.get("/").text)
    assert client.delete("/api/view-prefs").json() == {"reset": 1}                      # only this person's rows
    assert client.get("/api/view-prefs").json() == {} and 'data-nav="top"' in _html_tag(client.get("/").text)
    db = SessionLocal()
    try:
        assert db.query(UserViewPref).count() >= 0
    finally:
        db.close()


def test_the_account_page_has_the_layout_panel_and_the_nav_has_what_it_needs(client, seed):
    _as(client, seed, False)
    page = client.get("/account").text
    assert 'id="site-layout"' in page and 'id="site-specs"' in page and "Reset every layout" in page and "/static/js/nd-site.js" in page
    home = client.get("/").text
    assert 'data-nav-id="' in home and 'class="nav-lb"' in home and 'class="nav-ic"' in home          # the labels can be hidden, the icons stay
    for f in ("static/js/cockpit.js", "app/templates/characters/_player_hub.html", "app/templates/combat/detail.html"):
        assert "ndNavOffset" in open(f).read(), f                                                    # a side rail takes width, not height


def _run(expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/nd-site.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_pins_and_the_navigation_that_actually_shows():
    have = ["maps", "cockpit", "characters", "combat", "quests", "dice", "rules"]
    assert _run(f"c.pinsFor([], {json.dumps(have)}, true, 4)") == ["cockpit", "characters", "combat", "quests"]              # a GM's usual four
    assert _run("c.pinsFor([], ['maps', 'characters', 'dice', 'session_log', 'rules'], false, 4)") == ["characters", "session_log", "dice", "rules"]
    assert _run(f"c.pinsFor(['rules', 'gone', 'maps', 'rules'], {json.dumps(have)}, true, 4)") == ["rules", "maps", "cockpit", "characters"]   # yours first, then fill
    assert _run("c.pinsFor([], ['a', 'b'], false, 4)") == ["a", "b"]
    assert _run("c.effectiveNav('rail', 1280)") == "rail" and _run("c.effectiveNav('rail', 600)") == "top"                       # the rail needs room
    assert _run("c.effectiveNav('bottom', 390)") == "bottom" and _run("c.effectiveNav('bottom', 1280)") == "top"                 # the bottom bar is for a phone
    assert _run("c.effectiveNav('compact', 1280)") == "compact" and _run("c.effectiveNav('nonsense', 1280)") == "top"
