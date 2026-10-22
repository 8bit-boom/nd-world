"""Phase 3 (docs/RUST_REWRITE_PLAN.md): the writes the Rust gateway makes itself must leave the database exactly as Python leaves it.

Each case sends the same request twice - once to Python directly, once through the gateway - against two identical rows made for it, then
compares the answers (status, redirect, JSON) and the rows afterwards: every column, except ids (mapped) and timestamps (compared as
"changed or not", since the two requests run a moment apart). Refusals and odd input must reach Python unchanged, so their answers match too.
Runs only with a gateway to talk to (ND_TEST_BASE_URL) and Python directly (ND_GOLDEN_DIRECT_URL), like tests/test_gateway_golden.py.
"""
import json
from urllib.parse import urlencode

import pytest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD
from .test_gateway_golden import DIRECT, _signed_in, _via

pytestmark = pytest.mark.skipif(not DIRECT, reason="needs ND_TEST_BASE_URL (the gateway) and ND_GOLDEN_DIRECT_URL (Python directly)")

_STAMPS = ("created_at", "updated_at")


def _row(table, rid):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT * FROM {table} WHERE id = :i"), {"i": rid}).mappings().first()
        return dict(r) if r else None
    finally:
        db.close()


def _newest(table):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1")).mappings().first()
        return dict(r) if r else None
    finally:
        db.close()


def _same_rows(a, b, before_a=None, before_b=None, ignore=()):
    """Two rows equal except their ids and timestamps; a timestamp must have changed on both or on neither."""
    if a is None or b is None:
        assert a is None and b is None, (a, b)
        return
    for k in a:
        if k == "id" or k in ignore:
            continue
        if k in _STAMPS:
            if before_a is not None:
                assert (a[k] != before_a[k]) == (b[k] != before_b[k]), (k, before_a[k], a[k], before_b[k], b[k])
            continue
        assert a[k] == b[k], (k, a[k], b[k])


def _same_answer(ra, rb, id_a=None, id_b=None):
    assert ra.status_code == rb.status_code, (ra.status_code, rb.status_code, ra.text[:300], rb.text[:300])
    la, lb = ra.headers.get("location"), rb.headers.get("location")
    if la and id_a is not None:
        la = la.replace(str(id_a), "<id>")
        lb = lb.replace(str(id_b), "<id>")
    assert la == lb, (la, lb)
    if ra.headers.get("content-type", "").startswith("application/json"):
        assert ra.json() == rb.json(), (ra.text, rb.text)
        assert ra.content == rb.content, (ra.content, rb.content)


def _people(seed):
    return ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
            (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug))


# ── quests ────────────────────────────────────────────────────────────────────

def _quest(**kw):
    from app.database import SessionLocal
    from app.models import Quest
    db = SessionLocal()
    try:
        q = Quest(**kw)
        db.add(q)
        db.commit()
        return q.id
    finally:
        db.close()


_QUEST_FORMS = [
    {"title": "  New <quest>  ", "status": "complete", "category": "side", "summary": " s ", "body": "b **x**", "linked_entities_json": '[{"entity_id": 1}]'},
    {"title": "", "status": "", "category": ""},
    {},
    {"title": "T", "parent_id": "3", "assigned_party_id": "1"},
    {"title": "T", "linked_entities_json": "{nope"},
    {"title": "T", "parent_id": "x"},
    {"title": "T", "parent_id": " 4"},
    {"title": "Ünïcode ✓", "body": "line\r\nline"},
]


def test_quest_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"quests": {"player": "edit"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        wid = seed.world_a.id
        # create
        for form in _QUEST_FORMS:
            ra = d.post("/quests/new", data=form, follow_redirects=False)
            na = _newest("quests")
            rb = via.post("/quests/new", data=form, follow_redirects=False)
            nb = _newest("quests")
            created = ra.status_code == 303
            _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
            if created:
                _same_rows(na, nb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
        # edit / status / delete on two identical quests (one owned by player A, one GM-authored)
        for owner in (None, seed.player_a.id):
            for form in _QUEST_FORMS + [{"title": "Same", "status": "active", "category": "main", "summary": "", "body": "",
                                         "linked_entities_json": "[]"}]:
                ids = [_quest(world_id=wid, title="Same", created_by_user_id=owner, parent_id=None) for _ in range(2)]
                before = [_row("quests", i) for i in ids]
                ra = d.post(f"/quests/{ids[0]}/edit", data=form, follow_redirects=False)
                rb = via.post(f"/quests/{ids[1]}/edit", data=form, follow_redirects=False)
                _same_answer(ra, rb, ids[0], ids[1])
                _same_rows(_row("quests", ids[0]), _row("quests", ids[1]), before[0], before[1])
                served += rb.headers.get("x-nd-served-by") == "gateway"
            for body in ({"status": "failed"}, {"status": "  "}, {}, {"status": 3}, {"status": "active"}):
                ids = [_quest(world_id=wid, title="S", created_by_user_id=owner) for _ in range(2)]
                before = [_row("quests", i) for i in ids]
                ra = d.post(f"/api/quests/{ids[0]}/status", json=body)
                rb = via.post(f"/api/quests/{ids[1]}/status", json=body)
                _same_answer(ra, rb)
                _same_rows(_row("quests", ids[0]), _row("quests", ids[1]), before[0], before[1])
            ids = [_quest(world_id=wid, title="D", created_by_user_id=owner) for _ in range(2)]
            kids = [_quest(world_id=wid, title="child", parent_id=i) for i in ids]
            kb = [_row("quests", k) for k in kids]
            ra = d.post(f"/quests/{ids[0]}/delete", follow_redirects=False)
            rb = via.post(f"/quests/{ids[1]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            assert (_row("quests", ids[0]) is None) == (_row("quests", ids[1]) is None)
            ka, kb2 = _row("quests", kids[0]), _row("quests", kids[1])
            _same_rows(ka, kb2, kb[0], kb[1], ignore=("parent_id",))
            assert (ka["parent_id"] == ids[0]) == (kb2["parent_id"] == ids[1]) and (ka["parent_id"] is None) == (kb2["parent_id"] is None)
        # another world's quest and a missing one: refused the same way
        far = [_quest(world_id=seed.world_b.id, title="Far") for _ in range(2)]
        _same_answer(d.post(f"/quests/{far[0]}/delete", follow_redirects=False), via.post(f"/quests/{far[1]}/delete", follow_redirects=False))
        _same_answer(d.post("/quests/999999/edit", data={"title": "x"}, follow_redirects=False),
                     via.post("/quests/999999/edit", data={"title": "x"}, follow_redirects=False))
    assert served >= 20, served


# ── facts ─────────────────────────────────────────────────────────────────────

def _fact(**kw):
    from app.database import SessionLocal
    from app.models import Fact
    db = SessionLocal()
    try:
        f = Fact(**kw)
        db.add(f)
        db.commit()
        return f.id
    finally:
        db.close()


_FACT_FORMS = [
    {"content": "  The door was red.  ", "tags": " door, red ", "visible_to_players": "on", "next": "/facts"},
    {"content": "x", "next": "/sessions/12"},
    {"content": "x", "next": "https://evil.example/"},
    {"content": "", "next": "/sessions/3"},
    {"content": "Hidden", "game_session_id": "7"},
    {"content": "Bad id", "game_session_id": "seven"},
    {"content": "y", "tags": "   ", "visible_to_players": ""},
    {},
]


def test_fact_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"facts": {"player": "edit", "assistant": "edit"}})
        db.commit()
    finally:
        db.close()
    served = 0
    wid = seed.world_a.id
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in _FACT_FORMS:
            ra = d.post("/facts/new", data=form, follow_redirects=False)
            na = _newest("facts")
            rb = via.post("/facts/new", data=form, follow_redirects=False)
            nb = _newest("facts")
            _same_answer(ra, rb)
            if na and nb and na["id"] != nb["id"]:
                _same_rows(na, nb)
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for form in _FACT_FORMS:
            ids = [_fact(world_id=wid, content="Old", tags="a", visible_to_players=False, game_session_id=5) for _ in range(2)]
            before = [_row("facts", i) for i in ids]
            ra = d.post(f"/facts/{ids[0]}/edit", data=form, follow_redirects=False)
            rb = via.post(f"/facts/{ids[1]}/edit", data=form, follow_redirects=False)
            _same_answer(ra, rb)
            _same_rows(_row("facts", ids[0]), _row("facts", ids[1]), before[0], before[1])
        for form in ({"next": "/sessions/4"}, {}, {"next": "/x"}):
            ids = [_fact(world_id=wid, content="Del") for _ in range(2)]
            w0 = _row("worlds", wid)["recap_content_touch"]
            ra = d.post(f"/facts/{ids[0]}/delete", data=form, follow_redirects=False)
            w1 = _row("worlds", wid)["recap_content_touch"]
            rb = via.post(f"/facts/{ids[1]}/delete", data=form, follow_redirects=False)
            w2 = _row("worlds", wid)["recap_content_touch"]
            _same_answer(ra, rb)
            assert (_row("facts", ids[0]) is None) == (_row("facts", ids[1]) is None)
            assert (w1 != w0) == (w2 != w1)
        far = [_fact(world_id=seed.world_b.id, content="Far") for _ in range(2)]
        _same_answer(d.post(f"/facts/{far[0]}/edit", data={"content": "x"}, follow_redirects=False),
                     via.post(f"/facts/{far[1]}/edit", data={"content": "x"}, follow_redirects=False))
    assert served >= 7, served   # the GM's creates (players may only read facts: auth_gate refuses them, through Python)


# ── random tables ─────────────────────────────────────────────────────────────

def _table(**kw):
    from app.database import SessionLocal
    from app.models import RandomTable
    db = SessionLocal()
    try:
        t = RandomTable(**kw)
        db.add(t)
        db.commit()
        return t.id
    finally:
        db.close()


_TABLE_FORMS = [
    {"name": "  Loot Table!  ", "category": " loot ", "description": " d ", "entries_json": '[{"label": "A", "weight": 2}]'},
    {"name": "", "category": ""},
    {},
    {"name": "Loot Table!", "entries_json": "{bad"},
    {"name": "x" * 80},
    {"name": "Ünï"},
    {"name": "---"},
]


def test_table_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"tables": {"player": "edit"}})
        db.commit()
    finally:
        db.close()
    served = 0
    wid = seed.world_a.id
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in _TABLE_FORMS:
            ra = d.post("/tables/new", data=form, follow_redirects=False)
            na = _newest("random_tables")
            rb = via.post("/tables/new", data=form, follow_redirects=False)
            nb = _newest("random_tables")
            created = ra.status_code == 303
            _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
            if created:
                # the second one gets the next free slug: compare the slug's base
                assert nb["slug"].startswith(na["slug"].rsplit("-", 1)[0]), (na["slug"], nb["slug"])
                _same_rows(na, nb, ignore=("slug",))
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for owner, builtin, tw in ((None, False, wid), (seed.player_a.id, False, wid), (None, True, None), (None, False, None),
                                   (None, False, seed.world_b.id)):
            for form in _TABLE_FORMS:
                ids = [_table(world_id=tw, name="T", slug=f"t-{owner}-{builtin}-{tw}-{i}-{json.dumps(form)[:20]}-{email}", is_builtin=builtin,
                              created_by_user_id=owner, entries_json='[{"label": "x"}]') for i in range(2)]
                before = [_row("random_tables", i) for i in ids]
                ra = d.post(f"/tables/{ids[0]}/edit", data=form, follow_redirects=False)
                rb = via.post(f"/tables/{ids[1]}/edit", data=form, follow_redirects=False)
                _same_answer(ra, rb, ids[0], ids[1])
                _same_rows(_row("random_tables", ids[0]), _row("random_tables", ids[1]), before[0], before[1], ignore=("slug",))
            ids = [_table(world_id=tw, name="D", slug=f"del-{owner}-{builtin}-{tw}-{i}-{email}", is_builtin=builtin, created_by_user_id=owner)
                   for i in range(2)]
            ra = d.post(f"/tables/{ids[0]}/delete", follow_redirects=False)
            rb = via.post(f"/tables/{ids[1]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            assert (_row("random_tables", ids[0]) is None) == (_row("random_tables", ids[1]) is None)
        # rolls: random, so the shape and the bounds
        for entries in ('[{"label": "A", "weight": 3}, {"label": "B"}]', '[{"label": "Z", "weight": -1}]', "[]", '[{"label": 5, "weight": 0}]'):
            tid = _table(world_id=wid, name="R", slug=f"roll-{email}-{entries}", entries_json=entries)
            ra, rb = d.post(f"/api/tables/{tid}/roll"), via.post(f"/api/tables/{tid}/roll")
            assert ra.status_code == rb.status_code, (entries, ra.text, rb.text)
            if ra.status_code == 200:
                ja, jb = ra.json(), rb.json()
                assert list(ja) == list(jb) and ja["total"] == jb["total"] and 1 <= jb["roll"] <= jb["total"]
                assert jb["result"] in [e.get("label", "") for e in json.loads(entries)]
    assert served >= 6, served


# ── calendar ──────────────────────────────────────────────────────────────────

def test_calendar_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import CalendarEvent, World, WorldCalendar
    base = str(client.base_url)
    config = {"era_name": "Ash", "current_day": 40, "months": [{"name": "Frost", "days": 30, "color": "#abc"}, {"name": "Thaw", "days": 20}],
              "moons": [{"name": "Pale", "cycle_days": 28, "offset": 0.5}], "days_per_week": 5}
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"calendar": {"player": "edit"}})
        db.add(WorldCalendar(world_id=seed.world_a.id, config_json=json.dumps(config)))
        db.commit()
    finally:
        db.close()

    def cal():
        from sqlalchemy import text
        db = SessionLocal()
        try:
            return db.execute(text("SELECT config_json FROM world_calendars WHERE world_id = :w"), {"w": seed.world_a.id}).scalar()
        finally:
            db.close()

    def set_cal(text_):
        db = SessionLocal()
        try:
            db.query(WorldCalendar).filter(WorldCalendar.world_id == seed.world_a.id).update({"config_json": text_})
            db.commit()
        finally:
            db.close()

    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for body in ({"day": 12, "title": "  Duel ", "notes": "n", "color": "#f00", "entity_id": "3", "party_id": 0},
                     {"day": 999999999, "title": ""}, {}, {"day": "x"}, {"day": 2.5}, {"title": 5}, {"entity_id": True}, {"session_id": "0"}):
            ra = d.post("/api/calendar/events", json=body)
            na = _newest("calendar_events")
            rb = via.post("/api/calendar/events", json=body)
            nb = _newest("calendar_events")
            assert ra.status_code == rb.status_code, (body, ra.text, rb.text)
            if ra.status_code == 200:
                ja, jb = ra.json(), rb.json()
                assert {k: v for k, v in ja.items() if k != "id"} == {k: v for k, v in jb.items() if k != "id"}
                _same_rows(na, nb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for owner in (None, seed.player_a.id):
            db = SessionLocal()
            evs = [CalendarEvent(world_id=seed.world_a.id, day=3, title="E", created_by_user_id=owner) for _ in range(2)]
            db.add_all(evs); db.commit(); ids = [e.id for e in evs]; db.close()
            _same_answer(d.post(f"/api/calendar/events/{ids[0]}/delete"), via.post(f"/api/calendar/events/{ids[1]}/delete"))
            assert (_row("calendar_events", ids[0]) is None) == (_row("calendar_events", ids[1]) is None)
        # advance / set-date rewrite the stored config: run each on the same starting config through both servers
        for start in (json.dumps(config), "{}", json.dumps({**config, "current_day": 2999999})):
            for path, body in (("/api/calendar/advance", {"days": 5}), ("/api/calendar/advance", {}), ("/api/calendar/advance", {"days": -100}),
                               ("/api/calendar/advance", {"days": "3"}), ("/api/calendar/set-date", {"year": 3, "month_idx": 1, "dom": 99}),
                               ("/api/calendar/set-date", {"year": 0, "month_idx": -4}), ("/api/calendar/set-date", {})):
                set_cal(start)
                ra = d.post(path, json=body)
                after_a = cal()
                set_cal(start)
                rb = via.post(path, json=body)
                after_b = cal()
                _same_answer(ra, rb)
                assert after_a == after_b, (path, body, after_a, after_b)
                served += rb.headers.get("x-nd-served-by") == "gateway"
        set_cal(json.dumps(config))
    assert served >= 20, served


# ── boards ────────────────────────────────────────────────────────────────────

def _board(**kw):
    from app.database import SessionLocal
    from app.models import InvestBoard
    db = SessionLocal()
    try:
        b = InvestBoard(**kw)
        db.add(b)
        db.commit()
        return b.slug
    finally:
        db.close()


def test_board_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"boards": {"player": "edit"}})
        db.commit()
    finally:
        db.close()

    def by_slug(slug):
        from sqlalchemy import text
        db = SessionLocal()
        try:
            r = db.execute(text("SELECT * FROM invest_boards WHERE slug = :s"), {"s": slug}).mappings().first()
            return dict(r) if r else None
        finally:
            db.close()

    served = 0
    n = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in ({"name": "The Case!", "description": "d", "canvas_bg": "dark"}, {"name": "The Case!"}, {"name": "", "description": "x"},
                     {}, {"name": "Ünï"}, {"name": "x", "canvas_bg": ""}):
            ra = d.post("/boards/new", data=form, follow_redirects=False)
            na = _newest("invest_boards")
            rb = via.post("/boards/new", data=form, follow_redirects=False)
            nb = _newest("invest_boards")
            assert ra.status_code == rb.status_code, (form, ra.text[:200], rb.text[:200])
            if ra.status_code == 303:
                assert rb.headers["location"].startswith(ra.headers["location"].rsplit("-", 1)[0])
                _same_rows(na, nb, ignore=("slug",))
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for body in ({"nodes": [{"id": 1, "title": "Ünï <x>", "x": 10.5, "y": -3, "tags": ["a"]}], "groups": [{"id": "g"}], "edges": [{"from": 1, "to": 1}]},
                     {}, {"nodes": None}, {"nodes": "text", "edges": {"a": [1, 2.25e-7]}}, [1, 2]):
            n += 1
            slugs = [_board(world_id=seed.world_a.id, name="B", slug=f"save-{n}-{i}") for i in range(2)]
            before = [by_slug(x) for x in slugs]
            ra, rb = d.post(f"/boards/{slugs[0]}/save", json=body), via.post(f"/boards/{slugs[1]}/save", json=body)
            _same_answer(ra, rb)
            _same_rows(by_slug(slugs[0]), by_slug(slugs[1]), before[0], before[1], ignore=("slug",))
            served += rb.headers.get("x-nd-served-by") == "gateway"
        n += 1
        slugs = [_board(world_id=seed.world_a.id, name="D", slug=f"del-{n}-{i}") for i in range(2)]
        _same_answer(d.post(f"/boards/{slugs[0]}/delete", follow_redirects=False), via.post(f"/boards/{slugs[1]}/delete", follow_redirects=False))
        assert (by_slug(slugs[0]) is None) == (by_slug(slugs[1]) is None)
        far = [_board(world_id=seed.world_b.id, name="F", slug=f"far-{n}-{i}") for i in range(2)]
        _same_answer(d.post(f"/boards/{far[0]}/save", json={}), via.post(f"/boards/{far[1]}/save", json={}))
    assert served >= 7, served   # the GM's (players may not write boards: auth_gate refuses them, through Python)


# ── sessions ──────────────────────────────────────────────────────────────────

def _gsession(**kw):
    from app.database import SessionLocal
    from app.models import GameSession
    db = SessionLocal()
    try:
        g = GameSession(**kw)
        db.add(g)
        db.commit()
        return g.id
    finally:
        db.close()


def test_session_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import Entity, PlayerCharacter, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"sessions": {"player": "edit", "assistant": "edit"}})
        e = Entity(world_id=seed.world_a.id, kind="npc", name="Mara <m>")
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Hero", owner_user_id=seed.player_a.id)
        db.add_all([e, pc]); db.commit()
        eid, pcid = e.id, pc.id
    finally:
        db.close()
    served = 0
    forms = [{"title": " Night ", "session_num": "4", "session_date": " 2026-01-02 ", "summary": "s\r\nx", "party_id": "2", "xp_awarded": "150"},
             {}, {"title": "", "session_num": "", "xp_awarded": ""}, {"session_num": "four"}, {"session_num": " 4"}]
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in forms:
            ra = d.post("/sessions/new", data=form, follow_redirects=False)
            na = _newest("game_sessions")
            rb = via.post("/sessions/new", data=form, follow_redirects=False)
            nb = _newest("game_sessions")
            created = ra.status_code == 303
            _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
            if created:
                _same_rows(na, nb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for form in forms + [[("title", "T"), ("npc_entity_ids", str(eid)), ("npc_entity_ids", f"pc:{pcid}"), ("npc_entity_ids", "99999"),
                              ("npc_entity_ids", str(eid))], [("npc_entity_ids", "x")]]:
            ids = [_gsession(world_id=seed.world_a.id, title="Old", session_num=9) for _ in range(2)]
            before = [_row("game_sessions", i) for i in ids]
            kw = ({"content": urlencode(form), "headers": {"content-type": "application/x-www-form-urlencoded"}} if isinstance(form, list)
                  else {"data": form})
            ra = d.post(f"/sessions/{ids[0]}/edit", follow_redirects=False, **kw)
            rb = via.post(f"/sessions/{ids[1]}/edit", follow_redirects=False, **kw)
            _same_answer(ra, rb, ids[0], ids[1])
            _same_rows(_row("game_sessions", ids[0]), _row("game_sessions", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        prep = json.dumps([{"task": "Map", "done": False}, {"task": "NPCs"}, {"task": "Ünï", "done": True, "x": 1.5e-7}])
        for path, body in (("toggle", {"index": 1}), ("toggle", {"index": 9}), ("toggle", {}), ("toggle", {"index": None}), ("toggle", {"index": "1"}),
                           ("add", {"task": "  Loot  "}), ("add", {"task": ""}), ("add", {}), ("0/delete", None), ("7/delete", None)):
            ids = [_gsession(world_id=seed.world_a.id, title="P", prep_json=prep) for _ in range(2)]
            before = [_row("game_sessions", i) for i in ids]
            url = "/api/sessions/{}/prep/" + path
            ra = d.post(url.format(ids[0]), json=body) if body is not None else d.post(url.format(ids[0]))
            rb = via.post(url.format(ids[1]), json=body) if body is not None else via.post(url.format(ids[1]))
            _same_answer(ra, rb)
            _same_rows(_row("game_sessions", ids[0]), _row("game_sessions", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 15, served   # the GM's (session writes are GM / assistant routes in auth_gate)


# ── entity links and notes ────────────────────────────────────────────────────

def _entities(world_id, n):
    from app.database import SessionLocal
    from app.models import Entity
    db = SessionLocal()
    try:
        es = [Entity(world_id=world_id, kind="npc", name="E") for _ in range(n)]
        db.add_all(es)
        db.commit()
        return [e.id for e in es]
    finally:
        db.close()


def _links(src):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        return db.execute(text("SELECT COUNT(*) FROM entity_links WHERE source_id = :s"), {"s": src}).scalar()
    finally:
        db.close()


def _note(entity_id, **kw):
    from app.database import SessionLocal
    from app.models import EntityNote
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content="old", **kw)
        db.add(n)
        db.commit()
        return n.id
    finally:
        db.close()


def test_entity_link_and_note_writes_are_pythons(client, seed):
    base = str(client.base_url)
    served = 0
    [other] = _entities(seed.world_b.id, 1)
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        # link / unlink: a fresh pair, a duplicate link, another world's target, a missing one
        for twice, foreign, missing in ((False, False, False), (True, False, False), (False, True, False), (False, False, True)):
            sa, ta, sb, tb = _entities(seed.world_a.id, 4)
            if foreign:
                ta = tb = other
            if missing:
                ta = tb = 999999
            for _ in range(2 if twice else 1):
                ra = d.post(f"/entity/{sa}/link/{ta}", follow_redirects=False)
                rb = via.post(f"/entity/{sb}/link/{tb}", follow_redirects=False)
                _same_answer(ra, rb, sa, sb)
                assert _links(sa) == _links(sb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
            ra = d.post(f"/entity/{sa}/unlink/{ta}", follow_redirects=False)
            rb = via.post(f"/entity/{sb}/unlink/{tb}", follow_redirects=False)
            _same_answer(ra, rb, sa, sb)
            assert _links(sa) == _links(sb)
        # notes
        for form in ({"content": "  A <note>  ", "visible": "on"}, {"content": "x", "visible": ""}, {"content": ""}, {"content": "   "}, {},
                     {"content": "Ünï\r\nline"}):
            ea, eb = _entities(seed.world_a.id, 2)
            ra = d.post(f"/entity/{ea}/notes/new", data=form, follow_redirects=False)
            rb = via.post(f"/entity/{eb}/notes/new", data=form, follow_redirects=False)
            _same_answer(ra, rb, ea, eb)
            _same_rows(_newest_note(ea), _newest_note(eb), ignore=("entity_id",))
            served += rb.headers.get("x-nd-served-by") == "gateway"
            for action, data in (("toggle", None), ("edit", form), ("delete", None)):
                na, nb = _note(ea, visible_to_players=True), _note(eb, visible_to_players=True)
                before = [_row("entity_notes", na), _row("entity_notes", nb)]
                for ent_a, ent_b in ((ea, eb), (other, other), (999999, 999999)):
                    kw = {"data": data} if data is not None else {}
                    ra = d.post(f"/entity/{ent_a}/notes/{na}/{action}", follow_redirects=False, **kw)
                    rb = via.post(f"/entity/{ent_b}/notes/{nb}/{action}", follow_redirects=False, **kw)
                    _same_answer(ra, rb, ea, eb)
                    _same_rows(_row("entity_notes", na), _row("entity_notes", nb), before[0], before[1], ignore=("entity_id",))
                    served += rb.headers.get("x-nd-served-by") == "gateway"
            # a note of another entity
            na = _note(eb)
            ra = d.post(f"/entity/{ea}/notes/{na}/delete", follow_redirects=False)
            rb = via.post(f"/entity/{ea}/notes/{na}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            assert _row("entity_notes", na) is not None
    assert served >= 20, served   # the GM's (players reach entity writes only through Python's permission checks)


def _newest_note(entity_id):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT * FROM entity_notes WHERE entity_id = :e ORDER BY id DESC LIMIT 1"), {"e": entity_id}).mappings().first()
        return dict(r) if r else None
    finally:
        db.close()


def test_entity_field_rename_and_folder_writes_are_pythons(client, seed):
    base = str(client.base_url)
    served = 0
    [other] = _entities(seed.world_b.id, 1)
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for start, body in ((None, {"field": "subtype", "value": "  Big   tower  "}), (None, {"field": "subtype"}), ("x", {"field": "subtype", "value": "x"}),
                            ("x", {"field": "subtype", "value": " "}), (None, {"field": "name", "value": "y"}), (None, {"field": "subtype", "value": 3}),
                            (None, {"value": "y"}), (None, {"field": "subtype", "value": "é" * 80}), (None, {"field": "subtype", "value": "a\x1cb"})):
            ea, eb = _entities(seed.world_a.id, 2)
            if start:
                d.post(f"/api/entity/{ea}/field", json={"field": "subtype", "value": start}); d.post(f"/api/entity/{eb}/field", json={"field": "subtype", "value": start})
            before = [_row("entities", ea), _row("entities", eb)]
            ra = d.post(f"/api/entity/{ea}/field", json=body)
            rb = via.post(f"/api/entity/{eb}/field", json=body)
            if ra.headers.get("content-type", "").startswith("application/json") and isinstance(ra.json(), dict) and "id" in ra.json():
                assert ra.json() | {"id": 0} == rb.json() | {"id": 0}, (ra.text, rb.text)
                assert ra.status_code == rb.status_code
            else:
                _same_answer(ra, rb)
            _same_rows(_row("entities", ea), _row("entities", eb), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for body in ({"name": "  New\tname  "}, {"name": ""}, {"name": "E"}, {"name": 5}, {}, {"name": "ü" * 300}):
            ea, eb = _entities(seed.world_a.id, 2)
            for target_a, target_b in ((ea, eb), (other, other)):
                ra = d.post(f"/api/entity/{target_a}/rename", json=body)
                rb = via.post(f"/api/entity/{target_b}/rename", json=body)
                if ra.status_code == 200:
                    assert ra.json() | {"id": 0} == rb.json() | {"id": 0}, (ra.text, rb.text)
                else:
                    _same_answer(ra, rb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
            _same_rows(_row("entities", ea), _row("entities", eb))
        for make in (lambda a, b: {"entity_ids": a, "folder": "  Dungeon  "}, lambda a, b: {"entity_ids": a}, lambda a, b: {"entity_ids": a + [other, 999999], "folder": ""},
                     lambda a, b: {"entity_ids": [str(a[0])], "folder": "f"}, lambda a, b: {"folder": "f"}, lambda a, b: {"entity_ids": a + a, "folder": 3},
                     lambda a, b: {"entity_ids": a + a, "folder": "dup"}):
            group_a, group_b = _entities(seed.world_a.id, 2), _entities(seed.world_a.id, 2)
            before = [_row("entities", i) for i in group_a + group_b]
            ra = d.post("/api/entities/bulk-folder", json=make(group_a, None))
            rb = via.post("/api/entities/bulk-folder", json=make(group_b, None))
            _same_answer(ra, rb)
            for i in range(2):
                _same_rows(_row("entities", group_a[i]), _row("entities", group_b[i]), before[i], before[2 + i])
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 10, served   # the GM's


def _access(entity_id):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        return sorted(r[0] for r in db.execute(text("SELECT user_id FROM entity_player_access WHERE entity_id = :e"), {"e": entity_id}))
    finally:
        db.close()


def _entity_forms(seed):
    p = str(seed.player_a.id)
    return [
        [("kind", "npc"), ("name", " Mara <m> "), ("subtype", "merchant"), ("folder", "  Town  "), ("tags", "a,b"), ("aliases", " Mar, mar ,, M2,  "),
         ("roleplay_personality", "  gruff  "), ("image_url", " https://x/y.png "), ("summary", "s"), ("body", "b\r\nline"),
         ("visibility_mode", "players"), ("allowed_player_ids", p), ("allowed_player_ids", p), ("allowed_player_ids", "99999"),
         ("template_id", "3"), ("custom_fields_json", '{"hp": 5}'), ("rag_priority", "on")],
        [("kind", "location"), ("name", "Keep"), ("visibility_mode", "gm"), ("allowed_player_ids", p), ("template_id", "x"), ("custom_fields_json", "{bad")],
        [("kind", "item"), ("name", "Ünïcode ✓"), ("visibility_mode", ""), ("custom_fields_json", ""), ("rag_priority", ""), ("remove_image", "on")],
        [("kind", "npc"), ("name", "")],
        [("name", "x")],
        [("kind", "npc"), ("name", "x"), ("allowed_player_ids", "x")],
        [("kind", "npc"), ("name", "x"), ("custom_fields_json", "NaN")],
        [("kind", "npc"), ("name", "x"), ("aliases", "Ärger, ärger")],
        [("kind", "npc"), ("name", "x"), ("save_and_new", "1"), ("folder", " A/b c&d ")],
    ]


def _send(client, url, form, how):
    """POST the entity form urlencoded, or as a browser sends it: multipart with an empty image_file part (no file chosen - httpx would
    leave filename="" out), or with a real file ("upload", which is Python's)."""
    if how == "urlencoded":
        return client.post(url, content=urlencode(form), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    b = "----WebKitFormBoundaryNd7x"
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v.encode() + b"\r\n" for k, v in form]
    fname, data = ("", b"") if how == "multipart" else ("a.png", b"\x89PNG")
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="image_file"; filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                 + data + b"\r\n")
    body = b"".join(parts) + f"--{b}--\r\n".encode()
    return client.post(url, content=body, headers={"content-type": f"multipart/form-data; boundary={b}"}, follow_redirects=False)


def test_entity_create_edit_duplicate_delete_are_pythons(client, seed):
    base = str(client.base_url)
    served = 0
    [other] = _entities(seed.world_b.id, 1)
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in _entity_forms(seed):
            for how in ("urlencoded", "multipart", "upload"):
                if how == "upload" and form[0][0] != "kind":
                    continue
                ra = _send(d, "/new", form, how)
                na = _newest("entities")
                rb = _send(via, "/new", form, how)
                nb = _newest("entities")
                created = ra.status_code == 303 and ra.headers["location"].startswith("/entity/")
                _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
                if created:
                    _same_rows(na, nb, ignore=("image_url",) if how == "upload" else ())
                    assert _access(na["id"]) == _access(nb["id"])
                served += rb.headers.get("x-nd-served-by") == "gateway"
                ea, eb = _entities(seed.world_a.id, 2)
                for e in (ea, eb):
                    d.post(f"/entity/{e}/edit", data={"kind": "npc", "name": "E", "image_url": "/uploads/keep.png", "visibility_mode": "players",
                                                      "allowed_player_ids": str(seed.player_b.id)})
                before = [_row("entities", ea), _row("entities", eb)]
                ra = _send(d, f"/entity/{ea}/edit", form, how)
                rb = _send(via, f"/entity/{eb}/edit", form, how)
                _same_answer(ra, rb, ea, eb)
                _same_rows(_row("entities", ea), _row("entities", eb), before[0], before[1], ignore=("image_url",) if how == "upload" else ())
                assert _access(ea) == _access(eb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
        ra = _send(d, f"/entity/{other}/edit", _entity_forms(seed)[1], "urlencoded")
        rb = _send(via, f"/entity/{other}/edit", _entity_forms(seed)[1], "urlencoded")
        _same_answer(ra, rb)
        # duplicate and delete (with links, notes and access around it)
        ea, eb = _entities(seed.world_a.id, 2)
        [t] = _entities(seed.world_a.id, 1)
        for e in (ea, eb):
            d.post(f"/entity/{e}/edit", data={"kind": "npc", "name": "Gob", "aliases": "g", "custom_fields_json": '{"a": 1}', "visibility_mode": "players",
                                              "allowed_player_ids": str(seed.player_a.id), "rag_priority": "on", "roleplay_personality": "rp"})
            d.post(f"/entity/{e}/link/{t}"); d.post(f"/entity/{t}/link/{e}"); d.post(f"/entity/{e}/notes/new", data={"content": "n"})
        ra = d.post(f"/entity/{ea}/duplicate", follow_redirects=False)
        na = _newest("entities")
        rb = via.post(f"/entity/{eb}/duplicate", follow_redirects=False)
        nb = _newest("entities")
        _same_answer(ra, rb, na["id"], nb["id"])
        _same_rows(na, nb)
        served += rb.headers.get("x-nd-served-by") == "gateway"
        for target_a, target_b in ((other, other), (999999, 999999), (ea, eb)):
            ra = d.post(f"/entity/{target_a}/delete", follow_redirects=False)
            rb = via.post(f"/entity/{target_b}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            served += rb.headers.get("x-nd-served-by") == "gateway"
        assert _row("entities", ea) is None and _row("entities", eb) is None or (_row("entities", ea) is not None and _row("entities", eb) is not None)
        assert _links(t) == 0 or email != seed.gm.email
        assert _access(ea) == _access(eb) and _newest_note(ea) == _newest_note(eb) == None or email != seed.gm.email
    assert served >= 12, served   # the GM's; invalid custom_fields_json, non-ASCII aliases and uploads are Python's


def test_entity_bulk_visibility_alias_and_bulk_delete_are_pythons(client, seed):
    base = str(client.base_url)
    served = 0
    [other] = _entities(seed.world_b.id, 1)
    pa, pb = seed.player_a.id, seed.player_b.id
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for make in (lambda g: {"entity_ids": g, "visibility_mode": "gm"}, lambda g: {"entity_ids": g + g + [other], "visibility_mode": " players ", "allowed_player_ids": [pa, pa, pb]},
                     lambda g: {"entity_ids": g, "visibility_mode": "everyone"}, lambda g: {"entity_ids": g, "visibility_mode": "nope"},
                     lambda g: {"entity_ids": [str(g[0])], "visibility_mode": "gm"}, lambda g: {"visibility_mode": "gm"},
                     lambda g: {"entity_ids": g, "visibility_mode": "players", "allowed_player_ids": ["1"]}, lambda g: {"entity_ids": g, "visibility_mode": 3}):
            ga, gb = _entities(seed.world_a.id, 2), _entities(seed.world_a.id, 2)
            before = [_row("entities", i) for i in ga + gb]
            ra = d.post("/api/entities/bulk-visibility", json=make(ga))
            rb = via.post("/api/entities/bulk-visibility", json=make(gb))
            _same_answer(ra, rb)
            for i in range(2):
                _same_rows(_row("entities", ga[i]), _row("entities", gb[i]), before[i], before[2 + i])
                assert _access(ga[i]) == _access(gb[i])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for start, body in ((None, {"alias": "  The,Old  Man "}), ("Mar, Bo", {"alias": "Kit"}), ("Mar", {"alias": "mar"}), (None, {"alias": "e"}),
                            (None, {"alias": "x"}), (None, {"alias": "Ünï"}), (None, {"alias": 5}), (None, {}), (", ".join(f"a{i}" for i in range(20)), {"alias": "zz"}),
                            (None, {"alias": "y" * 90})):
            ea, eb = _entities(seed.world_a.id, 2)
            if start:
                for e in (ea, eb):
                    d.post(f"/entity/{e}/edit", data={"kind": "npc", "name": "E", "aliases": start})
            before = [_row("entities", ea), _row("entities", eb)]
            for ta, tb in ((ea, eb), (other, other)):
                ra = d.post(f"/api/entity/{ta}/alias", json=body)
                rb = via.post(f"/api/entity/{tb}/alias", json=body)
                _same_answer(ra, rb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
            _same_rows(_row("entities", ea), _row("entities", eb), before[0], before[1])
        for make in (lambda g: [("entity_ids", str(i)) for i in g] + [("folder", "A b/c"), ("q", "x&y")], lambda g: [("entity_ids", str(g[0])), ("entity_ids", ""), ("folder", "")],
                     lambda g: [("entity_ids", " 1")], lambda g: [("entity_ids", str(other))], lambda g: [("q", "")]):
            ga, gb = _entities(seed.world_a.id, 2), _entities(seed.world_a.id, 2)
            for e in ga + gb:
                d.post(f"/entity/{e}/notes/new", data={"content": "n"})
            for kind in ("npc", "location"):
                ra = d.post(f"/kind/{kind}/bulk-delete", content=urlencode(make(ga)), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
                rb = via.post(f"/kind/{kind}/bulk-delete", content=urlencode(make(gb)), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
                _same_answer(ra, rb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
                for i in range(2):
                    assert (_row("entities", ga[i]) is None) == (_row("entities", gb[i]) is None)
                    assert (_newest_note(ga[i]) is None) == (_newest_note(gb[i]) is None)
    assert served >= 15, served   # the GM's


# ── parties ───────────────────────────────────────────────────────────────────

def _party(**kw):
    from app.database import SessionLocal
    from app.models import Party
    db = SessionLocal()
    try:
        p = Party(**kw)
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


def test_party_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import PlayerCharacter, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"parties": {"player": "edit", "assistant": "edit"}})
        mine = PlayerCharacter(world_id=seed.world_a.id, name="Hero", owner_user_id=seed.player_a.id)
        theirs = PlayerCharacter(world_id=seed.world_a.id, name="Rogue", owner_user_id=seed.player_b.id)
        far = PlayerCharacter(world_id=seed.world_b.id, name="Far", owner_user_id=seed.player_b.id)
        db.add_all([mine, theirs, far]); db.commit()
        pc1, pc2, pc3 = mine.id, theirs.id, far.id
    finally:
        db.close()
    [ent] = _entities(seed.world_a.id, 1)
    [far_ent] = _entities(seed.world_b.id, 1)
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in ({"name": "  Crew  "}, {"name": ""}, {}, {"name": "   "}):
            ra = d.post("/parties/new", data=form, follow_redirects=False)
            na = _newest("parties")
            rb = via.post("/parties/new", data=form, follow_redirects=False)
            nb = _newest("parties")
            created = ra.status_code == 303
            _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
            if created:
                _same_rows(na, nb)
            served += rb.headers.get("x-nd-served-by") == "gateway"
        forms = [[("notes", " n "), ("goals", " g "), ("name", " New "), ("member_pc_ids", str(pc1)), ("member_pc_ids", str(pc3)), ("member_pc_ids", str(pc1)),
                  ("member_entity_ids", str(ent)), ("member_entity_ids", str(far_ent)), ("member_entity_ids", "999999")],
                 [("name", "")], [], [("member_pc_ids", "x")], [("member_pc_ids", " 1")], [("notes", "Ünï\r\nx")]]
        for form in forms:
            for members in (json.dumps([pc1]), json.dumps([pc2]), "[]"):
                ids = [_party(world_id=seed.world_a.id, name="P", member_pc_ids_json=members, member_entity_ids_json=json.dumps([ent])) for _ in range(2)]
                before = [_row("parties", i) for i in ids]
                ra = d.post(f"/parties/{ids[0]}/edit", content=urlencode(form), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
                rb = via.post(f"/parties/{ids[1]}/edit", content=urlencode(form), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
                _same_answer(ra, rb, ids[0], ids[1])
                _same_rows(_row("parties", ids[0]), _row("parties", ids[1]), before[0], before[1])
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for body in ({"kind": "map", "slug": " m ", "lat": 1, "lng": 2.5}, {"kind": "schematic", "slug": "s", "x": -0.1, "y": 1e20}, {"kind": "none"}, {},
                     {"kind": "map", "slug": "", "lat": 1, "lng": 2}, {"kind": "map", "slug": "m", "lat": "1", "lng": 2}, {"kind": "map", "slug": "m", "lat": 1},
                     {"kind": 5}, {"kind": "map", "slug": 3, "lat": 1, "lng": 2}):
            ids = [_party(world_id=seed.world_a.id, name="P", location_json='{"kind": "map"}') for _ in range(2)]
            before = [_row("parties", i) for i in ids]
            ra = d.post(f"/api/parties/{ids[0]}/location", json=body)
            rb = via.post(f"/api/parties/{ids[1]}/location", json=body)
            _same_answer(ra, rb)
            _same_rows(_row("parties", ids[0]), _row("parties", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for body in ({"kind": "pc", "id": pc1}, {"kind": "pc", "id": pc2}, {"kind": "pc", "id": pc3}, {"kind": "pc", "name": " Rogue "}, {"kind": "pc", "name": "Nobody"},
                     {"kind": "entity", "id": ent}, {"kind": "entity", "name": "E"}, {"kind": "x", "id": 1}, {"kind": "pc", "id": "1"}, {"kind": "pc"}, {"kind": "pc", "id": 1.0}):
            ids = [_party(world_id=seed.world_a.id, name="P", member_pc_ids_json=json.dumps([pc2])) for _ in range(2)]
            before = [_row("parties", i) for i in ids]
            ra = d.post(f"/api/parties/{ids[0]}/members/toggle", json=body)
            rb = via.post(f"/api/parties/{ids[1]}/members/toggle", json=body)
            _same_answer(ra, rb)
            _same_rows(_row("parties", ids[0]), _row("parties", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        ids = [_party(world_id=seed.world_a.id, name="P", member_pc_ids_json=json.dumps([pc1])) for _ in range(2)]
        qa, qb = _quest(world_id=seed.world_a.id, title="Q", assigned_party_id=ids[0]), _quest(world_id=seed.world_a.id, title="Q", assigned_party_id=ids[1])
        before = [_row("quests", qa), _row("quests", qb)]
        ra = d.post(f"/parties/{ids[0]}/delete", follow_redirects=False)
        rb = via.post(f"/parties/{ids[1]}/delete", follow_redirects=False)
        _same_answer(ra, rb)
        _same_rows(_row("parties", ids[0]), _row("parties", ids[1]))
        _same_rows(_row("quests", qa), _row("quests", qb), before[0], before[1], ignore=("assigned_party_id",))
        assert (_row("quests", qa)["assigned_party_id"] is None) == (_row("quests", qb)["assigned_party_id"] is None)
        served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 25, served


def _no_new_lids(loot_a, loot_b):
    """The two stashes alike, except the random lid each server gives a newly added item"""
    import re
    new = re.compile(r'"lid": "(?!old)[0-9a-f]{8}"')
    assert new.sub('"lid": "<new>"', loot_a or "") == new.sub('"lid": "<new>"', loot_b or ""), (loot_a, loot_b)
    a, b = json.loads(loot_a or "[]"), json.loads(loot_b or "[]")
    assert len(a) == len(b), (a, b)
    for x, y in zip(a, b):
        if x.get("lid", "").startswith("old") or y.get("lid", "").startswith("old"):
            assert x == y, (x, y)
        else:
            assert {**x, "lid": 0} == {**y, "lid": 0}, (x, y)
            assert list(x) == list(y)


def test_party_loot_is_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import PlayerCharacter, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"parties": {"player": "edit", "assistant": "edit"}})
        mine = PlayerCharacter(world_id=seed.world_a.id, name="Hero", owner_user_id=seed.player_a.id)
        theirs = PlayerCharacter(world_id=seed.world_a.id, name="Rogue", owner_user_id=seed.player_b.id)
        db.add_all([mine, theirs]); db.commit()
        pc1, pc2 = mine.id, theirs.id
    finally:
        db.close()
    stash = json.dumps([{"lid": "old1", "name": "Rope", "qty": 2, "notes": "n", "claimed_by": [pc2], "x": 1},
                        {"name": "Gem", "lid": "old2", "qty": 0, "claimed_by": []}])
    odd = json.dumps([{"name": "No lid"}])
    served = 0
    bodies = [{"action": "add", "name": "  Sword  ", "qty": 3, "notes": " sharp "}, {"action": "add", "name": "Ünï"}, {"action": "add", "name": " "},
              {"action": "add", "name": "x", "qty": 0}, {"action": "add", "name": "x", "qty": "2"}, {"action": "add", "name": "x", "notes": 5},
              {"action": "remove", "lid": "old2"}, {"action": "remove", "index": 0}, {"action": "remove", "index": 5}, {"action": "remove", "lid": "gone"},
              {"action": "claim", "lid": "old1", "pc_id": pc1}, {"action": "claim", "lid": "old1", "pc_id": pc2}, {"action": "unclaim", "lid": "old1", "pc_id": pc2},
              {"action": "claim", "index": 1, "pc_id": 999}, {"action": "give", "lid": "old1", "pc_id": pc1}, {"action": "nope"}, {}]
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for loot in (stash, odd):
            for body in bodies:
                ids = [_party(world_id=seed.world_a.id, name="P", member_pc_ids_json=json.dumps([pc1, pc2]), loot_json=loot) for _ in range(2)]
                before = [_row("parties", i) for i in ids]
                ra = d.post(f"/api/parties/{ids[0]}/loot", json=body)
                rb = via.post(f"/api/parties/{ids[1]}/loot", json=body)
                assert ra.status_code == rb.status_code, (body, ra.text, rb.text)
                if ra.status_code == 200:
                    _no_new_lids(json.dumps(ra.json()["loot"]), json.dumps(rb.json()["loot"]))
                else:
                    _same_answer(ra, rb)
                _same_rows(_row("parties", ids[0]), _row("parties", ids[1]), before[0], before[1], ignore=("loot_json",))
                _no_new_lids(_row("parties", ids[0])["loot_json"], _row("parties", ids[1])["loot_json"])
                served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 12, served
