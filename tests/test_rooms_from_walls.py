"""Finding the rooms in a map's walls (app/rooms_from_walls.py): what makes a map imported from Universal VTT furnishable.

Enclosed areas become rooms, the open outside does not, a door or window on the line between two areas becomes a mark on that edge,
and a map whose walls close nothing says so instead of inventing rooms."""
import json

import pytest

from app import map_rooms, rooms_from_walls as R
from app.database import SessionLocal
from app.models import Schematic

from .conftest import GM_PASSWORD, login


def W(i, pts, kind="wall"):
    return {"id": i, "pts": pts, "kind": kind}


# a building (100,100)-(450,250) on a 50 px grid: two rooms (4x3 and 3x3 squares) with a door between and a window in the north wall
BUILDING = [W("t", [[100, 100], [450, 100]]), W("b", [[100, 250], [450, 250]]), W("l", [[100, 100], [100, 250]]), W("r", [[450, 100], [450, 250]]),
            W("m1", [[300, 100], [300, 160]]), W("m2", [[300, 210], [300, 250]]), W("d", [[300, 160], [300, 210]], "door"), W("win", [[200, 100], [250, 100]], "window")]


def test_two_rooms_the_outside_is_left_out_and_marks_land_on_the_right_edges():
    st, info = R.detect(BUILDING, 600, 400, 50)
    assert info["rooms"] == 2 and info["doors"] == 1 and info["windows"] == 1 and st["clear"] is True
    clean, _ = map_rooms.clean_rooms(st)                                         # valid stored form
    assert len(clean["spaces"]) == 2 and clean["cols"] == 12 and clean["rows"] == 8
    ids = []
    for i in range(0, len(clean["cells"]), 2):
        ids += [clean["cells"][i]] * clean["cells"][i + 1]
    at = lambda c, r: ids[r * clean["cols"] + c]
    assert at(3, 3) != 0 and at(3, 3) != at(7, 3) and at(7, 3) != 0 and at(0, 0) == 0         # two rooms, outside empty
    assert sum(1 for v in ids if v == at(3, 3)) == 12 and sum(1 for v in ids if v == at(7, 3)) == 9
    assert [6, 3, "v", "door"] in clean["marks"] and [4, 2, "h", "window"] in clean["marks"]


def test_nothing_enclosed_and_nothing_to_read_are_reported():
    open_walls = [W("a", [[100, 100], [400, 100]]), W("b", [[100, 100], [100, 300]])]
    st, why = R.detect(open_walls, 600, 400, 50)
    assert st is None and "No enclosed rooms" in why
    assert R.detect([], 600, 400, 50)[0] is None and "no walls" in R.detect([], 600, 400, 50)[1]
    assert R.detect(BUILDING, 100000, 100000, 50)[0] is None                       # too many squares for the tool


def test_a_building_that_fills_the_map_is_not_mistaken_for_the_outside():
    box = [W("t", [[0, 0], [600, 0]]), W("b", [[0, 400], [600, 400]]), W("l", [[0, 0], [0, 400]]), W("r", [[600, 0], [600, 400]]), W("m", [[300, 0], [300, 400]])]
    st, info = R.detect(box, 600, 400, 50)
    assert info["rooms"] == 2                                                       # both halves touch the border, neither is >50% alone... each is exactly half
    assert info["outside_squares"] == 0


def test_the_grid_offset_is_respected():
    shifted = [{**w, "pts": [[x + 20, y + 10] for x, y in w["pts"]]} for w in BUILDING]
    st, info = R.detect(shifted, 600, 400, 50, 20, 10)
    assert info["rooms"] == 2 and (st["ox"], st["oy"]) == (20, 10)


def test_hostile_and_sloppy_walls_do_not_crash():
    junk = [{"pts": [[1, 1]]}, {"kind": "wall"}, W("x", [[0, 0], [0, 0]]), W("n", [[float("inf"), 0], [5, 5]])]
    R.detect([j for j in junk if isinstance(j.get("pts"), list)], 600, 400, 50)    # must not raise


# ── the route ────────────────────────────────────────────────────────────────

def _map(seed, grid=True):
    db = SessionLocal()
    try:
        m = Schematic(world_id=seed.world_a.id, name="Imported", slug="imp", is_html=False, canvas_width=600, canvas_height=400, elements_json="[]",
                      walls_json=json.dumps(BUILDING), grid_type="square" if grid else "none",
                      grid_config_json=json.dumps({"cell_size": 50, "offset_x": 0, "offset_y": 0}) if grid else "{}")
        db.add(m)
        db.commit()
    finally:
        db.close()


def test_the_route_finds_rooms_and_needs_a_square_grid(client, seed):
    _map(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/schematic/imp/rooms/detect")
    assert r.status_code == 200 and r.json()["info"]["rooms"] == 2 and r.json()["state"]["clear"] is True
    db = SessionLocal()
    try:
        assert not (db.query(Schematic).filter(Schematic.slug == "imp").first().rooms_json or "").strip("{} ")      # nothing stored until confirmed
    finally:
        db.close()
    db = SessionLocal()
    try:
        m = db.query(Schematic).filter(Schematic.slug == "imp").first()
        m.grid_type = "none"
        db.commit()
    finally:
        db.close()
    assert client.post("/maps/schematic/imp/rooms/detect").status_code == 400
