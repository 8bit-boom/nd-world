"""nd-cartographer, embedded (crate::cartographer + the compiled-in ndcart_server):
the map studio served natively by the Python-free gateway under /cartographer/studio
on its own origin, plus the encounter-template port (/api/combat/templates) and the
one-click "play this map" scene assembly.

The studio is compiled into the gateway — no separate binary, download, child process
or reverse proxy — so these tests need no stub and no network: they run against the
gateway's own router on the shared test database. The play tests use the gateway's own
combat/schematic primitives."""
import json
import os

import pytest

from .test_gateway_golden import GM_PASSWORD, _python_free_gateway

pytestmark = pytest.mark.skipif(
    not os.environ.get("ND_TEST_BASE_URL") and not os.environ.get("DB_PATH"),
    reason="needs the test database",
)


def _player_client(seed, url):
    import httpx
    from .conftest import PLAYER_PASSWORD
    p = httpx.Client(base_url=url, timeout=60, follow_redirects=False)
    assert p.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"}).status_code in (302, 303)
    p.cookies.set("active_world", seed.world_a.slug)
    return p


def test_studio_is_mounted_natively(client, seed, tmp_path):
    """The studio is compiled into the gateway and served at /cartographer/studio/*
    on its own origin (no loopback port), GM-only. Always installed and running —
    there is nothing to install or uninstall."""
    proc, url, gm = _python_free_gateway(seed, extra_env={"ND_ACCEL_ROOT": str(tmp_path / "accel")})
    try:
        # the management page renders the embed
        page = gm.get("/cartographer")
        assert page.status_code == 200
        assert "Map Maker" in page.text
        assert "/cartographer/studio" in page.text

        # status: always installed/running, the URL is the same-origin mount path
        st = gm.get("/api/cartographer/status").json()
        assert st["installed"] and st["running"] and st["enabled"]
        assert st["url"] == "/cartographer/studio"
        assert "127.0.0.1" not in (st["url"] or "")

        # the studio answers under the prefix: health, the index (base tag), app.js
        health = gm.get("/cartographer/studio/health")
        assert health.status_code == 200, health.text
        assert health.json()["app"] == "nd-cartographer"
        idx = gm.get("/cartographer/studio")
        assert idx.status_code == 200, idx.text
        assert '<base href="/cartographer/studio/">' in idx.text
        assert gm.get("/cartographer/studio/app.js", timeout=60).status_code == 200
        # the trailing slash redirects to the index (the studio's nest redirect)
        assert gm.get("/cartographer/studio/", follow_redirects=False).status_code in (301, 302, 303, 307, 308)

        # a player cannot reach the studio or its management page
        player = _player_client(seed, url)
        assert player.get("/cartographer/studio/health").status_code == 403
        assert player.get("/cartographer").status_code == 403
    finally:
        proc.terminate()


def test_nd_world_smart_context_reads_cartographer_maps(client, seed, tmp_path):
    """Persisted Cartographer projects become read-only world context for nd-world AI."""
    accel = tmp_path / "accel"
    doc = accel / "cartographer" / "data" / "doc-lost-cathedral"
    doc.mkdir(parents=True)
    (doc / "manifest.json").write_text(json.dumps({"name": "The Lost Cathedral"}), encoding="utf-8")
    (doc / "snapshot.json").write_text(json.dumps({
        "journal_len": 1,
        "floors": [{
            "name": "Crypt",
            "grid": {"spaces": {"a": {"name": "Vestibule"}}},
            "labels": [{"text": "The drowned reliquary"}],
            "markers": [{"label": "Black Stair", "note": "The cathedral relic is hidden below"}],
        }],
    }), encoding="utf-8")

    proc, url, gm = _python_free_gateway(seed, extra_env={"ND_ACCEL_ROOT": str(accel)})
    try:
        r = gm.post("/api/ai/world-context-smart", json={"query": "drowned reliquary cathedral", "limit": 25, "notes_limit": 5})
        assert r.status_code == 200, r.text
        context = r.json()["context"]
        assert "Cartographer maps:" in context
        assert "The Lost Cathedral" in context
        assert "drowned reliquary" in context
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