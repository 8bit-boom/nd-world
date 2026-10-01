"""Party roster comparison page and the one-file .ndc bundle export.

GET /parties/{id}/roster lays the members side by side (stats, effective maxima,
CA/Speed, edges, cyberware, conditions, custom-sheet resource tracks). The full
tier (GM, or an assistant with Parties:edit in their own world) always sees it; a
player only when the world lets players see the party AND the Parties section is
readable to them. GM companions never appear for non-GMs.
GET /parties/{id}/export.ndc is the GM's bundle: every member character in the
same JSON array format a single-character export uses (importable into the
Android app / desktop editor).
"""
import json

from app.database import SessionLocal
from app.models import Entity, Party, PlayerCharacter, SheetTemplate, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

STATS = [{"id": k, "value": v} for k, v in
         (("str", 3), ("dex", 4), ("bod", 3), ("per", 2), ("wil", 2), ("int", 5), ("cha", 1), ("itu", 2))]


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _set(model, pk, **fields):
    db = SessionLocal()
    try:
        row = db.get(model, pk)
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def _scene(seed):
    vex = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Vex", level=3,
                               race="Human", char_class="Rogue", stats_json=json.dumps(STATS), max_hp=0, current_hp=9,
                               minor_edge="Lucky", major_edge="Veteran", conditions_json=json.dumps(["Burn"]),
                               cyberware_json=json.dumps([{"name": "Optic Implant"}, {"name": "Neural Link"}])))
    mira = _add(PlayerCharacter(world_id=seed.world_a.id, name="Mira", level=5, race="Elf", char_class="Mage",
                                stats_json=json.dumps(STATS), max_hp=30, current_hp=30))
    spy = _add(Entity(world_id=seed.world_a.id, kind="character", name="Secret Spy", visible_to_players=False))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([vex, mira]),
                       member_entity_ids_json=json.dumps([spy])))
    other = _add(PlayerCharacter(world_id=seed.world_a.id, name="Outsider"))
    other_party = _add(Party(world_id=seed.world_a.id, name="Rivals", member_pc_ids_json=json.dumps([other])))
    return vex, mira, party, other_party


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _levels(world, **levels):
    _set(World, world.id, section_access_json=json.dumps(levels))


# ── roster ───────────────────────────────────────────────────────────────────

def test_gm_sees_everything_side_by_side(client, seed):
    vex, mira, party, _ = _scene(seed)
    _gm(client, seed)
    r = client.get(f"/parties/{party}/roster")
    assert r.status_code == 200
    html = r.text
    for needle in ("Vex", "Mira", "Human", "Elf", "Rogue", "Mage", "Lucky", "Veteran", "Optic Implant", "Neural Link", "Burn"):
        assert needle in html, needle
    assert "9 / 22" in html or "9/22" in html, "HP uses the effective (stat-derived) max"
    assert "30" in html
    assert "Outsider" not in html, "only this party's members"
    assert "@media print" in html


def test_gm_sees_companions_but_a_player_never_sees_hidden_ones(client, seed):
    _, _, party, _ = _scene(seed)
    _set(World, seed.world_a.id, players_see_party=True)
    _levels(seed.world_a, parties={"player": "read"})
    _gm(client, seed)
    assert "Secret Spy" in client.get(f"/parties/{party}/roster").text
    client.post("/logout")
    _player(client, seed)
    r = client.get(f"/parties/{party}/roster")
    assert r.status_code == 200 and "Secret Spy" not in r.text and "Mira" in r.text


def test_player_needs_players_see_party_and_parties_read(client, seed):
    _, _, party, _ = _scene(seed)
    _player(client, seed)
    _levels(seed.world_a, parties={"player": "read"})
    _set(World, seed.world_a.id, players_see_party=False)
    assert client.get(f"/parties/{party}/roster").status_code == 404, "players_see_party is off"
    _set(World, seed.world_a.id, players_see_party=True)
    _levels(seed.world_a, parties={"player": "none"})
    assert client.get(f"/parties/{party}/roster").status_code in (403, 404), "Parties is closed to players"
    _levels(seed.world_a, parties={"player": "read"})
    assert client.get(f"/parties/{party}/roster").status_code == 200


def test_other_worlds_party_is_not_visible(client, seed):
    _, _, party, _ = _scene(seed)
    _set(World, seed.world_a.id, players_see_party=True)
    _levels(seed.world_a, parties={"player": "read"})
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert client.get(f"/parties/{party}/roster").status_code in (403, 404)
    assert client.get("/parties/99999/roster").status_code in (403, 404)


def test_assistant_with_parties_edit_sees_it_even_if_players_cant(client, seed):
    _, _, party, _ = _scene(seed)
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_a.id,
                                             WorldMembership.user_id == seed.player_a.id).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()
    _levels(seed.world_a, parties={"assistant": "edit", "player": "read"})
    _set(World, seed.world_a.id, players_see_party=False)
    _player(client, seed)
    assert client.get(f"/parties/{party}/roster").status_code == 200


def test_custom_sheet_members_show_their_resource_tracks(client, seed):
    db = SessionLocal()
    try:
        tpl = next((t for t in db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").all()
                    if any(f.get("type") == "resource" for f in json.loads(t.fields_json or "[]"))), None)
        if tpl is None:
            tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
            tpl.fields_json = json.dumps([{"id": "grit", "label": "Grit", "type": "resource"}])
            db.commit()
        field = next(f for f in json.loads(tpl.fields_json) if f.get("type") == "resource")
        tpl_id = tpl.id
    finally:
        db.close()
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Hunter", sheet_template_id=tpl_id,
                              custom_fields_json=json.dumps({f"{field['id']}_current": 4, f"{field['id']}_max": 7})))
    party = _add(Party(world_id=seed.world_a.id, name="Pack", member_pc_ids_json=json.dumps([pc])))
    _gm(client, seed)
    html = client.get(f"/parties/{party}/roster").text
    from app.sheet_systems import short_label
    assert "Hunter" in html and short_label(field.get("label", field["id"])) in html and "4" in html and "7" in html


def test_roster_link_is_on_the_party_page(client, seed):
    _, _, party, _ = _scene(seed)
    _gm(client, seed)
    html = client.get(f"/parties/{party}").text
    assert f"/parties/{party}/roster" in html


# ── .ndc bundle ──────────────────────────────────────────────────────────────

def test_gm_exports_the_whole_party_as_one_ndc(client, seed):
    vex, mira, party, _ = _scene(seed)
    _gm(client, seed)
    r = client.get(f"/parties/{party}/export.ndc")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and r.headers["content-disposition"].endswith('.ndc"')
    chars = json.loads(r.text)
    assert isinstance(chars, list) and sorted(c["name"] for c in chars) == ["Mira", "Vex"]
    single = json.loads(client.get(f"/characters/{vex}/export.ndc").text)[0]
    assert next(c for c in chars if c["name"] == "Vex") == single, "same schema as the single-character export"


def test_export_is_gm_only(client, seed):
    _, _, party, _ = _scene(seed)
    _set(World, seed.world_a.id, players_see_party=True)
    _levels(seed.world_a, parties={"player": "read"})
    _player(client, seed)
    assert client.get(f"/parties/{party}/export.ndc").status_code in (403, 404)
    client.post("/logout")
    _gm(client, seed)
    assert client.get("/parties/99999/export.ndc").status_code == 404
    empty = _add(Party(world_id=seed.world_a.id, name="Empty"))
    r = client.get(f"/parties/{empty}/export.ndc")
    assert r.status_code == 200 and json.loads(r.text) == []
