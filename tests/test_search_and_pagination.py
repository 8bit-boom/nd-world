"""Regression tests for the performance/search pass: extending /search beyond
entities must not leak GM-only content (quests, sessions) to players, and must
respect the same character-visibility rule the character sheet route itself
uses. Also covers pagination's out-of-range clamping.
"""
from app.database import SessionLocal
from app.models import GameSession, PlayerCharacter, Quest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_search_hides_quests_and_sessions_from_players(client, seed):
    db = SessionLocal()
    try:
        db.add(Quest(world_id=seed.world_a.id, title="Find the Zzyzx Idol", summary="A rare artifact"))
        db.add(GameSession(world_id=seed.world_a.id, title="The Zzyzx Heist", session_num=1))
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/search", params={"q": "Zzyzx"})
    assert r.status_code == 200
    assert "Find the Zzyzx Idol" not in r.text
    assert "The Zzyzx Heist" not in r.text


def test_search_shows_quests_and_sessions_to_gm(client, seed):
    db = SessionLocal()
    try:
        db.add(Quest(world_id=seed.world_a.id, title="Find the Wyrmglass", summary="A rare artifact"))
        db.add(GameSession(world_id=seed.world_a.id, title="The Wyrmglass Heist", session_num=1))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/search", params={"q": "Wyrmglass"})
    assert r.status_code == 200
    assert "Find the Wyrmglass" in r.text
    assert "The Wyrmglass Heist" in r.text


def test_search_shows_own_character_but_not_unowned_npc_to_player(client, seed):
    db = SessionLocal()
    try:
        db.add(PlayerCharacter(world_id=seed.world_a.id, name="Zorblatt the GM NPC", owner_user_id=None))
        db.add(PlayerCharacter(world_id=seed.world_a.id, name="Zorblatt the Owned",
                                owner_user_id=seed.player_a.id))
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/search", params={"q": "Zorblatt"})
    assert r.status_code == 200
    assert "Zorblatt the Owned" in r.text
    assert "Zorblatt the GM NPC" not in r.text


def test_search_across_worlds_stays_scoped(client, seed):
    """A player in World A must not see a same-named quest/character that
    actually belongs to World B, even though search doesn't accept a
    world_id parameter from the client (it uses the active_world cookie)."""
    db = SessionLocal()
    try:
        db.add(Quest(world_id=seed.world_b.id, title="Cross-World Leak Quest"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/search", params={"q": "Cross-World"})
    assert r.status_code == 200
    assert "Cross-World Leak Quest" not in r.text


def test_pagination_clamps_out_of_range_page(client, seed):
    # 60 rows > PAGE_SIZE (50), so this actually spans two pages — with only
    # one page the pagination control isn't rendered at all (nothing to
    # clamp), which would make this test pass for the wrong reason.
    db = SessionLocal()
    try:
        for i in range(60):
            db.add(GameSession(world_id=seed.world_a.id, title=f"Session {i}", session_num=i + 1))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/sessions")
    assert "Page 1 of 2" in r.text

    r = client.get("/sessions", params={"page": 999})
    assert r.status_code == 200
    assert "Page 2 of 2" in r.text
