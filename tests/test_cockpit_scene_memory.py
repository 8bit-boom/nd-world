"""A scene remembers how the GM arranged it (static/js/cockpit-layout.js rememberRects / recallRects under Node), and the opt-in offer of the
Combat scene when a fight starts (the `running` flag of /api/combat/recent and the Layouts-menu row)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import CombatSession

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(body: str):
    script = ("global.window = global; require(process.env.LAYOUT); const L = window.ndCockpitLayout;"
              "const mk = (id, type, ref, x, parked) => ({ id, type, ref, x, y: 10, w: 300, h: 200, z: 1, parked: !!parked });" + body)
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "LAYOUT": str(ROOT / "static/js/cockpit-layout.js")})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_a_scene_arrangement_is_remembered_by_what_each_window_shows_not_by_its_id():
    out = _run("""
      const mem = L.rememberRects([mk('1', 'map', 'm', 5), mk('2', 'dice', '', 400), mk('3', 'dice', '', 800), mk('4', 'notes', '', 0, true)], { w: 1600, h: 900 });
      const next = [mk('9', 'dice', '', 0), mk('8', 'map', 'm', 0), mk('7', 'dice', '', 0)];                         // new ids, other order
      const ok = L.recallRects(next, mem, { w: 1600, h: 900 });
      console.log(JSON.stringify({ keys: Object.keys(mem.rects), ok, x: next.map(p => p.id + ':' + p.x) }));""")
    assert out["keys"] == ["map:m", "dice:", "dice:#2"]                      # a parked window is not part of the arrangement; two dice are told apart by order
    assert out["ok"] is True and out["x"] == ["9:400", "8:5", "7:800"]


@needs_node
def test_a_memory_that_does_not_cover_every_showing_window_is_not_used_and_a_smaller_screen_scales_it():
    out = _run("""
      const mem = L.rememberRects([mk('1', 'map', 'm', 5)], { w: 1600, h: 900 });
      const extra = [mk('1', 'map', 'm', 0), mk('2', 'quests', '', 0)];
      const covers = L.recallRects(extra, mem, { w: 1600, h: 900 });
      const small = [mk('1', 'map', 'm', 0)];
      const scaled = L.recallRects(small, mem, { w: 800, h: 450 });
      console.log(JSON.stringify({ covers, extraX: extra.map(p => p.x), scaled, w: small[0].w, none: L.recallRects(small, null, { w: 800, h: 450 }) }));""")
    assert out["covers"] is False and out["extraX"] == [0, 0]               # a new kind of window: tile afresh rather than overlap
    assert out["scaled"] is True and out["w"] == 240 and out["none"] is False


def _gm(client, seed):
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_combat_recent_says_which_fights_are_running(client, seed):
    from datetime import datetime
    db = SessionLocal()
    try:
        db.add(CombatSession(world_id=seed.world_a.id, name="Still going", combatants_json="[]"))
        db.add(CombatSession(world_id=seed.world_a.id, name="Over", combatants_json="[]", finished_at=datetime.utcnow()))
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    rows = {c["name"]: c for c in client.get("/api/combat/recent").json()["combats"]}
    assert rows["Still going"]["running"] is True and rows["Over"]["running"] is False


def test_the_layouts_menu_has_the_opt_in_row(client, seed):
    _gm(client, seed)
    page = client.get("/cockpit").text
    assert 'id="ck-combathint-row"' in page and "Offer Combat when a fight starts" in page
