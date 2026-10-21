"""The lightbox opens a large upload as its display copy and loads the ORIGINAL only when asked (Full size, a click to actual size, zooming
past the copy, or "Always open at full size" kept in this browser) - static/js/nd-base-lightbox.js. The scale maths under Node, the wiring
in base.html."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _node(expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/nd-base-lightbox.js'); console.log(JSON.stringify({expr}))"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_fit_and_actual_size():
    # a 2,560 x 1,440 display copy laid out as is, on a 1,440 x 900 window
    v = {"W": 1440, "H": 900, "w": 2560, "h": 1440, "natW": 2560, "dpr": 1}
    fit = _node(f"c.fitScale({json.dumps(v)})")
    assert fit == pytest.approx(min(1440 * 0.96 / 2560, 900 * 0.92 / 1440))
    assert _node(f"c.actualScale({json.dumps(v)})") == 1
    # the 3,840 px original loaded into the same box: actual size is 1.5x that box; on a 3x phone screen a third of it
    assert _node(f"c.actualScale({json.dumps({**v, 'natW': 3840})})") == 1.5
    assert _node(f"c.actualScale({json.dumps({**v, 'natW': 3840, 'dpr': 3})})") == pytest.approx(0.5)
    assert _node(f"c.maxScale({json.dumps({**v, 'natW': 3840})})") == 6
    small = {"W": 1440, "H": 900, "w": 400, "h": 300, "natW": 400, "dpr": 1}
    assert _node(f"c.fitScale({json.dumps(small)})") == 1, "a small picture is never blown up to fit"


@needs_node
def test_panning_keeps_the_picture_in_view():
    v = {"W": 1000, "H": 800, "w": 2000, "h": 1000, "natW": 2000, "dpr": 1}
    assert _node(f"c.clampPan({json.dumps(v)}, 0.25, 999, 999)") == [250, 275], "smaller than the window: centred"
    assert _node(f"c.clampPan({json.dumps(v)}, 1, 50, -5000)") == [0, -200], "larger: no empty edge shows"
    # zooming keeps the point under the pointer still
    tx, ty = _node(f"c.zoomAt({json.dumps(v)}, 1, -500, -100, 2, 500, 400)")
    assert (500 - tx) / 2 == pytest.approx(500 - (-500)) and (400 - ty) / 2 == pytest.approx(400 - (-100))


def test_the_lightbox_loads_the_original_only_on_request():
    base = (ROOT / "app" / "templates" / "base.html").read_text()
    js = (ROOT / "static" / "js" / "nd-base-lightbox.js").read_text()
    assert "js/nd-base-lightbox.js" in base and base.index("js/nd-base-lightbox.js") < base.index("js/nd-lightbox-bridge.js")
    for marker in ('id="lightbox-stage"', 'id="lightbox-full"', 'id="lightbox-always"', 'id="lightbox-orig"', "ndLightbox.open(src, big)", "ndLightbox.close()"):
        assert marker in base, marker
    assert 'src + "?display=1"' in js and "nd_lightbox_full" in js and "localStorage" in js
    assert "loadFull" in js and "actualScale(v) * 1.05" in js, "zooming past the copy loads the original"
