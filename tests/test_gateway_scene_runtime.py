"""Rust-only tests for the linked map/combat Phase 1 scene runtime."""
import json
from sqlalchemy import text

from app.database import SessionLocal
from app.models import CombatSession, PlayerCharacter, Schematic

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
