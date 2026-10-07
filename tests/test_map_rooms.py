"""The room-drawing tool's stored state (app/map_rooms.py, routes in app/routers/map_walls.py)."""
import json

import pytest

from app import map_rooms as R
from app.database import SessionLocal
from app.models import Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _state(**over):
    d = {"cell": 50, "ox": 0, "oy": 0, "cols": 4, "rows": 3, "cells": [0, 1, 1, 3, 2, 2, 0, 6],
         "spaces": [{"id": 1, "name": "Hall", "kind": "hall"}, {"id": 2, "name": "Cell", "kind": "cell"}],
         "marks": [[2, 1, "h", "door"], [1, 1, "v", "window"]]}
    d.update(over)
    return d


def test_a_good_state_is_kept_and_normalised():
    s, w = R.clean_rooms(_state())
    assert s["cols"] == 4 and s["rows"] == 3 and s["cells"] == [0, 1, 1, 3, 2, 2, 0, 6] and len(s["marks"]) == 2 and w == []


def test_nothing_drawn_is_the_empty_state():
    for empty in (None, {}, []):
        assert R.clean_rooms(empty) == ({}, [])


@pytest.mark.parametrize("bad", [
    "x", 5, {"cell": 50, "cols": 4, "rows": 3}, _state(cell=1), _state(cell=1e9), _state(cols=0), _state(cols=500, rows=500),
    _state(cols=True), _state(cells=[1, 5]), _state(cells=[1]), _state(cells=[1, 100]), _state(cells=[-1, 12]), _state(cells=[1, 0, 1, 12]),
    _state(cells="no"), _state(cell=float("nan")),
])
def test_a_state_that_does_not_add_up_is_refused(bad):
    with pytest.raises(ValueError):
        R.clean_rooms(bad)


def test_unknown_rooms_junk_marks_and_empty_rooms_are_dropped():
    s, _ = R.clean_rooms(_state(cells=[7, 2, 1, 10], spaces=[{"id": 1, "name": "  A   very long " + "x" * 200, "kind": "dragon"}, {"id": 2, "name": "No floor"}, {"id": 1}, "x"],
                                marks=[[0, 0, "h", "door"], [0, 0, "h", "window"], [9, 9, "h", "door"], [3, 0, "v", "wizard"], [4, 1, "v", "door"], [1, 3, "h", "door"], [0, 3, "v", "door"], "x", [1, 1]]))
    assert s["cells"] == [0, 2, 1, 10]                                    # the cells of room 7 (which does not exist) are empty
    assert [sp["id"] for sp in s["spaces"]] == [1] and len(s["spaces"][0]["name"]) <= R.MAX_NAME and s["spaces"][0]["kind"] == "other"
    assert s["marks"] == [[0, 0, "h", "door"], [4, 1, "v", "door"], [1, 3, "h", "door"]]     # in bounds, first wins, known kinds


def test_adjacent_runs_merge():
    s, _ = R.clean_rooms(_state(cells=[0, 2, 0, 3, 1, 7]))
    assert s["cells"] == [0, 5, 1, 7]


# ── routes ───────────────────────────────────────────────────────────────────

def _map(seed, slug="rooms-map"):
    db = SessionLocal()
    try:
        db.add(Schematic(world_id=seed.world_a.id, name="Dungeon", slug=slug, is_html=False, elements_json="[]"))
        db.commit()
        return slug
    finally:
        db.close()


def test_rooms_round_trip_and_permissions(client, seed):
    slug = _map(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/maps/schematic/{slug}/rooms.json").status_code == 403
    assert client.post(f"/maps/schematic/{slug}/rooms", json=_state()).status_code == 403
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/maps/schematic/{slug}/rooms.json").json() == {}
    r = client.post(f"/maps/schematic/{slug}/rooms", json=_state())
    assert r.status_code == 200 and r.json()["rooms"]["cols"] == 4
    assert client.get(f"/maps/schematic/{slug}/rooms.json").json()["spaces"][0]["name"] == "Hall"
    assert client.post(f"/maps/schematic/{slug}/rooms", json=_state(cells=[1, 5])).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/rooms", content="{bad", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/rooms", json={}).json()["rooms"] == {}          # clearing
    assert client.get("/maps/schematic/nope/rooms.json").status_code == 404
