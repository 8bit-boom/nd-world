"""Journal dictation (static/js/nd-dictate.js, under Node): how spoken text is added to what is already written."""
import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")


def _join(before, spoken):
    script = f"const d = require('./static/js/nd-dictate.js'); console.log(JSON.stringify(d.join({json.dumps(before)}, {json.dumps(spoken)})));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_join_spacing_and_capitals():
    assert _join("", "we met the smith") == "We met the smith"
    assert _join("We met the smith.", "he lied") == "We met the smith. He lied"
    assert _join("We met the smith", "and he lied") == "We met the smith and he lied"
    assert _join("Line one\n", "next") == "Line one\nNext"
    assert _join("kept  ", "  more   words ") == "kept more words"
    assert _join("unchanged", "   ") == "unchanged"


def test_not_supported_under_node_is_harmless():
    script = "const d = require('./static/js/nd-dictate.js'); console.log(d.supported, typeof d.attach(null, null));"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.stdout.split() == ["false", "function"]
