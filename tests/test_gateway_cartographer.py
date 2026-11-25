"""nd-cartographer, embedded (crate::cartographer): the one-click-installable map
maker answered by the Python-free gateway, plus the encounter-template port
(/api/combat/templates) and the one-click "play this map" scene assembly.

The install lifecycle runs against a stub ndcart binary (ND_CARTOGRAPHER_BIN_DIR —
a tiny script that binds the requested port and answers /health), so it needs no
network and no real studio. The play tests use the gateway's own combat/schematic
primitives, no stub."""
import json
import os
import stat
import sys

import pytest

from .test_gateway_golden import GM_PASSWORD, _python_free_gateway

pytestmark = pytest.mark.skipif(
    not os.environ.get("ND_TEST_BASE_URL") and not os.environ.get("DB_PATH"),
    reason="needs the test database",
)

STUB_NDCART = r'''
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer
addr = ("127.0.0.1", 8300)
for i, a in enumerate(sys.argv):
    if a == "--listen":
        h, p = sys.argv[i + 1].rsplit(":", 1)
        addr = (h, int(p))
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "app": "nd-cartographer", "version": "0.0.0-stub"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
HTTPServer(addr, H).serve_forever()
'''


def _stub_binary(tmp_path):
    d = tmp_path / "carto-bin"
    d.mkdir()
    exe = d / ("ndcart.exe" if os.name == "nt" else "ndcart")
    exe.write_text("#!" + sys.executable + "\n" + STUB_NDCART, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return d


def _player_client(seed, url):
    import httpx
    from .conftest import PLAYER_PASSWORD
    p = httpx.Client(base_url=url, timeout=60, follow_redirects=False)
    assert p.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"}).status_code in (302, 303)
    p.cookies.set("active_world", seed.world_a.slug)
    return p


def test_cartographer_install_start_stop_uninstall(client, seed, tmp_path):
    """One click installs the map maker (a prepared binary), it runs, stops, starts
    again, and one click uninstalls it. GM only."""
    bindir = _stub_binary(tmp_path)
    proc, url, gm = _python_free_gateway(
        seed,
        extra_env={"ND_CARTOGRAPHER_BIN_DIR": str(bindir), "ND_CARTOGRAPHER_VERSION": "v0.0.0-stub",
                   "ND_ACCEL_ROOT": str(tmp_path / "accel")},
    )
    try:
        # a leftover from an earlier run is cleaned first (the binary lives in a
        # folder next to the shared test database, like lore_forge's state)
        import shutil
        gm.post("/cartographer/uninstall")
        shutil.rmtree(gm.get("/api/cartographer/status").json()["dir"], ignore_errors=True)
        # not installed yet: the page renders, the status says so
        st = gm.get("/api/cartographer/status").json()
        assert st["installed"] is False and st["running"] is False
        assert gm.get("/cartographer").status_code == 200
        assert "Map Maker" in gm.get("/cartographer").text

        # one click: install copies the prepared binary and launches it
        r = gm.post("/cartographer/install")
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["installed"] and st["running"] and st["enabled"] and st["autostart"]
        assert st["version"] == "v0.0.0-stub"
        # the managed studio answers through the gateway's proxy (same origin)
        assert gm.get("/cartographer/studio/health").json()["status"] == "ok"

        # stop keeps it installed; start brings it back
        assert gm.post("/cartographer/stop").json()["running"] is False
        assert gm.post("/cartographer/start").json()["running"] is True

        # uninstall stops it and clears the registration (the binary is kept)
        st = gm.post("/cartographer/uninstall").json()
        assert st["enabled"] is False and st["running"] is False and st["url"] is None
        assert st["installed"] is True
    finally:
        proc.terminate()


def test_combat_template_port_matches_the_creature_lines(client, seed):
    """/api/combat/templates (ported from app/routers/combat.py): "name xN" becomes
    N combatants; the template is GM information."""
    proc, url, gm = _python_free_gateway(seed)
    try:
        r = gm.post("/api/combat/templates", json={"name": "Crypt guards", "creatures": ["goblin x2", "ogre"], "ruleset": ""})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Crypt guards" and body["combatants"] == 3
        # a bad creature list is refused
        assert gm.post("/api/combat/templates", json={"name": "x", "creatures": []}).status_code == 400
        assert gm.post("/api/combat/templates", json={"name": "", "creatures": ["goblin"]}).status_code == 400
    finally:
        proc.terminate()


def test_play_manifest_and_one_click_play(client, seed, tmp_path):
    """Publish a play manifest (NPCs + loot per room), then one click builds a
    combat linked to the schematic with NPC and item tokens placed on it."""
    proc, url, gm = _python_free_gateway(seed, extra_env={"ND_ACCEL_ROOT": str(tmp_path / "accel")})
    try:
        r = gm.post("/maps/schematic/new", data={"name": "Play Crypt", "canvas_width": "2000", "canvas_height": "1500", "canvas_bg": "dark"})
        assert r.status_code in (302, 303), r.text
        slug = r.headers["location"].rsplit("/", 1)[-1]

        rooms = [{"space": "a", "name": "Guard post", "creatures": ["goblin x2", "ogre"],
                  "loot": [{"name": "gold", "item_id": None, "qty": 50}]}]
        r = gm.post(f"/api/maps/schematic/{slug}/play-manifest", json={"rooms": rooms})
        assert r.status_code == 200, r.text
        assert r.json()["rooms"] == 1

        r = gm.post(f"/api/maps/schematic/{slug}/play", json={})
        assert r.status_code == 200, r.text
        played = r.json()
        cid = played["combat_id"]
        assert played["tracker"] == f"/combat/{cid}"
        assert played["player_view"] == f"/maps/schematic/{slug}/view"

        # the combat holds the room's NPCs (two goblins + an ogre)
        state = gm.get(f"/api/combat/{cid}/state").json()
        names = [c["name"] for c in state["combatants"]]
        assert names.count("goblin") == 2 and "ogre" in names

        # the schematic is linked to the combat and carries NPC + item tokens
        import sqlite3
        db = sqlite3.connect(os.environ["DB_PATH"])
        linked, elements_json = db.execute("SELECT combat_session_id, elements_json FROM schematics WHERE slug = ?", (slug,)).fetchone()
        assert linked == cid
        tokens = [e for e in json.loads(elements_json) if e.get("type") == "token"]
        assert any(t.get("source") == "item" and "gold" in t.get("name", "") for t in tokens)
        assert sum(1 for t in tokens if t.get("combatant_id")) == 3
    finally:
        proc.terminate()


def test_studio_is_proxied_through_the_gateway(client, seed, tmp_path):
    """The studio is reached at /cartographer/studio/* on the gateway's own origin
    (reverse-proxied to the child's loopback port), so a browser never needs the
    child's port directly. GM-only; 503 when the studio is stopped."""
    from .conftest import PLAYER_PASSWORD
    bindir = _stub_binary(tmp_path)
    proc, url, gm = _python_free_gateway(
        seed,
        extra_env={"ND_CARTOGRAPHER_BIN_DIR": str(bindir), "ND_CARTOGRAPHER_VERSION": "v0.0.0-stub",
                   "ND_ACCEL_ROOT": str(tmp_path / "accel")},
    )
    try:
        # not running yet: the proxy answers 503, not a connection error
        assert gm.get("/cartographer/studio/health").status_code == 503
        gm.post("/cartographer/install")
        # the stub answers health for any path; the proxy forwards the full path
        r = gm.get("/cartographer/studio/health")
        assert r.status_code == 200, r.text
        assert r.json()["app"] == "nd-cartographer"
        # the embed URL is the same-origin proxy path, not a loopback port
        st = gm.get("/api/cartographer/status").json()
        assert st["url"] == "/cartographer/studio"
        assert "127.0.0.1" not in (st["url"] or "")
        # a player cannot reach the studio through the proxy
        player = _player_client(seed, url)
        assert player.get("/cartographer/studio/health").status_code == 403
        # stopped: 503 again
        gm.post("/cartographer/stop")
        assert gm.get("/cartographer/studio/health").status_code == 503
    finally:
        proc.terminate()