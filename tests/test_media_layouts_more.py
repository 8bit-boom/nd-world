"""The layouts the view-layouts plan left for later (docs/LEFTOVERS_PLAN.md, step 15): the audio table, the video theater, and two picture
layouts - justified rows and compare. Registry entries, the page wiring, and the pure helpers under Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import view_prefs

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _node(module, expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/{module}'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_registry_offers_the_new_layouts():
    modes = lambda sec: [m["id"] for m in view_prefs.spec(sec)["modes"]]
    assert modes("audio")[-1] == "table" and modes("video")[-1] == "theater"
    assert modes("images")[-2:] == ["justified", "compare"] and modes("album")[-2:] == ["justified", "compare"]
    labels = {m["id"]: m["label"] for m in view_prefs.spec("images")["modes"]}
    assert labels["justified"] == "Justified rows" and labels["compare"] == "Compare"
    assert view_prefs.clean("video", {"mode": "theater"})["mode"] == "theater"
    assert view_prefs.clean("audio", {"mode": "theater"})["mode"] == "rows", "a layout of another page is not accepted"


@needs_node
def test_audio_table_helpers():
    assert _node("audio-views.js", "[0, 512, 2048, 350000, 1048576, 3670016, 10485760, 128974848].map(c.fmtSize)") == \
        ["0 B", "512 B", "2 KB", "342 KB", "1.0 MB", "3.5 MB", "10 MB", "123 MB"]
    assert _node("audio-views.js", "[c.headingSort('name','name'), c.headingSort('-name','name'), c.headingSort('name','size'), c.headingSort('name','date'), c.headingSort('-date','date'), c.headingSort('date','date')]") == \
        ["-name", "name", "size", "-date", "date", "-date"]


@needs_node
def test_video_theater_steps_wrap():
    assert _node("video-views.js", "[c.stepIndex(0, 3, 1), c.stepIndex(2, 3, 1), c.stepIndex(0, 3, -1), c.stepIndex(-1, 3, 1), c.stepIndex(-1, 3, -1), c.stepIndex(0, 0, 1)]") == [1, 0, 2, 0, 2, -1]


def test_the_pages_load_the_scripts_and_the_styles_exist():
    assert "js/video-views.js" in (ROOT / "app" / "templates" / "video_library.html").read_text()
    css = (ROOT / "static" / "style.css").read_text()
    for sel in ('[data-view="table"] .audio-clip-row', ".av-thead", '[data-view="theater"]', ".vv-stage", '[data-view="justified"]', "--nd-ar", ".nd-cmp-panes"):
        assert sel in css, sel
    js = (ROOT / "static" / "js" / "nd-views.js").read_text()
    assert "function fitAspect" in js and "function cmpPick" in js and "set: change" in js
