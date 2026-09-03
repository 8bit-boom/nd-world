"""Tests for the shared dice roller: parser grammar, roll storage, the
page + JSON API surfaces, and world/role scoping."""
import json

from app.database import SessionLocal
from app.models import DiceRoll
from app.routers.dice import parse_and_roll

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _login_player_in(client, seed, world):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", world.slug)


# ── parser ─────────────────────────────────────────────────────────────────

def test_parse_simple_notation():
    breakdown, total = parse_and_roll("2d6+3")
    assert len(breakdown) == 2
    assert breakdown[0]["term"] == "+2d6"
    assert len(breakdown[0]["rolls"]) == 2
    assert all(1 <= r <= 6 for r in breakdown[0]["rolls"])
    assert breakdown[0]["sum"] == sum(breakdown[0]["rolls"])
    assert breakdown[1] == {"term": "+3", "sum": 3}
    assert total == sum(b["sum"] for b in breakdown)


def test_parse_bare_die_and_negative_modifier():
    _, total = parse_and_roll("d20")
    assert 1 <= total <= 20
    breakdown, total = parse_and_roll("1d20-1")
    assert total == breakdown[0]["sum"] - 1


def test_parse_multiple_dice_terms():
    breakdown, total = parse_and_roll("4d8+2d6+1")
    assert [b["term"] for b in breakdown] == ["+4d8", "+2d6", "+1"]
    assert total == sum(b["sum"] for b in breakdown)


def test_parse_rejects_garbage():
    for bad in ["", "   ", "banana", "2d6+banana", "2dd6", "d", "2d6++3", "((2d6))"]:
        try:
            parse_and_roll(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_parse_rejects_abuse_sized_rolls():
    for bad in ["0d6", "51d6", "2d1", "2d1001", "1d1d1d1d1d1d1d1d1"]:
        try:
            parse_and_roll(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


# ── routes ──────────────────────────────────────────────────────────────────

def test_gm_can_roll_via_api_and_it_lands_in_history(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/dice/roll", json={"notation": "2d6+3"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_name"] == "GM"
    assert 5 <= body["total"] <= 15
    assert len(body["breakdown"]) == 2

    hist = client.get("/api/dice/history").json()["rolls"]
    assert len(hist) == 1
    assert hist[0]["notation"] == "2d6+3"
    assert hist[0]["total"] == body["total"]


def test_api_rejects_invalid_notation(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/dice/roll", json={"notation": "banana"})
    assert r.status_code == 400
    assert "dice notation" in r.json()["detail"]


def test_player_can_roll_and_read_log(client, seed):
    _login_player_in(client, seed, seed.world_a)
    r = client.post("/api/dice/roll", json={"notation": "1d20"})
    assert r.status_code == 200
    assert r.json()["user_name"] == "Player A"

    page = client.get("/dice")
    assert page.status_code == 200
    assert "1d20" in page.text


def test_roll_log_is_world_scoped(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/api/dice/roll", json={"notation": "2d6"})

    # A member of world A sees the shared log...
    _login_player_in(client, seed, seed.world_a)
    assert len(client.get("/api/dice/history").json()["rolls"]) == 1

    # ...a member of world B only ever sees world B's (empty) log.
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    hist = client.get("/api/dice/history").json()["rolls"]
    assert hist == []


def test_form_roll_redirects_and_persists(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/dice", data={"notation": "3d6"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        roll = db.query(DiceRoll).filter(DiceRoll.world_id == seed.world_a.id).one()
        assert roll.notation == "3d6"
        assert 3 <= roll.total <= 18
        assert json.loads(roll.breakdown)[0]["term"] == "+3d6"
    finally:
        db.close()


def test_form_error_redirects_with_message(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/dice", data={"notation": "nope"}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_dice_page_requires_login(client, seed):
    r = client.get("/dice", follow_redirects=False)
    assert r.status_code in (303, 401)


def test_anonymous_cannot_call_roll_api(client, seed):
    r = client.post("/api/dice/roll", json={"notation": "1d6"})
    assert r.status_code in (303, 401, 403)
