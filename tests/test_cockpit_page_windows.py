"""The cockpit's "Page" window: any page of the app as a window (opened from the quick switcher or the Add dialog).

A window shows its page in an iframe, and the page's address is part of a saved layout - data that crosses from the server and
from localStorage into an iframe's src. So the address is a path on THIS site and nothing else, checked twice (the server drops
anything else when it saves; the page refuses to build a window from it) with the same rule."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.routers import cockpit as cockpit_router

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

VALID = ["/", "/tables", "/kind/character", "/sessions/12", "/entity/5?tab=notes", "/maps/silt-docks#top", "/x" * 59 + "/"]
INVALID = ["", "tables", "//evil.example/x", "///x", "https://evil.example/", "javascript:alert(1)", "data:text/html,hi",
           "/a b", "/a\tb", "/a\nb", "/x\n", "/a\\b", "/\\evil.example", "/" + "x" * 120, " /tables"]


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _panel(ref, type_="page", i=1):
    return {"id": f"p{i}", "type": type_, "ref": ref, "title": "📄 x", "x": 8, "y": 8, "w": 600, "h": 500, "z": 1}


def _saved(client):
    return client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"]


def test_page_is_a_panel_type_the_server_keeps():
    assert "page" in cockpit_router.PANEL_TYPES


@pytest.mark.parametrize("ref", VALID)
def test_a_path_on_this_site_is_saved(client, seed, ref):
    assert len(ref) <= 120
    _login_gm(client, seed)
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": [_panel(ref)]}, "presets": {}}).status_code == 200
    saved = _saved(client)
    assert len(saved) == 1 and saved[0]["type"] == "page" and saved[0]["ref"] == ref


@pytest.mark.parametrize("ref", INVALID)
def test_anything_that_is_not_a_path_on_this_site_never_becomes_a_window(client, seed, ref):
    _login_gm(client, seed)
    ws = {"current": {"panels": [_panel(ref), _panel("", "dice", 2)]}, "presets": {"A": {"panels": [_panel(ref)]}}}
    assert client.post("/api/cockpit/workspace", json=ws).status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]
    assert [p["type"] for p in back["current"]["panels"]] == ["dice"], "the page window is dropped, its neighbours are not"
    assert back["presets"]["A"]["panels"] == []


def test_a_missing_or_non_string_address_is_dropped(client, seed):
    _login_gm(client, seed)
    panels = [dict(_panel("/x"), ref=None), dict(_panel("/x", i=2), ref=5), dict(_panel("/x", i=3), ref=["/x"]), dict(_panel("/ok", i=4))]
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": panels}, "presets": {}}).status_code == 200
    # None / 5 / a list become "None" / "5" / "['/x']" strings in the server's str() and are rejected as addresses
    assert [p["ref"] for p in _saved(client)] == ["/ok"]


def test_other_window_types_keep_whatever_reference_they_had(client, seed):
    _login_gm(client, seed)
    panels = [_panel("silt-docks", "map"), _panel("42", "entity", 2), _panel("", "dice", 3)]
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": panels}, "presets": {}}).status_code == 200
    assert [(p["type"], p["ref"]) for p in _saved(client)] == [("map", "silt-docks"), ("entity", "42"), ("dice", "")]


def test_the_accent_colour_still_accepts_a_colour_and_nothing_with_a_trailing_newline(client, seed):
    _login_gm(client, seed)
    panels = [dict(_panel("", "dice", 1), accent="#00f0ff"), dict(_panel("", "dice", 2), accent="#00f0ff\n"), dict(_panel("", "dice", 3), accent="red")]
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": panels}, "presets": {}}).status_code == 200
    assert [p["accent"] for p in _saved(client)] == ["#00f0ff", "", ""]


# ── the page checks the same rule before it builds a window ────────────────────────────────────────────────────

@needs_node
def test_the_page_and_the_server_agree_on_what_an_address_is():
    js = (ROOT / "static/js/cockpit.js").read_text()
    m = re.search(r"function validPagePath\(ref\) \{.*?\}\n", js)
    assert m, "validPagePath is missing from cockpit.js"
    cands = VALID + INVALID
    script = m.group(0) + "\nconsole.log(JSON.stringify(" + json.dumps(cands) + ".map(validPagePath)));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 0, r.stderr
    client_says = json.loads(r.stdout)
    server_says = [bool(cockpit_router._PAGE_PATH_RE.match(c)) for c in cands]
    assert client_says == server_says
    assert all(server_says[: len(VALID)]) and not any(server_says[len(VALID):])


def test_a_page_window_is_only_built_from_a_valid_address():
    js = (ROOT / "static/js/cockpit.js").read_text()
    # the address of every window's page is ONE mapping (framePath); a page window gets one only when its ref is a path on this site ...
    path = js.split("function framePath(p) {", 1)[1].split("function fillBody", 1)[0]
    assert "case 'page': return validPagePath(p.ref) ? p.ref : null;" in path
    # ... and fillBody builds an iframe only from what framePath returns, otherwise says so
    body = js.split("function fillBody(p, body) {", 1)[1].split("} else if (p.type === 'combat') {", 1)[0]
    assert "const fp = framePath(p);" in body and "mkIframe(fp)" in body
    assert "This window has no valid page address." in body


def test_the_add_dialog_lists_the_sites_pages_and_takes_an_address(client, seed):
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    assert "page:    { icon: '📄', label: 'Page'" in html and "picker: 'page'" in html
    js = (ROOT / "static/js/cockpit.js").read_text()
    for needle in ("function pageRows()", "function pageHref(href)", "this address", "key === 'page'"):
        assert needle in js, needle
    # the menu's links carry ?w=slug and a window adds its own: the saved address must not
    assert "replace(/([?&])w=[^&#]*&?/, '$1')" in js
