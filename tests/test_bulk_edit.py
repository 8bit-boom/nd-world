"""Tests for the GM/Assistant "AI-assisted find & replace" tool
(app/routers/bulk_edit.py): POST /api/bulk-edit/parse (natural-language
instruction -> literal {find, replace}, model mocked here), POST
/api/bulk-edit/preview (deterministic search across entities/notes/
characters), and POST /api/bulk-edit/apply (deterministic substitution,
scoped to caller-approved targets only). The model never performs the
actual text substitution — see _apply_replace in that module — so these
tests exercise the real find/replace logic directly rather than mocking it.
"""
import json

from app import ai as ai_module
from app.database import SessionLocal
from app.models import Entity, EntityNote, PlayerCharacter, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _add_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "race"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _add_note(entity_id, content):
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content=content)
        db.add(n)
        db.commit()
        db.refresh(n)
        return n.id
    finally:
        db.close()


def _add_pc(world_id, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, name=kw.pop("name", "Hero"), **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


# ── /api/bulk-edit/parse ────────────────────────────────────────────────────

def test_parse_returns_ai_extracted_terms(client, seed, monkeypatch):
    async def fake_parse(instruction, model=""):
        assert "Skinwalker" in instruction
        return {"understood": True, "find": "Skinwalker", "replace": "Ashwalker", "note": ""}
    monkeypatch.setattr(ai_module, "parse_find_replace_instruction", fake_parse)

    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/parse", json={"instruction": "Change the race name Skinwalker to Ashwalker everywhere"})
    assert r.status_code == 200
    data = r.json()
    assert data["understood"] is True
    assert data["find"] == "Skinwalker"
    assert data["replace"] == "Ashwalker"


def test_parse_surfaces_not_understood(client, seed, monkeypatch):
    async def fake_parse(instruction, model=""):
        return {"understood": False, "find": "", "replace": "", "note": "That's several unrelated changes."}
    monkeypatch.setattr(ai_module, "parse_find_replace_instruction", fake_parse)

    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/parse", json={"instruction": "Rewrite the whole lore section to be scarier"})
    assert r.status_code == 200
    assert r.json()["understood"] is False


def test_parse_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(instruction, model=""):
        raise ValueError("AI unavailable")
    monkeypatch.setattr(ai_module, "parse_find_replace_instruction", fake_parse)

    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/parse", json={"instruction": "change X to Y"})
    assert r.status_code == 502


def test_parse_rejects_blank_instruction(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/parse", json={"instruction": "   "})
    assert r.status_code == 400


def test_player_cannot_call_parse(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/bulk-edit/parse", json={"instruction": "change X to Y"})
    assert r.status_code == 403


def test_assistant_can_call_parse(client, seed, monkeypatch):
    async def fake_parse(instruction, model=""):
        return {"understood": True, "find": "X", "replace": "Y", "note": ""}
    monkeypatch.setattr(ai_module, "parse_find_replace_instruction", fake_parse)

    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id,
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/worlds/switch/{seed.world_a.slug}?next=/", follow_redirects=False).status_code == 303
    r = client.post("/api/bulk-edit/parse", json={"instruction": "change X to Y"})
    assert r.status_code == 200


# ── /api/bulk-edit/preview ──────────────────────────────────────────────────

def test_preview_finds_matches_across_entities_notes_and_characters(client, seed):
    eid = _add_entity(seed.world_a.id, name="Skinwalker", kind="race",
                       summary="A Skinwalker is a shapeshifter.", body="Skinwalkers roam the woods.")
    _add_note(eid, "The Skinwalker clan is ancient.")
    _add_pc(seed.world_a.id, name="Anders", race="Skinwalker", backstory="Raised among the Skinwalker.")

    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/preview", json={"find": "Skinwalker", "replace": "Ashwalker"})
    assert r.status_code == 200
    data = r.json()
    types = {(c["target_type"], c["field"]) for c in data["candidates"]}
    assert ("entity", "name") in types
    assert ("entity", "summary") in types
    assert ("entity", "body") in types
    assert ("entity_note", "content") in types
    assert ("player_character", "race") in types
    assert ("player_character", "backstory") in types
    assert data["total_occurrences"] >= 6


def test_preview_shows_correct_before_after_excerpts(client, seed):
    _add_entity(seed.world_a.id, name="A Skinwalker Tale", kind="note", summary="", body="")
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/preview", json={"find": "Skinwalker", "replace": "Ashwalker"})
    data = r.json()
    name_candidate = next(c for c in data["candidates"] if c["field"] == "name")
    assert "Skinwalker" in name_candidate["before_excerpt"]
    assert "Ashwalker" in name_candidate["after_excerpt"]
    assert name_candidate["occurrences"] == 1


def test_preview_case_sensitive_by_default(client, seed):
    _add_entity(seed.world_a.id, name="skinwalker lore", kind="note", summary="", body="")
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/preview", json={"find": "Skinwalker", "replace": "Ashwalker", "case_insensitive": False})
    assert r.json()["candidates"] == []

    r2 = client.post("/api/bulk-edit/preview", json={"find": "Skinwalker", "replace": "Ashwalker", "case_insensitive": True})
    assert len(r2.json()["candidates"]) == 1


def test_preview_no_matches_returns_empty(client, seed):
    _add_entity(seed.world_a.id, name="Elf", kind="race")
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/preview", json={"find": "Skinwalker", "replace": "Ashwalker"})
    assert r.json()["candidates"] == []
    assert r.json()["total_occurrences"] == 0


def test_preview_rejects_blank_find(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/preview", json={"find": "  ", "replace": "Y"})
    assert r.status_code == 400


def test_preview_scoped_to_active_world(client, seed):
    _add_entity(seed.world_a.id, name="Skinwalker A", kind="race")
    _add_entity(seed.world_b.id, name="Skinwalker B", kind="race")
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/preview", json={"find": "Skinwalker", "replace": "Ashwalker"})
    names = {c["target_name"] for c in r.json()["candidates"]}
    assert "Skinwalker A" in names
    assert "Skinwalker B" not in names


def test_player_cannot_call_preview(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/bulk-edit/preview", json={"find": "X", "replace": "Y"})
    assert r.status_code == 403


# ── /api/bulk-edit/apply ────────────────────────────────────────────────────

def test_apply_updates_selected_targets_only(client, seed):
    eid1 = _add_entity(seed.world_a.id, name="Skinwalker", kind="race", summary="A shapeshifter.")
    eid2 = _add_entity(seed.world_a.id, name="Other Skinwalker Mention", kind="note")
    _login_gm(client, seed)

    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [{"target_type": "entity", "target_id": eid1, "field": "name"}],
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    db = SessionLocal()
    try:
        e1 = db.query(Entity).filter(Entity.id == eid1).first()
        e2 = db.query(Entity).filter(Entity.id == eid2).first()
        assert e1.name == "Ashwalker"
        assert e2.name == "Other Skinwalker Mention"  # untouched — not selected
    finally:
        db.close()


def test_apply_updates_entity_note_content(client, seed):
    eid = _add_entity(seed.world_a.id, name="Whatever", kind="race")
    nid = _add_note(eid, "The Skinwalker clan is ancient.")
    _login_gm(client, seed)

    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [{"target_type": "entity_note", "target_id": nid, "field": "content"}],
    })
    assert r.json()["updated"] == 1

    db = SessionLocal()
    try:
        n = db.query(EntityNote).filter(EntityNote.id == nid).first()
        assert n.content == "The Ashwalker clan is ancient."
    finally:
        db.close()


def test_apply_updates_player_character_fields(client, seed):
    pcid = _add_pc(seed.world_a.id, name="Anders", race="Skinwalker", backstory="Raised among the Skinwalker.")
    _login_gm(client, seed)

    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [
            {"target_type": "player_character", "target_id": pcid, "field": "race"},
            {"target_type": "player_character", "target_id": pcid, "field": "backstory"},
        ],
    })
    assert r.json()["updated"] == 2

    db = SessionLocal()
    try:
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pcid).first()
        assert pc.race == "Ashwalker"
        assert pc.backstory == "Raised among the Ashwalker."
    finally:
        db.close()


def test_apply_recomputes_from_current_value_ignores_stale_target(client, seed):
    """Even if a target no longer contains `find` (edited/renamed since the
    preview was fetched), apply must not blindly trust a stale request —
    it recomputes from the row's CURRENT value and skips a no-op."""
    eid = _add_entity(seed.world_a.id, name="Already Renamed", kind="race")
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [{"target_type": "entity", "target_id": eid, "field": "name"}],
    })
    assert r.json()["updated"] == 0
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == eid).first().name == "Already Renamed"
    finally:
        db.close()


def test_apply_never_crosses_world_boundary(client, seed):
    other_eid = _add_entity(seed.world_b.id, name="Skinwalker", kind="race")
    _login_gm(client, seed)  # active world is world_a
    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [{"target_type": "entity", "target_id": other_eid, "field": "name"}],
    })
    assert r.json()["updated"] == 0
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == other_eid).first().name == "Skinwalker"
    finally:
        db.close()


def test_apply_rejects_no_targets(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/apply", json={"find": "X", "replace": "Y", "targets": []})
    assert r.status_code == 400


def test_apply_rejects_blank_find(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/apply", json={"find": "  ", "replace": "Y", "targets": [{"target_type": "entity", "target_id": 1, "field": "name"}]})
    assert r.status_code == 400


def test_player_cannot_call_apply(client, seed):
    eid = _add_entity(seed.world_a.id, name="Skinwalker", kind="race")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [{"target_type": "entity", "target_id": eid, "field": "name"}],
    })
    assert r.status_code == 403
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == eid).first().name == "Skinwalker"
    finally:
        db.close()


def test_apply_skips_unrecognized_field(client, seed):
    eid = _add_entity(seed.world_a.id, name="Skinwalker", kind="race")
    _login_gm(client, seed)
    r = client.post("/api/bulk-edit/apply", json={
        "find": "Skinwalker", "replace": "Ashwalker",
        "targets": [{"target_type": "entity", "target_id": eid, "field": "custom_fields_json"}],
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 0


# ── Page ────────────────────────────────────────────────────────────────────

def test_bulk_edit_page_loads_for_gm(client, seed):
    _login_gm(client, seed)
    r = client.get("/tools/bulk-edit")
    assert r.status_code == 200
    assert "Find" in r.text and "Replace" in r.text


def test_bulk_edit_page_forbidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/tools/bulk-edit")
    assert r.status_code == 403
