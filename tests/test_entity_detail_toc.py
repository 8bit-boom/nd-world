"""Tests for the entity-detail sidebar TOC + search (app/main.py's `detail`
route, app/templates/entities/detail.html) — the same "navigate by content"
idea as the Rules page (rules.html), reusing its own _rules_toc/
split_rules_sections helpers with levels="123"/include_h1=True (Rules
itself stays H2/H3-only — see _rules_toc's own comment on why: real
GM-authored long-form documents commonly use H1 for their top-level
chapters, e.g. "# Part I — ..."). Opt-in: the two-column layout only
appears when the entity's body actually has headings, so the vast majority
of entities (characters, items, ...) with short/flat bodies are completely
unaffected.
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


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


_LONG_BODY = """Intro paragraph before any heading.

## Part One
Some content in part one.

### Sub Point
Nested detail here.

## Part Two
Some content in part two.
"""


def test_toc_and_sidebar_appear_for_body_with_headings(client, seed):
    eid = _add_entity(seed.world_a.id, name="Player's Guide", body=_LONG_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'class="ed-toc-wrap"' in r.text
    assert 'id="ed-search"' in r.text
    assert ">Part One<" in r.text
    assert ">Part Two<" in r.text
    assert ">Sub Point<" in r.text


def test_headings_get_ids_and_toc_links_match(client, seed):
    eid = _add_entity(seed.world_a.id, name="Player's Guide", body=_LONG_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert 'id="part-one"' in r.text
    assert 'href="#part-one"' in r.text
    assert 'id="part-two"' in r.text
    assert 'href="#part-two"' in r.text


def test_body_split_into_sections_for_search(client, seed):
    eid = _add_entity(seed.world_a.id, name="Player's Guide", body=_LONG_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert 'class="ed-section" data-section-id="part-one"' in r.text
    assert 'class="ed-section" data-section-id="part-two"' in r.text


def test_no_sidebar_for_short_flat_body(client, seed):
    eid = _add_entity(seed.world_a.id, name="A Dagger", kind="item",
                       body="A simple dagger. Nothing fancy.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'class="ed-toc-wrap"' not in r.text
    assert 'id="ed-search"' not in r.text
    # The body still renders — just without the TOC wrapper.
    assert "A simple dagger" in r.text


def test_no_sidebar_when_entity_has_no_body(client, seed):
    eid = _add_entity(seed.world_a.id, name="Empty NPC", kind="character", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'class="ed-toc-wrap"' not in r.text


def test_player_sees_toc_for_visible_note(client, seed):
    eid = _add_entity(seed.world_a.id, name="Player's Guide", body=_LONG_BODY, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'class="ed-toc-wrap"' in r.text
    assert ">Part One<" in r.text


def test_stat_block_and_toc_coexist_for_item_with_headings(client, seed):
    """item/feat entities render an extra stat block (app.rendering.parse_stats,
    which scans the raw markdown for a "## Attributes" bullet section — see
    that function's own docstring — unrelated to the TOC pipeline) above the
    body. Make sure adding the TOC sidebar didn't disturb that independent
    code path, and that its own "## Attributes" heading also becomes a
    normal TOC entry alongside "## Lore"."""
    body = "## Attributes\n* **STR**: 12\n* **Weight**: 3\n\n## Lore\nForged in the old district.\n"
    eid = _add_entity(seed.world_a.id, name="Ancient Blade", kind="item", body=body, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "stat-block" in r.text
    assert "STR" in r.text and "12" in r.text
    assert 'class="ed-toc-wrap"' in r.text
    assert ">Lore<" in r.text


# ── H1-chapter documents (real GM-authored player's-guide style content) ──
# A GM-uploaded reference document (the motivating case for this whole
# feature) commonly structures itself with H1 for its top-level chapters —
# "# Part I — ..." — and H2/H3 underneath, unlike Rules' own convention
# (its markdown never uses a bare # heading, since that's the page's own
# <h1> title). These entries lock in that H1 is a real split point here.

_CHAPTER_BODY = """# The Long Guide

Intro before any chapter.

# Part I — You Are a Hunter

You play a Hunter.

## The Hunter's Burden

Three truths.

# Part II — The Sky Above the Hunt

The Moon matters.
"""


def test_h1_chapters_become_top_level_toc_entries(client, seed):
    eid = _add_entity(seed.world_a.id, name="The Long Guide", body=_CHAPTER_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert 'class="ed-toc-wrap"' in r.text
    assert '>Part I — You Are a Hunter<' in r.text
    assert '>Part II — The Sky Above the Hunt<' in r.text
    # The H2 nested under Part I is still a separate TOC entry too.
    assert ">The Hunter's Burden<" in r.text


def test_h1_chapters_get_own_toc_class_and_split_into_sections(client, seed):
    eid = _add_entity(seed.world_a.id, name="The Long Guide", body=_CHAPTER_BODY, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/entity/{eid}")
    assert 'class="toc-entry toc-h1"' in r.text
    assert 'class="ed-section" data-section-id="part-i-you-are-a-hunter"' in r.text
    assert 'class="ed-section" data-section-id="part-ii-the-sky-above-the-hunt"' in r.text
