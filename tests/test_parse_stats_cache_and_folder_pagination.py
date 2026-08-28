"""Tests for plan item Speed 4.1: an LRU cache around app.rendering.parse_stats
keyed by (entity.id, entity.updated_at) so /kind/{kind} folder table views
don't re-run the regex parser over every entity's body on every request, plus
pagination of the leaf-folder table itself so a huge folder doesn't ship one
unbounded <table> to the browser."""
import datetime

from app.database import SessionLocal
from app.deps import PAGE_SIZE
from app.models import Entity
from app.rendering import clear_parse_stats_cache, parse_stats_cached

from .conftest import GM_PASSWORD, login


def setup_function(_):
    clear_parse_stats_cache()


# ── parse_stats_cached ──────────────────────────────────────────────────────

def test_cache_hit_returns_the_same_object_without_reparsing(monkeypatch):
    calls = []
    import app.rendering as rendering_module
    real_parse_stats = rendering_module.parse_stats

    def _counting_parse_stats(body):
        calls.append(body)
        return real_parse_stats(body)

    monkeypatch.setattr(rendering_module, "parse_stats", _counting_parse_stats)

    ts = datetime.datetime(2026, 1, 1, 12, 0, 0)
    body = "## Attributes\n* **HP**: 10\n"
    first = parse_stats_cached(1, ts, body)
    second = parse_stats_cached(1, ts, body)

    assert len(calls) == 1  # second call was served from cache, not reparsed
    assert first is second
    assert first == [{"key": "HP", "val": "10", "special": False}]


def test_different_updated_at_is_a_cache_miss(monkeypatch):
    calls = []
    import app.rendering as rendering_module
    real_parse_stats = rendering_module.parse_stats

    def _counting_parse_stats(body):
        calls.append(body)
        return real_parse_stats(body)

    monkeypatch.setattr(rendering_module, "parse_stats", _counting_parse_stats)

    body = "## Attributes\n* **HP**: 10\n"
    parse_stats_cached(1, datetime.datetime(2026, 1, 1), body)
    parse_stats_cached(1, datetime.datetime(2026, 1, 2), body)  # entity was edited

    assert len(calls) == 2


def test_clear_cache_forces_a_fresh_parse(monkeypatch):
    calls = []
    import app.rendering as rendering_module
    real_parse_stats = rendering_module.parse_stats
    monkeypatch.setattr(rendering_module, "parse_stats", lambda b: (calls.append(b), real_parse_stats(b))[1])

    ts = datetime.datetime(2026, 1, 1)
    parse_stats_cached(1, ts, "## Attributes\n* **HP**: 10\n")
    clear_parse_stats_cache()
    parse_stats_cached(1, ts, "## Attributes\n* **HP**: 10\n")

    assert len(calls) == 2


# ── /kind/{kind} leaf-folder table pagination ───────────────────────────────

def _make_entities(world_id, n, folder):
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=world_id, kind="item", name=f"Item {i:03d}", folder=folder)
                    for i in range(n)])
        db.commit()
    finally:
        db.close()


def test_folder_under_page_size_ships_everything_with_no_pager(client, seed):
    _make_entities(seed.world_a.id, 3, "Loot")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/kind/item?folder=Loot")
    assert r.status_code == 200
    assert r.text.count('class="row-cb"') == 3
    assert "pagination" not in r.text


def test_folder_over_page_size_paginates_and_page_param_advances(client, seed):
    total = PAGE_SIZE + 10
    _make_entities(seed.world_a.id, total, "Loot")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r1 = client.get("/kind/item?folder=Loot")
    assert r1.status_code == 200
    assert r1.text.count('class="row-cb"') == PAGE_SIZE
    assert "Page 1 of 2" in r1.text

    r2 = client.get("/kind/item?folder=Loot&page=2")
    assert r2.status_code == 200
    assert r2.text.count('class="row-cb"') == total - PAGE_SIZE
    assert "Page 2 of 2" in r2.text

    # No entity appears on both pages.
    import re
    ids_page1 = set(re.findall(r'row-cb" data-id="(\d+)"', r1.text))
    ids_page2 = set(re.findall(r'row-cb" data-id="(\d+)"', r2.text))
    assert ids_page1.isdisjoint(ids_page2)
    assert len(ids_page1) + len(ids_page2) == total


def test_out_of_range_page_clamps_to_the_last_page(client, seed):
    _make_entities(seed.world_a.id, PAGE_SIZE + 10, "Loot")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/kind/item?folder=Loot&page=999")
    assert r.status_code == 200
    assert "Page 2 of 2" in r.text


def test_grid_view_of_a_leaf_folder_is_not_paginated(client, seed):
    # Speed 4.1 only paginates the table view — a folder viewed as a card
    # grid still ships every entity (thumbnails already lazy-load).
    total = PAGE_SIZE + 5
    _make_entities(seed.world_a.id, total, "Loot")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/kind/item?folder=Loot&view=grid")
    assert r.status_code == 200
    assert r.text.count('class="row-cb card-cb"') == total
