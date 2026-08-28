"""Tests for the FTS5-with-ILIKE-fallback upgrade to /search's main entity
match (plan item Speed 4.2) — app.main._search_entities. Before this,
/search's entity query was always an unbounded ILIKE %q% scan; now it tries
the entity_fts index first (fast, ranked, and — per app.retrieval.
find_relevant_entities_fts's own docstring — only matches whole-word
prefixes, not an arbitrary mid-word substring), falling back to the
original substring ILIKE scan when FTS can't be used at all: no searchable
word (a single stray character), or FTS5 itself failing. Both paths are
now capped at _SEARCH_RESULT_CAP, where the ILIKE path previously wasn't.
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, login


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_word_prefix_query_matches_via_fts(client, seed):
    _make_entity(seed.world_a.id, name="Ashfall Keep", summary="A ruined fortress.")
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/search", params={"q": "Ash"})
    assert r.status_code == 200
    assert "Ashfall Keep" in r.text


def test_body_only_match_still_found_via_fts(client, seed):
    """The headline reason entity_fts exists at all — ILIKE's own body
    match already worked, but this confirms the FTS path (now primary)
    matches body text too, not just name/summary/tags."""
    _make_entity(seed.world_a.id, name="Old Man Harrow", body="He guards the Undermarket vault.")
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/search", params={"q": "Undermarket"})
    assert r.status_code == 200
    assert "Old Man Harrow" in r.text


def test_single_character_query_falls_back_to_ilike_and_still_works(client, seed):
    """No word reaches the 2-char minimum a real FTS query needs — must
    fall back to the substring path rather than silently finding nothing."""
    _make_entity(seed.world_a.id, name="Zyx", summary="A one-off name fragment.")
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/search", params={"q": "Z"})
    assert r.status_code == 200
    assert "Zyx" in r.text


def test_mid_word_substring_matches_via_ilike_fallback_when_fts_unavailable(client, seed, monkeypatch):
    """The one capability the FTS-primary path genuinely can't offer (see
    _search_entities' own docstring): a query that's a substring in the
    MIDDLE of a word ("shfa" inside "Ashfall"), which FTS5's prefix
    matching can't find but ILIKE's leading-wildcard %q% still can. Proves
    the fallback path (forced here by monkeypatching FTS to fail, the same
    way it degrades on a real FTS5-unavailable SQLite build) preserves
    that capability rather than just being dead code."""
    _make_entity(seed.world_a.id, name="Ashfall Keep", summary="A ruined fortress.")

    import app.retrieval as retrieval_module

    def _broken_fts(db, world_id, words, limit, user=None, kind=None):
        raise Exception("simulated FTS5 failure")
    monkeypatch.setattr(retrieval_module, "find_relevant_entities_fts", _broken_fts)

    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/search", params={"q": "shfa"})
    assert r.status_code == 200
    assert "Ashfall Keep" in r.text


def test_results_capped(client, seed):
    for i in range(30):
        _make_entity(seed.world_a.id, name=f"Ashfall Guard {i}", kind="character")
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/search", params={"q": "Ashfall"})
    assert r.status_code == 200
    assert r.text.count("Ashfall Guard") <= 25


def test_kind_filter_applies_before_the_cap_not_after(client, seed):
    """25 matching characters plus 1 matching location — filtering by
    kind=location must still find the location even though it'd be
    "result #26" if the cap were applied before the kind filter instead of
    in the same query."""
    for i in range(25):
        _make_entity(seed.world_a.id, name=f"Ashfall Guard {i}", kind="character")
    _make_entity(seed.world_a.id, name="Ashfall Outpost", kind="location")

    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/search", params={"q": "Ashfall", "kind": "location"})
    assert r.status_code == 200
    assert "Ashfall Outpost" in r.text
    assert "Ashfall Guard" not in r.text
