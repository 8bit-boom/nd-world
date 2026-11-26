"""Rust-only tests for the linked map/combat Phase 1 scene runtime."""
import json
import httpx
import os
from sqlalchemy import text

from app.database import SessionLocal
from app.models import CombatSession, InviteCode, PlayerCharacter, Schematic, WorldMembership

from .conftest import PLAYER_PASSWORD
from .test_gateway_golden import GM_PASSWORD, _python_free_gateway


def test_rust_only_patch_token_syncs_and_checks_revisions(client, seed):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.gm.id,
                             name="GM Hero", current_hp=10, max_hp=10)
        db.add(pc)
        db.flush()
        combat = CombatSession(world_id=seed.world_a.id, name="Linked Fight",
                               combatants_json=json.dumps([{
                                   "id": "c1", "name": "Hero", "source": "pc",
                                   "pc_id": pc.id, "hp": 10, "max_hp": 10,
                                   "conditions": ["Prone"],
                               }]))
        db.add(combat)
        db.flush()
        schematic = Schematic(world_id=seed.world_a.id, name="Linked Map",
                              slug="rust-linked-map", canvas_width=1000,
                              canvas_height=800,
                              elements_json=json.dumps([{
                                  "id": "tok1", "type": "token", "x": 10, "y": 20,
                                  "combatant_id": "c1", "pc_id": pc.id,
                                  "source": "pc", "name": "Hero",
                                  "hp": 10, "max_hp": 10,
                                  "conditions": ["Prone"],
                                  "visible_to_players": True,
                              }]), combat_session_id=combat.id)
        db.add(schematic)
        db.commit()
        combat_id = combat.id
        pc_id = pc.id
    finally:
        db.close()

    proc, base, gm = _python_free_gateway(seed)
    try:
        r = gm.patch(
            "/api/maps/schematic/rust-linked-map/tokens/tok1",
            json={"x": 125, "y": 150, "hp": 4, "conditions": ["Prone", "Burn"],
                  "scene_rev": 0, "combat_rev": 0},
        )
        assert r.status_code == 200, r.text
        assert r.json()["scene_rev"] == 1
        assert r.headers["x-nd-served-by"] == "gateway"

        db = SessionLocal()
        try:
            scene = db.query(Schematic).filter(Schematic.slug == "rust-linked-map").first()
            fight = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
            elements = json.loads(scene.elements_json)
            token = next(e for e in elements if e["id"] == "tok1")
            combatant = json.loads(fight.combatants_json)[0]
            assert token["x"] == 125 and token["y"] == 150
            assert token["hp"] == 4
            assert combatant["hp"] == 4
            assert combatant["conditions"] == ["Prone", "Burn"]
            stored_scene_rev = db.execute(
                text("SELECT scene_rev FROM schematics WHERE slug = 'rust-linked-map'")
            ).scalar_one()
            assert stored_scene_rev == 1
            assert fight.rev == 1
        finally:
            db.close()

        stale_scene = gm.patch(
            "/api/maps/schematic/rust-linked-map/tokens/tok1",
            json={"x": 200, "scene_rev": 0, "combat_rev": 1},
        )
        assert stale_scene.status_code == 409
        assert stale_scene.json()["scene_rev"] == 1

        stale_combat = gm.patch(
            "/api/maps/schematic/rust-linked-map/tokens/tok1",
            json={"x": 201, "scene_rev": 1, "combat_rev": 0},
        )
        assert stale_combat.status_code == 409
        assert stale_combat.json()["combat_rev"] == 1

        stale_tracker = gm.post(
            f"/combat/{combat_id}/state",
            json={"combatants": [{
                "id": "c1", "name": "Overwrite", "source": "pc", "pc_id": pc_id,
                "hp": 1, "max_hp": 10, "conditions": [],
            }], "rev": 0, "round_num": 1, "active_idx": 0},
        )
        assert stale_tracker.status_code == 409
        assert stale_tracker.json()["state"]["rev"] == 1
        assert stale_tracker.json()["state"]["combatants"][0]["hp"] == 4

        tracked = gm.post(
            f"/combat/{combat_id}/state",
            json={"combatants": [{
                "id": "c1", "name": "Hero", "source": "pc", "pc_id": pc_id,
                "hp": 3, "max_hp": 10, "conditions": ["Burn"],
            }], "rev": 1, "round_num": 1, "active_idx": 0},
        )
        assert tracked.status_code == 200, tracked.text

        db = SessionLocal()
        try:
            scene = db.query(Schematic).filter(Schematic.slug == "rust-linked-map").first()
            token = next(e for e in json.loads(scene.elements_json) if e["id"] == "tok1")
            assert token["hp"] == 3
            assert token["conditions"] == ["Burn"]
            stored_scene_rev = db.execute(
                text("SELECT scene_rev FROM schematics WHERE slug = 'rust-linked-map'")
            ).scalar_one()
            assert stored_scene_rev >= 2
        finally:
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_rust_only_legacy_ollama_pull_streams_progress(client, seed):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/pull":
                assert body["name"] == "llama3.2:3b"
                assert body["stream"] is True
                payload = b'{"status":"pulling manifest"}\n{"status":"success"}\n'
            elif self.path == "/api/generate":
                assert body["model"] == "llama3.2:3b"
                assert body["keep_alive"] == 0
                payload = b'{"done":true}\n'
            elif self.path == "/api/chat":
                assert body["model"] == "llama3.2:3b"
                assert body["stream"] is False
                payload = b'{"model":"llama3.2:3b","eval_count":100,"eval_duration":2000000000,"prompt_eval_count":20,"prompt_eval_duration":500000000,"load_duration":250000000,"total_duration":2750000000}\n'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proc, base, gm = _python_free_gateway(seed, {
        "OLLAMA_URL": f"http://127.0.0.1:{server.server_address[1]}",
    })
    try:
        r = gm.post("/api/ai/pull", json={"model_id": "llama3.2:3b"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "pulling manifest" in r.text
        assert "[DONE]" in r.text
        unloaded = gm.post("/api/ai/unload", json={"model_id": "llama3.2:3b"})
        assert unloaded.status_code == 200
        assert unloaded.json() == {"ok": True}
        benchmark = gm.post("/api/ai/benchmark", json={"model": "llama3.2:3b"})
        assert benchmark.status_code == 200
        assert benchmark.json()["model"] == "llama3.2:3b"
        assert benchmark.json()["tokens_per_sec"] == 50.0
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server.shutdown()
        server.server_close()


def test_rust_only_prompt_preset_create(client, seed):
    proc, base, gm = _python_free_gateway(seed)
    try:
        r = gm.post("/api/ai/prompt-presets", json={
            "scope": "image",
            "label": "Rust Neon",
            "icon": "🎨",
            "text": "cyberpunk portrait",
            "negative": "blurry",
        })
        assert r.status_code == 200
        assert r.headers["x-nd-served-by"] == "gateway"
        preset = r.json()
        assert preset["scope"] == "image"
        assert preset["label"] == "Rust Neon"
        assert preset["text"] == "cyberpunk portrait"
        assert preset["negative"] == "blurry"

        assert gm.post("/api/ai/prompt-presets", json={"scope": "bogus", "label": "x"}).status_code == 400
        assert gm.post("/api/ai/prompt-presets", json={"scope": "chat", "label": "   "}).status_code == 400

        player = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
        login = player.post("/login", data={
            "email": seed.player_a.email,
            "password": PLAYER_PASSWORD,
            "next": "/",
        })
        assert login.status_code == 303
        assert player.post("/api/ai/prompt-presets", json={
            "scope": "chat", "label": "Player", "text": "not allowed",
        }).status_code == 403

        db = SessionLocal()
        try:
            from app.models import PromptPreset
            row = db.get(PromptPreset, preset["id"])
            assert row is not None
            assert row.world_id == seed.world_a.id
        finally:
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_rust_only_world_delete(client, seed):
    from app.models import Entity, GameSession, World

    db = SessionLocal()
    try:
        doomed = Entity(world_id=seed.world_a.id, kind="location", name="Doomed World")
        safe = Entity(world_id=seed.world_b.id, kind="location", name="Safe World")
        session = GameSession(world_id=seed.world_a.id, title="Doomed Recording")
        db.add_all([doomed, safe, session])
        db.commit()
        db.refresh(doomed)
        db.refresh(safe)
        db.refresh(session)
        doomed_id, safe_id, session_id = doomed.id, safe.id, session.id
    finally:
        db.close()

    uploads = __import__("pathlib").Path(os.environ["DB_PATH"]).parent / "uploads"
    live = uploads / "live" / str(session_id)
    live.mkdir(parents=True, exist_ok=True)
    (live / "000000.webm").write_bytes(b"fake")

    proc, base, gm = _python_free_gateway(seed, {"ND_DECLINE_TRACE": "1"})
    try:
        deleted = gm.post(f"/worlds/{seed.world_a.id}/delete", follow_redirects=False)
        assert deleted.status_code == 303, (deleted.status_code, deleted.text)
        assert deleted.headers["location"] == "/worlds"
        assert deleted.headers["x-nd-served-by"] == "gateway"
        assert not live.exists()

        db = SessionLocal()
        try:
            assert db.get(World, seed.world_a.id) is None
            assert db.get(Entity, doomed_id) is None
            assert db.get(Entity, safe_id) is not None
            assert db.get(World, seed.world_b.id) is not None
        finally:
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_rust_only_signed_in_join_redeems_invite(client, seed):
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_b.id,
            WorldMembership.user_id == seed.player_a.id,
        ).delete(synchronize_session=False)
        db.add(InviteCode(code="rust-only-good", world_id=seed.world_b.id))
        db.commit()
    finally:
        db.close()

    proc, base, gm = _python_free_gateway(seed)
    try:
        player = httpx.Client(base_url=base, timeout=60, follow_redirects=False)
        login = player.post("/login", data={
            "email": seed.player_a.email,
            "password": PLAYER_PASSWORD,
            "next": "/",
        })
        assert login.status_code == 303
        joined = player.get("/join/rust-only-good")
        assert joined.status_code == 303
        assert joined.headers["location"] == "/"
        assert joined.headers["x-nd-served-by"] == "gateway"

        db = SessionLocal()
        try:
            membership = db.query(WorldMembership).filter(
                WorldMembership.world_id == seed.world_b.id,
                WorldMembership.user_id == seed.player_a.id,
            ).first()
            invite = db.query(InviteCode).filter(InviteCode.code == "rust-only-good").first()
            assert membership is not None
            assert membership.role == "player"
            assert invite.uses_count == 1
        finally:
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_rust_only_duplicate_links_are_guarded(client, seed):
    db = SessionLocal()
    try:
        combat = CombatSession(world_id=seed.world_a.id, name="Duplicate IDs",
                               combatants_json=json.dumps([
                                   {"id": "dup", "name": "One", "source": "manual", "hp": 8, "max_hp": 8},
                                   {"id": "dup", "name": "Two", "source": "manual", "hp": 7, "max_hp": 7},
                               ]))
        db.add(combat)
        db.flush()
        db.add(Schematic(world_id=seed.world_a.id, name="Duplicate Map",
                         slug="rust-duplicate-map", canvas_width=1000,
                         canvas_height=800,
                         elements_json=json.dumps([
                             {"id": "tok1", "type": "token", "combatant_id": "dup",
                              "hp": 6, "max_hp": 8, "conditions": []},
                             {"id": "tok2", "type": "token", "combatant_id": "dup",
                              "hp": 5, "max_hp": 8, "conditions": []},
                         ]),
                         combat_session_id=combat.id))
        db.commit()
    finally:
        db.close()

    proc, base, gm = _python_free_gateway(seed)
    try:
        r = gm.post("/combat/1/state", json={}) if False else None
        r = gm.post(
            "/maps/schematic/rust-duplicate-map/pull-combat",
        )
        assert r.status_code == 200
        db = SessionLocal()
        try:
            scene = db.query(Schematic).filter(Schematic.slug == "rust-duplicate-map").first()
            elements = json.loads(scene.elements_json)
            linked = [e for e in elements if e.get("combatant_id") == "dup"]
            assert len(linked) <= 1
            if linked:
                assert "hp" in linked[0]
        finally:
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
