"""Tests for the Parties feature (app/routers/parties.py): creating a
party with a name, and renaming one afterward via the party detail page's
edit form — previously the detail page's edit form had no name input at
all, so a party stuck with its create-time name (or the "New Party"
default) could never be renamed through the UI even though the server
route already accepted a `name` field.
"""
import json

from app.database import SessionLocal
from app.models import Party

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_party(world_id, name="New Party"):
    db = SessionLocal()
    try:
        p = Party(world_id=world_id, name=name)
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def test_party_create_uses_given_name(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/parties/new", data={"name": "The Silver Vanguard"}, follow_redirects=False)
    assert r.status_code == 303
    party_id = int(r.headers["location"].rsplit("/", 1)[-1])
    db = SessionLocal()
    try:
        assert db.get(Party, party_id).name == "The Silver Vanguard"
    finally:
        db.close()


def test_party_detail_edit_form_has_a_name_input(client, seed):
    party_id = _make_party(seed.world_a.id, name="New Party")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{party_id}")
    assert r.status_code == 200
    assert 'name="name"' in r.text
    assert 'value="New Party"' in r.text


def test_party_rename_via_edit_form(client, seed):
    party_id = _make_party(seed.world_a.id, name="New Party")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{party_id}/edit", data={"name": "The Ashfall Company", "notes": "met in a tavern"},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        party = db.get(Party, party_id)
        assert party.name == "The Ashfall Company"
        assert party.notes == "met in a tavern"
    finally:
        db.close()

    r2 = client.get(f"/parties/{party_id}")
    assert "The Ashfall Company" in r2.text


def test_party_edit_blank_name_keeps_existing_name(client, seed):
    """The route falls back to the existing name rather than blanking it out
    — matches _apply_form-style conventions elsewhere in this app where an
    empty submitted value doesn't clobber a required field."""
    party_id = _make_party(seed.world_a.id, name="Original Name")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{party_id}/edit", data={"name": "   ", "notes": ""}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(Party, party_id).name == "Original Name"
    finally:
        db.close()


# ── Round-2 coverage: view grants, edit levels, loot, launch-combat, ─────────
#    vitals/history (previously only create/rename were tested).

def _add_pc(world_id, owner=None, name="PC", hp=20, max_hp=30, ac=15, level=3):
    from app.models import PlayerCharacter
    db = SessionLocal()
    try:
        pc = PlayerCharacter(
            world_id=world_id, name=name, owner_user_id=owner.id if owner else None,
            current_hp=hp, max_hp=max_hp, armor_class=ac, level=level,
        )
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _party_with(world_id, pc_ids=(), name="The Party"):
    db = SessionLocal()
    try:
        p = Party(world_id=world_id, name=name, member_pc_ids_json=json.dumps(list(pc_ids)))
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def _pin(client, slug="world-a"):
    client.cookies.set("active_world", slug)


def _open_parties_for_players(world_id):
    """Default parties player level is 'none' — open read so membership
    gates (not the section gate) are what each test exercises."""
    from app.deps import world_section_access
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        current = world_section_access(w)
        current["parties"]["player"] = "edit"
        w.section_access_json = json.dumps(current)
        db.commit()
    finally:
        db.close()


def _make_member_player(world_id, pc_name="Member PC"):
    """A player whose owned PC becomes the party member — member-level editor."""
    from app import auth as auth_module
    from app.models import PlayerCharacter, User, WorldMembership
    db = SessionLocal()
    try:
        player = User(email=f"{pc_name.lower().replace(' ', '')}@test.local",
                      password_hash=auth_module.hash_password(PLAYER_PASSWORD),
                      display_name=pc_name + " Player", is_gm=False)
        db.add(player)
        db.commit()
        db.refresh(player)
        db.add(WorldMembership(world_id=world_id, user_id=player.id))
        pc = PlayerCharacter(world_id=world_id, name=pc_name, owner_user_id=player.id,
                             current_hp=20, max_hp=30, armor_class=15, level=3)
        db.add(pc)
        db.commit()
        db.refresh(player)
        db.refresh(pc)
        return player, pc.id
    finally:
        db.close()


def test_player_without_view_grant_gets_404(client, seed):
    """Parties default to player 'none' in the section matrix."""
    pid = _make_party(seed.world_a.id, name="Hidden")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.get(f"/parties/{pid}").status_code == 404


def test_cross_world_party_not_readable_by_scoped_player(client, seed):
    """world_row_visible checks the CALLER's membership in the party's own
    world — a world-A player can't read world-B parties by walking ids."""
    pid = _make_party(seed.world_b.id, name="World B Party")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client, "world-a")
    assert client.get(f"/parties/{pid}").status_code in (403, 404)


def test_member_player_edits_notes_but_not_membership(client, seed):
    player, pc_id = _make_member_player(seed.world_a.id)
    pid = _party_with(seed.world_a.id, pc_ids=[pc_id], name="Member Party")
    _open_parties_for_players(seed.world_a.id)
    login(client, player.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post(f"/parties/{pid}/edit", data={
        "notes": "The party agreed to split the take.",
        "name": "Renamed By Member",                    # must be ignored
        "member_pc_ids": str(pc_id),                    # must be ignored
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        p = db.get(Party, pid)
        assert p.notes == "The party agreed to split the take."
        assert p.name == "Member Party"
        assert json.loads(p.member_pc_ids_json) == [pc_id]
    finally:
        db.close()


def test_gm_edit_changes_membership_and_name(client, seed):
    pc_id = _add_pc(seed.world_a.id, name="Joiner")
    pid = _make_party(seed.world_a.id, name="Before")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/parties/{pid}/edit", data={
        "name": "After", "notes": "", "member_pc_ids": str(pc_id),
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        p = db.get(Party, pid)
        assert p.name == "After"
        assert json.loads(p.member_pc_ids_json) == [pc_id]
    finally:
        db.close()


def test_loot_add_and_remove_as_member(client, seed):
    player, pc_id = _make_member_player(seed.world_a.id)
    pid = _party_with(seed.world_a.id, pc_ids=[pc_id])
    _open_parties_for_players(seed.world_a.id)
    login(client, player.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post(f"/api/parties/{pid}/loot", json={
        "action": "add", "name": "Potion of harbor-walking", "qty": 2, "notes": "sticky",
    })
    assert r.status_code == 200
    db = SessionLocal()
    try:
        loot = json.loads(db.get(Party, pid).loot_json)
        assert loot == [{"name": "Potion of harbor-walking", "qty": 2, "notes": "sticky"}]
    finally:
        db.close()
    r = client.post(f"/api/parties/{pid}/loot", json={"action": "remove", "index": 0})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert json.loads(db.get(Party, pid).loot_json) == []
    finally:
        db.close()


def test_launch_combat_builds_roster_and_records_party(client, seed):
    from app.models import CombatSession, Entity
    pc_id = _add_pc(seed.world_a.id, name="Fighter", hp=10, max_hp=30, ac=18)
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="creature", name="Pack Mule",
                     visible_to_players=True)
        db.add(ent)
        db.commit()
        db.refresh(ent)
        entity_id = ent.id
    finally:
        db.close()
    pid = _party_with(seed.world_a.id, pc_ids=[pc_id], name="Combat Party")
    db = SessionLocal()
    try:
        p = db.get(Party, pid)
        p.member_entity_ids_json = json.dumps([entity_id])
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/parties/{pid}/launch-combat")
    assert r.status_code == 200
    cs_id = int(r.json()["redirect"].rsplit("/", 1)[1])
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cs_id)
        assert cs.party_id == pid                       # history link recorded
        names = {c["name"] for c in json.loads(cs.combatants_json)}
        assert "Fighter" in names and "Pack Mule" in names
    finally:
        db.close()


def test_launch_combat_rejected_for_member_level(client, seed):
    player, pc_id = _make_member_player(seed.world_a.id)
    pid = _party_with(seed.world_a.id, pc_ids=[pc_id])
    _open_parties_for_players(seed.world_a.id)
    login(client, player.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post(f"/api/parties/{pid}/launch-combat").status_code == 403


def test_party_detail_shows_vitals_and_history(client, seed):
    from app.models import CombatSession
    pc_id = _add_pc(seed.world_a.id, name="Wounded", hp=3, max_hp=30, ac=17)
    pid = _party_with(seed.world_a.id, pc_ids=[pc_id], name="Vitals Party")
    db = SessionLocal()
    try:
        db.add(CombatSession(world_id=seed.world_a.id, name="Bar Fight",
                             party_id=pid, combatants_json="[]"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.get(f"/parties/{pid}")
    assert r.status_code == 200
    assert "Member Vitals" in r.text
    assert "HP 3/30" in r.text and "AC 17" in r.text
    assert "Bar Fight" in r.text
    assert "Party History" in r.text
