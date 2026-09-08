"""Tests for the Facts panel embedded directly on a session's own detail
page (app/routers/sessions.py's session_detail + sessions/detail.html) —
the "integrate Facts into Sessions" follow-up to the Facts audit/grouping
work: a GM can now log/review/AI-parse facts for a session without leaving
its page, sharing the same Recap→Facts widget /facts uses via the extracted
static/js/facts-recap-parser.js module. /facts itself stays fully intact as
a standalone, cross-session page — these are additive, not a replacement.
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


def test_session_detail_shows_only_its_own_facts_in_order(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    s2 = _make_session(seed.world_a.id, 2, "Second")
    _make_fact(seed.world_a.id, "s1 fact alpha", s1)
    _make_fact(seed.world_a.id, "s1 fact beta", s1)
    _make_fact(seed.world_a.id, "s2 fact only", s2)
    _make_fact(seed.world_a.id, "unfiled fact", None)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/sessions/{s1}").text
    assert "s1 fact alpha" in html
    assert "s1 fact beta" in html
    assert html.index("s1 fact alpha") < html.index("s1 fact beta")
    assert "s2 fact only" not in html
    assert "unfiled fact" not in html


def test_session_detail_facts_panel_wires_shared_js_module(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/sessions/{s1}").text
    assert '<script src="/static/js/facts-recap-parser.js"></script>' in html
    assert f"ndFactsRecapParser({{ fixedSessionId: {s1}" in html
    assert 'id="parse-btn"' in html
    assert 'id="draft-panel"' in html


def test_new_session_form_has_no_facts_panel(client, seed):
    # gsession is None on the "+ New Session" form — the whole Facts panel
    # (and every other {% if gsession %} section) must be skipped cleanly,
    # not raise on an undefined `facts`.
    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/sessions/new")
    assert r.status_code == 200
    assert 'id="draft-panel"' not in r.text


def test_quick_add_from_session_page_redirects_back_to_session(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(
        "/facts/new",
        data={"content": "Logged from the session page", "game_session_id": str(s1), "next": f"/sessions/{s1}"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/sessions/{s1}"

    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.content == "Logged from the session page"
        assert fact.game_session_id == s1
    finally:
        db.close()


def test_edit_from_session_page_redirects_back_to_session(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    fact_id = _make_fact(seed.world_a.id, "Original", s1)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(
        f"/facts/{fact_id}/edit",
        data={"content": "Edited from the session page", "visible_to_players": "1", "next": f"/sessions/{s1}"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/sessions/{s1}"

    db = SessionLocal()
    try:
        fact = db.get(Fact, fact_id)
        assert fact.content == "Edited from the session page"
    finally:
        db.close()


def test_delete_from_session_page_redirects_back_to_session(client, seed):
    s1 = _make_session(seed.world_a.id, 1, "First")
    fact_id = _make_fact(seed.world_a.id, "To be deleted", s1)
    _login_gm_in(client, seed, seed.world_a)
    r = client.post(f"/facts/{fact_id}/delete", data={"next": f"/sessions/{s1}"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/sessions/{s1}"

    db = SessionLocal()
    try:
        assert db.get(Fact, fact_id) is None
    finally:
        db.close()


def test_next_redirect_rejects_arbitrary_targets(client, seed):
    """The next-redirect whitelist (facts.py's _safe_next) only accepts
    "/facts" or "/sessions/<digits>" — anything else (an open-redirect
    attempt, a malformed session path) falls back to "/facts"."""
    _login_gm_in(client, seed, seed.world_a)
    for bad_next in ("https://evil.example.com/", "/sessions/abc", "//evil.example.com", "/sessions/", "/other-page"):
        r = client.post(
            "/facts/new", data={"content": "x", "next": bad_next}, follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/facts"


def test_next_defaults_to_facts_when_omitted(client, seed):
    # Backward compatible: the standalone /facts page's own forms don't
    # send `next` at all, and must keep redirecting to /facts exactly as
    # before this feature existed.
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/facts/new", data={"content": "plain add"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/facts"


def test_background_jobs_extract_facts_routes_to_session_when_known(client, seed):
    """The Background Jobs page's "📋 Extract facts" hand-off button used to
    always send the GM to /facts. When the job's game_session_id is known,
    it should route straight to that session's own embedded Facts panel
    instead — one less click, matching sessions/detail.html's new panel."""
    _login_gm_in(client, seed, seed.world_a)
    html = client.get("/background-jobs").text
    assert "job.game_session_id ? `/sessions/${job.game_session_id}` : '/facts'" in html


def test_facts_recap_parser_js_served(client, seed):
    r = client.get("/static/js/facts-recap-parser.js")
    assert r.status_code == 200
    assert "function ndFactsRecapParser" in r.text


def test_facts_from_another_world_dont_leak_into_session_panel(client, seed):
    s_a = _make_session(seed.world_a.id, 1, "World A Session")
    s_b = _make_session(seed.world_b.id, 1, "World B Session")
    _make_fact(seed.world_a.id, "Belongs to world A", s_a)
    _make_fact(seed.world_b.id, "Belongs to world B", s_b)

    _login_gm_in(client, seed, seed.world_a)
    html = client.get(f"/sessions/{s_a}").text
    assert "Belongs to world A" in html
    assert "Belongs to world B" not in html
