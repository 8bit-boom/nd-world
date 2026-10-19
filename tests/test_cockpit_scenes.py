"""The cockpit's scenes one click away: the chips in the header and the tiles on the empty cockpit (GM only - a player's cockpit has no scenes)
and the Alt + Shift + 1-4 keys (static/js/cockpit-keys.js under Node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _press(code, alt=True, shift=False):
    script = ("global.window = global; require(process.env.KEYS); "
              f"console.log(JSON.stringify(window.ndCockpitKeys.shortcutFor({{code: {json.dumps(code)}, alt: {json.dumps(alt)}, shift: {json.dumps(shift)}}})))")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, env={"KEYS": str(ROOT / "static/js/cockpit-keys.js"), "PATH": __import__("os").environ["PATH"]})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_alt_shift_digits_pick_a_scene_and_alt_digits_still_show_a_window():
    assert _press("Digit1", shift=True) == {"action": "scene", "n": 1} and _press("Digit4", shift=True) == {"action": "scene", "n": 4}
    assert _press("Digit5", shift=True) is None                                   # only four scenes
    assert _press("Digit2") == {"action": "show", "n": 2}                          # unchanged


def test_gm_cockpit_has_the_scene_bar_and_the_player_cockpit_does_not(client, seed):
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm = client.get("/cockpit").text
    assert 'id="ck-scenebar"' in gm and 'id="ck-welcome-scenes"' in gm and "Start from a scene" in gm
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    pl = client.get("/player-cockpit").text
    assert 'id="ck-scenebar"' not in pl and 'id="ck-welcome-scenes"' not in pl and "pin your first panels" in pl
