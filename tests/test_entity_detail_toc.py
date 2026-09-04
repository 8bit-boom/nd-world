"""Tests for the entity-detail sidebar TOC + search (app/main.py's `detail`
route, app/templates/entities/detail.html) — the same "navigate by content"
idea as the Rules page (rules.html), reusing its own _rules_toc/
split_rules_sections helpers. Opt-in: the two-column layout only appears
when the entity's body actually has H2/H3 markdown headings, so the vast
majority of entities (characters, items, ...) with short/flat bodies are
completely unaffected.
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
