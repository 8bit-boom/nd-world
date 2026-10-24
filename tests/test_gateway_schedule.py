"""The session planner through the gateway (rust/nd-gateway/src/schedule.rs) against Python's (app/routers/schedule.py): the same steps -
plans, slots, votes, confirm (with and without a session log), reopen, cancel, delete, every refusal - run once directly on Python and
once through the gateway, each from a clean world; the answers (ids renumbered, "now" and DTSTAMP masked), the state and the .ics files
must match, and every gateway answer must be its own."""
import re

import pytest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD
from .test_gateway_golden import DIRECT, _signed_in, _via

pytestmark = pytest.mark.skipif(not DIRECT, reason="needs ND_TEST_BASE_URL (the gateway) and ND_GOLDEN_DIRECT_URL (Python directly)")


def _clear(world_id):
    from app.database import SessionLocal
    from app.models import GameSession, SessionPlan, SessionPlanSlot, SessionPlanVote
    db = SessionLocal()
    try:
        for m in (SessionPlanVote, SessionPlanSlot, SessionPlan):
            db.query(m).filter(m.world_id == world_id).delete()
        db.query(GameSession).filter(GameSession.world_id == world_id, GameSession.title.like("Plan %")).delete()
        db.commit()
    finally:
        db.close()


def _run(gm, player):
    """The steps, with every id renumbered by order of appearance so the two runs compare."""
    out = []
    known = {}

    def num(kind, real):
        if real is None:
            return None
        return known.setdefault((kind, real), f"{kind}{len([k for k in known if k[0] == kind])}")

    def call(who, method, path, body=None, raw=None):
        c = gm if who == "gm" else player
        if method == "GET":
            r = c.get(path, follow_redirects=False)
        elif raw is not None:
            r = c.post(path, content=raw, headers={"content-type": "application/json"})
        else:
            r = c.post(path, json=body)
        text = r.text
        if "json" in r.headers.get("content-type", ""):
            data = r.json()
            text = _renumber(data, num)
        text = re.sub(r"DTSTAMP:\d{8}T\d{6}Z", "DTSTAMP:~", str(text))
        out.append((who, method, re.sub(r"/\d+(?=/|\.ics)", "/#", path), r.status_code, r.headers.get("content-type"), r.headers.get("content-disposition"), text))
        served.append(r.headers.get("x-nd-served-by"))
        return r

    served = []
    p = lambda r: r.json()["id"]
    a = p(call("gm", "POST", "/api/schedule/plans", {"title": "  Plan one ", "notes": "Bring dice; snacks, too", "location": "Mo's\nplace",
                                                        "duration_min": "240", "slots": ["2031-05-04T18:30:00+02:00", "2031-05-04T16:30Z", "2031-05-11T18:00Z", "2031-06-01"]}))
    b = p(call("player", "POST", "/api/schedule/plans", {"title": "Plan two", "slots": [], "duration_min": 3}))
    call("gm", "POST", "/api/schedule/plans", {"title": "   "})
    call("gm", "POST", "/api/schedule/plans", {"title": "x", "slots": "2031-01-01"})
    call("gm", "POST", "/api/schedule/plans", {"title": "x", "slots": ["1999-12-31T23:00Z"]})
    call("gm", "POST", "/api/schedule/plans", {"title": "x", "slots": [True]})
    call("gm", "POST", "/api/schedule/plans", {"title": "x", "slots": [f"2031-01-{d:02d}" for d in range(1, 23)]})
    call("gm", "POST", "/api/schedule/plans", raw=b"[1]")
    call("gm", "POST", "/api/schedule/plans", raw=b"{oops")
    st = call("gm", "GET", "/api/schedule/state").json()
    slots = [s["id"] for s in next(x for x in st["plans"] if x["id"] == a)["slots"]]
    s2 = p(call("player", "POST", f"/api/schedule/plans/{a}/slots", {"starts_at": "2031-07-01T20:00Z"}))
    call("player", "POST", f"/api/schedule/plans/{a}/slots", {"starts_at": "2031-07-01T20:00:59Z"})     # the same minute: the same slot
    call("player", "POST", f"/api/schedule/plans/{a}/slots", {"starts_at": "soon"[:0]})
    call("player", "POST", f"/api/schedule/plans/{a}/slots", {"starts_at": 7})
    call("player", "POST", "/api/schedule/plans/999999/slots", {"starts_at": "2031-07-01T20:00Z"})
    for who, sid, choice in (("gm", slots[0], "yes"), ("player", slots[0], "maybe"), ("player", slots[1], "no"), ("player", slots[0], "yes"),
                             ("player", slots[1], "clear"), ("gm", slots[2], "nope"), ("gm", 999999, "yes"), ("player", s2, "maybe")):
        call(who, "POST", f"/api/schedule/slots/{sid}/vote", {"choice": choice})
    call("player", "POST", f"/api/schedule/plans/{a}/edit", {"title": "hijack"})
    call("gm", "POST", f"/api/schedule/plans/{a}/edit", {"title": ""})
    call("gm", "POST", f"/api/schedule/plans/{a}/edit", {"title": "Plan one, renamed", "duration_min": "x", "notes": 12, "location": None})
    call("player", "POST", f"/api/schedule/plans/{b}/edit", {"duration_min": 2000})
    call("player", "POST", f"/api/schedule/slots/{slots[2]}/delete")
    call("player", "POST", f"/api/schedule/slots/{s2}/delete")
    call("player", "POST", f"/api/schedule/plans/{a}/confirm", {"slot_id": slots[0]})
    call("gm", "POST", f"/api/schedule/plans/{a}/confirm", {"slot_id": "x"})
    call("gm", "POST", f"/api/schedule/plans/{a}/confirm", {"slot_id": 999999})
    call("gm", "GET", f"/schedule/plans/{a}.ics")
    call("gm", "POST", f"/api/schedule/plans/{a}/confirm", {"slot_id": str(slots[0]), "create_session": 1, "session_date": "2031-02-30"})
    call("gm", "POST", f"/api/schedule/plans/{a}/confirm", {"slot_id": slots[0]})
    call("gm", "POST", f"/api/schedule/slots/{slots[0]}/delete")
    call("player", "POST", f"/api/schedule/slots/{slots[1]}/vote", {"choice": "yes"})
    call("player", "POST", f"/api/schedule/slots/{slots[0]}/vote", {"choice": "no"})
    call("player", "GET", f"/schedule/plans/{a}.ics")
    call("player", "GET", f"/schedule/plans/{b}.ics")
    call("player", "GET", "/schedule/confirmed.ics")
    call("gm", "GET", "/api/schedule/state")
    call("player", "GET", "/api/schedule/state")
    call("player", "POST", f"/api/schedule/plans/{a}/reopen")
    call("gm", "POST", f"/api/schedule/plans/{a}/reopen")
    call("gm", "POST", f"/api/schedule/plans/{a}/reopen")
    call("gm", "POST", f"/api/schedule/plans/{a}/confirm", {"slot_id": slots[1], "create_session": True, "session_date": "2031-05-10"})
    call("player", "POST", f"/api/schedule/plans/{a}/cancel")
    call("player", "POST", f"/api/schedule/plans/{b}/cancel")
    call("player", "POST", f"/api/schedule/slots/{slots[0]}/vote", {"choice": "yes"})
    call("gm", "POST", f"/api/schedule/plans/{b}/confirm", {"slot_id": 1})
    call("player", "POST", f"/api/schedule/plans/{a}/delete")
    call("gm", "GET", "/api/schedule/state")
    call("player", "POST", f"/api/schedule/plans/{b}/delete")
    call("gm", "POST", f"/api/schedule/plans/{a}/delete")
    call("gm", "GET", "/api/schedule/state")
    return out, served


def _renumber(v, num, key=None):
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k == "now":
                out[k] = "~"
            elif k == "votes" and isinstance(x, dict):
                out[k] = {str(num("u", int(u))): c for u, c in x.items()}
            else:
                out[k] = _renumber(x, num, k)
        return out
    if isinstance(v, list):
        return [_renumber(x, num, key) for x in v]
    if isinstance(v, int) and not isinstance(v, bool) and key in ("id", "confirmed_slot_id", "session_id"):
        return num(key, v)
    if isinstance(v, str) and key == "session_href":
        return re.sub(r"/sessions/\d+", "/sessions/#", v)
    return v


def test_schedule_is_pythons(client, seed):
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    _clear(seed.world_a.id)
    a, _ = _run(gm, player)
    _clear(seed.world_a.id)
    b, served = _run(_via(gm, base), _via(player, base))
    _clear(seed.world_a.id)
    for x, y in zip(a, b):
        assert x == y, (x, y)
    assert len(a) == len(b)
    assert all(s == "gateway" for s in served), [(x[:4], s) for x, s in zip(b, served) if s != "gateway"]


def test_schedule_page_is_pythons(client, seed):
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    _clear(seed.world_a.id)
    gm.post("/api/schedule/plans", json={"title": "Plan <page>", "notes": "n & m", "slots": ["2031-05-04T18:30Z"]})
    for who in (gm, _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        a, b = who.get("/schedule"), _via(who, base).get("/schedule")
        mask = lambda t: re.sub(r'&#34;now&#34;: &#34;[^&]*&#34;|"now": "[^"]*"', "now", t)
        assert (a.status_code, mask(a.text)) == (b.status_code, mask(b.text))
        assert b.headers.get("x-nd-served-by") == "gateway"
    _clear(seed.world_a.id)
