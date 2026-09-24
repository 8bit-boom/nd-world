"""Regression tests for docs/MOBILE_UI_UX_AUDIT.md's "Sidebar-TOC trio"
(entity detail, rules) on mobile: the TOC sidebar becomes a horizontal
block above the content below 768px (.ed-toc-wrap/.rules-toc-wrap's own
max-width:768px rule), capped at 40vh but not collapsible — a GM opening
a long entity/rules page on a phone had to scroll past up to 40vh of TOC
links before reaching the actual content, on every single visit.

Fix: wrap the Contents list in a <details open> element (so desktop/no-JS
mobile keeps today's always-visible sidebar exactly as before) and add a
small script that closes it specifically when the page loads under the
same 768px breakpoint the CSS already uses."""
from app.database import SessionLocal
from app.models import Entity, World

from .conftest import GM_PASSWORD, login

_LONG_BODY = """Intro paragraph before any heading.

## Part One
Some content in part one.

### Sub Point
Nested detail here.

## Part Two
Some content in part two.
"""


def _add_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "note"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_entity_toc_is_a_details_element_open_by_default(client, seed):
    eid = _add_entity(seed.world_a.id, name="Player's Guide", body=_LONG_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert '<details class="toc-details" id="ed-toc-details" open>' in r.text
    assert "<summary>Contents</summary>" in r.text
    # The entries themselves are still rendered exactly as before, just
    # nested one level deeper inside <details> — same anchors, same page.
    assert 'href="#part-one"' in r.text
    assert 'href="#sub-point"' in r.text


def test_entity_toc_collapses_on_mobile_via_matchmedia(client, seed):
    eid = _add_entity(seed.world_a.id, name="Player's Guide", body=_LONG_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    script = r.text.split("getElementById('ed-toc-details')", 1)[1].split("})();", 1)[0]
    assert "matchMedia('(max-width: 768px)')" in script
    assert "d.open = false" in script


def test_rules_toc_is_a_details_element_open_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = "## Part One\n\nintro text\n\n### Sub Point\n\nnested\n\n## Part Two\n\nother\n"
        db.commit()
    finally:
        db.close()
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert '<details class="toc-details" id="rules-toc-details" open>' in r.text
    assert "<summary>Contents</summary>" in r.text
    assert "onclick=\"return rulesTocClick(this)\"" in r.text


def test_rules_toc_collapses_on_mobile_via_matchmedia(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    script = r.text.split("getElementById('rules-toc-details')", 1)[1].split("})();", 1)[0]
    assert "matchMedia('(max-width: 768px)')" in script
    assert "d.open = false" in script
