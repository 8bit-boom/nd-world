"""Tests for the three generic PlayerCharacter export routes added alongside
.ndc/.foundry.json: GET /characters/{pc_id}/export.json (re-importable via
POST /api/import/execute, kind=player_character), export.md (a readable
Markdown sheet), and export.pdf (a printable PDF) — see
app/routers/characters.py's _pc_export_sections/_pc_to_import_dict/
_pc_to_markdown/_pc_to_pdf_bytes and docs/IMPORT_JSON_GUIDE.md.
"""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


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


def _add_custom_template(world_id, fields):
    db = SessionLocal()
    try:
        t = SheetTemplate(
            world_id=world_id, name="Hunter Sheet", slug="hunter-sheet-test",
            sheet_mode="custom", fields_json=json.dumps(fields),
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return t.id
    finally:
        db.close()


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── Access control ──────────────────────────────────────────────────────────

def test_gm_can_export_all_three_formats(client, seed):
    pc_id = _add_pc(seed.world_a.id, name="Anders de Valliere")
    _login_gm(client, seed)
    for ext, content_type in (("json", "application/json"), ("md", "text/markdown"), ("pdf", "application/pdf")):
        r = client.get(f"/characters/{pc_id}/export.{ext}")
        assert r.status_code == 200, (ext, r.text[:200])
        assert r.headers["content-type"].startswith(content_type)
        assert f'.{ext}"' in r.headers["content-disposition"]


def test_owning_player_can_export(client, seed):
    pc_id = _add_pc(seed.world_a.id, name="Anders", owner_user_id=seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc_id}/export.json")
    assert r.status_code == 200


def test_other_player_cannot_export(client, seed):
    pc_id = _add_pc(seed.world_a.id, name="Anders", owner_user_id=seed.player_a.id)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc_id}/export.json")
    assert r.status_code == 403


def test_export_404_for_missing_character(client, seed):
    _login_gm(client, seed)
    r = client.get("/characters/999999/export.json")
    assert r.status_code == 404


# ── export.json ──────────────────────────────────────────────────────────────

def test_export_json_contains_expected_fields_and_omits_empty_ones(client, seed):
    pc_id = _add_pc(
        seed.world_a.id, name="Anders de Valliere", race="Human", char_class="Widow-Bound Hunter",
        level=3, xp=450, backstory="Hunts the beasts that took his mother's life.",
        equipment_json=json.dumps(["Grandcouteau (greatknife)"]),
    )
    _login_gm(client, seed)
    r = client.get(f"/characters/{pc_id}/export.json")
    data = r.json()
    assert data["name"] == "Anders de Valliere"
    assert data["race"] == "Human"
    assert data["char_class"] == "Widow-Bound Hunter"
    assert data["level"] == 3
    assert data["xp"] == 450
    assert "Hunts the beasts" in data["backstory"]
    assert data["equipment_json"] == ["Grandcouteau (greatknife)"]
    # Never-set fields shouldn't clutter the export.
    assert "player_name" not in data
    assert "feats_json" not in data


def test_export_json_round_trips_through_import_execute(client, seed):
    pc_id = _add_pc(
        seed.world_a.id, name="Anders de Valliere", race="Human", level=2,
        backstory="Original backstory.",
    )
    _login_gm(client, seed)
    draft = client.get(f"/characters/{pc_id}/export.json").json()
    draft["backstory"] = "Updated after a re-import round trip."

    r = client.post("/api/import/execute", json={"json_text": json.dumps(draft), "kind": "player_character"})
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        matches = db.query(PlayerCharacter).filter(
            PlayerCharacter.world_id == seed.world_a.id, PlayerCharacter.name == "Anders de Valliere",
        ).all()
        # Upserted by name, not duplicated.
        assert len(matches) == 1
        assert matches[0].id == pc_id
        assert matches[0].backstory == "Updated after a re-import round trip."
    finally:
        db.close()


# ── export.md ────────────────────────────────────────────────────────────────

def test_export_md_native_sheet_includes_resources_and_prose(client, seed):
    pc_id = _add_pc(
        seed.world_a.id, name="Kestrel", race="Cyborg", char_class="Fixer", level=4,
        current_hp=10, max_hp=15, backstory="A backstory.", notes="Some session notes.",
        equipment_json=json.dumps([{"name": "Pistol", "qty": 1}]),
    )
    _login_gm(client, seed)
    r = client.get(f"/characters/{pc_id}/export.md")
    md = r.text
    assert md.startswith("# Kestrel")
    assert "Cyborg" in md and "Fixer" in md and "Level 4" in md
    assert "## Resources" in md
    assert "10/15" in md
    assert "## Equipment" in md
    assert "Pistol" in md
    assert "## Backstory" in md and "A backstory." in md
    assert "## Notes" in md and "Some session notes." in md


def test_export_md_custom_sheet_uses_template_fields_not_native_sections(client, seed):
    tpl_id = _add_custom_template(seed.world_a.id, [
        {"id": "hunt_sentence", "label": "Sentence", "type": "textarea", "section": "The Hunter Sentence"},
        {"id": "abilities", "label": "Ability", "type": "list", "section": "Abilities",
         "item_fields": [{"id": "name", "label": "Name"}, {"id": "effect", "label": "Effect"}]},
    ])
    pc_id = _add_pc(
        seed.world_a.id, name="Anders", sheet_template_id=tpl_id,
        custom_fields_json=json.dumps({
            "hunt_sentence": "I use a hunter's craft because of my father's cowardice.",
            "abilities": [{"name": "Beastfall Stride", "effect": "+1 move"}],
        }),
    )
    _login_gm(client, seed)
    r = client.get(f"/characters/{pc_id}/export.md")
    md = r.text
    assert "## The Hunter Sentence" in md
    assert "hunter's craft" in md
    assert "## Abilities" in md
    assert "Beastfall Stride" in md
    assert "Name: Beastfall Stride" in md and "Effect: +1 move" in md
    # Custom-mode sheets don't have N&D's fixed ability-score/equipment sections.
    assert "## Ability Scores" not in md
    assert "## Equipment" not in md


# ── export.pdf ───────────────────────────────────────────────────────────────

def test_export_pdf_returns_valid_pdf_bytes(client, seed):
    pc_id = _add_pc(seed.world_a.id, name="Anders", backstory="Some backstory text.")
    _login_gm(client, seed)
    r = client.get(f"/characters/{pc_id}/export.pdf")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
