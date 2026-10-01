"""Parties must never reveal GM-hidden NPCs/quests to players or assistants
(assistants see what players see), while a GM sees everything — and an
assistant saving the member editor must not silently drop the hidden
companions the GM added.

Covers the editor pick list, the quick-add datalist/endpoint, the read-only
member list, the printable summary, party-assigned quests, and the member
counts on the party list and map pins.
"""
import json
from types import SimpleNamespace

from app.database import SessionLocal
from app.models import (
    Entity, Party, PlayerCharacter, Quest, User, World, WorldMembership, entity_player_access,
)
from app.routers.parties import visible_member_count

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, _PLAYER_PASSWORD_HASH, login


# ── Setup ────────────────────────────────────────────────────────────────────

def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _user(email, world, role):
    db = SessionLocal()
    try:
        u = User(email=email, password_hash=_PLAYER_PASSWORD_HASH, display_name=email.split("@")[0], is_gm=False)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.add(WorldMembership(world_id=world.id, user_id=u.id, role=role))
        db.commit()
        return u.id
    finally:
        db.close()


def _open_sections(world, **levels):
    db = SessionLocal()
    try:
        w = db.get(World, world.id)
        w.section_access_json = json.dumps(levels)
        db.commit()
    finally:
        db.close()


def _entity(world, name, hidden=False, kind="character"):
    return _add(Entity(world_id=world.id, kind=kind, name=name, visible_to_players=not hidden))


class Table:
    """A world with a party holding one visible and one hidden companion."""

    def __init__(self, seed):
        _open_sections(
            seed.world_a,
            parties={"player": "read", "assistant": "edit"},
            quests={"player": "read", "assistant": "edit"},
        )
        self.seed = seed
        self.pc = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Hero"))
        self.open_npc = _entity(seed.world_a, "Friendly Cook")
        self.secret_npc = _entity(seed.world_a, "Secret Traitor", hidden=True)
        self.unused_open = _entity(seed.world_a, "Spare Guard")
        self.unused_secret = _entity(seed.world_a, "Spare Assassin", hidden=True)
        self.foreign_npc = _entity(seed.world_b, "Other World NPC")
        self.party = _add(Party(
            world_id=seed.world_a.id, name="The Crew", member_pc_ids_json=json.dumps([self.pc]),
            member_entity_ids_json=json.dumps([self.open_npc, self.secret_npc]),
        ))
        self.assistant_email = "asst@test.local"
        _user(self.assistant_email, seed.world_a, "assistant")

    def members(self):
        db = SessionLocal()
        try:
            return json.loads(db.get(Party, self.party).member_entity_ids_json)
        finally:
            db.close()


def _as_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)


def _as_assistant(client, t):
    login(client, t.assistant_email, PLAYER_PASSWORD)


def _as_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    # A GM belongs to every world, so routes scoped to the ACTIVE world
    # (party list, members/toggle) need the cookie pointing at this one.
    client.cookies.set("active_world", seed.world_a.slug)


# ── Detail page ──────────────────────────────────────────────────────────────

def test_player_never_sees_a_hidden_companion(client, seed):
    t = Table(seed)
    _as_player(client, seed)
    html = client.get(f"/parties/{t.party}").text
    assert "Friendly Cook" in html
    assert "Secret Traitor" not in html and "Spare Assassin" not in html


def test_assistant_editor_lists_only_visible_entities(client, seed):
    t = Table(seed)
    _as_assistant(client, t)
    r = client.get(f"/parties/{t.party}")
    assert r.status_code == 200
    html = r.text
    assert 'id="ent-list"' in html, "the assistant should get the full editor"
    for visible in ("Friendly Cook", "Spare Guard"):
        assert visible in html
    for secret in ("Secret Traitor", "Spare Assassin"):
        assert secret not in html, f"{secret} leaked to an assistant"
    assert "Other World NPC" not in html


def test_gm_sees_every_companion_in_the_editor(client, seed):
    t = Table(seed)
    _as_gm(client, seed)
    html = client.get(f"/parties/{t.party}").text
    for name in ("Friendly Cook", "Secret Traitor", "Spare Guard", "Spare Assassin"):
        assert name in html
    assert "Other World NPC" not in html


def test_companion_shared_with_me_specifically_is_visible(client, seed):
    t = Table(seed)
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=t.secret_npc, user_id=seed.player_a.id))
        db.commit()
    finally:
        db.close()
    _as_player(client, seed)
    assert "Secret Traitor" in client.get(f"/parties/{t.party}").text


# ── Assigned quests ──────────────────────────────────────────────────────────

def test_party_quests_hide_gm_hidden_ones_from_players(client, seed):
    t = Table(seed)
    _add(Quest(world_id=seed.world_a.id, title="Public Errand", assigned_party_id=t.party, visible_to_players=True))
    _add(Quest(world_id=seed.world_a.id, title="Hidden Betrayal Plot", assigned_party_id=t.party, visible_to_players=False))
    _as_player(client, seed)
    for url in (f"/parties/{t.party}", f"/parties/{t.party}/summary"):
        html = client.get(url).text
        assert "Public Errand" in html, url
        assert "Hidden Betrayal Plot" not in html, url
    _as_gm(client, seed)
    for url in (f"/parties/{t.party}", f"/parties/{t.party}/summary"):
        assert "Hidden Betrayal Plot" in client.get(url).text, url


def test_party_quests_vanish_when_the_quests_section_is_closed(client, seed):
    t = Table(seed)
    _add(Quest(world_id=seed.world_a.id, title="Public Errand", assigned_party_id=t.party, visible_to_players=True))
    _open_sections(seed.world_a, parties={"player": "read"}, quests={"player": "none"})
    _as_player(client, seed)
    assert "Public Errand" not in client.get(f"/parties/{t.party}").text


# ── Printable summary ────────────────────────────────────────────────────────

def test_summary_hides_hidden_companions(client, seed):
    t = Table(seed)
    _as_player(client, seed)
    html = client.get(f"/parties/{t.party}/summary").text
    assert "Friendly Cook" in html and "Secret Traitor" not in html
    _as_gm(client, seed)
    assert "Secret Traitor" in client.get(f"/parties/{t.party}/summary").text


# ── Writes ───────────────────────────────────────────────────────────────────

def _save(client, t, entity_ids, pc_ids=None):
    return client.post(
        f"/parties/{t.party}/edit",
        data={"name": "The Crew", "notes": "", "goals": "",
              "member_pc_ids": [str(i) for i in (pc_ids if pc_ids is not None else [t.pc])],
              "member_entity_ids": [str(i) for i in entity_ids]},
        follow_redirects=False,
    )


def test_assistant_save_keeps_the_gms_hidden_companions(client, seed):
    """The assistant's editor never listed the secret NPC, so their submit
    omits it — saving must not drop it from the party."""
    t = Table(seed)
    _as_assistant(client, t)
    assert _save(client, t, [t.open_npc, t.unused_open]).status_code == 303
    members = t.members()
    assert t.secret_npc in members, "hidden companion was silently dropped"
    assert set(members) == {t.open_npc, t.unused_open, t.secret_npc}


def test_assistant_can_still_remove_a_visible_companion(client, seed):
    t = Table(seed)
    _as_assistant(client, t)
    _save(client, t, [])
    assert t.members() == [t.secret_npc]


def test_assistant_cannot_add_hidden_or_foreign_entities_by_forging_ids(client, seed):
    t = Table(seed)
    _as_assistant(client, t)
    _save(client, t, [t.open_npc, t.unused_secret, t.foreign_npc])
    members = t.members()
    assert t.unused_secret not in members and t.foreign_npc not in members
    assert set(members) == {t.open_npc, t.secret_npc}


def test_gm_save_replaces_the_list_including_hidden_ones(client, seed):
    t = Table(seed)
    _as_gm(client, seed)
    _save(client, t, [t.unused_secret])
    assert t.members() == [t.unused_secret]


def test_non_numeric_member_ids_are_a_400_not_a_500(client, seed):
    t = Table(seed)
    _as_gm(client, seed)
    r = client.post(f"/parties/{t.party}/edit",
                    data={"name": "x", "member_entity_ids": ["abc"]}, follow_redirects=False)
    assert r.status_code == 400


# ── Quick-add ────────────────────────────────────────────────────────────────

def _toggle(client, t, **body):
    return client.post(f"/api/parties/{t.party}/members/toggle", json={"kind": "entity", **body})


def test_assistant_quick_add_cannot_touch_hidden_entities(client, seed):
    t = Table(seed)
    _as_assistant(client, t)
    assert _toggle(client, t, name="Spare Assassin").status_code == 404
    assert _toggle(client, t, id=t.unused_secret).status_code == 404
    assert _toggle(client, t, id=t.secret_npc).status_code == 404, "can't remove the GM's hidden member either"
    assert t.members() == [t.open_npc, t.secret_npc]
    r = _toggle(client, t, name="Spare Guard")
    assert r.status_code == 200 and r.json()["added"] is True


def test_gm_quick_add_can_toggle_hidden_entities(client, seed):
    t = Table(seed)
    _as_gm(client, seed)
    assert _toggle(client, t, name="Spare Assassin").json()["added"] is True
    assert t.unused_secret in t.members()


# ── Counts ───────────────────────────────────────────────────────────────────

def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user, is_assistant=False))


def test_member_counts_exclude_hidden_companions_for_non_gms(client, seed):
    t = Table(seed)
    db = SessionLocal()
    try:
        party = db.get(Party, t.party)
        gm = db.get(User, seed.gm.id)
        player = db.get(User, seed.player_a.id)
        assert visible_member_count(db, _req(gm), party) == 3, "GM: 1 PC + 2 companions"
        assert visible_member_count(db, _req(player), party) == 2, "player: hidden companion not counted"
    finally:
        db.close()


def test_party_list_counts_match_what_the_viewer_may_know(client, seed):
    t = Table(seed)
    _as_player(client, seed)
    assert "2 members" in client.get("/parties").text
    _as_gm(client, seed)
    assert "3 members" in client.get("/parties").text
