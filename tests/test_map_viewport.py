"""Zoom / pan / pinch maths for the live map (static/js/map-viewport.js), run under Node.

The properties that matter on a phone: the view never leaves the canvas, zooming keeps the point under the finger
still, pinching zooms and pans together, and nothing ever becomes NaN whatever the browser reports."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
MOD = str(ROOT / "static/js/map-viewport.js")


def _js(expr: str):
    script = f"global.window = global; const V = require({json.dumps(MOD)}); console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


B = "{w:2000,h:1500}"


@needs_node
def test_fit_shows_the_whole_canvas():
    assert _js(f"V.fit({B})") == {"x": 0, "y": 0, "w": 2000, "h": 1500}


@needs_node
def test_zoom_keeps_the_point_under_the_cursor_fixed():
    v = _js(f"V.zoomAt(V.fit({B}), {B}, 2, 1000, 750)")
    assert v["w"] == 1000 and v["h"] == 750
    # canvas point (1000, 750) sits in the middle of the window both before and after
    assert v["x"] + v["w"] / 2 == pytest.approx(1000) and v["y"] + v["h"] / 2 == pytest.approx(750)
    corner = _js(f"V.zoomAt(V.fit({B}), {B}, 4, 400, 300)")
    assert (400 - corner["x"]) / corner["w"] == pytest.approx(400 / 2000)
    assert (300 - corner["y"]) / corner["h"] == pytest.approx(300 / 1500)


@needs_node
def test_zoom_is_limited_both_ways_and_the_view_stays_inside_the_canvas():
    zin = _js(f"V.zoomAt(V.fit({B}), {B}, 1e9, 1990, 1490)")
    assert zin["w"] == pytest.approx(2000 / 12)
    assert 0 <= zin["x"] <= 2000 - zin["w"] and 0 <= zin["y"] <= 1500 - zin["h"]
    assert _js(f"V.zoomAt(V.fit({B}), {B}, 1e-9, 5, 5)") == {"x": 0, "y": 0, "w": 2000, "h": 1500}


@needs_node
def test_pan_moves_by_screen_pixels_and_stops_at_the_edges():
    start = f"V.zoomAt(V.fit({B}), {B}, 2, 1000, 750)"  # w=1000 h=750 at x=500 y=375
    moved = _js(f"V.panBy({start}, {B}, -100, 0, {{w:500,h:375}})")  # 0.5 px per canvas... scale = 0.5
    assert moved["x"] == pytest.approx(500 + 200)
    far = _js(f"V.panBy({start}, {B}, 1e7, 1e7, {{w:500,h:375}})")
    assert far["x"] == 0 and far["y"] == 0
    far = _js(f"V.panBy({start}, {B}, -1e7, -1e7, {{w:500,h:375}})")
    assert far["x"] == 1000 and far["y"] == 750


@needs_node
def test_pinch_zooms_by_the_change_in_distance_and_follows_the_midpoint():
    screen = "{w:400,h:300}"
    prev = "[{x:150,y:150},{x:250,y:150}]"
    spread = "[{x:100,y:150},{x:300,y:150}]"   # twice as far apart, same midpoint
    v = _js(f"V.pinch(V.fit({B}), {B}, {prev}, {spread}, {screen})")
    assert v["w"] == pytest.approx(1000)
    drag = f"V.pinch(V.zoomAt(V.fit({B}), {B}, 2, 1000, 750), {B}, {prev}, [{{x:170,y:150}},{{x:270,y:150}}], {screen})"
    start = _js(f"V.zoomAt(V.fit({B}), {B}, 2, 1000, 750)")
    moved = _js(drag)
    assert moved["w"] == pytest.approx(start["w"]) and moved["x"] < start["x"]  # fingers moved right, map follows


@needs_node
def test_nothing_ever_becomes_not_a_number():
    for call in (
        f"V.zoomAt({{x:NaN,y:Infinity,w:-5,h:0}}, {B}, NaN, NaN, undefined)",
        f"V.panBy(null, {B}, NaN, Infinity, null)",
        f"V.pinch(undefined, {B}, [{{x:1,y:1}},{{x:1,y:1}}], [{{x:NaN,y:0}},{{x:5,y:Infinity}}], {{w:0,h:0}})",
        f"V.constrain({{}}, {{w:0,h:NaN}})",
        f"V.centerOn(V.fit({B}), {B}, 'a', null)",
    ):
        v = _js(call)
        assert all(isinstance(v[k], (int, float)) and v[k] == v[k] and abs(v[k]) < 1e12 for k in "xywh"), (call, v)


@needs_node
def test_center_on_a_token_stays_inside_and_to_canvas_inverts_the_mapping():
    z = f"V.zoomAt(V.fit({B}), {B}, 4, 1000, 750)"
    c = _js(f"V.centerOn({z}, {B}, 0, 0)")
    assert c["x"] == 0 and c["y"] == 0
    # a view 500x375 shown on a 400x300 screen: scale 0.8; the screen centre is the view centre
    p = _js("V.toCanvas({x:100,y:200,w:500,h:375}, {w:400,h:300}, 200, 150)")
    assert p == {"x": 350, "y": 387.5}


@needs_node
def test_css_pixels_per_inch_from_the_diagonal():
    # a 1920x1080 panel of 43 inches: 2203 px diagonal / 43 = ~51.2 px per inch
    assert _js("V.cssPerInch(1920, 1080, 43)") == pytest.approx(51.23, abs=0.01)
    for bad in ("V.cssPerInch(0, 1080, 43)", "V.cssPerInch(1920, 1080, 0)", "V.cssPerInch(1920, 1080, 1)",
                "V.cssPerInch(NaN, 1080, 43)", "V.cssPerInch(1920, 1080, 9999)", "V.cssPerInch(1920, 1080, 'x')"):
        assert _js(bad) is None, bad


@needs_node
def test_true_scale_one_square_is_one_inch():
    # 50-unit squares, 100 px per inch, a 1000x500 px window: it shows 10 x 5 squares = 500 x 250 units
    v = _js(f"V.trueScale({B}, {{w:1000,h:500}}, 50, 100, 1000, 750)")
    assert v["w"] == pytest.approx(500) and v["h"] == pytest.approx(250)
    assert v["x"] == pytest.approx(750) and v["y"] == pytest.approx(625)
    # near the edge the view stays on the map
    corner = _js(f"V.trueScale({B}, {{w:1000,h:500}}, 50, 100, 0, 0)")
    assert corner["x"] == 0 and corner["y"] == 0
    # a map smaller than the screen is centred, not stretched: 400x300 canvas, view 500x250 wide
    small = _js("V.trueScale({w:400,h:300}, {w:1000,h:500}, 50, 100, 200, 150)")
    assert small["w"] == pytest.approx(500) and small["x"] == pytest.approx(-50)
    assert small["y"] == pytest.approx(25)
    # nonsense falls back to showing the whole map
    assert _js(f"V.trueScale({B}, {{w:1000,h:500}}, 0, 100, 1, 1)") == {"x": 0, "y": 0, "w": 2000, "h": 1500}
    assert _js(f"V.trueScale({B}, {{w:1000,h:500}}, 50, NaN, 1, 1)") == {"x": 0, "y": 0, "w": 2000, "h": 1500}
