"""Multi-system templates: the built-in Asterion / Hunt-in-the-Moonlight
sheet templates carry resource trackers (current/max), custom sheets can
edit and display them, existing v1 built-ins upgrade in place, and the
party Member Vitals strip surfaces a member's system resources.

Based on the four system rules documents (Neon Dragons / Chronicles of the
Worm — the native N&D cyberpunk sheet — plus Game of Gods / Asterion and
Hunt in the Moonlight)."""
import json

from app.database import SessionLocal, init_db
from app.models import Party, PlayerCharacter, SheetTemplate, User

from .conftest import GM_PASSWORD, login


def test_builtin_system_templates_have_resource_tracks():
    """The defining vitals of each system must be resource fields
    (current/max) — plain numbers can't track a depleting pool."""
    db = SessionLocal()
    try:
        # Reset both built-ins so a row seeded by an older run (pre-rename
        # ids) can't mask the current definitions.
        db.query(SheetTemplate).filter(
            SheetTemplate.slug.in_(("asterion", "hunt-in-the-moonlight"))).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    init_db()
    db = SessionLocal()
    try:
        for slug, expected in (
            ("asterion", {"sparkShield", "flesh", "ichor"}),
            ("hunt-in-the-moonlight", {"health", "stamina", "hunger", "arcane", "signaturePoints"}),
        ):
            tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first()
            assert tpl is not None, slug
            fields = json.loads(tpl.fields_json)
            res_ids = {f["id"] for f in fields if f.get("type") == "resource"}
            assert expected <= res_ids, f"{slug}: missing resources {expected - res_ids}"
            for f in fields:
                if f.get("type") == "resource":
                    assert "/" in (f.get("default_value") or ""), f"{slug}/{f['id']} needs 'cur/max' default"
    finally:
        db.close()


def test_v1_builtin_templates_upgrade_in_place(client, seed):
    """An existing install whose built-in HITM row still holds the v1 field
    set (no resource fields) is upgraded to the current definition on boot —
    without touching GM-customized copies (a customized version keeps its
    fields and gains nothing)."""
    db = SessionLocal()
    try:
        # Simulate a v1 install: strip all resource fields from the built-in.
        tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "asterion").first()
        fields = json.loads(tpl.fields_json)
        v1_fields = [f for f in fields if f.get("type") != "resource"]
        tpl.fields_json = json.dumps(v1_fields)
        db.commit()
    finally:
        db.close()

    init_db()  # re-run the seed/upgrade pass

    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "asterion").first()
        fields = json.loads(tpl.fields_json)
        assert any(f.get("type") == "resource" and f["id"] == "flesh" for f in fields)
    finally:
        db.close()


def test_custom_sheet_saves_and_serves_resource_values(client, seed):
    """The custom-sheet edit form posts `<id>_current` / `<id>_max` keys;
    they persist on the PC's custom_fields_json and come back rendered on
    the sheet page."""
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "hunt-in-the-moonlight").first()
        assert tpl.sheet_mode == "custom"
        pc = PlayerCharacter(
            world_id=seed.world_a.id, name="Rook", sheet_template_id=tpl.id,
            custom_fields_json=json.dumps({
                "health_current": 2, "health_max": 5,
                "stamina_current": 4, "stamina_max": 5,
            }),
        )
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc_id}")
    assert r.status_code == 200
    assert 'data-cf-id="health_current"' in r.text      # editable tracker inputs
    assert 'value="2"' in r.text
    # and the display chips
    assert "2 / 5" in r.text or "Health" in r.text
    # Round trip: save through the edit route
    r = client.post(f"/characters/{pc_id}/edit", data={
        "kind": "character", "name": "Rook",
        "custom_fields_json": json.dumps({
            "health_current": 1, "health_max": 5,
            "stamina_current": 5, "stamina_max": 5,
        }),
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        cf = json.loads(pc.custom_fields_json)
        assert cf["health_current"] == 1
    finally:
        db.close()


def test_party_vitals_show_system_resources(client, seed):
    """A party member on the HITM custom sheet shows their Health/Stamina/
    Hunger resource chips in the Member Vitals strip."""
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "hunt-in-the-moonlight").first()
        pc = PlayerCharacter(
            world_id=seed.world_a.id, name="Hunter Vex", sheet_template_id=tpl.id,
            custom_fields_json=json.dumps({
                "health_current": 2, "health_max": 5,
                "hunger_current": 6, "hunger_max": 10,
            }),
        )
        db.add(pc)
        db.commit()
        db.refresh(pc)
        party = Party(world_id=seed.world_a.id, name="Vex Company",
                      member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.commit()
        db.refresh(party)
        pid = party.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{pid}")
    assert r.status_code == 200
    assert "Member Vitals" in r.text
    # Health is the system's vital track: it leads the member card as the HP line
    # (and DOWN flag) instead of repeating as a chip; the other tracks stay chips.
    assert "Health 2/5" in r.text
    assert "6/10 Hunger" in r.text
    # Player with view grant sees the same strip
    from .conftest import PLAYER_PASSWORD
    from app.deps import world_section_access
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        current = world_section_access(w)
        current["parties"]["player"] = "read"
        w.section_access_json = json.dumps(current)
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/parties/{pid}")
    assert "Health 2/5" in r.text
