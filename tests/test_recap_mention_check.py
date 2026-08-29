"""Tests for plan item QoL 3.4: a one-click "Check all mentioned" button on
the session detail page's Entities Featured picker (app/templates/sessions/
detail.html). Entirely client-side (checkMentionedEntities() scans the
Summary textarea for exact, case-insensitive, whole-word matches of each
candidate's name and checks the matching row) — no new backend route, so
these tests confirm the button/JS are wired into the rendered page; the
actual match logic is covered by a live browser check (see session notes)."""
from app.database import SessionLocal
from app.models import Entity, GameSession

from .conftest import GM_PASSWORD, login


def _make_session(world_id, title="Session 1", summary=""):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title=title, session_num=1, summary=summary)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _make_entity(world_id, kind, name):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, name=name)
        db.add(e)
        db.commit()
        return e.id
    finally:
        db.close()


def test_check_mentioned_button_present_on_existing_session(client, seed):
    _make_entity(seed.world_a.id, "character", "Elena the Rogue")
    sid = _make_session(seed.world_a.id, summary="Elena the Rogue robbed the bank.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert 'onclick="checkMentionedEntities()"' in r.text
    assert "function checkMentionedEntities()" in r.text
    assert 'id="npc-search-input"' in r.text


def test_check_mentioned_button_absent_on_new_session_form(client, seed):
    # The Entities Featured picker (and this button) only exist once a
    # session has been created — matches the {% if gsession %} gate already
    # used for the rest of that section.
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/sessions/new")
    assert r.status_code == 200
    assert "checkMentionedEntities" not in r.text
