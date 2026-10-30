"""Ctrl+V of a picture into the drop zones: static/js/file-dropzone.js run in a real Chromium (tests/js/paste_zone.mjs). Needs node and the playwright
package (NODE_PATH, as the cloud sandbox has); skipped where they are missing."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_pasted_pictures_reach_the_drop_zones():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("the playwright package is not available to node")
    r = subprocess.run([node, str(ROOT / "tests" / "js" / "paste_zone.mjs")], capture_output=True, text=True, timeout=120, env=dict(os.environ))
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
