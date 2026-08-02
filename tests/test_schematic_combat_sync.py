"""Phase 11 regression tests: schematic_save_elements, schematic_link_combat,
schematic_pull_combat, and schematic_push_combat previously had no shape
validation (a malformed elements payload could brick a schematic — every
later read assumes elements_json decodes to a list of dicts), no error
handling for a non-numeric combat_session_id (an unhandled ValueError 500),
and no locking on their read-modify-write of elements_json/combatants_json
(unlike move-token/pickup-item/buy-item, fixed in Phase 4) — so a concurrent
player action could silently discard a pull-combat's newly created tokens.
"""
import asyncio
import json
import threading

import pytest
from fastapi import HTTPException

from app import main as main_module
from app.database import SessionLocal
from app.models import CombatSession, PlayerCharacter, Schematic

from .conftest import GM_PASSWORD, login


def _make_schematic(world_id, slug, name="Test Schematic", elements=None, combat_session_id=None):
    db = SessionLocal()
    try:
        s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False,
                      canvas_width=2000, canvas_height=1500, canvas_bg="dark",
                      elements_json=json.dumps(elements or []), combat_session_id=combat_session_id)
        db.add(s)
        db.commit()
        db.refresh(s)
        return s
    finally:
        db.close()


def _make_combat(world_id, combatants=None, name="Test Fight"):
    db = SessionLocal()
    try:
        cs = CombatSession(world_id=world_id, name=name, combatants_json=json.dumps(combatants or []))
        db.add(cs)
        db.commit()
        db.refresh(cs)
        return cs
    finally:
        db.close()


def _make_pc(world_id, owner_id, name="Hero"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_id, name=name)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def test_save_elements_rejects_non_list(client, seed):
    s = _make_schematic(seed.world_a.id, "elements-shape")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/elements", json={"elements": "not-a-list"})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert json.loads(db.query(Schematic).filter(Schematic.id == s.id).first().elements_json) == []
    finally:
        db.close()


def test_save_elements_rejects_non_dict_items(client, seed):
    s = _make_schematic(seed.world_a.id, "elements-shape-2")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/elements", json={"elements": ["not-a-dict"]})
    assert r.status_code == 400


def test_save_elements_happy_path(client, seed):
    s = _make_schematic(seed.world_a.id, "elements-ok")
    login(client, seed.gm.email, GM_PASSWORD)
    elements = [{"id": "wall1", "type": "shape"}]
    r = client.post(f"/maps/schematic/{s.slug}/elements", json={"elements": elements})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert json.loads(db.query(Schematic).filter(Schematic.id == s.id).first().elements_json) == elements
    finally:
        db.close()


def test_link_combat_invalid_id_rejected_not_500(client, seed):
    s = _make_schematic(seed.world_a.id, "link-invalid")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/link-combat", data={"combat_session_id": "not-a-number"})
    assert r.status_code == 400


def test_link_combat_happy_path(client, seed):
    s = _make_schematic(seed.world_a.id, "link-ok")
    cs = _make_combat(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/link-combat", data={"combat_session_id": str(cs.id)},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.query(Schematic).filter(Schematic.id == s.id).first().combat_session_id == cs.id
    finally:
        db.close()


def test_pull_combat_creates_and_refreshes_tokens(client, seed):
    cs = _make_combat(seed.world_a.id, combatants=[
        {"id": "c1", "name": "Goblin", "source": "entity", "hp": 5, "max_hp": 5, "conditions": []},
    ])
    s = _make_schematic(seed.world_a.id, "pull-ok", combat_session_id=cs.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/pull-combat")
    assert r.status_code == 200
    elements = r.json()["elements"]
    assert len(elements) == 1
    assert elements[0]["combatant_id"] == "c1"
    assert elements[0]["hp"] == 5

    # Second pull with updated hp should refresh the existing token in place,
    # not duplicate it.
    db = SessionLocal()
    try:
        fresh_cs = db.query(CombatSession).filter(CombatSession.id == cs.id).first()
        combatants = json.loads(fresh_cs.combatants_json)
        combatants[0]["hp"] = 2
        fresh_cs.combatants_json = json.dumps(combatants)
        db.commit()
    finally:
        db.close()
    r2 = client.post(f"/maps/schematic/{s.slug}/pull-combat")
    elements2 = r2.json()["elements"]
    assert len(elements2) == 1
    assert elements2[0]["hp"] == 2


def test_pull_combat_entity_sourced_tokens_default_visible_to_players(client, seed):
    """Creature/NPC combatants used to come in with visible_to_players=False
    (only "pc"-sourced combatants defaulted visible) — a GM pulling monsters
    into a schematic had to manually toggle each one on before players could
    see them. Per explicit request, entity-sourced (creature/NPC) combatants
    now default visible too, same as PCs; only "manual" (typed directly into
    the combat tracker, no PC/Entity link) still defaults hidden."""
    cs = _make_combat(seed.world_a.id, combatants=[
        {"id": "c1", "name": "Goblin", "source": "entity", "hp": 5, "max_hp": 5, "conditions": []},
        {"id": "c2", "name": "Hero", "source": "pc", "hp": 10, "max_hp": 10, "conditions": []},
        {"id": "c3", "name": "Ambusher", "source": "manual", "hp": 8, "max_hp": 8, "conditions": []},
    ])
    s = _make_schematic(seed.world_a.id, "pull-visibility", combat_session_id=cs.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/pull-combat")
    assert r.status_code == 200
    elements = {e["combatant_id"]: e for e in r.json()["elements"]}
    assert elements["c1"]["visible_to_players"] is True
    assert elements["c2"]["visible_to_players"] is True
    assert elements["c3"]["visible_to_players"] is False


def test_push_combat_syncs_hp_back(client, seed):
    cs = _make_combat(seed.world_a.id, combatants=[
        {"id": "c1", "name": "Goblin", "source": "entity", "hp": 5, "max_hp": 5, "conditions": []},
    ])
    elements = [{"id": "tok1", "type": "token", "combatant_id": "c1", "hp": 1, "max_hp": 5, "conditions": ["Prone"]}]
    s = _make_schematic(seed.world_a.id, "push-ok", elements=elements, combat_session_id=cs.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/push-combat")
    assert r.status_code == 200
    assert r.json()["synced"] == ["Goblin"]
    db = SessionLocal()
    try:
        fresh_cs = db.query(CombatSession).filter(CombatSession.id == cs.id).first()
        combatant = json.loads(fresh_cs.combatants_json)[0]
        assert combatant["hp"] == 1
        assert combatant["conditions"] == ["Prone"]
    finally:
        db.close()


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    def __init__(self, user, body):
        self.state = _FakeState(user)
        self._body = body

    async def json(self):
        return self._body


def test_concurrent_pull_combat_and_move_token_dont_clobber(client, seed, monkeypatch):
    """The core reliability claim: pull-combat's BEGIN IMMEDIATE lock must
    serialize it against a concurrent move-token on the same schematic, so
    the loser re-reads post-lock state instead of blindly overwriting
    elements_json with a stale in-memory copy that's missing the other
    request's change. Same deterministic-contention technique as the Phase 4
    concurrent buy/pickup tests: gate the first request mid-transaction
    (already holding SQLite's RESERVED lock) so the second's own BEGIN
    IMMEDIATE genuinely blocks on real lock contention.
    """
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    cs = _make_combat(seed.world_a.id, combatants=[
        {"id": "c1", "name": "Goblin", "source": "entity", "hp": 5, "max_hp": 5, "conditions": []},
    ])
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 10, "y": 10, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "race-pull", elements=elements, combat_session_id=cs.id)

    ready = threading.Event()
    release = threading.Event()
    calls = []
    original_dumps = json.dumps

    def gated_dumps(obj, *a, **kw):
        # schematic_pull_combat's only json.dumps call is the final
        # elements_json write — pause it there, after it has already read
        # elements/combatants but before it commits, so the concurrent
        # move-token's own BEGIN IMMEDIATE has something to block on.
        if isinstance(obj, list) and obj and obj[0].get("id") == "tok1" and len(calls) == 0:
            calls.append(1)
            ready.set()
            assert release.wait(timeout=5), "test deadlocked waiting to release pull-combat"
        return original_dumps(obj, *a, **kw)

    monkeypatch.setattr(main_module.json, "dumps", gated_dumps)

    results = {}

    def do_pull():
        db = SessionLocal()
        try:
            r = main_module.schematic_pull_combat(s.slug, db)
            results["pull"] = (200, r)
        except HTTPException as e:
            results["pull"] = (e.status_code, None)
        finally:
            db.close()

    def do_move():
        db = SessionLocal()
        try:
            user = db.query(main_module.User).filter(main_module.User.email == seed.player_a.email).first()
            req = _FakeRequest(user, {"token_id": "tok1", "x": 99, "y": 99})
            try:
                body = asyncio.run(main_module.schematic_move_own_token(s.slug, req, db))
                results["move"] = (200, body)
            except HTTPException as e:
                results["move"] = (e.status_code, None)
        finally:
            db.close()

    t_pull = threading.Thread(target=do_pull)
    t_pull.start()
    assert ready.wait(timeout=5), "pull-combat never reached the locked section"

    t_move = threading.Thread(target=do_move)
    t_move.start()
    t_move.join(timeout=1)  # let move-token's own BEGIN IMMEDIATE actually attempt + block
    release.set()
    t_pull.join(timeout=5)
    t_move.join(timeout=5)

    assert results["pull"][0] == 200
    assert results["move"][0] == 200

    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        final_elements = json.loads(fresh.elements_json)
        tok = next(e for e in final_elements if e["id"] == "tok1")
        # move-token ran after pull-combat committed (serialized by the lock),
        # so its write must survive rather than being clobbered.
        assert (tok["x"], tok["y"]) == (99, 99)
        # And pull-combat's new combatant token must also still be present —
        # proving move-token's write didn't itself clobber the pull.
        assert any(e.get("combatant_id") == "c1" for e in final_elements)
    finally:
        db.close()
