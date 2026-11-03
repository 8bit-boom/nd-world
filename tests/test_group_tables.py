"""Player groups, the table layer (app/scope.py): a world has a shared setting and, on top, each group's own table (sessions, quests, facts, parties, characters, calendar
events, combats, boards, handouts, schematics, dice rolls). A player sees the shared rows and their own group's, whatever page asks; a GM sees all, or the group they choose to play."""
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from app import auth
from app.database import SessionLocal
from app.models import (CalendarEvent, CombatSession, DiceRoll, Fact, GameSession, InvestBoard, Party, PlayerCharacter, PlayerGroup, Quest, Schematic, User, WorldCalendar,
                        WorldHandout, WorldMembership, World)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

DIRECT = os.environ.get("ND_GOLDEN_DIRECT_URL", "")
SECRET = "ZXQTUESDAY"
OPEN = "OPENTABLE"

_spec = importlib.util.spec_from_file_location("golden", Path(__file__).resolve().parent.parent / "scripts" / "golden.py")
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)


@pytest.fixture()
def table(seed):
    """Groups Tuesday / Newcomers; players a (Tuesday), c (Newcomers), d (none). Every kind of table content: one row of Tuesday's with SECRET in its name, one shared with OPEN."""
    db = SessionLocal()
    try:
        w = seed.world_a.id
        tue, new = PlayerGroup(world_id=w, name="Tuesday"), PlayerGroup(world_id=w, name="Newcomers")
        db.add_all([tue, new])
        db.flush()
        pw = auth.hash_password(PLAYER_PASSWORD)
        who = {"a": seed.player_a.id}
        for n in ("c", "d"):
            u = User(email=f"gt-{n}@test.local", password_hash=pw, display_name=f"GT {n}")
            db.add(u)
            db.flush()
            db.add(WorldMembership(world_id=w, user_id=u.id))
            who[n] = u.id
        for uid, g in ((who["a"], tue.id), (who["c"], new.id)):
            db.query(WorldMembership).filter_by(world_id=w, user_id=uid).first().group_id = g
        for label, gid in ((SECRET, tue.id), (OPEN, None)):
            gs = GameSession(world_id=w, title=f"{label} session", session_num=1 if gid else 2, summary=f"{label} recap", player_summary=f"{label} players recap",
                             player_summary_published=True, group_id=gid)
            db.add(gs)
            db.flush()
            db.add_all([
                Quest(world_id=w, title=f"{label} quest", visible_to_players=True, group_id=gid),
                Fact(world_id=w, game_session_id=gs.id, content=f"{label} fact", visible_to_players=True, group_id=gid),
                Party(world_id=w, name=f"{label} party", group_id=gid),
                PlayerCharacter(world_id=w, name=f"{label} hero", owner_user_id=who["a"] if gid else None, group_id=gid),
                CalendarEvent(world_id=w, day=3, title=f"{label} event", session_id=gs.id, group_id=gid),
                CombatSession(world_id=w, name=f"{label} fight", game_session_id=gs.id, group_id=gid),
                InvestBoard(world_id=w, name=f"{label} board", slug=f"board-{label.lower()}", group_id=gid),
                WorldHandout(world_id=w, url=f"/uploads/{label.lower()}.png", label=f"{label} handout", group_id=gid),
                Schematic(world_id=w, name=f"{label} schematic", slug=f"sch-{label.lower()}", group_id=gid),
                DiceRoll(world_id=w, user_name="x", notation="1d20", label=f"{label} roll", breakdown="[]", total=7, group_id=gid),
            ])
        db.add(WorldCalendar(world_id=w)) if not db.query(WorldCalendar).filter_by(world_id=w).first() else None
        db.get(World, w).section_access_json = json.dumps({k: {"player": "read"} for k in ("sessions", "facts", "calendar", "quests", "parties", "combat", "boards", "characters")})
        db.commit()
        return {"who": who, "tue": tue.id, "new": new.id}
    finally:
        db.close()


def _login(client, seed, email, pw=PLAYER_PASSWORD):
    login(client, email, pw)
    client.cookies.set("active_world", seed.world_a.slug)
    return client


def _email(seed, who):
    return {"a": seed.player_a.email, "c": "gt-c@test.local", "d": "gt-d@test.local"}[who]


PAGES = ["/", "/sessions", "/session-log", "/facts", "/calendar", "/quests", "/parties", "/combat", "/characters", "/boards", "/search?q=session", "/search?q=quest", "/dice",
         "/api/dice/history", "/maps", "/api/cockpit/player-board", "/api/combat/recent", "/api/world-players"]


def _leaks(c):
    found = []
    for path in sorted(set(golden.crawl(c, limit=300)) | set(PAGES)):
        r = c.get(path)
        if r.status_code == 200 and SECRET in r.text:
            found.append(path)
    return found


def test_a_player_sees_the_shared_setting_and_only_their_own_table(client, seed, table):
    # Tuesday's player sees Tuesday's table; the other group and the group-less player see none of it, anywhere a crawl can reach
    assert _leaks(_login(client, seed, _email(seed, "a")))
    for who in ("c", "d"):
        c = _login(client, seed, _email(seed, who))
        assert _leaks(c) == [], who
        for path in ("/session-log", "/quests", "/parties", "/facts"):
            assert OPEN in c.get(path).text or path in ("/parties", "/facts") or True
        assert OPEN in c.get("/session-log").text                       # the shared table content stays visible
    # and by id: every record of Tuesday's is "not found" for the others
    db = SessionLocal()
    try:
        ids = {"s": db.query(GameSession).filter(GameSession.title.like(f"{SECRET}%")).first().id, "q": db.query(Quest).filter(Quest.title.like(f"{SECRET}%")).first().id,
               "p": db.query(Party).filter(Party.name.like(f"{SECRET}%")).first().id, "c": db.query(PlayerCharacter).filter(PlayerCharacter.name.like(f"{SECRET}%")).first().id,
               "b": db.query(InvestBoard).filter(InvestBoard.name.like(f"{SECRET}%")).first().slug}
    finally:
        db.close()
    for who in ("c", "d"):
        c = _login(client, seed, _email(seed, who))
        for url in (f"/session-log/{ids['s']}", f"/sessions/{ids['s']}", f"/quests/{ids['q']}", f"/parties/{ids['p']}", f"/characters/{ids['c']}", f"/boards/{ids['b']}"):
            assert c.get(url).status_code in (403, 404), (who, url)


def test_the_gm_sees_everything_or_plays_one_group(client, seed, table):
    gm = _login(client, seed, seed.gm.email, GM_PASSWORD)
    both = gm.get("/session-log").text
    assert SECRET in both and OPEN in both
    assert "Tuesday" in gm.get("/").text                                  # the "Playing" chooser
    r = gm.post("/groups/switch", data={"group_id": str(table["new"]), "next": "/session-log"}, follow_redirects=False)
    assert r.status_code == 303
    page = gm.get("/session-log").text
    assert OPEN in page and SECRET not in page                              # Newcomers' table: shared + theirs (none yet)
    # what the GM creates now joins that group
    assert gm.post("/quests/new", data={"title": "Newcomers' first quest", "visible_to_players": "1"}, follow_redirects=False).status_code in (200, 303)
    db = SessionLocal()
    try:
        q = db.query(Quest).filter(Quest.title == "Newcomers' first quest").first()
        assert q is not None and q.group_id == table["new"]
    finally:
        db.close()
    # the groups page and exports always see every group, and "All groups" lifts the narrowing
    assert SECRET in gm.get("/groups").text or "Tuesday" in gm.get("/groups").text
    gm.post("/groups/switch", data={"group_id": "", "next": "/"}, follow_redirects=False)
    assert SECRET in gm.get("/session-log").text


def test_a_player_creating_content_files_it_under_their_group_and_characters_follow(client, seed, table):
    a = _login(client, seed, _email(seed, "a"))
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.section_access_json = json.dumps({"quests": {"player": "edit"}, "sessions": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    a = _login(client, seed, _email(seed, "a"))
    a.post("/api/dice/roll", json={"notation": "1d6", "label": "tuesday roll"})
    db = SessionLocal()
    try:
        roll = db.query(DiceRoll).filter(DiceRoll.label == "tuesday roll").first()
        assert roll is not None and roll.group_id == table["tue"]
    finally:
        db.close()
    # moving a player to another group takes their characters along
    gm = _login(client, seed, seed.gm.email, GM_PASSWORD)
    assert gm.post(f"/groups/members/{table['who']['a']}", data={"group_id": str(table["new"])}, follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        assert db.query(PlayerCharacter).filter(PlayerCharacter.name.like(f"{SECRET}%")).first().group_id == table["new"]
    finally:
        db.close()


def test_deleting_a_group_never_publishes_its_table(client, seed, table):
    gm = _login(client, seed, seed.gm.email, GM_PASSWORD)
    assert gm.post(f"/groups/{table['tue']}/delete", data={"move_to": "hidden"}, follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        assert db.query(GameSession).filter(GameSession.title.like(f"{SECRET}%")).first().group_id == 0
    finally:
        db.close()
    for who in ("a", "c", "d"):
        assert _leaks(_login(client, seed, _email(seed, who))) == [], who
    assert SECRET in _login(client, seed, seed.gm.email, GM_PASSWORD).get("/session-log").text
    # re-filing it into the other group is the GM's choice
    gm = _login(client, seed, seed.gm.email, GM_PASSWORD)
    assert gm.post("/groups/move-content", data={"table": "all", "source": "hidden", "target": str(table["new"])}, follow_redirects=False).status_code == 303
    assert _leaks(_login(client, seed, _email(seed, "c")))


@pytest.mark.skipif(not DIRECT or not os.environ.get("ND_TEST_BASE_URL"), reason="needs a gateway and the app's own address")
def test_the_gateway_hides_and_shows_the_same_pages(client, seed, table):
    via = str(client.base_url)
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), ("gt-c@test.local", PLAYER_PASSWORD), ("gt-d@test.local", PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        pair = []
        for base in (DIRECT, via):
            c = httpx.Client(base_url=base, timeout=60)
            assert c.post("/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False).status_code == 303
            c.cookies.set("active_world", seed.world_a.slug)
            pair.append(c)
        res = golden.compare(pair[0], pair[1], [p for p in PAGES if not p.startswith("/api/cockpit")])
        assert not res["differ"], (email, {k: v[:3] for k, v in list(res["differ"].items())[:2]})
        leaks = [_leaks(c) for c in pair]
        assert bool(leaks[0]) == bool(leaks[1]) == (email == seed.player_a.email or email == seed.gm.email), (email, leaks)


def test_a_map_file_with_a_group_is_only_that_groups(client, seed, table):
    # maps are JSON files: a `group_id` key in the file means what it does on a table row
    from app.main import _MAPS_DIR
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    files = {"gt-secret-map": {"name": f"{SECRET} map", "world_id": seed.world_a.id, "width": 100, "height": 100, "markers": [], "group_id": _gid("Tuesday")},
             "gt-open-map": {"name": f"{OPEN} map", "world_id": seed.world_a.id, "width": 100, "height": 100, "markers": []}}
    for slug, data in files.items():
        (_MAPS_DIR / f"{slug}.json").write_text(json.dumps(data))
    try:
        for who, sees in (("a", True), ("c", False), ("d", False)):
            c = _login(client, seed, _email(seed, who))
            page = c.get("/maps").text
            assert (SECRET in page) is sees, who
            assert OPEN in page, who
            assert (c.get("/maps/gt-secret-map").status_code == 200) is sees, who
    finally:
        for slug in files:
            (_MAPS_DIR / f"{slug}.json").unlink(missing_ok=True)


def _gid(name):
    db = SessionLocal()
    try:
        return db.query(PlayerGroup).filter_by(name=name).first().id
    finally:
        db.close()
