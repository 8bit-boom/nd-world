"""The Sheet tab is split into pages (a strip under the header) so nobody scrolls
through fourteen sections to find Stamina. Server side this is pure grouping and
markup: sheet_systems.sheet_pages() decides which sections share a page, and the
templates tag every block with data-sheet-page. The one rule that matters: a
section is NEVER dropped — whatever the template holds lands on some page.
"""
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from markupsafe import escape

from app import database as d
from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate
from app.sheet_systems import BUILTIN_SYSTEMS, sheet_pages

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _sections(fields):
    out = []
    for f in fields:
        if f["section"] not in out:
            out.append(f["section"])
    return out


def _tpl(slug, builtin=True):
    return SimpleNamespace(slug=slug, is_builtin=builtin)


# ── the grouping rule ────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug,fields", [("hunt-in-the-moonlight", d._HITM_FIELDS), ("asterion", d._ASTERION_FIELDS)])
def test_builtin_systems_cover_every_section_exactly_once(slug, fields):
    names = _sections(fields)
    sp = sheet_pages(_tpl(slug), names)
    assert sp and len(sp["pages"]) >= 3
    assert set(sp["section_page"]) == set(names), "a section fell off every page"
    ids = [p["id"] for p in sp["pages"]]
    assert len(ids) == len(set(ids))
    assert set(sp["section_page"].values()) <= set(ids)
    assert "more" not in ids, "the shipped sections should all be placed on purpose"
    assert sp["conditions"] in ids and sp["linked"] in ids


def test_sections_the_gm_added_land_on_a_more_page():
    names = _sections(d._HITM_FIELDS) + ["House Rules"]
    sp = sheet_pages(_tpl("hunt-in-the-moonlight"), names)
    assert sp["section_page"]["House Rules"] == "more"
    assert sp["pages"][-1]["id"] == "more"


def test_sections_the_gm_removed_do_not_leave_empty_pages():
    names = [n for n in _sections(d._HITM_FIELDS) if n not in ("Moon Calendar", "Session Record", "Notes")]
    sp = sheet_pages(_tpl("hunt-in-the-moonlight"), names)
    assert all(any(pid == p["id"] for pid in sp["section_page"].values()) for p in sp["pages"])
    assert "log" not in [p["id"] for p in sp["pages"]]


def test_a_custom_template_gets_a_page_per_section_once_it_is_long_enough():
    names = ["Basics", "Combat", "Gear", "Lore"]
    sp = sheet_pages(_tpl("my-own", builtin=False), names)
    assert [p["label"] for p in sp["pages"]] == names
    assert sp["conditions"] == sp["pages"][0]["id"] and sp["linked"] == sp["pages"][-1]["id"]
    assert len({sp["section_page"][n] for n in names}) == 4


def test_short_templates_stay_a_single_scroll():
    assert sheet_pages(_tpl("my-own", builtin=False), ["Basics", "Combat", "Gear"]) is None
    assert sheet_pages(_tpl("nd-default"), ["Trackers"]) is None
    assert sheet_pages(None, []) is None


def test_every_builtin_page_lists_only_known_sections():
    for slug, fields in (("hunt-in-the-moonlight", d._HITM_FIELDS), ("asterion", d._ASTERION_FIELDS)):
        names = set(_sections(fields))
        for page in BUILTIN_SYSTEMS[slug]["pages"]:
            assert set(page["sections"]) <= names, (slug, page["id"], set(page["sections"]) - names)


# ── the rendered pages ───────────────────────────────────────────────────────

class _PageOf(HTMLParser):
    """For every element with the given class, the data-sheet-page of its nearest tagged ancestor."""
    VOID = {"input", "br", "img", "meta", "link", "hr"}

    def __init__(self, css_class):
        super().__init__()
        self.css_class, self.stack, self.found = css_class, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.css_class in (a.get("class") or "").split():
            self.found.append(next((pg for _, pg in reversed(self.stack) if pg), None))
        if tag not in self.VOID:
            self.stack.append((tag, a.get("data-sheet-page")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


class _Tagged(HTMLParser):
    """Collects every element carrying data-sheet-page and the nav's chips."""

    def __init__(self):
        super().__init__()
        self.blocks, self.chips = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-sheet-page" in a:
            self.blocks.append((tag, a["data-sheet-page"], "page-on" in (a.get("class") or "").split()))
        if "data-sp" in a:
            self.chips.append(a["data-sp"])


def _parse(html):
    p = _Tagged()
    p.feed(html)
    return p


def _pc(seed, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Anders", **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _tpl_id(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _as_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_hunt_sheet_renders_one_page_per_group_with_the_first_showing(client, seed):
    pc = _pc(seed, sheet_template_id=_tpl_id("hunt-in-the-moonlight"), custom_fields_json="{}")
    _as_player(client, seed)
    html = client.get(f"/characters/{pc}").text
    parsed = _parse(html)
    assert parsed.chips[-1] == "all" and len(parsed.chips) >= 4
    page_ids = parsed.chips[:-1]
    sections = [b for b in parsed.blocks if b[0] == "div"]
    assert {b[1] for b in sections} <= set(page_ids)
    assert len([b for b in parsed.blocks if "cx-section" in html and b[1] in page_ids]) >= len(_sections(d._HITM_FIELDS))
    shown = {b[1] for b in parsed.blocks if b[2]}
    assert shown == {page_ids[0]}, "only the first page is visible before the script runs"
    for name in _sections(d._HITM_FIELDS):
        assert str(escape(name)) in html, f"{name} vanished from the sheet"


def test_conditions_and_save_survive_the_paging(client, seed):
    pc = _pc(seed, sheet_template_id=_tpl_id("hunt-in-the-moonlight"), custom_fields_json="{}")
    _as_player(client, seed)
    html = client.get(f"/characters/{pc}").text
    p = _PageOf("cond-chip")
    p.feed(html)
    assert p.found and set(p.found) == {"status"}, f"every condition chip belongs on the Status page: {set(p.found)}"
    s = _PageOf("btn-primary")
    s.feed(html)
    assert None in s.found, "the Save button must stay outside every page"


def test_native_sheet_pages(client, seed):
    pc = _pc(seed, backstory="A tale.")
    _as_player(client, seed)
    html = client.get(f"/characters/{pc}").text
    parsed = _parse(html)
    assert parsed.chips == ["overview", "gear", "story", "dice", "all"]
    by_page = {}
    for tag, pid, on in parsed.blocks:
        by_page.setdefault(pid, []).append(on)
    assert set(by_page) == {"overview", "gear", "story", "dice"}
    visible = {pid for _, pid, on in parsed.blocks if on}
    assert visible == {"overview"}, f"only Overview is visible before the script runs, got {visible}"
    # the vital controls live on the first page
    ov = html.index('data-sheet-page="overview"')
    assert ov < html.index('id="hp-current"') < html.index('data-sheet-page="gear"')
    assert html.index('data-sheet-page="gear"') < html.index('id="feats-list"') < html.index('data-sheet-page="story"')
    assert html.index('data-sheet-page="dice"') < html.index('id="dice-log"')


def test_native_sheet_without_story_has_no_story_chip(client, seed):
    pc = _pc(seed)
    _as_player(client, seed)
    parsed = _parse(client.get(f"/characters/{pc}").text)
    assert "story" not in parsed.chips and parsed.chips == ["overview", "gear", "dice", "all"]


def test_new_character_form_pages_too(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/new?template_id={_tpl_id('asterion')}")
    assert r.status_code == 200
    parsed = _parse(r.text)
    assert parsed.chips[-1] == "all" and len(parsed.chips) >= 4


def test_script_and_styles_are_wired(client, seed):
    pc = _pc(seed)
    _as_player(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert "/static/js/sheet-pages.js" in html
    css = open("static/style.css").read()
    for needle in (".sheet-paged:not(.show-all) [data-sheet-page]:not(.page-on)", ".sheet-pages button[aria-selected=\"true\"]", "@media print"):
        assert needle in css, needle
    js = open("static/js/sheet-pages.js").read()
    for needle in ("ndSheetPage:", "addEventListener('beforeprint'", "addEventListener('afterprint'", "'ArrowRight'", "show-all"):
        assert needle in js, needle
