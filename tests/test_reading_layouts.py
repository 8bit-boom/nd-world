"""Phase 5 reading layouts (rules Book / Wide, search flat / cards, facts timeline / table, recap Sections): the registry sections, the pure
helpers in nd-pagelayout.js / rules-book.js / recap-sections.js under Node, and the pages' wiring (the saved choice is the person's own)."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_reading_sections_offer_their_modes_and_reject_others():
    want = {"rules": ["scroll", "book", "wide"], "search_results": ["grouped", "flat", "cards"],
            "facts": ["groups", "timeline", "table"], "recap": ["full", "sections"]}
    for sec, ids in want.items():
        sp = view_prefs.spec(sec)
        assert [m["id"] for m in sp["modes"]] == ids and sp["default"] == ids[0], sec
        assert view_prefs.clean(sec, {"mode": ids[-1]})["mode"] == ids[-1]
        assert view_prefs.clean(sec, {"mode": "nonsense"})["mode"] == ids[0]


def _run(mod, expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/{mod}.js'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_pickmode_prefers_url_then_account_then_browser_then_default():
    ids = '["a","b","c"]'
    assert _run("nd-pagelayout", f"c.pickMode({ids}, 'a', 'c', 'b', 'a')") == "c"
    assert _run("nd-pagelayout", f"c.pickMode({ids}, 'a', 'zz', 'b', 'c')") == "b"
    assert _run("nd-pagelayout", f"c.pickMode({ids}, 'a', '', '', 'c')") == "c"
    assert _run("nd-pagelayout", f"c.pickMode({ids}, 'a', '', 'x', 'y')") == "a"
    assert _run("nd-pagelayout", f"c.pickMode({ids}, 'q', '', '', '')") == "a"


@needs_node
def test_book_keys_swipes_and_labels():
    assert _run("rules-book", "[c.stepKey('ArrowRight', 0, 5), c.stepKey('ArrowRight', 4, 5), c.stepKey('ArrowLeft', 0, 5), c.stepKey('Home', 3, 5), c.stepKey('End', 1, 5), c.stepKey('x', 1, 5), c.stepKey('Home', 0, 0)]") == [1, 4, 0, 0, 4, -1, -1]
    assert _run("rules-book", "[c.swipeStep(-100, 10, 300), c.swipeStep(100, 10, 300), c.swipeStep(-100, 80, 300), c.swipeStep(-30, 0, 300), c.swipeStep(-100, 0, 900)]") == [1, -1, 0, 0, 0]
    assert _run("rules-book", "[c.label('  Hello   world ', 0), c.label('', 2), c.clamp(9, 3), c.clamp(-1, 3)]") == ["Hello world", "Section 3", 2, 0]


@needs_node
def test_recap_groups_start_at_each_heading_and_keep_leading_text():
    got = _run("recap-sections", "c.groupIndexes(['P', 'P', 'H2', 'P', 'UL', 'H3', 'H4', 'P'])")
    assert got == [{"start": 0, "end": 2, "heading": -1}, {"start": 2, "end": 5, "heading": 2}, {"start": 5, "end": 8, "heading": 5}]
    assert _run("recap-sections", "c.groupIndexes([])") == []


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_pages_render_their_bar_and_the_saved_choice_per_person(client, seed):
    _as(client, seed, True)
    for path, sec in (("/rules", "rules"),):
        page = client.get(path).text
        assert f'data-pl="{sec}"' in page and f'data-pl-bar="{sec}"' in page and "nd-pagelayout.js" in page, path
    assert "rules-book.js" in client.get("/rules").text
    assert client.post("/api/view-prefs/rules", json={"mode": "book"}).status_code == 200
    assert 'data-pl-saved="book"' in client.get("/rules").text
    _as(client, seed, False)
    assert 'data-pl-saved="book"' not in client.get("/rules").text


def test_search_and_facts_wire_their_layout_bar_when_there_is_something_to_show():
    for tpl, sec in (("search.html", "search_results"), ("facts/list.html", "facts")):          # the seed has neither results nor facts; check the templates
        src = open(f"app/templates/{tpl}").read()
        assert f'pl_bar("{sec}")' in src and f'pl_attrs("{sec}", request)' in src
