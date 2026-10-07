"""Exporting a map for other tools (static/js/map-export.js): Universal VTT and a Foundry v13 scene.

The converters are pure functions, run under Node. The strongest check is the ROUND TRIP: what the browser exports is read back
by the Python importer (app/uvtt_import.py) and must give the same walls and doors, at the exported scale."""
import base64
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import uvtt_import as U

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
MOD = str(ROOT / "static/js/map-export.js")

WALLS = [
    {"id": "a", "kind": "wall", "pts": [[100, 100], [400, 100], [400, 225]]},
    {"id": "d", "kind": "door", "state": "closed", "pts": [[400, 225], [400, 275]]},
    {"id": "o", "kind": "door", "state": "open", "pts": [[100, 250], [100, 300]]},
    {"id": "s", "kind": "secret", "state": "closed", "pts": [[200, 400], [250, 400]]},
    {"id": "w", "kind": "window", "pts": [[300, 100], [350, 100]]},
    {"id": "b", "kind": "wall", "pts": [[400, 275], [400, 400], [100, 400], [100, 300]]},
]


def _js(expr: str, **ctx):
    script = f"global.window = global; const E = require({json.dumps(MOD)}); const ctx = {json.dumps(ctx)}; console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 60, 90)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


BASE = dict(walls=WALLS, cell=50, canvasW=500, canvasH=450, ppg=70, hasGrid=True, offsetX=0, offsetY=0)


@needs_node
def test_universal_vtt_is_in_squares_with_doors_as_portals():
    r = _js("E.toUvtt(Object.assign({}, ctx, {imageB64: 'AAAA'}))", **BASE)
    f = r["file"]
    assert f["format"] == 0.3 and f["image"] == "AAAA"
    assert f["resolution"] == {"map_origin": {"x": 0, "y": 0}, "map_size": {"x": 10, "y": 9}, "pixels_per_grid": 70}
    assert [len(l) for l in f["line_of_sight"]] == [3, 2, 2, 4]                               # wall, secret (as a wall), window (as a wall), wall
    assert f["line_of_sight"][0][0] == {"x": 2, "y": 2}                                       # 100 px / 50 px per square
    closed = next(p for p in f["portals"] if p["closed"])
    assert closed["bounds"] == [{"x": 8, "y": 4.5}, {"x": 8, "y": 5.5}] and closed["position"] == {"x": 8, "y": 5}
    assert closed["rotation"] == pytest.approx(1.570796, abs=1e-6) and closed["freestanding"] is False
    assert [p["closed"] for p in f["portals"]] == [True, False]
    assert any("secret door" in w for w in r["warnings"]) and any("window" in w for w in r["warnings"])


@needs_node
def test_the_picture_size_follows_the_squares():
    r = _js("E.toUvtt(Object.assign({}, ctx, {imageB64: ''}))", **BASE)
    assert (r["width"], r["height"], r["ppg"]) == (700, 630, 70)


@needs_node
def test_a_giant_picture_is_made_smaller_with_a_warning():
    r = _js("E.toUvtt(Object.assign({}, ctx, {canvasW: 10000, canvasH: 10000, ppg: 280, imageB64: ''}))", **BASE)
    assert r["width"] * r["height"] <= 40e6 and r["ppg"] < 280 and any("smaller" in w for w in r["warnings"])


@needs_node
def test_a_map_too_big_for_any_picture_is_refused_clearly():
    script = (f"global.window = global; const E = require({json.dumps(MOD)}); try {{ E.toUvtt({{walls: [], cell: 50, canvasW: 20000, canvasH: 20000, ppg: 70}}); console.log('no error') }} "
              f"catch (e) {{ console.log(e.message) }}")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60).stdout
    assert "too large to export" in out


@needs_node
def test_an_offset_or_missing_grid_is_reported():
    r = _js("E.toUvtt(Object.assign({}, ctx, {offsetX: 20, hasGrid: false, imageB64: ''}))", **BASE)
    assert any("offset" in w for w in r["warnings"]) and any("no square grid" in w for w in r["warnings"])


@needs_node
def test_junk_walls_and_points_are_skipped_not_exported():
    junk = [{"kind": "wall", "pts": [[1, 1]]}, None, {"pts": "x"}, {"kind": "wall", "pts": [[0, 0], [None, 5], ["a", 1], [100, 0]]},
            {"kind": "door", "pts": [[0, 0], [50, 0]]}]
    r = _js("E.toUvtt(Object.assign({}, ctx, {walls: ctx.junk, imageB64: ''}))", **{**BASE, "junk": junk})
    assert r["file"]["line_of_sight"] == [[{"x": 0, "y": 0}, {"x": 2, "y": 0}]] and len(r["file"]["portals"]) == 1
    json.dumps(r["file"], allow_nan=False)                                                    # no NaN / Infinity anywhere


# ── the round trip through the importer ──────────────────────────────────────

@needs_node
def test_what_the_browser_exports_the_importer_reads_back():
    r = _js("E.toUvtt(Object.assign({}, ctx, {imageB64: ctx.img}))", **BASE, img=_png(700, 630))
    back = U.parse_uvtt(json.dumps(r["file"]).encode())
    assert (back["width"], back["height"], back["cell"]) == (700, 630, 70.0)
    k = 70 / 50
    doors = [w for w in back["walls"] if w["kind"] == "door"]
    assert [(w["state"], w["pts"]) for w in doors] == [
        ("closed", [[400 * k, 225 * k], [400 * k, 275 * k]]), ("open", [[100 * k, 250 * k], [100 * k, 300 * k]])]
    walls = [w for w in back["walls"] if w["kind"] == "wall"]
    assert walls[0]["pts"] == [[100 * k, 100 * k], [400 * k, 100 * k], [400 * k, 225 * k]]
    assert len(walls) == 4                                                                    # wall, window-as-wall, secret-as-wall, wall


# ── Foundry ──────────────────────────────────────────────────────────────────

@needs_node
def test_the_foundry_scene_obeys_the_v13_field_rules():
    r = _js("(()=>{const s = E.toFoundryScene(Object.assign({}, ctx, {imageSrc: 'nd-world/m.png', name: 'Keep'})); return {s, bad: E.validateScene(s.scene)}})()", **BASE)
    sc = r["s"]["scene"]
    assert r["bad"] == [] and sc["name"] == "Keep" and sc["background"]["src"] == "nd-world/m.png"
    assert (sc["width"], sc["height"], sc["grid"]["size"], sc["grid"]["type"]) == (700, 630, 70, 1)
    walls = sc["walls"]
    assert all(all(isinstance(v, int) for v in w["c"]) and w["move"] in (0, 20) for w in walls)
    door = next(w for w in walls if w["door"] == 1 and w["ds"] == 0)
    assert door["c"] == [560, 315, 560, 385]                                                  # 400,225-275 px * 1.4
    assert any(w["door"] == 1 and w["ds"] == 1 for w in walls)                                # the open door
    assert any(w["door"] == 2 for w in walls)                                                 # Foundry HAS secret doors
    window = next(w for w in walls if w["sight"] == 0)
    assert window["move"] == 20 and window["light"] == 0                                      # walk-blocking, see-through
    st = r["s"]["stats"]
    assert st["doors"] == 2 and st["secretDoors"] == 1 and st["windows"] == 1


@needs_node
def test_the_checker_catches_what_foundry_would_refuse():
    bad = _js("E.validateScene({grid:{size:10,type:9,distance:0}, width:0, height:5, walls:[{c:[1.5,2,3,4], move:10, door:5}]})")
    text = " ".join(bad)
    assert "grid.size" in text and "grid.type" in text and "grid.distance" in text and "width" in text
    assert "4 integers" in text and "move = 10" in text and "door = 5" in text


# ── lights ───────────────────────────────────────────────────────────────────

LIGHTS = [{"id": "t", "x": 250, "y": 150, "range": 4, "color": "#ff9933", "intensity": 0.8, "on": True},
          {"id": "off", "x": 50, "y": 50, "range": 2, "on": False}, {"id": "bad", "x": None, "y": 1, "range": 3}]


@needs_node
def test_universal_vtt_carries_the_lights_that_are_on_and_the_darkness():
    f = _js("E.toUvtt(Object.assign({}, ctx, {imageB64: ''})).file", **BASE, lights=LIGHTS, darkness=0.85)
    assert f["lights"] == [{"position": {"x": 5, "y": 3}, "range": 4, "intensity": 0.8, "color": "ffff9933", "shadows": True}]
    assert f["environment"]["ambient_light"] == "ff262626"                                       # 15% of full brightness
    plain = _js("E.toUvtt(Object.assign({}, ctx, {imageB64: ''})).file", **BASE)
    assert plain["lights"] == [] and plain["environment"]["ambient_light"] == "ffffffff"


@needs_node
def test_foundry_gets_every_light_with_integer_positions_and_the_darkness_level():
    r = _js("(()=>{const s = E.toFoundryScene(Object.assign({}, ctx, {imageSrc: 'x.png'})); return {s, bad: E.validateScene(s.scene)}})()", **BASE, lights=LIGHTS, darkness=0.6)
    sc = r["s"]["scene"]
    assert r["bad"] == [] and len(sc["lights"]) == 2 and sc["environment"]["darknessLevel"] == 0.6
    torch = sc["lights"][0]
    assert (torch["x"], torch["y"]) == (350, 210) and torch["hidden"] is False and torch["config"]["dim"] == 20 and torch["config"]["bright"] == 10   # 4 squares * 5 ft
    assert torch["config"]["color"] == "#ff9933" and isinstance(torch["config"]["shadows"], (int, float))
    assert sc["lights"][1]["hidden"] is True                                                    # a light that is off stays in the scene, hidden
    assert r["s"]["stats"]["lights"] == 2


@needs_node
def test_the_round_trip_keeps_lights_visible_to_the_importer():
    r = _js("E.toUvtt(Object.assign({}, ctx, {imageB64: ctx.img}))", **BASE, lights=LIGHTS, darkness=0.5, img=_png(700, 630))
    back = U.parse_uvtt(json.dumps(r["file"]).encode())
    assert back["lights"] and back["lights"][0]["color"] == "#ff9933" and back["lights"][0]["x"] == 350 and back["lights"][0]["range"] == 4
    assert back["darkness"] == pytest.approx(0.5, abs=0.01)
