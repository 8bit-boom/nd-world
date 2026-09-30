"""The player's character hub (app/routers/character_hub.py): owner-only
Quests / Notes / Known-world / Schedule data and the private journal + goals,
plus the tab bar on the character page (both sheet layouts).

/api/characters/ is a blanket player-safe prefix in _is_player_safe, so these
handlers are the ONLY enforcement — the first block pins that down for every
route, the rest pin the "shows what you could already see, never more" rules.
"""
import json
from datetime import date, datetime, timedelta

from app.database import SessionLocal
from app.models import (
    CalendarEvent, CharacterJournalEntry, Entity, GameSession, Party, PlayerCharacter,
    PrivateNote, Quest, SheetTemplate, World, WorldCalendar, WorldMembership, entity_player_access,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── Setup helpers ────────────────────────────────────────────────────────────

def _pc(owner, world, name="Hero"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world.id, owner_user_id=owner.id if owner else None, name=name)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _party(world, pc_ids, name="The Crew"):
    db = SessionLocal()
    try:
        p = Party(world_id=world.id, name=name, member_pc_ids_json=json.dumps(pc_ids))
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _set_world(world_id, **fields):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in fields.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _as(client, user, password=PLAYER_PASSWORD):
    login(client, user.email, password)


def _hub(pc_id, tab):
    return f"/api/characters/{pc_id}/hub/{tab}"


# ── Owner-only access on every route ─────────────────────────────────────────

READ_TABS = ("quests", "notes", "world", "schedule")


def test_owner_can_read_every_tab(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    for tab in READ_TABS:
        assert client.get(_hub(pc, tab)).status_code == 200, tab


def test_everyone_else_gets_404_on_every_tab(client, seed):
    """Another player in the SAME world, a player from a DIFFERENT world, and
    the GM (who does not own the character) are all refused identically."""
    pc = _pc(seed.player_a, seed.world_a)
    db = SessionLocal()
    try:
        # A second member of world_a, so "same world, not the owner" is real.
        from app.models import User
        from .conftest import _PLAYER_PASSWORD_HASH
        mate = User(email="mate@test.local", password_hash=_PLAYER_PASSWORD_HASH, display_name="Mate", is_gm=False)
        db.add(mate)
        db.commit()
        db.refresh(mate)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=mate.id))
        db.commit()
        mate_email = mate.email
    finally:
        db.close()

    for email, pw in (
        (mate_email, PLAYER_PASSWORD),
        (seed.player_b.email, PLAYER_PASSWORD),
        (seed.gm.email, GM_PASSWORD),
    ):
        login(client, email, pw)
        for tab in READ_TABS:
            assert client.get(_hub(pc, tab)).status_code == 404, (email, tab)


def test_unknown_character_404s(client, seed):
    _as(client, seed.player_a)
    assert client.get(_hub(999999, "quests")).status_code == 404


def test_anonymous_is_refused(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    r = client.get(_hub(pc, "quests"))
    assert r.status_code in (401, 403)


def test_non_owner_cannot_write_and_nothing_is_persisted(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    eid = client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "journal", "title": "mine"}).json()["id"]

    _as(client, seed.player_b)
    assert client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "journal", "title": "x"}).status_code == 404
    assert client.post(f"/api/characters/{pc}/hub/entries/{eid}", json={"title": "hacked"}).status_code == 404
    assert client.post(f"/api/characters/{pc}/hub/entries/{eid}/delete").status_code == 404

    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post(f"/api/characters/{pc}/hub/entries/{eid}", json={"title": "gm edit"}).status_code == 404

    db = SessionLocal()
    try:
        rows = db.query(CharacterJournalEntry).all()
        assert [(r.title, r.character_id) for r in rows] == [("mine", pc)]
    finally:
        db.close()


def test_entry_of_another_character_is_not_reachable_through_my_character(client, seed):
    """Owner of character A can't touch character B's entry by pairing B's
    entry id with A's (valid) path."""
    a = _pc(seed.player_a, seed.world_a, "A")
    eid = _add(CharacterJournalEntry(
        world_id=seed.world_b.id, character_id=_pc(seed.player_b, seed.world_b, "B"),
        kind="journal", title="B's secret",
    ))
    _as(client, seed.player_a)
    assert client.post(f"/api/characters/{a}/hub/entries/{eid}", json={"title": "x"}).status_code == 404
    assert client.post(f"/api/characters/{a}/hub/entries/{eid}/delete").status_code == 404


def test_a_gm_who_owns_a_character_gets_the_hub(client, seed):
    pc = _pc(seed.gm, seed.world_a, "GM's own PC")
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get(_hub(pc, "quests")).status_code == 200


# ── Quests & goals ───────────────────────────────────────────────────────────

def test_quests_tab_hides_gm_hidden_quests_and_marks_my_party(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    party = _party(seed.world_a, [pc])
    _set_world(seed.world_a.id, section_access_json=json.dumps({"quests": {"player": "read"}}))
    _add(Quest(world_id=seed.world_a.id, title="Shown", visible_to_players=True, assigned_party_id=party))
    _add(Quest(world_id=seed.world_a.id, title="Other party's", visible_to_players=True))
    _add(Quest(world_id=seed.world_a.id, title="TOP SECRET PLOT", summary="twist", visible_to_players=False))
    _as(client, seed.player_a)
    data = client.get(_hub(pc, "quests")).json()
    titles = {q["title"]: q for q in data["quests"]}
    assert set(titles) == {"Shown", "Other party's"}
    assert titles["Shown"]["mine"] is True and titles["Other party's"]["mine"] is False
    assert "TOP SECRET PLOT" not in json.dumps(data) and "twist" not in json.dumps(data)
    assert data["quests"][0]["title"] == "Shown", "my party's quest sorts first"


def test_quests_tab_respects_a_closed_quests_section(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _add(Quest(world_id=seed.world_a.id, title="Visible but section closed", visible_to_players=True))
    _set_world(seed.world_a.id, section_access_json=json.dumps({"quests": {"player": "none"}}))
    _as(client, seed.player_a)
    data = client.get(_hub(pc, "quests")).json()
    assert data["section"] == "none" and data["quests"] == [] and data["quests_href"] is None


def test_quests_only_from_the_characters_own_world(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _set_world(seed.world_a.id, section_access_json=json.dumps({"quests": {"player": "read"}}))
    _add(Quest(world_id=seed.world_b.id, title="World B quest", visible_to_players=True))
    _as(client, seed.player_a)
    assert client.get(_hub(pc, "quests")).json()["quests"] == []


def test_subquests_roll_up_instead_of_listing_separately(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _set_world(seed.world_a.id, section_access_json=json.dumps({"quests": {"player": "read"}}))
    parent = _add(Quest(world_id=seed.world_a.id, title="Parent", visible_to_players=True))
    _add(Quest(world_id=seed.world_a.id, title="Step 1", parent_id=parent, status="complete", visible_to_players=True))
    _add(Quest(world_id=seed.world_a.id, title="Step 2", parent_id=parent, visible_to_players=True))
    _as(client, seed.player_a)
    quests = client.get(_hub(pc, "quests")).json()["quests"]
    assert [q["title"] for q in quests] == ["Parent"]
    assert (quests[0]["subs_done"], quests[0]["subs_total"]) == (1, 2)


# ── Journal & goals CRUD ─────────────────────────────────────────────────────

def test_goal_lifecycle(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    g = client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "goal", "title": "Avenge my mentor"}).json()
    assert g["kind"] == "goal" and g["status"] == "active"

    done = client.post(f"/api/characters/{pc}/hub/entries/{g['id']}", json={"status": "achieved"}).json()
    assert done["status"] == "achieved" and done["title"] == "Avenge my mentor"

    goals = client.get(_hub(pc, "quests")).json()["goals"]
    assert [x["id"] for x in goals] == [g["id"]]

    assert client.post(f"/api/characters/{pc}/hub/entries/{g['id']}/delete").json() == {"ok": True}
    assert client.get(_hub(pc, "quests")).json()["goals"] == []


def test_journal_entry_lifecycle_with_party_session(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    party = _party(seed.world_a, [pc])
    sid = _add(GameSession(world_id=seed.world_a.id, title="The Heist", session_num=3, party_id=party))
    _as(client, seed.player_a)

    e = client.post(f"/api/characters/{pc}/hub/entries",
                    json={"kind": "journal", "title": "Night one", "body": "We broke in.", "session_id": sid}).json()
    assert e["session"] == "#3 The Heist" and e["status"] == ""

    upd = client.post(f"/api/characters/{pc}/hub/entries/{e['id']}", json={"body": "We broke in. Badly."}).json()
    assert upd["body"] == "We broke in. Badly." and upd["session_id"] == sid

    cleared = client.post(f"/api/characters/{pc}/hub/entries/{e['id']}", json={"session_id": None}).json()
    assert cleared["session_id"] is None

    notes = client.get(_hub(pc, "notes")).json()
    assert [j["id"] for j in notes["journal"]] == [e["id"]]
    assert notes["sessions"] == [{"id": sid, "label": "#3 The Heist"}]


def test_validation(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    other_party_session = _add(GameSession(world_id=seed.world_a.id, title="Not mine", session_num=1))
    _as(client, seed.player_a)
    post = lambda body: client.post(f"/api/characters/{pc}/hub/entries", json=body).status_code
    assert post({"kind": "diary", "title": "x"}) == 400
    assert post({"kind": "journal"}) == 400
    assert post({"kind": "journal", "title": "  ", "body": " "}) == 400
    assert post({"kind": "journal", "title": "x" * 201}) == 400
    assert post({"kind": "journal", "body": "x" * 20001}) == 400
    assert post({"kind": "goal", "title": "x", "status": "whenever"}) == 400
    assert post({"kind": "journal", "title": "x", "session_id": other_party_session}) == 400
    assert post({"kind": "journal", "title": "x", "session_id": "abc"}) == 400
    r = client.post(f"/api/characters/{pc}/hub/entries", content="not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert client.post(f"/api/characters/{pc}/hub/entries", json=[1, 2]).status_code == 400

    j = client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "journal", "title": "ok"}).json()["id"]
    g = client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "goal", "title": "ok"}).json()["id"]
    upd = lambda eid, body: client.post(f"/api/characters/{pc}/hub/entries/{eid}", json=body).status_code
    assert upd(j, {"status": "achieved"}) == 400, "journal entries have no status"
    assert upd(g, {"session_id": other_party_session}) == 400, "goals don't link sessions"
    assert upd(g, {"status": "nonsense"}) == 400
    assert upd(j, {"title": "", "body": ""}) == 400, "can't blank an entry completely"
    assert upd(j, {"kind": "goal"}) == 200, "kind is ignored, not changeable"
    db = SessionLocal()
    try:
        assert db.get(CharacterJournalEntry, j).kind == "journal"
    finally:
        db.close()


def test_journal_text_is_returned_verbatim_not_as_markup(client, seed):
    """The UI renders via textContent; the API must hand back the raw text
    (no server-side HTML mangling that could hide or double-encode it)."""
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    e = client.post(f"/api/characters/{pc}/hub/entries",
                    json={"kind": "journal", "title": "<b>x</b>", "body": "<script>alert(1)</script>"}).json()
    assert e["title"] == "<b>x</b>" and e["body"] == "<script>alert(1)</script>"


# ── Notes from the GM ────────────────────────────────────────────────────────

def test_gm_notes_are_only_mine_excerpted_and_tag_free(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _add(PrivateNote(world_id=seed.world_a.id, player_user_id=seed.player_a.id, author_id=seed.gm.id,
                     title="For you", content="<p>The <b>vault</b> code is 1234.</p>"))
    _add(PrivateNote(world_id=seed.world_a.id, player_user_id=seed.player_b.id, author_id=seed.gm.id,
                     title="For B", content="B-only secret"))
    _add(PrivateNote(world_id=seed.world_b.id, player_user_id=seed.player_a.id, author_id=seed.gm.id,
                     title="Other world", content="wrong world"))
    _as(client, seed.player_a)
    data = client.get(_hub(pc, "notes")).json()
    assert [n["title"] for n in data["gm_notes"]] == ["For you"]
    assert data["gm_notes"][0]["excerpt"] == "The vault code is 1234."
    assert data["thread_href"].startswith("/worlds/") and f"/notes/{seed.player_a.id}" in data["thread_href"]
    blob = json.dumps(data)
    assert "B-only secret" not in blob and "wrong world" not in blob


def test_journal_is_private_to_each_character(client, seed):
    a = _pc(seed.player_a, seed.world_a, "A")
    _add(CharacterJournalEntry(world_id=seed.world_a.id, character_id=a, kind="journal", title="A's diary"))
    second = _pc(seed.player_a, seed.world_a, "A2")
    _as(client, seed.player_a)
    assert [e["title"] for e in client.get(_hub(a, "notes")).json()["journal"]] == ["A's diary"]
    assert client.get(_hub(second, "notes")).json()["journal"] == []


# ── Known world ──────────────────────────────────────────────────────────────

def _entity(world, name, visible=True, summary="", aliases=None):
    return _add(Entity(world_id=world.id, kind="location", name=name, visible_to_players=visible,
                       summary=summary, aliases=aliases))


def test_world_tab_never_leaks_hidden_entities(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _entity(seed.world_a, "Open Square")
    _entity(seed.world_a, "Hidden Vault", visible=False, summary="gm only")
    _entity(seed.world_b, "Other World Place")
    _as(client, seed.player_a)
    for q in ("", "Vault", "Place", "Square"):
        blob = json.dumps(client.get(_hub(pc, "world"), params={"q": q}).json())
        assert "Hidden Vault" not in blob and "gm only" not in blob, q
        assert "Other World Place" not in blob, q
    assert "Open Square" in json.dumps(client.get(_hub(pc, "world")).json())


def test_world_tab_shows_entities_revealed_to_me_specifically(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    mine = _entity(seed.world_a, "Secret Door", visible=False)
    theirs = _entity(seed.world_a, "Someone Else's Secret", visible=False)
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=mine, user_id=seed.player_a.id))
        db.execute(entity_player_access.insert().values(entity_id=theirs, user_id=seed.player_b.id))
        db.commit()
    finally:
        db.close()
    _as(client, seed.player_a)
    data = client.get(_hub(pc, "world")).json()
    assert [e["name"] for e in data["revealed"]] == ["Secret Door"]
    assert "Someone Else's Secret" not in json.dumps(data)
    found = client.get(_hub(pc, "world"), params={"q": "secret"}).json()["results"]
    assert [e["name"] for e in found] == ["Secret Door"]


def test_world_tab_strips_gm_only_text_from_summaries(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _entity(seed.world_a, "Old Mill", summary="A mill.[gmonly] The owner is the killer.[/gmonly] Quiet.")
    _as(client, seed.player_a)
    blob = json.dumps(client.get(_hub(pc, "world")).json())
    assert "the killer" not in blob and "A mill." in blob


def test_world_search_matches_aliases_and_treats_wildcards_literally(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _entity(seed.world_a, "The Crown", aliases="Gilded Circlet")
    _entity(seed.world_a, "Plain Rock")
    _as(client, seed.player_a)
    by_alias = client.get(_hub(pc, "world"), params={"q": "circlet"}).json()["results"]
    assert [e["name"] for e in by_alias] == ["The Crown"]
    for wild in ("%", "_", "\\"):
        assert client.get(_hub(pc, "world"), params={"q": wild}).json()["results"] == [], wild


# ── Schedule ─────────────────────────────────────────────────────────────────

def _calendar(world, current_day):
    _add(WorldCalendar(world_id=world.id, config_json=json.dumps({
        "era_name": "Year", "current_day": current_day,
        "months": [{"name": "Frostmonth", "days": 30}], "days_per_week": 7, "moons": [],
    })))


def test_schedule_lists_only_my_upcoming_events(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    other_pc = _pc(seed.player_b, seed.world_a, "Other")
    party = _party(seed.world_a, [pc])
    other_party = _party(seed.world_a, [other_pc], "Rivals")
    _set_world(seed.world_a.id, section_access_json=json.dumps({"calendar": {"player": "read"}}))
    _calendar(seed.world_a, 10)
    w = seed.world_a.id
    _add(CalendarEvent(world_id=w, day=10, title="Today's duel", character_id=pc))
    _add(CalendarEvent(world_id=w, day=15, title="Party feast", party_id=party))
    _add(CalendarEvent(world_id=w, day=12, title="Rivals' ball", party_id=other_party))
    _add(CalendarEvent(world_id=w, day=13, title="Someone else's quest", character_id=other_pc))
    _add(CalendarEvent(world_id=w, day=3, title="Long ago", character_id=pc))
    _add(CalendarEvent(world_id=w, day=20, title="Unlinked festival"))
    _as(client, seed.player_a)
    data = client.get(_hub(pc, "schedule")).json()
    assert [e["title"] for e in data["events"]] == ["Today's duel", "Party feast"]
    assert data["events"][0]["days_away"] == 0 and data["events"][1]["days_away"] == 5
    assert data["events"][1]["date"] == "Frostmonth 15, Year 1"
    assert data["today"] == "Frostmonth 10, Year 1"


def test_schedule_respects_a_closed_calendar_and_works_without_one(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    _set_world(seed.world_a.id, section_access_json=json.dumps({"calendar": {"player": "read"}}))
    assert client.get(_hub(pc, "schedule")).json()["events"] == [], "no calendar row yet -> empty, not an error"
    _calendar(seed.world_a, 1)
    _add(CalendarEvent(world_id=seed.world_a.id, day=2, title="Hidden by section", character_id=pc))
    _set_world(seed.world_a.id, section_access_json=json.dumps({"calendar": {"player": "none"}}))
    data = client.get(_hub(pc, "schedule")).json()
    assert data["calendar_section"] == "none" and data["events"] == [] and data["calendar_href"] is None


def test_schedule_next_sessions_are_my_partys_future_dates_only(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    other = _pc(seed.player_b, seed.world_a, "Other")
    party = _party(seed.world_a, [pc])
    other_party = _party(seed.world_a, [other], "Rivals")
    w = seed.world_a.id
    soon = (date.today() + timedelta(days=3)).isoformat()
    later = (date.today() + timedelta(days=30)).isoformat()
    past = (date.today() - timedelta(days=3)).isoformat()
    _add(GameSession(world_id=w, title="Later", session_num=5, session_date=later, party_id=party))
    _add(GameSession(world_id=w, title="Soon", session_num=4, session_date=soon, party_id=party))
    _add(GameSession(world_id=w, title="Past", session_num=3, session_date=past, party_id=party))
    _add(GameSession(world_id=w, title="Rivals soon", session_num=2, session_date=soon, party_id=other_party))
    _add(GameSession(world_id=w, title="Undated", session_num=1, session_date=None, party_id=party))
    _add(GameSession(world_id=w, title="Junk date", session_num=6, session_date="someday", party_id=party))
    _as(client, seed.player_a)
    sessions = client.get(_hub(pc, "schedule")).json()["sessions"]
    assert [s["title"] for s in sessions] == ["Soon", "Later"]


# ── Cleanup ──────────────────────────────────────────────────────────────────

def _journal_count():
    db = SessionLocal()
    try:
        return db.query(CharacterJournalEntry).count()
    finally:
        db.close()


def test_deleting_a_character_deletes_its_journal(client, seed):
    keep = _pc(seed.player_a, seed.world_a, "Keeper")
    gone = _pc(seed.player_a, seed.world_a, "Goner")
    _as(client, seed.player_a)
    for pc in (keep, gone):
        client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "journal", "title": f"pc{pc}"})
        client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "goal", "title": f"goal{pc}"})
    assert _journal_count() == 4
    r = client.post(f"/characters/{gone}/delete", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert {e.character_id for e in db.query(CharacterJournalEntry).all()} == {keep}
    finally:
        db.close()


def test_retiring_a_character_to_an_npc_deletes_its_journal(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "journal", "title": "diary"})
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/characters/{pc}/retire-to-npc", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert _journal_count() == 0


def test_deleting_a_world_deletes_its_journal_entries(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    client.post(f"/api/characters/{pc}/hub/entries", json={"kind": "goal", "title": "g"})
    assert _journal_count() == 1
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post(f"/worlds/{seed.world_a.id}/delete", follow_redirects=False).status_code == 303
    assert _journal_count() == 0


# ── The tab bar on the character page ────────────────────────────────────────

def test_owner_sees_the_hub_on_the_native_sheet(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert 'id="pc-hub"' in html and f'data-pc-id="{pc}"' in html
    for tab in ("sheet", "journey", "quests", "notes", "world", "schedule"):
        assert f'data-tab="{tab}"' in html
    assert "isn't in a party yet" in html


def test_owner_sees_the_hub_on_a_custom_sheet_too(client, seed):
    db = SessionLocal()
    try:
        custom = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        assert custom is not None, "expected a built-in custom-mode sheet template"
        custom_id = custom.id
    finally:
        db.close()
    pc = _pc(seed.player_a, seed.world_a)
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, pc).sheet_template_id = custom_id
        db.commit()
    finally:
        db.close()
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert 'id="pc-hub"' in html


def test_party_members_journey_panel_shows_the_party(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _party(seed.world_a, [pc], name="The Night Crew")
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert "The Night Crew" in html and "isn't in a party yet" not in html


def test_gm_and_fellow_players_get_the_plain_sheet(client, seed):
    """Viewing someone else's sheet (GM, or a party-mate when
    players_see_party is on) must not render the hub at all."""
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.gm.email, GM_PASSWORD)
    gm_html = client.get(f"/characters/{pc}").text
    assert 'id="pc-hub"' not in gm_html

    db = SessionLocal()
    try:
        from app.models import User
        from .conftest import _PLAYER_PASSWORD_HASH
        mate = User(email="mate2@test.local", password_hash=_PLAYER_PASSWORD_HASH, display_name="Mate2", is_gm=False)
        db.add(mate)
        db.commit()
        db.refresh(mate)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=mate.id))
        db.commit()
        mate_email = mate.email
    finally:
        db.close()
    login(client, mate_email, PLAYER_PASSWORD)
    r = client.get(f"/characters/{pc}")
    assert r.status_code == 200 and 'id="pc-hub"' not in r.text
