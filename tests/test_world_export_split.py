"""Per-category world export (app/routers/export.py): GET
/worlds/{id}/export/split (the picker page) plus separate downloads for
rules, player characters, sheet templates, and entities-by-kind. Covers
GM-only access, correct scoping to the requested world, and that each
download's shape round-trips through the /import page's own detect_kind()
(same JSON shapes entity_bulk/player_character_bulk/world_rules/
field_template already expect).
"""
import json

from app.database import SessionLocal
from app.models import Entity, PlayerCharacter, SheetTemplate
from app.routers.importer import detect_kind

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add_entity(world_id, kind, name, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, name=name, **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e
    finally:
        db.close()


def _add_pc(world_id, name, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, name=name, **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def _add_sheet_template(world_id, name, slug, fields):
    db = SessionLocal()
    try:
        t = SheetTemplate(world_id=world_id, name=name, slug=slug, sheet_mode="custom",
                           fields_json=json.dumps(fields))
        db.add(t)
        db.commit()
        db.refresh(t)
        return t
    finally:
        db.close()


def test_export_split_page_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/export/split")
    assert r.status_code == 403


def test_export_split_page_shows_counts(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _add_entity(seed.world_a.id, "race", "Test Race")
    _add_entity(seed.world_a.id, "note", "Test Note")
    r = client.get(f"/worlds/{seed.world_a.id}/export/split")
    assert r.status_code == 200
    assert "race" in r.text.lower()
    assert "note" in r.text.lower()


def test_export_split_page_404_for_unknown_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/worlds/999999/export/split")
    assert r.status_code == 404


def test_export_entities_by_kind_scoped_and_shaped(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _add_entity(seed.world_a.id, "race", "Race In A", subtype="standard", folder="Races", tags="race",
                summary="s", body="b")
    _add_entity(seed.world_a.id, "profession", "Profession In A")
    _add_entity(seed.world_b.id, "race", "Race In B")

    r = client.get(f"/worlds/{seed.world_a.id}/export/entities/race.json")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["name"] == "Race In A"
    assert data[0]["kind"] == "race"
    assert data[0]["subtype"] == "standard"
    assert data[0]["body"] == "b"

    # re-importable: detect_kind must recognize this as entity_bulk
    detected = detect_kind(data)
    assert detected["kind"] == "entity_bulk"
    assert detected["count"] == 1


def test_export_entities_by_kind_unknown_kind_404(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/export/entities/not-a-kind.json")
    assert r.status_code == 404


def test_export_entities_by_kind_empty_returns_empty_array(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/export/entities/creature.json")
    assert r.status_code == 200
    assert r.json() == []


def test_export_rules_present(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.get(type(seed.world_a), seed.world_a.id)
        w.rules_md = "# Custom Rules\n\nSome content."
        db.commit()
    finally:
        db.close()

    r = client.get(f"/worlds/{seed.world_a.id}/export/rules.json")
    assert r.status_code == 200
    data = r.json()
    assert data["rules_md"] == "# Custom Rules\n\nSome content."
    assert detect_kind(data)["kind"] == "world_rules"


def test_export_rules_absent_404(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/export/rules.json")
    assert r.status_code == 404


def test_export_player_characters_scoped_and_shaped(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _add_pc(seed.world_a.id, "PC In A", race="Human", char_class="Hacker", max_hp=10, current_hp=8)
    _add_pc(seed.world_b.id, "PC In B")

    r = client.get(f"/worlds/{seed.world_a.id}/export/player-characters.json")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["name"] == "PC In A"
    assert data[0]["race"] == "Human"
    assert data[0]["max_hp"] == 10

    detected = detect_kind(data)
    assert detected["kind"] == "player_character_bulk"
    assert detected["count"] == 1


def test_export_player_characters_preserves_custom_fields(client, seed):
    """A character on a custom sheet_mode template stores its data in
    custom_fields_json — must survive export (this is exactly what the NDC
    export format used elsewhere would silently drop)."""
    login(client, seed.gm.email, GM_PASSWORD)
    _add_pc(seed.world_a.id, "Custom Sheet PC", custom_fields_json=json.dumps({"health": "5", "shock": "5"}))

    r = client.get(f"/worlds/{seed.world_a.id}/export/player-characters.json")
    data = r.json()
    assert data[0]["custom_fields"] == {"health": "5", "shock": "5"}


def test_export_sheet_template(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    fields = [{"id": "health", "label": "Health", "type": "number", "section": "Resources", "default_value": "5"}]
    t = _add_sheet_template(seed.world_a.id, "Test Template", "test-template", fields)

    r = client.get(f"/worlds/{seed.world_a.id}/export/templates/{t.id}.json")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Test Template"
    assert data["fields"] == fields

    detected = detect_kind(data)
    assert detected["kind"] == "field_template"


def test_export_sheet_template_wrong_world_404(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    t = _add_sheet_template(seed.world_a.id, "A's Template", "a-template", [])
    r = client.get(f"/worlds/{seed.world_b.id}/export/templates/{t.id}.json")
    assert r.status_code == 404


def test_export_then_reimport_races_round_trips(client, seed):
    """The real end-to-end proof: export a kind, feed the exact bytes back
    through /api/import/execute (the same endpoint the /import page uses),
    and confirm it lands as a matching entity — not just that detect_kind()
    recognizes the shape."""
    login(client, seed.gm.email, GM_PASSWORD)
    _add_entity(seed.world_a.id, "race", "Roundtrip Race", subtype="advanced",
                folder="Races/Advanced", tags="race,advanced", summary="A test race.",
                body="## Abilities\nSome ability text.")

    exported = client.get(f"/worlds/{seed.world_a.id}/export/entities/race.json").json()
    assert len(exported) == 1

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.post("/api/import/execute", json={"json_text": json.dumps(exported)})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = SessionLocal()
    try:
        imported = db.query(Entity).filter(Entity.world_id == seed.world_b.id, Entity.kind == "race").first()
        assert imported is not None
        assert imported.name == "Roundtrip Race"
        assert imported.subtype == "advanced"
        assert imported.body == "## Abilities\nSome ability text."
    finally:
        db.close()


def test_export_sheet_template_builtin_not_exportable(client, seed):
    """Built-in templates (world_id=NULL) aren't world-scoped and every
    install already has them — they shouldn't resolve under any world's
    export path."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        builtin = db.query(SheetTemplate).filter(SheetTemplate.slug == "nd-default").first()
        assert builtin is not None
        builtin_id = builtin.id
    finally:
        db.close()
    r = client.get(f"/worlds/{seed.world_a.id}/export/templates/{builtin_id}.json")
    assert r.status_code == 404
