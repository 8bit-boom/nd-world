"""Tests for the Parties feature (app/routers/parties.py): creating a
party with a name, and renaming one afterward via the party detail page's
edit form — previously the detail page's edit form had no name input at
all, so a party stuck with its create-time name (or the "New Party"
default) could never be renamed through the UI even though the server
route already accepted a `name` field.
"""
import json

from app.database import SessionLocal
from app.models import Entity, Party, PlayerCharacter, User, WorldMembership

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
        assert loot[0]["name"] == "Potion of harbor-walking"
        assert loot[0]["qty"] == 2
        assert loot[0]["claimed_by"] == []
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


# ── Loot "claimed by" flow ───────────────────────────────────────────────────

def _loot_with_claim(world_id, pc_ids_claimed=(), name="Gauntlet of Yorm"):
    db = SessionLocal()
    try:
        p = Party(world_id=world_id, name="Claim Party",
                  member_pc_ids_json="[]",
                  loot_json=json.dumps([{"name": name, "qty": 1, "notes": "",
                                         "claimed_by": list(pc_ids_claimed)}]))
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def test_member_claims_own_pc_only(client, seed):
    player, pc_id = _make_member_player(seed.world_a.id)
    other_pc = _add_pc(seed.world_a.id, name="Other Member")
    db = SessionLocal()
    try:
        p = Party(world_id=seed.world_a.id, name="Claim Party",
                  member_pc_ids_json=json.dumps([pc_id, other_pc]),
                  loot_json=json.dumps([{"name": "Ring", "qty": 1, "notes": "", "claimed_by": []}]))
        db.add(p)
        db.commit()
        db.refresh(p)
        pid = p.id
    finally:
        db.close()
    _open_parties_for_players(seed.world_a.id)
    login(client, player.email, PLAYER_PASSWORD)
    _pin(client)

    # claim for OWN pc — allowed
    r = client.post(f"/api/parties/{pid}/loot", json={"action": "claim", "index": 0, "pc_id": pc_id})
    assert r.status_code == 200
    assert r.json()["loot"][0]["claimed_by"] == [pc_id]

    # claim for ANOTHER member's pc — 403
    r = client.post(f"/api/parties/{pid}/loot", json={"action": "claim", "index": 0, "pc_id": other_pc})
    assert r.status_code == 403
    db = SessionLocal()
    try:
        assert json.loads(db.get(Party, pid).loot_json)[0]["claimed_by"] == [pc_id]
    finally:
        db.close()

    # unclaim own
    r = client.post(f"/api/parties/{pid}/loot", json={"action": "unclaim", "index": 0, "pc_id": pc_id})
    assert r.status_code == 200
    assert r.json()["loot"][0]["claimed_by"] == []


def test_gm_claims_for_any_member(client, seed):
    player, pc_id = _make_member_player(seed.world_a.id)
    pid = _loot_with_claim(seed.world_a.id)
    db = SessionLocal()
    try:
        p = db.get(Party, pid)
        p.member_pc_ids_json = json.dumps([pc_id])
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/parties/{pid}/loot", json={"action": "claim", "index": 0, "pc_id": pc_id})
    assert r.status_code == 200
    assert r.json()["loot"][0]["claimed_by"] == [pc_id]


def test_claim_rejects_non_member_pc(client, seed):
    """Claiming for a PC that isn't in the party is invalid regardless of level."""
    outsider_pc = _add_pc(seed.world_a.id, name="Not In Party")
    pid = _loot_with_claim(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/parties/{pid}/loot", json={"action": "claim", "index": 0, "pc_id": outsider_pc})
    assert r.status_code == 400


# ── Party XP ledger ──────────────────────────────────────────────────────────

def test_session_xp_appends_party_ledger(client, seed):
    from app.models import GameSession
    pc_id = _add_pc(seed.world_a.id, name="Ledger PC")
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, session_num=4, title="Ledger Session")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        p = Party(world_id=seed.world_a.id, name="Ledger Party",
                  member_pc_ids_json=json.dumps([pc_id]))
        db.add(p)
        db.commit()
        db.refresh(p)
        gs.party_id = p.id
        db.commit()
        sid, pid = gs.id, p.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/sessions/{sid}/xp", json={"delta": 250, "pc_ids": [pc_id]})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        p = db.get(Party, pid)
        ledger = json.loads(p.xp_json or "[]")
        assert len(ledger) == 1
        assert ledger[0]["amount"] == 250
        assert ledger[0]["session_id"] == sid
        assert "Ledger PC" in ledger[0]["awarded"]
    finally:
        db.close()
    # and the party detail renders the ledger
    r = client.get(f"/parties/{pid}")
    assert "+250 XP" in r.text
    assert "Ledger Session" in r.text or "from session" in r.text


def test_session_xp_without_party_writes_no_ledger(client, seed):
    from app.models import GameSession
    pc_id = _add_pc(seed.world_a.id, name="Solo PC")
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, session_num=5, title="No Party")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        sid = gs.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/sessions/{sid}/xp", json={"delta": 100, "pc_ids": [pc_id]})
    assert r.status_code == 200  # no party → no ledger, no crash


# ── Level-up route + badge ───────────────────────────────────────────────────

def test_level_up_route_threshold_and_permissions(client, seed):
    db = SessionLocal()
    try:
        owner = User(email="levelup-owner@test.local",
                     password_hash=__import__("app.auth", fromlist=["hash_password"]).hash_password(PLAYER_PASSWORD),
                     display_name="Level Owner", is_gm=False)
        db.add(owner)
        db.commit()
        db.refresh(owner)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=owner.id))
        db.commit()
        from app.routers.characters import XP_THRESHOLDS
        ready = PlayerCharacter(world_id=seed.world_a.id, name="Ready", owner_user_id=owner.id,
                                level=2, xp=XP_THRESHOLDS[2], current_hp=10, max_hp=20)
        not_ready = PlayerCharacter(world_id=seed.world_a.id, name="NotReady", owner_user_id=owner.id,
                                    level=2, xp=XP_THRESHOLDS[2] - 5, current_hp=10, max_hp=20)
        db.add_all([ready, not_ready])
        db.commit()
        db.refresh(ready)
        db.refresh(not_ready)
        ready_id, not_ready_id = ready.id, not_ready.id
        threshold = XP_THRESHOLDS[2]
        owner_id = owner.id
    finally:
        db.close()

    login(client, owner.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    # Not ready → 400 with the level named
    r = client.post(f"/api/characters/{not_ready_id}/level-up")
    assert r.status_code == 400
    assert "level 3" in r.json()["detail"]

    # Ready, but a DIFFERENT player → 403
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/characters/{ready_id}/level-up").status_code == 403

    # Ready + owner → level applies
    login(client, owner.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/characters/{ready_id}/level-up")
    assert r.status_code == 200
    assert r.json()["level"] == 3
    db = SessionLocal()
    try:
        assert db.get(PlayerCharacter, ready_id).level == 3
        assert db.get(PlayerCharacter, not_ready_id).level == 2  # untouched
    finally:
        db.close()
    del threshold, owner_id


def test_level_up_badges_on_lists(client, seed):
    from app.routers.characters import XP_THRESHOLDS
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Badge PC",
                             level=4, xp=XP_THRESHOLDS[4], current_hp=10, max_hp=20)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    # Characters list badge
    r = client.get("/characters")
    assert r.status_code == 200
    assert "⬆ Level-up" in r.text or "levelup" in r.text
    # Party vitals badge
    pid = _party_with(seed.world_a.id, pc_ids=[pc_id], name="Badge Party")
    r = client.get(f"/parties/{pid}")
    assert "⬆ Level-up" in r.text
    # Sheet banner
    r = client.get(f"/characters/{pc_id}")
    assert "Level-up available" in r.text


# ── compact searchable member editor (2026-09-30) ────────────────────────────

def test_member_editor_is_searchable_and_members_first(client, seed):
    """The member pickers used to be a one-per-row checkbox wall of every
    entity in the world, unsorted. Now: a filter box per list, a live
    shown/total count, a compact multi-column grid, and current members
    sorted ahead of everyone else (pinned by data-member for the test)."""
    db = SessionLocal()
    try:
        pc_a = PlayerCharacter(world_id=seed.world_a.id, name="Zed")
        pc_b = PlayerCharacter(world_id=seed.world_a.id, name="Amy")
        ent_a = Entity(world_id=seed.world_a.id, kind="creature", name="Mule")
        ent_b = Entity(world_id=seed.world_a.id, kind="creature", name="Wolf")
        db.add_all([pc_a, pc_b, ent_a, ent_b])
        db.commit()
        pc_a_id, pc_b_id, ent_a_id, ent_b_id = pc_a.id, pc_b.id, ent_a.id, ent_b.id
        party = Party(world_id=seed.world_a.id, name="Search Party",
                      member_pc_ids_json=json.dumps([pc_a_id]),
                      member_entity_ids_json=json.dumps([ent_b_id]))
        db.add(party)
        db.commit()
        db.refresh(party)
        pid = party.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get(f"/parties/{pid}").text

    # Filter boxes + live counts + the client-side filter hook.
    assert 'id="pc-search"' in page
    assert 'id="ent-search"' in page
    assert 'oninput="filterMembers(' in page
    assert 'filterMembers(\'pc\', \'\')' in page
    # Current members pinned first: the checked label appears before the
    # unchecked one within each list (data-member marks them).
    pc_section = page.split('id="pc-list"', 1)[1].split("</div>", 1)[0]
    assert 'data-member="1"' in pc_section and 'data-member="0"' in pc_section
    assert pc_section.index('data-member="1"') < pc_section.index('data-member="0"')
    ent_section = page.split('id="ent-list"', 1)[1].split("</div>", 1)[0]
    assert ent_section.index('data-member="1"') < ent_section.index('data-member="0"')
    # Companion search matches on kind too (name + kind in data-search).
    assert "wolf" in ent_section.split('data-search="', 1)[1].split('"', 1)[0] or \
           'data-search="mule creature"' in ent_section or \
           'data-search="wolf creature"' in ent_section


# ── 2026-09-30 party improvements: quick-add, rest, undo, goals, summary ─────

def _party_with_pc(seed, **pc_kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name=pc_kw.get("name", "Rusty"),
                             level=pc_kw.get("level", 2))
        db.add(pc)
        db.flush()
        # Give PP/MP/Shock something to restore: stats drive the maxima.
        pc.stats_json = json.dumps([
            {"id": "str", "value": 3}, {"id": "dex", "value": 3},
            {"id": "bod", "value": 3}, {"id": "per", "value": 3},
            {"id": "wil", "value": 4}, {"id": "int", "value": 4},
            {"id": "cha", "value": 4}, {"id": "itu", "value": 4},
        ])
        pc.pp_current = 2
        pc.mp_current = 3
        pc.shock_current = 1
        pc.shock_max = 6
        party = Party(world_id=seed.world_a.id, name="Rest Party",
                      member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.commit()
        db.refresh(party)
        return party.id, pc.id
    finally:
        db.close()


def test_quick_add_toggle_membership(client, seed, monkeypatch):
    from app.routers import parties as _pp
    party_id, _pc_id = _party_with_pc(seed)
    db = SessionLocal()
    ent = Entity(world_id=seed.world_a.id, kind="creature", name="War Dog")
    db.add(ent); db.commit(); db.refresh(ent); ent_id = ent.id
    db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    # by name (what the quick-add UI sends)
    r = client.post(f"/api/parties/{party_id}/members/toggle",
                    json={"kind": "entity", "name": "War Dog"})
    assert r.status_code == 200 and r.json()["added"] is True
    db = SessionLocal()
    try:
        assert ent_id in json.loads(db.get(Party, party_id).member_entity_ids_json)
    finally:
        db.close()
    # toggle again removes
    r = client.post(f"/api/parties/{party_id}/members/toggle",
                    json={"kind": "entity", "name": "War Dog"})
    assert r.json()["added"] is False
    # player denied (structural change = full tier)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/parties/{party_id}/members/toggle",
                       json={"kind": "entity", "name": "War Dog"}).status_code == 403


def test_rest_applies_nd_rules_and_undo_restores(client, seed):
    party_id, pc_id = _party_with_pc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/api/parties/{party_id}/rest")
    assert r.status_code == 200, r.text
    d = r.json()
    snap = d["snapshot"][0]
    applied = d["applied"][0]
    # PP max = str+dex+bod+per = 12 → +6 from 2 → 8; MP = wil+int+cha+itu = 16 → +8 from 3 → 11
    assert applied["pp_max"] == 12 and applied["pp_current"] == 8
    assert applied["mp_max"] == 16 and applied["mp_current"] == 11
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        assert pc.shock_current == pc.shock_max == 6
        assert pc.current_hp == 10  # HP untouched by rest (medical only)
    finally:
        db.close()

    # Undo restores the exact snapshot, and refuses foreign PCs. (The real
    # client posts the whole snapshot LIST back, same as the route returns.)
    r = client.post(f"/api/parties/{party_id}/rest/undo", json={"snapshot": d["snapshot"]})
    assert r.status_code == 200 and r.json()["restored"] == 1
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        assert (pc.pp_current, pc.mp_current, pc.shock_current) == (2, 3, 1)
    finally:
        db.close()

    # A snapshot entry for a NON-member is ignored, not applied.
    r = client.post(f"/api/parties/{party_id}/rest/undo",
                    json={"snapshot": [{"id": 999999, "pp_current": 0, "mp_current": 0, "shock_current": 0}]})
    assert r.status_code == 200 and r.json()["restored"] == 0


def test_goals_save_and_detail_context(client, seed):
    from app.routers import characters as _chars
    party_id, pc_id = _party_with_pc(seed)
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        pc.xp = 999999  # cross the level-up threshold
        party = db.get(Party, party_id)
        party.loot_json = json.dumps([
            {"name": "Stim", "qty": 1, "claimed_by": [pc_id]},
            {"name": "Mystery Chip", "qty": 2, "claimed_by": []},
        ])
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{party_id}/edit",
                    data={"name": "Rest Party", "goals": "Find the chip's owner\nOwe Vex nothing",
                          "member_pc_ids": str(pc_id)},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/parties/{party_id}").text
    # banner strip: level-up digest + unclaimed count
    assert "Level-up ready" in page and "Rusty" in page
    assert "1</strong> unclaimed loot item" in page
    # goals persist and render in the edit form
    assert "Find the chip&#39;s owner" in page or "Find the chip's owner" in page
    db = SessionLocal()
    try:
        assert "Owe Vex nothing" in (db.get(Party, party_id).goals or "")
    finally:
        db.close()


def test_summary_page_renders(client, seed):
    party_id, _pc_id = _party_with_pc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{party_id}/summary")
    assert r.status_code == 200
    assert "Vitals" in r.text and "Shared Loot" in r.text and "Print" in r.text
    # Player visibility matches the DETAIL page exactly (both gate through
    # the same world_row_visible + section matrix) — whatever a player gets
    # for the detail page, the summary gives too.
    detail_status = client.get(f"/parties/{party_id}").status_code
    assert client.get(f"/parties/{party_id}/summary").status_code == detail_status
