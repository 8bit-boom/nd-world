"""Party and cockpit views must honour the world's per-section access matrix.

The party page's History block lists session titles, combat names and calendar
events tied to the party; those rows carry no per-row visibility flag, so the
ONLY thing keeping a title out of a player's hands is the Sessions / Combat /
Calendar section level. The player cockpit's quest and party boards likewise
ignored the Quests / Parties levels. Also: the AI Insights button is GM-only
(its endpoint 403s everyone else) and a character's backstory strips
[gmonly] blocks for non-GMs like every other prose field.
"""
import json

from app.database import SessionLocal
from app.models import CalendarEvent, CombatSession, GameSession, Party, PlayerCharacter, Quest, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _levels(world, **levels):
    db = SessionLocal()
    try:
        db.get(World, world.id).section_access_json = json.dumps(levels)
        db.commit()
    finally:
        db.close()


def _scene(seed):
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Hero",
                              backstory="Public past. [gmonly]Secretly the heir.[/gmonly]"))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc]),
                       xp_json=json.dumps([{"ts": "2026-01-01T00:00:00", "amount": 50, "session_id": 1,
                                            "awarded": {"Hero": 50}}])))
    _add(GameSession(world_id=seed.world_a.id, title="The Secret Heist", session_num=7, party_id=party))
    _add(CombatSession(world_id=seed.world_a.id, name="Ambush At Dawn", party_id=party))
    _add(CalendarEvent(world_id=seed.world_a.id, day=4, title="Coronation Plot", party_id=party))
    _add(Quest(world_id=seed.world_a.id, title="Find The Heir", status="active", visible_to_players=True,
               assigned_party_id=party))
    return pc, party


def _player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _open_all(world):
    _levels(world, parties={"player": "read"}, sessions={"player": "read"}, combat={"player": "read"},
            calendar={"player": "read"}, quests={"player": "read"})


def test_open_sections_show_history_titles(client, seed):
    _, party = _scene(seed)
    _open_all(seed.world_a)
    _player(client, seed)
    html = client.get(f"/parties/{party}").text
    assert "The Secret Heist" in html and "Ambush At Dawn" in html and "Coronation Plot" in html


def test_closed_sections_hide_their_history_rows(client, seed):
    _, party = _scene(seed)
    _levels(seed.world_a, parties={"player": "read"}, sessions={"player": "none"}, combat={"player": "none"},
            calendar={"player": "none"})
    _player(client, seed)
    r = client.get(f"/parties/{party}")
    assert r.status_code == 200
    for secret in ("The Secret Heist", "Ambush At Dawn", "Coronation Plot"):
        assert secret not in r.text, f"{secret!r} leaked through the party page"
    assert "/sessions/1" not in r.text, "XP ledger still links to a session the viewer can't open"


def test_each_history_section_is_gated_independently(client, seed):
    _, party = _scene(seed)
    _levels(seed.world_a, parties={"player": "read"}, sessions={"player": "read"}, combat={"player": "none"},
            calendar={"player": "none"})
    _player(client, seed)
    html = client.get(f"/parties/{party}").text
    assert "The Secret Heist" in html
    assert "Ambush At Dawn" not in html and "Coronation Plot" not in html


def test_gm_always_sees_history(client, seed):
    _, party = _scene(seed)
    _levels(seed.world_a, parties={"player": "read"}, sessions={"player": "none"}, combat={"player": "none"},
            calendar={"player": "none"})
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/parties/{party}").text
    assert "The Secret Heist" in html and "Ambush At Dawn" in html and "Coronation Plot" in html


def test_player_cockpit_board_respects_quest_and_party_levels(client, seed):
    _scene(seed)
    _open_all(seed.world_a)
    _player(client, seed)
    board = client.get("/api/cockpit/player-board").json()
    assert [q["title"] for q in board["quests"]] == ["Find The Heir"]
    assert [p["name"] for p in board["parties"]] == ["Crew"]

    _levels(seed.world_a, parties={"player": "none"}, quests={"player": "none"})
    board = client.get("/api/cockpit/player-board").json()
    assert board["quests"] == [], "quest titles leaked past a closed Quests section"
    assert board["parties"] == [], "party roster leaked past a closed Parties section"
    assert [m["name"] for m in board["my_pcs"]] == ["Hero"], "a player's own character is never hidden from them"


def test_ai_insights_button_is_gm_only(client, seed):
    _, party = _scene(seed)
    _open_all(seed.world_a)
    _player(client, seed)
    assert "ai-insights-btn" not in client.get(f"/parties/{party}").text
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert "ai-insights-btn" in client.get(f"/parties/{party}").text


def test_gm_sees_gmonly_backstory_but_it_is_stripped_for_others(client, seed):
    pc, _ = _scene(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_html = client.get(f"/characters/{pc}").text
    assert "Secretly the heir" in gm_html
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc}")
    if r.status_code == 200:  # another player may or may not see the sheet at all; if they do, no secret
        assert "Secretly the heir" not in r.text and "Public past" in r.text
