"""Universal VTT import (app/uvtt_import.py, POST /maps/schematic/import-uvtt).

The fixture is a REAL Dungeondraft 1.0.1.3 export (its picture stripped for size; the test puts one back), so the field
facts are the format's, not guesses: a non-zero map_origin, absolute coordinates, a portal per door, objects_line_of_sight.
Hostile and sloppy files matter as much: this is a file from the internet that every player's browser will draw."""
import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app import uvtt_import as U
from app.database import SessionLocal
from app.models import Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

FIXTURE = Path(__file__).resolve().parent.parent / "docs/map-creator-research/prototypes/fixtures/dungeondraft-1.0.1.3-sample.uvtt.json"


def _png_b64(w=640, h=640, colour=(40, 90, 60)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _sample(**over):
    d = json.loads(FIXTURE.read_text())
    d["image"] = _png_b64()                                   # 10 squares at 64 px
    d["resolution"]["pixels_per_grid"] = 64
    d.update(over)
    return d


def _raw(d):
    return json.dumps(d).encode()


# ── the real sample ──────────────────────────────────────────────────────────

def test_a_real_dungeondraft_file_becomes_walls_doors_and_a_grid():
    r = U.parse_uvtt(_raw(_sample()))
    assert (r["width"], r["height"], r["cell"]) == (640, 640, 64.0)
    kinds = [w["kind"] for w in r["walls"]]
    assert kinds.count("door") == 2 and kinds.count("wall") >= 5            # walls + object outline + two portals
    # coordinates are relative to the map: the origin (2, 1) is removed and squares become pixels
    door = next(w for w in r["walls"] if w["kind"] == "door")
    assert door["pts"] == [[(7 - 2) * 64, (2.5 - 1) * 64], [(7 - 2) * 64, (3.5 - 1) * 64]] and door["state"] == "closed"
    first = r["walls"][0]
    assert first["pts"][0] == [(7 - 2) * 64, (2 - 1) * 64]
    assert any("light" in w for w in r["warnings"]) and any("Windows are doors" in w for w in r["warnings"])
    assert r["stats"]["lights_skipped"] == 2 and r["stats"]["doors"] == 2


def test_everything_stays_on_the_picture():
    r = U.parse_uvtt(_raw(_sample()))
    for w in r["walls"]:
        for x, y in w["pts"]:
            assert -1 <= x <= r["width"] + 1 and -1 <= y <= r["height"] + 1, w


def test_a_picture_that_disagrees_with_the_header_wins():
    r = U.parse_uvtt(_raw(_sample(image=_png_b64(1280, 1280))))              # twice the header's pixels
    assert r["cell"] == 128.0 and any("used as is" in w for w in r["warnings"])


def test_a_huge_picture_is_scaled_down_with_its_walls():
    big = _sample(image=_png_b64(24000, 400))
    big["resolution"]["map_size"] = {"x": 300, "y": 5}
    big["resolution"]["map_origin"] = {"x": 0, "y": 0}
    big["line_of_sight"] = [[{"x": 0, "y": 0}, {"x": 300, "y": 0}]]
    big["portals"] = []
    r = U.parse_uvtt(_raw(big))
    assert r["width"] == 20000 and r["walls"][0]["pts"][1][0] == pytest.approx(20000, abs=1) and any("scaled down" in w for w in r["warnings"])


# ── refusals ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"{}", b'{"resolution": 5}', b"\xff\xfe\x00"])
def test_garbage_is_refused(raw):
    with pytest.raises(U.UvttError):
        U.parse_uvtt(raw)


@pytest.mark.parametrize("patch", [
    {"image": ""}, {"image": None}, {"image": "!!!not base64!!!"}, {"image": base64.b64encode(b"hello").decode()},
])
def test_a_missing_or_broken_picture_is_refused(patch):
    with pytest.raises(U.UvttError):
        U.parse_uvtt(_raw(_sample(**patch)))


@pytest.mark.parametrize("res", [
    {"map_size": {"x": 0, "y": 5}, "pixels_per_grid": 64}, {"map_size": {"x": 10, "y": 10}, "pixels_per_grid": 0},
    {"map_size": {"x": 10, "y": 10}, "pixels_per_grid": 1e9}, {"map_size": {"x": 1e9, "y": 10}, "pixels_per_grid": 64},
    {"map_size": "10", "pixels_per_grid": 64}, {"pixels_per_grid": 64},
])
def test_nonsense_resolution_is_refused(res):
    with pytest.raises(U.UvttError):
        U.parse_uvtt(_raw(_sample(resolution=res)))


def test_non_finite_and_junk_coordinates_are_skipped_not_trusted():
    d = _sample()
    d["line_of_sight"] = [[{"x": 2, "y": 1}, {"x": 5, "y": 1}], [{"x": "a", "y": 1}, {"x": 2, "y": 2}], "junk", [{"x": 3}], [{"x": 1e999, "y": 0}, {"x": 2, "y": 2}]]
    d["objects_line_of_sight"] = "no"
    d["portals"] = [None, {"bounds": [1]}, {"bounds": [{"x": 4, "y": 4}, {"x": 4, "y": 4}]}, {"bounds": [{"x": 4, "y": 4}, {"x": 5, "y": 4}], "closed": False}]
    r = U.parse_uvtt(json.dumps(d).replace("1e999", "Infinity").encode())          # json.dumps would refuse; a hostile file need not
    assert [w["kind"] for w in r["walls"]] == ["wall", "door"] and r["walls"][1]["state"] == "open"
    assert any("objects_line_of_sight" in w for w in r["warnings"])


def test_counts_are_capped():
    d = _sample()
    d["line_of_sight"] = [[{"x": 2 + i % 5, "y": 1}, {"x": 3 + i % 5, "y": 2}] for i in range(U.MAX_POLYLINES + 50)]
    r = U.parse_uvtt(_raw(d))
    assert len(r["walls"]) <= U.MAX_POLYLINES and any("limits" in w for w in r["warnings"])


def test_the_stored_picture_is_a_fresh_webp():
    r = U.parse_uvtt(_raw(_sample()))
    out = U.encode_image(r["image"])
    assert Image.open(io.BytesIO(out)).format == "WEBP"


# ── the route ────────────────────────────────────────────────────────────────

def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _post(client, d, name="", filename="Cave of Echoes.dd2vtt"):
    return client.post("/maps/schematic/import-uvtt", files={"file": (filename, _raw(d), "application/json")}, data={"name": name})


def test_import_creates_a_playable_schematic(client, seed):
    _gm(client, seed)
    r = _post(client, _sample())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["slug"] == "cave-of-echoes" and d["url"] == "/maps/schematic/cave-of-echoes" and d["stats"]["doors"] == 2
    db = SessionLocal()
    try:
        s = db.query(Schematic).filter(Schematic.slug == "cave-of-echoes").first()
        assert (s.canvas_width, s.canvas_height, s.grid_type) == (640, 640, "square")
        assert json.loads(s.grid_config_json)["cell_size"] == 64.0
        walls = json.loads(s.walls_json)
        assert {w["kind"] for w in walls} == {"wall", "door"} and json.loads(s.fog_json)["enabled"] is False
    finally:
        db.close()
    assert client.get("/uploads/schematics/cave-of-echoes.webp").status_code == 200
    assert client.get("/maps/schematic/cave-of-echoes").status_code == 200                     # the editor opens it
    # a second import with the same name gets its own slug
    assert _post(client, _sample()).json()["slug"] == "cave-of-echoes-2"


def test_import_refuses_bad_files_with_a_clear_message_and_leaves_nothing_behind(client, seed):
    _gm(client, seed)
    r = _post(client, _sample(image=""))
    assert r.status_code == 400 and "no embedded picture" in r.json()["detail"]
    assert client.post("/maps/schematic/import-uvtt", files={"file": ("x.dd2vtt", b"nope", "application/json")}).status_code == 400
    assert client.post("/maps/schematic/import-uvtt").status_code == 422
    db = SessionLocal()
    try:
        assert db.query(Schematic).filter(Schematic.name.like("%Echoes%")).count() == 0
    finally:
        db.close()


def test_players_cannot_import(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _post(client, _sample()).status_code == 403
