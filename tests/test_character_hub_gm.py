"""The GM looking at a player's character gets the hub tabs too — read-only and
from the PLAYER's point of view.

Journey / Quests & goals / Known world / Schedule are shared information, so a
global GM may open them on any character. The owner's private Notes & journal tab
(the GM's own notes to them plus their diary), and every write (journal/goal
entries, loot claims), stay owner-only: a GM is refused exactly like a stranger.
The Known-world tab must show what THE PLAYER can see — never the GM's
unfiltered view of the world.
"""
import json

from app.database import SessionLocal
from app.models import (
    CharacterJournalEntry, Entity, Party, PlayerCharacter, PrivateNote, SheetTemplate, User,
    WorldMembership, entity_player_access,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, _PLAYER_PASSWORD_HASH, login


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _pc(seed, owner="a", name="Hero", **kw):
    uid = {"a": seed.player_a.id, "b": seed.player_b.id, None: None}[owner]
    return _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=uid, name=name, **kw))


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _hub(pc, tab):
    return f"/api/characters/{pc}/hub/{tab}"


# ── API: shared tabs open to the GM, private ones stay closed ────────────────

def test_gm_can_read_the_shared_tabs(client, seed):
    pc = _pc(seed)
    _gm(client, seed)
    for tab in ("quests", "world", "schedule"):
        assert client.get(_hub(pc, tab)).status_code == 200, tab


def test_gm_can_read_the_tabs_of_an_unowned_character(client, seed):
    pc = _pc(seed, owner=None, name="GM-managed")
    _gm(client, seed)
    assert client.get(_hub(pc, "quests")).status_code == 200


def test_notes_and_journal_stay_private_from_the_gm(client, seed):
    pc = _pc(seed)
    _add(CharacterJournalEntry(world_id=seed.world_a.id, character_id=pc, kind="journal", title="Dear diary",
                               body="the GM must not read this"))
    _add(PrivateNote(world_id=seed.world_a.id, player_user_id=seed.player_a.id, author_id=seed.gm.id,
                     title="Psst", content="note to player"))
    _gm(client, seed)
    r = client.get(_hub(pc, "notes"))
    assert r.status_code == 404 and "diary" not in r.text.lower()


def test_gm_cannot_write_through_the_hub(client, seed):
    pc = _pc(seed)
    eid = _add(CharacterJournalEntry(world_id=seed.world_a.id, character_id=pc, kind="goal", title="mine"))
    _gm(client, seed)
    assert client.post(_hub(pc, "entries"), json={"kind": "goal", "title": "gm goal"}).status_code == 404
    assert client.post(_hub(pc, f"entries/{eid}"), json={"title": "hacked"}).status_code == 404
    assert client.post(_hub(pc, f"entries/{eid}/delete")).status_code == 404
    assert client.post(_hub(pc, "loot"), json={"action": "claim", "lid": "x"}).status_code == 404
    db = SessionLocal()
    try:
        assert [(r.title) for r in db.query(CharacterJournalEntry).all()] == ["mine"]
    finally:
        db.close()


def test_other_players_are_still_refused_every_tab(client, seed):
    pc = _pc(seed)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    for tab in ("quests", "notes", "world", "schedule"):
        assert client.get(_hub(pc, tab)).status_code == 404, tab


def test_a_gm_assistant_is_not_treated_as_a_gm_here(client, seed):
    """Only a real GM account gets the read-only view; assistants see what players see."""
    db = SessionLocal()
    try:
        helper = User(email="helper@test.local", password_hash=_PLAYER_PASSWORD_HASH, display_name="Helper", is_gm=False)
        db.add(helper)
        db.commit()
        db.refresh(helper)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=helper.id, role="assistant"))
        db.commit()
        email = helper.email
    finally:
        db.close()
    pc = _pc(seed)
    login(client, email, PLAYER_PASSWORD)
    assert client.get(_hub(pc, "quests")).status_code == 404


# ── Known world: the player's view, not the GM's ─────────────────────────────

def test_gm_world_tab_shows_what_the_player_sees(client, seed):
    pc = _pc(seed)
    pub = _add(Entity(world_id=seed.world_a.id, name="Public Tavern", kind="location", visible_to_players=True))
    hidden = _add(Entity(world_id=seed.world_a.id, name="Villain Lair", kind="location", visible_to_players=False))
    revealed = _add(Entity(world_id=seed.world_a.id, name="Secret For Archie", kind="npc", visible_to_players=False))
    other = _add(Entity(world_id=seed.world_a.id, name="Secret For Bob", kind="npc", visible_to_players=False))
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=revealed, user_id=seed.player_a.id))
        db.execute(entity_player_access.insert().values(entity_id=other, user_id=seed.player_b.id))
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    d = client.get(_hub(pc, "world")).json()
    assert [e["name"] for e in d["revealed"]] == ["Secret For Archie"], "revealed = shared with THIS character's owner"
    names = {e["name"] for e in d["recent"]}
    assert "Public Tavern" in names and "Villain Lair" not in names and "Secret For Bob" not in names
    found = {e["name"] for e in client.get(_hub(pc, "world") + "?q=Villain").json()["results"]}
    assert "Villain Lair" not in found, "search must not expose GM-only entities in the player's view"
    found = {e["name"] for e in client.get(_hub(pc, "world") + "?q=Secret").json()["results"]}
    assert found == {"Secret For Archie"}


def test_owner_world_tab_is_unchanged(client, seed):
    pc = _pc(seed)
    _add(Entity(world_id=seed.world_a.id, name="Villain Lair", kind="location", visible_to_players=False))
    _add(Entity(world_id=seed.world_a.id, name="Public Tavern", kind="location", visible_to_players=True))
    _player(client, seed)
    names = {e["name"] for e in client.get(_hub(pc, "world")).json()["recent"]}
    assert names == {"Public Tavern"}


def test_gm_goals_are_listed_read_only(client, seed):
    pc = _pc(seed)
    _add(CharacterJournalEntry(world_id=seed.world_a.id, character_id=pc, kind="goal", title="Avenge the Crow",
                               status="active"))
    _add(CharacterJournalEntry(world_id=seed.world_a.id, character_id=pc, kind="journal", title="Diary page"))
    _gm(client, seed)
    d = client.get(_hub(pc, "quests")).json()
    assert [g["title"] for g in d["goals"]] == ["Avenge the Crow"]


# ── The page: tabs rendered for the GM ───────────────────────────────────────

def _custom_template_id():
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first().id
    finally:
        db.close()


def test_gm_sees_the_hub_tabs_on_both_sheet_layouts_without_notes(client, seed):
    native = _pc(seed, name="Native")
    custom = _pc(seed, name="Custom", sheet_template_id=_custom_template_id())
    _gm(client, seed)
    for pc in (native, custom):
        html = client.get(f"/characters/{pc}").text
        assert 'id="pc-hub"' in html and 'data-mode="gm"' in html, pc
        for tab in ("sheet", "journey", "quests", "world", "schedule"):
            assert f'data-tab="{tab}"' in html, (pc, tab)
        assert 'data-tab="notes"' not in html, f"{pc}: the private Notes & journal tab must not show for the GM"


def test_owner_still_gets_notes_and_the_claim_buttons(client, seed):
    pc = _pc(seed)
    db = SessionLocal()
    try:
        party = Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc]),
                      loot_json=json.dumps([{"lid": "aaaaaaaa", "name": "Rope", "qty": 1, "notes": "", "claimed_by": []}]))
        db.add(party)
        db.commit()
    finally:
        db.close()
    _player(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert 'data-mode="owner"' in html and 'data-tab="notes"' in html
    assert 'data-claim="claim"' in html
    _gm(client, seed)
    gm_html = client.get(f"/characters/{pc}").text
    assert "Rope" in gm_html, "the GM sees the party stash"
    assert "data-claim=" not in gm_html.replace("[data-claim]", ""), "the GM cannot claim loot for a player"


def test_other_players_still_get_the_plain_sheet(client, seed):
    pc = _pc(seed)
    db = SessionLocal()
    try:
        mate = User(email="mate3@test.local", password_hash=_PLAYER_PASSWORD_HASH, display_name="Mate3", is_gm=False)
        db.add(mate)
        db.commit()
        db.refresh(mate)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=mate.id))
        db.commit()
        email = mate.email
    finally:
        db.close()
    login(client, email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc}")
    assert r.status_code == 200 and 'id="pc-hub"' not in r.text


def test_gm_banner_names_the_owning_player(client, seed):
    pc = _pc(seed)
    _gm(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert 'id="pch-gm-note"' in html and "Player A" in html
    nobody = _pc(seed, owner=None, name="Orphan")
    html = client.get(f"/characters/{nobody}").text
    assert 'id="pch-gm-note"' in html and "no owning player" in html
