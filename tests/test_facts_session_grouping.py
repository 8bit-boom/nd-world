"""Tests for the Facts page's session-grouped layout (app/routers/facts.py's
_fact_groups + facts/list.html) — replaces the old single reverse-
chronological list that mixed every session together, making a world with
more than a handful of facts "a pain to deal with" (every row repeating the
same session label with no way to tell where one session's facts ended and
the next began).
"""
from app.database import SessionLocal
from app.models import Fact, GameSession

from .conftest import GM_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _make_session(world_id, session_num, title):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title=title, session_num=session_num)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _make_fact(world_id, content, game_session_id=None):
    db = SessionLocal()
    try:
        f = Fact(world_id=world_id, content=content, game_session_id=game_session_id, visible_to_players=True)
        db.add(f)
        db.commit()
        db.refresh(f)
        return f.id
    finally:
        db.close()


def _header_marker(label):
    """The exact markup facts/list.html wraps a group's label in — distinct
    from the plain "#N Title" text, which also appears once per session in
    each of the page's several unrelated <select> dropdowns (the recap/
    tale/quick-add/per-fact-edit session pickers), so a bare substring
    search would false-positive on those instead of testing the group
    header itself."""
    return f"<span>{label}</span>"


def test_facts_grouped_under_their_own_session_header(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "The Opening")
    s2 = _make_session(seed.world_a.id, 2, "The Chapel in the Mire")
    _make_fact(seed.world_a.id, "Fact from session one", s1)
    _make_fact(seed.world_a.id, "Fact from session two", s2)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    # Each session's group header renders exactly once — not repeated once
    # per fact card the way the old layout's per-fact session badge did.
    assert html.count(_header_marker("#1 The Opening")) == 1
    assert html.count(_header_marker("#2 The Chapel in the Mire")) == 1


def test_most_recent_session_listed_first_and_open(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    s2 = _make_session(seed.world_a.id, 5, "Fifth")
    _make_fact(seed.world_a.id, "old news", s1)
    _make_fact(seed.world_a.id, "new news", s2)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    # #5's group header appears before #1's in document order.
    assert html.index(_header_marker("#5 Fifth")) < html.index(_header_marker("#1 First"))
    # The first <details> group is open; later ones aren't.
    first_details = html[html.index("<details"):]
    assert first_details.split(">", 1)[0].count("open") == 1


def test_facts_within_a_session_are_chronological_not_reverse(client, seed):
    """The old world-wide list sorted newest-first regardless of session,
    which read backwards within any one session purely from insertion
    timing. Grouped facts should read oldest-first — the story in order."""
    s1 = _make_session(seed.world_a.id, 1, "Prologue")
    _make_fact(seed.world_a.id, "The party arrives at the village.", s1)
    _make_fact(seed.world_a.id, "The party investigates the ruins.", s1)
    _make_fact(seed.world_a.id, "The beast is defeated.", s1)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert (html.index("The party arrives at the village.")
            < html.index("The party investigates the ruins.")
            < html.index("The beast is defeated."))


def test_unsessioned_facts_get_their_own_bucket_first(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "Session One")
    _make_fact(seed.world_a.id, "Filed fact", s1)
    _make_fact(seed.world_a.id, "Unfiled fact", None)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert _header_marker("🗂 No session") in html
    assert html.index(_header_marker("🗂 No session")) < html.index(_header_marker("#1 Session One"))


def test_no_unsessioned_bucket_when_every_fact_has_a_session(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "Session One")
    _make_fact(seed.world_a.id, "Filed fact", s1)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert "No session" not in html


def test_facts_page_still_works_with_zero_facts(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/facts")
    assert r.status_code == 200
    assert "No facts logged yet." in r.text


def test_fact_count_summary_line(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "Session One")
    _make_fact(seed.world_a.id, "One", s1)
    _make_fact(seed.world_a.id, "Two", s1)
    _make_fact(seed.world_a.id, "Three", None)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert "3 facts in 2 sessions" in html


def test_facts_search_and_expand_collapse_js_present(client, seed):
    """The search/expand-all controls only render once there's at least one
    group to search — a page with zero facts has nothing to search."""
    s1 = _make_session(seed.world_a.id, 1, "Session One")
    _make_fact(seed.world_a.id, "A fact", s1)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert "function factsSearch(" in html
    assert "function factsSetAllGroups(" in html
    assert 'id="facts-search"' in html


def test_deleting_a_session_unlinks_its_facts_instead_of_orphaning_them(client, seed):
    """Audit finding: session_delete already nulled CombatSession.
    game_session_id on delete but left Fact.game_session_id dangling —
    harmless for rendering (the grouping above already treats an
    unresolvable id as unfiled defensively) but genuinely dirty data.
    Deleting the session should re-file its facts into "No session", not
    leave them pointing at a row that no longer exists."""
    s1 = _make_session(seed.world_a.id, 1, "Doomed Session")
    fact_id = _make_fact(seed.world_a.id, "A fact from the doomed session", s1)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/sessions/{s1}/delete", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        fact = db.get(Fact, fact_id)
        assert fact is not None  # the fact itself must survive
        assert fact.game_session_id is None
    finally:
        db.close()


def test_facts_from_another_world_dont_leak_into_grouping(client, seed):
    s_a = _make_session(seed.world_a.id, 1, "World A Session")
    s_b = _make_session(seed.world_b.id, 1, "World B Session")
    _make_fact(seed.world_a.id, "Belongs to world A", s_a)
    _make_fact(seed.world_b.id, "Belongs to world B", s_b)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/facts").text
    assert "Belongs to world A" in html
    assert "Belongs to world B" not in html
    assert "World B Session" not in html
