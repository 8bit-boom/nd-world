"""Dice thrown on the 3D tray (static/js/dice-tray.js) are logged through POST /api/dice/record.

The browser reports what the dice SHOWED; the server checks it against the notation (the right number of dice, each
within its range) and stores it in the same shape as a server-side roll, so the roll log, history and everything that
reads them cannot tell the two apart."""
import json

import pytest

from app.database import SessionLocal
from app.models import DiceRoll
from app.routers.dice import parse_and_roll, parse_recorded

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _login_player_in(client, seed, world):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", world.slug)


# ── the parser that checks thrown values ─────────────────────────────────────────────────────────────

def test_recorded_values_are_totalled_like_a_server_roll():
    breakdown, total = parse_recorded("2d6+3", [4, 5])
    assert total == 12
    assert breakdown == [{"term": "+2d6", "rolls": [4, 5], "sum": 9}, {"term": "+3", "sum": 3}]


def test_recorded_values_follow_the_notation_order_and_signs():
    breakdown, total = parse_recorded("d20-1d4+2d8", [15, 3, 6, 7])
    assert [b["term"] for b in breakdown] == ["+1d20", "-1d4", "+2d8"]
    assert total == 15 - 3 + 13


def test_a_percentile_die_takes_a_value_from_1_to_100():
    _, total = parse_recorded("d100+5", [100])
    assert total == 105
    _, total = parse_recorded("1d100", [1])
    assert total == 1


def test_a_recorded_roll_looks_exactly_like_a_server_roll_for_the_same_notation():
    for text in ("d20", "2d6+3", "4d8+2d6+1", "3d4-2", "d100"):
        rolled, _ = parse_and_roll(text)
        values = [r for b in rolled for r in b.get("rolls", [])]
        recorded, total = parse_recorded(text, values)
        assert recorded == rolled
        assert total == sum(b["sum"] for b in rolled)


@pytest.mark.parametrize("notation,values", [
    ("2d6", [3]),                    # too few
    ("2d6", [3, 4, 5]),              # too many
    ("2d6", [3, 7]),                 # a d6 cannot show 7
    ("2d6", [0, 3]),                 # nor 0
    ("1d6", [-2]),
    ("1d100", [101]),
    ("1d20", [True]),                # a boolean is not a die face
    ("1d20", [3.5]),
    ("1d20", ["12"]),
    ("1d20", [None]),
    ("1d20", "12"),                  # not a list
    ("1d20", None),
    ("3", [1]),                      # a flat number takes no dice
    ("banana", [1]),
    ("", []),
])
def test_values_that_do_not_fit_the_notation_are_rejected(notation, values):
    with pytest.raises(ValueError):
        parse_recorded(notation, values)


def test_an_absurd_number_of_values_is_rejected_before_anything_else():
    with pytest.raises(ValueError):
        parse_recorded("1d6", [1] * 5000)


# ── the route ────────────────────────────────────────────────────────────────────────────────────────

def test_gm_records_a_thrown_roll_and_it_lands_in_the_shared_history(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/dice/record", json={"notation": "2d6+3", "values": [4, 5]})
    assert r.status_code == 200
    body = r.json()
    assert body["user_name"] == "GM" and body["total"] == 12 and body["notation"] == "2d6+3"
    assert body["breakdown"][0] == {"term": "+2d6", "rolls": [4, 5], "sum": 9}
    hist = client.get("/api/dice/history").json()["rolls"]
    assert len(hist) == 1 and hist[0]["total"] == 12 and hist[0]["id"] == body["id"]


def test_a_player_can_record_a_throw_too(client, seed):
    _login_player_in(client, seed, seed.world_a)
    r = client.post("/api/dice/record", json={"notation": "d20", "values": [17]})
    assert r.status_code == 200 and r.json()["user_name"] == "Player A" and r.json()["total"] == 17


def test_the_stored_row_has_the_same_shape_as_a_server_roll(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/api/dice/record", json={"notation": "3d6", "values": [1, 2, 3]})
    db = SessionLocal()
    try:
        roll = db.query(DiceRoll).filter(DiceRoll.world_id == seed.world_a.id).one()
        assert roll.total == 6 and roll.notation == "3d6"
        assert json.loads(roll.breakdown) == [{"term": "+3d6", "rolls": [1, 2, 3], "sum": 6}]
    finally:
        db.close()


@pytest.mark.parametrize("payload", [
    {"notation": "2d6", "values": [3]},
    {"notation": "2d6", "values": [3, 9]},
    {"notation": "banana", "values": [3]},
    {"notation": "d20", "values": []},
    {"notation": "d20"},
    {"values": [3]},
])
def test_the_route_refuses_values_that_do_not_fit(client, seed, payload):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/dice/record", json=payload)
    assert r.status_code in (400, 422)
    assert client.get("/api/dice/history").json()["rolls"] == []


def test_a_recorded_roll_is_world_scoped(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/api/dice/record", json={"notation": "d6", "values": [4]})
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    assert client.get("/api/dice/history").json()["rolls"] == []


def test_anonymous_cannot_record(client, seed):
    r = client.post("/api/dice/record", json={"notation": "d6", "values": [4]})
    assert r.status_code in (303, 401, 403)
