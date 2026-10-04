"""Session planner (app/routers/schedule.py, app/ical.py, templates/schedule/index.html): the table agrees on WHEN to
play. Anyone in the world proposes time slots, everyone marks each Yes / Maybe / No, a GM (or assistant / owner) confirms
one. Confirmed sessions are RSVPs on that slot, can become a session log, show up in each player's hub Schedule tab, on
the in-world calendar page, and download as .ics.

Times are stored in UTC and shown in each viewer's own time zone by the browser.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import ical, live
from app.database import SessionLocal
from app.models import (
    GameSession, PlayerCharacter, SessionPlan, SessionPlanSlot, SessionPlanVote, User, World, WorldMembership,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _iso(days=7, hour=18, minute=0, tz=timezone.utc):
    """An ISO time `days` from now at hour:minute UTC, in the form a browser sends (…Z)."""
    base = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    return base.astimezone(tz).isoformat().replace("+00:00", "Z")


def _as(client, seed, who, world=None):
    user = {"gm": seed.gm, "a": seed.player_a, "b": seed.player_b}[who]
    login(client, user.email, GM_PASSWORD if who == "gm" else PLAYER_PASSWORD)
    client.cookies.set("active_world", (world or (seed.world_b if who == "b" else seed.world_a)).slug)


def _make_plan(client, title="Session 12", slots=None, **extra):
    r = client.post("/api/schedule/plans", json={"title": title, "slots": slots if slots is not None else [_iso(7), _iso(8)], **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _state(client):
    r = client.get("/api/schedule/state")
    assert r.status_code == 200, r.text
    return r.json()


def _plan(client, plan_id):
    return next(p for p in _state(client)["plans"] if p["id"] == plan_id)


def _vote(client, slot_id, choice):
    return client.post(f"/api/schedule/slots/{slot_id}/vote", json={"choice": choice})


def _make_assistant(seed, user, role="assistant"):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == user.id).first()
        m.role = role
        db.commit()
    finally:
        db.close()


# ── .ics ────────────────────────────────────────────────────────────────────────────────────────

def test_ics_text_escaping():
    assert ical.escape_text("a,b;c\\d\nline2") == "a\\,b\\;c\\\\d\\nline2"
    assert ical.escape_text("a\r\nb") == "a\\nb"
    assert ical.escape_text(None) == ""


def test_ics_lines_fold_at_75_octets_without_splitting_a_character():
    line = "DESCRIPTION:" + "é" * 100                       # two bytes each
    folded = ical.fold_line(line)
    parts = folded.split("\r\n")
    assert all(len(p.encode()) <= 75 for p in parts)
    assert all(p.startswith(" ") for p in parts[1:])
    assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == line
    assert ical.fold_line("short") == "short"


def test_ics_event_has_the_required_properties_and_crlf_line_ends():
    start = datetime(2031, 5, 4, 18, 30)
    text = ical.build_calendar([{
        "uid": "plan-7@nd-world", "start": start, "end": start + timedelta(hours=3),
        "summary": "Session 12: The Heist", "description": "Bring snacks, dice", "location": "Discord #table",
        "stamp": datetime(2031, 1, 1, 9, 0),
    }], name="World A sessions")
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    for needle in ("VERSION:2.0", "PRODID:", "CALSCALE:GREGORIAN", "X-WR-CALNAME:World A sessions", "BEGIN:VEVENT",
                   "UID:plan-7@nd-world", "DTSTAMP:20310101T090000Z", "DTSTART:20310504T183000Z", "DTEND:20310504T213000Z",
                   "SUMMARY:Session 12: The Heist", "DESCRIPTION:Bring snacks\\, dice", "LOCATION:Discord #table",
                   "STATUS:CONFIRMED", "END:VEVENT"):
        assert needle in text, needle
    assert "\n" not in text.replace("\r\n", "")


def test_ics_with_no_events_is_still_a_valid_calendar():
    text = ical.build_calendar([])
    assert "BEGIN:VCALENDAR" in text and "BEGIN:VEVENT" not in text


# ── who can use it ──────────────────────────────────────────────────────────────────────────────────

def test_a_player_in_the_world_can_open_the_planner(client, seed):
    _as(client, seed, "a")
    r = client.get("/schedule")
    assert r.status_code == 200
    state = json.loads(re.search(r'<script id="schedule-state" type="application/json">(.*?)</script>', r.text, re.S).group(1))
    assert state["me"]["id"] == seed.player_a.id and state["me"]["can_manage"] is False
    assert state["plans"] == _state(client)["plans"] and state["participants"] == _state(client)["participants"]


def test_the_gm_can_manage(client, seed):
    _as(client, seed, "gm")
    assert _state(client)["me"]["can_manage"] is True


def test_participants_are_the_members_and_the_gm(client, seed):
    _as(client, seed, "a")
    names = {p["id"]: p for p in _state(client)["participants"]}
    assert seed.player_a.id in names and seed.gm.id in names and seed.player_b.id not in names
    assert names[seed.gm.id]["role"] == "gm" and names[seed.player_a.id]["role"] == "player"


def test_plans_never_cross_worlds(client, seed):
    _as(client, seed, "gm")
    plan = _make_plan(client)
    _as(client, seed, "b")                                  # player B only belongs to world B
    assert _state(client)["plans"] == []
    assert _vote(client, 1, "yes").status_code == 404
    assert client.post(f"/api/schedule/plans/{plan}/confirm", json={"slot_id": 1}).status_code in (403, 404)
    assert client.get(f"/schedule/plans/{plan}.ics").status_code == 404


def test_player_b_pointing_at_world_a_still_only_gets_their_own_world(client, seed):
    _as(client, seed, "gm")
    _make_plan(client)
    _as(client, seed, "b", world=seed.world_a)              # cookie names a world they are not in
    assert _state(client)["plans"] == []


# ── proposing ───────────────────────────────────────────────────────────────────────────────────────

def test_anyone_can_start_a_plan_with_slots(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client, title="One-shot night", notes="Bring a character", location="Discord", duration_min=240,
                     slots=["2031-05-04T20:00:00+02:00", "2031-05-05T18:00:00Z"])
    p = _plan(client, pid)
    assert p["title"] == "One-shot night" and p["status"] == "open" and p["duration_min"] == 240
    assert p["created_by"]["id"] == seed.player_a.id and p["can_edit"] is True
    assert [s["starts_at"] for s in p["slots"]] == ["2031-05-04T18:00:00Z", "2031-05-05T18:00:00Z"]       # UTC, in order
    assert p["slots"][0]["ends_at"] == "2031-05-04T22:00:00Z"


def test_a_plan_needs_a_title_and_valid_times(client, seed):
    _as(client, seed, "a")
    assert client.post("/api/schedule/plans", json={"title": "  ", "slots": []}).status_code == 400
    assert client.post("/api/schedule/plans", json={"title": "x", "slots": ["tomorrow-ish"]}).status_code == 400
    assert client.post("/api/schedule/plans", json={"title": "x", "slots": [12345]}).status_code == 400
    assert client.post("/api/schedule/plans", json={"title": "x", "slots": ["1850-01-01T00:00:00Z"]}).status_code == 400
    assert client.post("/api/schedule/plans", json={"title": "x", "slots": "nope"}).status_code == 400
    assert client.post("/api/schedule/plans", data="not json", headers={"content-type": "application/json"}).status_code == 400
    assert _state(client)["plans"] == []                    # nothing half-created


def test_limits_on_slots_and_plans(client, seed):
    _as(client, seed, "a")
    too_many = [_iso(1 + i) for i in range(25)]
    assert client.post("/api/schedule/plans", json={"title": "x", "slots": too_many}).status_code == 400
    r = client.post("/api/schedule/plans", json={"title": "t" * 400, "notes": "n" * 5000, "location": "l" * 500, "duration_min": 99999,
                                                 "slots": [_iso(1), _iso(1)]})
    assert r.status_code == 200
    p = _plan(client, r.json()["id"])
    assert len(p["title"]) <= 160 and len(p["notes"]) <= 1500 and len(p["location"]) <= 200
    assert p["duration_min"] == 1440 and len(p["slots"]) == 1, "a repeated time is one slot"
    assert _plan(client, _make_plan(client, duration_min="abc"))["duration_min"] == 180


def test_anyone_can_propose_another_slot_and_remove_their_own(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client, slots=[_iso(7)])
    _as(client, seed, "a")
    r = client.post(f"/api/schedule/plans/{pid}/slots", json={"starts_at": _iso(9, 19)})
    assert r.status_code == 200
    p = _plan(client, pid)
    mine = next(s for s in p["slots"] if s["proposed_by"]["id"] == seed.player_a.id)
    gms = next(s for s in p["slots"] if s["proposed_by"]["id"] == seed.gm.id)
    assert mine["can_delete"] is True and gms["can_delete"] is False
    assert client.post(f"/api/schedule/slots/{gms['id']}/delete").status_code == 403
    assert client.post(f"/api/schedule/slots/{mine['id']}/delete").status_code == 200
    assert len(_plan(client, pid)["slots"]) == 1
    # a duplicate time is not a second slot
    assert client.post(f"/api/schedule/plans/{pid}/slots", json={"starts_at": gms["starts_at"]}).status_code == 200
    assert len(_plan(client, pid)["slots"]) == 1
    assert client.post(f"/api/schedule/plans/{pid}/slots", json={"starts_at": "never"}).status_code == 400


def test_a_manager_can_remove_any_slot_and_deleting_a_slot_drops_its_votes(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    assert _vote(client, slot, "yes").status_code == 200
    _as(client, seed, "gm")
    assert client.post(f"/api/schedule/slots/{slot}/delete").status_code == 200
    db = SessionLocal()
    try:
        assert db.query(SessionPlanVote).filter(SessionPlanVote.slot_id == slot).count() == 0
    finally:
        db.close()


def test_only_the_creator_or_a_manager_edits_a_plan(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    assert client.post(f"/api/schedule/plans/{pid}/edit", json={"title": "Renamed", "location": "My place", "notes": "n", "duration_min": 120}).status_code == 200
    p = _plan(client, pid)
    assert (p["title"], p["location"], p["duration_min"]) == ("Renamed", "My place", 120)
    assert client.post(f"/api/schedule/plans/{pid}/edit", json={"title": " "}).status_code == 400
    # a second member of the world who is neither creator nor manager
    db = SessionLocal()
    try:
        other = User(email="player-c@test.local", password_hash=seed.player_a.password_hash, display_name="Player C")
        db.add(other)
        db.commit()
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=other.id))
        db.commit()
        email = other.email
    finally:
        db.close()
    login(client, email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/schedule/plans/{pid}/edit", json={"title": "Hijack"}).status_code == 403
    assert client.post(f"/api/schedule/plans/{pid}/cancel").status_code == 403
    assert client.post(f"/api/schedule/plans/{pid}/delete").status_code == 403


# ── voting ──────────────────────────────────────────────────────────────────────────────────────────

def test_voting_yes_maybe_no_and_changing_your_mind(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    s1, s2 = [s["id"] for s in _plan(client, pid)["slots"]]
    _as(client, seed, "a")
    assert _vote(client, s1, "yes").status_code == 200
    assert _vote(client, s2, "maybe").status_code == 200
    p = _plan(client, pid)
    assert p["slots"][0]["mine"] == "yes" and p["slots"][1]["mine"] == "maybe"
    assert p["slots"][0]["counts"] == {"yes": 1, "maybe": 0, "no": 0}
    assert _vote(client, s1, "no").status_code == 200                         # changed their mind: one vote per slot
    assert _plan(client, pid)["slots"][0]["counts"] == {"yes": 0, "maybe": 0, "no": 1}
    assert _vote(client, s1, "clear").status_code == 200
    p = _plan(client, pid)
    assert p["slots"][0]["mine"] is None and p["slots"][0]["counts"] == {"yes": 0, "maybe": 0, "no": 0}
    db = SessionLocal()
    try:
        assert db.query(SessionPlanVote).filter(SessionPlanVote.slot_id == s1).count() == 0
    finally:
        db.close()


def test_everyones_votes_are_visible_by_name_and_only_you_vote_for_you(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    assert _vote(client, slot, "yes").status_code == 200
    _as(client, seed, "a")
    assert _vote(client, slot, "maybe").status_code == 200
    s = _plan(client, pid)["slots"][0]
    assert s["votes"] == {str(seed.gm.id): "yes", str(seed.player_a.id): "maybe"}
    assert s["counts"] == {"yes": 1, "maybe": 1, "no": 0}
    # a body naming someone else's id is ignored: the vote is always the caller's
    assert client.post(f"/api/schedule/slots/{slot}/vote", json={"choice": "no", "user_id": seed.gm.id}).status_code == 200
    assert _plan(client, pid)["slots"][0]["votes"][str(seed.gm.id)] == "yes"


def test_bad_votes_are_refused(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    assert _vote(client, slot, "definitely").status_code == 400
    assert client.post(f"/api/schedule/slots/{slot}/vote", json={}).status_code == 400
    assert _vote(client, 99999, "yes").status_code == 404


def test_votes_of_people_who_left_the_world_do_not_count(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    assert _vote(client, slot, "yes").status_code == 200
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.user_id == seed.player_a.id).delete()
        db.commit()
    finally:
        db.close()
    _as(client, seed, "gm")
    assert _plan(client, pid)["slots"][0]["counts"]["yes"] == 0


def test_cancelled_plans_take_no_votes(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    assert client.post(f"/api/schedule/plans/{pid}/cancel").status_code == 200
    assert _plan(client, pid)["status"] == "cancelled"
    assert _vote(client, slot, "yes").status_code == 400
    assert client.post(f"/api/schedule/plans/{pid}/slots", json={"starts_at": _iso(3)}).status_code == 400


# ── confirming ───────────────────────────────────────────────────────────────────────────────────────

def test_a_player_cannot_confirm_but_the_gm_can(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][1]["id"]
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot}).status_code == 403
    _as(client, seed, "gm")
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot}).status_code == 200
    p = _plan(client, pid)
    assert p["status"] == "confirmed" and p["confirmed_slot_id"] == slot


def test_an_assistant_and_an_owner_can_confirm(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    _make_assistant(seed, seed.player_a, "assistant")
    _as(client, seed, "a")
    assert _state(client)["me"]["can_manage"] is True
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot}).status_code == 200
    _as(client, seed, "gm")
    assert client.post(f"/api/schedule/plans/{pid}/reopen").status_code == 200
    _make_assistant(seed, seed.player_a, "owner")
    _as(client, seed, "a")
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot}).status_code == 200


def test_confirm_needs_one_of_this_plans_slots(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    other = _make_plan(client)
    foreign = _plan(client, other)["slots"][0]["id"]
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": foreign}).status_code == 400
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={}).status_code == 400
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": "x"}).status_code == 400
    assert _plan(client, pid)["status"] == "open"


def test_a_confirmed_plan_must_be_reopened_before_choosing_again(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    s1, s2 = [s["id"] for s in _plan(client, pid)["slots"]]
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": s1}).status_code == 200
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": s2}).status_code == 400
    assert client.post(f"/api/schedule/plans/{pid}/reopen").status_code == 200
    p = _plan(client, pid)
    assert p["status"] == "open" and p["confirmed_slot_id"] is None
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": s2}).status_code == 200
    assert client.post(f"/api/schedule/plans/{pid}/reopen").status_code == 200
    assert client.post(f"/api/schedule/plans/{pid}/reopen").status_code == 400          # nothing to reopen


def test_after_confirming_the_chosen_slot_is_the_rsvp(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    s1, s2 = [s["id"] for s in _plan(client, pid)["slots"]]
    _as(client, seed, "a")
    _vote(client, s1, "yes")
    _as(client, seed, "gm")
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": s1}).status_code == 200
    _as(client, seed, "a")
    assert _vote(client, s1, "no").status_code == 200, "a player can still say they can't make it"
    assert _plan(client, pid)["slots"][0]["votes"][str(seed.player_a.id)] == "no"
    assert _vote(client, s2, "yes").status_code == 400, "the other slots are closed"
    assert client.post(f"/api/schedule/plans/{pid}/slots", json={"starts_at": _iso(30)}).status_code == 400


def test_confirming_can_create_the_session_log_once(client, seed):
    _as(client, seed, "gm")
    db = SessionLocal()
    try:
        db.add(GameSession(world_id=seed.world_a.id, title="Earlier", session_num=11))
        db.commit()
    finally:
        db.close()
    pid = _make_plan(client, title="Session 12: The Heist", slots=["2031-05-04T18:00:00Z"])
    slot = _plan(client, pid)["slots"][0]["id"]
    r = client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot, "create_session": True, "session_date": "2031-05-04"})
    assert r.status_code == 200
    p = _plan(client, pid)
    assert p["session_id"] and p["session_href"].startswith(f"/sessions/{p['session_id']}")
    db = SessionLocal()
    try:
        gs = db.get(GameSession, p["session_id"])
        assert (gs.title, gs.session_num, gs.session_date, gs.world_id) == ("Session 12: The Heist", 12, "2031-05-04", seed.world_a.id)
    finally:
        db.close()
    assert client.post(f"/api/schedule/plans/{pid}/reopen").status_code == 200
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot, "create_session": True}).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(GameSession).filter(GameSession.world_id == seed.world_a.id).count() == 2, "no second log"
    finally:
        db.close()


def test_the_session_date_falls_back_to_the_slots_utc_date_and_bad_values_are_ignored(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client, slots=["2031-05-04T22:30:00Z"])
    slot = _plan(client, pid)["slots"][0]["id"]
    assert client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot, "create_session": True, "session_date": "not a date"}).status_code == 200
    db = SessionLocal()
    try:
        assert db.get(GameSession, _plan(client, pid)["session_id"]).session_date == "2031-05-04"
    finally:
        db.close()


def test_cancel_and_delete(client, seed):
    _as(client, seed, "a")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    _vote(client, slot, "yes")
    assert client.post(f"/api/schedule/plans/{pid}/delete").status_code == 200       # the creator may delete their own
    assert _state(client)["plans"] == []
    db = SessionLocal()
    try:
        assert db.query(SessionPlan).count() == 0 and db.query(SessionPlanSlot).count() == 0 and db.query(SessionPlanVote).count() == 0
    finally:
        db.close()
    assert client.post("/api/schedule/plans/999/delete").status_code == 404


def test_every_change_pings_live_sync(client, seed):
    _as(client, seed, "gm")
    before = live.version(seed.world_a.id)
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    after_create = live.version(seed.world_a.id)
    _vote(client, slot, "yes")
    after_vote = live.version(seed.world_a.id)
    client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot})
    assert before < after_create < after_vote < live.version(seed.world_a.id)


# ── .ics downloads ─────────────────────────────────────────────────────────────────────────────────────

def test_a_confirmed_plan_downloads_as_ics(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client, title="Session 12", location="Discord", notes="Bring snacks", duration_min=120, slots=["2031-05-04T18:00:00Z"])
    slot = _plan(client, pid)["slots"][0]["id"]
    assert client.get(f"/schedule/plans/{pid}.ics").status_code == 404, "nothing decided yet"
    client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot})
    _as(client, seed, "a")
    r = client.get(f"/schedule/plans/{pid}.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"] and ".ics" in r.headers["content-disposition"]
    for needle in ("DTSTART:20310504T180000Z", "DTEND:20310504T200000Z", "SUMMARY:Session 12", "LOCATION:Discord",
                   f"UID:plan-{pid}-", "Bring snacks"):
        assert needle in r.text, needle
    assert client.get("/schedule/plans/99999.ics").status_code == 404


def test_cancelled_and_open_plans_do_not_export(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    slot = _plan(client, pid)["slots"][0]["id"]
    client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": slot})
    client.post(f"/api/schedule/plans/{pid}/cancel")
    assert client.get(f"/schedule/plans/{pid}.ics").status_code == 404


def test_all_confirmed_sessions_in_one_ics(client, seed):
    _as(client, seed, "gm")
    soon = _make_plan(client, title="Soon", slots=[_iso(3)])
    later = _make_plan(client, title="Later", slots=[_iso(20)])
    old = _make_plan(client, title="Long ago", slots=[_iso(-90)])
    undecided = _make_plan(client, title="Undecided", slots=[_iso(5)])
    for pid in (soon, later, old):
        client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": _plan(client, pid)["slots"][0]["id"]})
    text = client.get("/schedule/confirmed.ics").text
    assert "SUMMARY:Soon" in text and "SUMMARY:Later" in text
    assert "Long ago" not in text and "Undecided" not in text
    assert text.count("BEGIN:VEVENT") == 2
    _as(client, seed, "b")
    assert "BEGIN:VEVENT" not in client.get("/schedule/confirmed.ics").text


# ── where else it shows up ─────────────────────────────────────────────────────────────────────────────────

def test_the_calendar_page_links_to_the_planner_and_names_the_next_session(client, seed):
    _as(client, seed, "gm")
    page = client.get("/calendar").text
    assert 'href="/schedule' in page and 'id="cal-next-session"' not in page
    pid = _make_plan(client, title="Session 12", slots=[_iso(3)])
    client.post(f"/api/schedule/plans/{pid}/confirm", json={"slot_id": _plan(client, pid)["slots"][0]["id"]})
    page = client.get("/calendar").text
    assert 'id="cal-next-session"' in page and "Session 12" in page


def test_the_nav_offers_the_planner_to_everyone(client, seed):
    for who in ("gm", "a"):
        _as(client, seed, who)
        assert 'data-ql-ref="/schedule"' in client.get("/").text or 'href="/schedule' in client.get("/").text, who


def test_the_hub_schedule_tab_shows_the_next_session_and_polls_waiting(client, seed):
    _as(client, seed, "gm")
    confirmed = _make_plan(client, title="Session 12", location="Discord", slots=[_iso(3)])
    client.post(f"/api/schedule/plans/{confirmed}/confirm", json={"slot_id": _plan(client, confirmed)["slots"][0]["id"]})
    _make_plan(client, title="Open poll one")
    answered = _make_plan(client, title="Open poll two")
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Ryn", owner_user_id=seed.player_a.id)
        db.add(pc)
        db.commit()
        pc_id = pc.id
    finally:
        db.close()
    _as(client, seed, "a")
    _vote(client, _plan(client, answered)["slots"][0]["id"], "yes")
    data = client.get(f"/api/characters/{pc_id}/hub/schedule").json()
    assert data["next_session"]["title"] == "Session 12" and data["next_session"]["location"] == "Discord"
    assert data["next_session"]["starts_at"].endswith("Z") and data["next_session"]["my"] is None
    assert data["polls_waiting"] == 1, "one open poll the player has not answered yet"
    assert data["schedule_href"].startswith("/schedule")


def test_world_delete_removes_plans_slots_and_votes(client, seed):
    _as(client, seed, "gm")
    pid = _make_plan(client)
    _vote(client, _plan(client, pid)["slots"][0]["id"], "yes")
    assert client.post(f"/worlds/{seed.world_a.id}/delete").status_code in (200, 303)
    db = SessionLocal()
    try:
        assert db.query(SessionPlan).count() == 0 and db.query(SessionPlanSlot).count() == 0 and db.query(SessionPlanVote).count() == 0
        assert db.get(World, seed.world_a.id) is None
    finally:
        db.close()
