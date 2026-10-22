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
        es = [Entity(world_id=world_id, kind="npc", name=f"E{i}") for i in range(n)]
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
