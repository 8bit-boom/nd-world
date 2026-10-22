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


# ── characters ────────────────────────────────────────────────────────────────

def _pc(**kw):
    from app.database import SessionLocal
    from app.models import PlayerCharacter
    db = SessionLocal()
    try:
        p = PlayerCharacter(**kw)
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


def _logs(pc_id):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        return [dict(r) for r in db.execute(text("SELECT actor, kind, text, undo_json, undone FROM character_log WHERE character_id = :c ORDER BY id"),
                                             {"c": pc_id}).mappings()]
    finally:
        db.close()


_STATS = json.dumps([{"id": k, "value": v} for k, v in (("str", 3), ("dex", 2.9), ("bod", ""), ("per", None), ("wil", 4), ("int", 1), ("cha", 0), ("itu", 2))]
                    + [{"id": "", "value": 9}, {"value": 5}, "x", {"id": "str", "value": 5}])


def _pc_variants(seed, tpl):
    w, o = seed.world_a.id, seed.player_a.id
    return [dict(world_id=w, owner_user_id=o, name="A", stats_json=_STATS, current_hp=5, shock_current=1, pp_current=2, mp_current=3),
            dict(world_id=w, owner_user_id=o, name="B", stats_json="[]", max_hp=30, shock_max=6, current_hp=None, temp_hp=4),
            dict(world_id=w, owner_user_id=o, name="C", stats_json=_STATS, sheet_template_id=tpl, current_hp=2),
            dict(world_id=w, owner_user_id=seed.player_b.id, name="D", stats_json="{bad", current_hp=1),
            dict(world_id=w, owner_user_id=o, name="E", stats_json=json.dumps([{"id": "str", "value": "4"}]), current_hp=1)]


def test_character_vitals_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import SheetTemplate
    base = str(client.base_url)
    db = SessionLocal()
    try:
        t = SheetTemplate(name="Custom", slug="custom-gw-test", sheet_mode="custom", world_id=seed.world_a.id)
        db.add(t); db.commit()
        tpl = t.id
    finally:
        db.close()
    served = 0
    calls = [("hp-async", {"action": "delta", "value": -3}), ("hp-async", {"action": "delta", "value": 99}), ("hp-async", {"value": 7}), ("hp-async", {}),
             ("hp-async", {"action": "temp", "value": 5}), ("hp-async", {"action": "max", "value": 3}), ("hp-async", {"action": "max", "value": -1}),
             ("hp-async", {"value": "3"}), ("hp-async", {"value": True}), ("hp-async", {"value": 2.5}), ("hp-async", {"value": None}), ("hp-async", {"action": 5, "value": 1}),
             ("shock", {"action": "delta", "value": 2}), ("shock", {"value": 100}), ("shock", {"action": "rest"}), ("pp", {"action": "rest"}), ("pp", {"action": "delta", "value": -9}),
             ("mp", {"action": "rest"}), ("mp", {"value": 4}), ("mp", {"action": "other", "value": 4})]
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for variant in _pc_variants(seed, tpl):
            for path, body in calls:
                ids = [_pc(**variant) for _ in range(2)]
                before = [_row("player_characters", i) for i in ids]
                ra = d.post(f"/api/characters/{ids[0]}/{path}", json=body)
                rb = via.post(f"/api/characters/{ids[1]}/{path}", json=body)
                _same_answer(ra, rb)
                _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1], ignore=("name",))
                assert _logs(ids[0]) == _logs(ids[1]), (path, body, _logs(ids[0]), _logs(ids[1]))
                served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 60, served


def test_character_xp_conditions_and_currency_are_pythons(client, seed):
    base = str(client.base_url)
    w, o = seed.world_a.id, seed.player_a.id
    coins = json.dumps([{"abbr": "GP", "label": "Gold", "value": 12}, {"label": "Scrip", "value": 2.5}, "junk", {"abbr": "sp", "value": None}])
    variants = [dict(world_id=w, owner_user_id=o, name="A", level=3, xp=1000, conditions_json=json.dumps(["Burn", "stunned", " Prone  x ", 5, "burn"]), currency_json=coins),
                dict(world_id=w, owner_user_id=o, name="B", level=20, xp=0, conditions_json="[]", currency_json="[]"),
                dict(world_id=w, owner_user_id=o, name="C", level=1, xp=None, conditions_json=json.dumps([f"c{i}" for i in range(12)]),
                     currency_json=json.dumps([{"abbr": "Ä", "value": 1}])),
                dict(world_id=w, owner_user_id=seed.player_b.id, name="D", level=25, conditions_json="{bad", currency_json=json.dumps([{"abbr": "GP", "value": "7"}]))]
    calls = [("xp", {"delta": 250}), ("xp", {"delta": -5000}), ("xp", {}), ("xp", {"delta": "5"}), ("xp", {"delta": 1.5}),
             ("conditions", {"action": "add", "name": "  Blinded\x07  now "}), ("conditions", {"action": "add", "name": "BURN"}), ("conditions", {"name": "burn"}),
             ("conditions", {"action": "remove", "name": "Stunned"}), ("conditions", {"action": "set", "conditions": ["a", "A", "", 3, "b" * 60]}),
             ("conditions", {"action": "set", "conditions": "x"}), ("conditions", {"action": "add", "name": ""}), ("conditions", {"action": "add", "name": "Ünï"}),
             ("conditions", {"action": "nope", "name": "x"}), ("conditions", {"action": "add", "name": "new"}),
             ("currency", {"abbr": " gp ", "action": "delta", "value": -20}), ("currency", {"abbr": "gold", "value": 7}), ("currency", {"abbr": "scrip", "action": "delta", "value": 1}),
             ("currency", {"abbr": "SP", "value": 3}), ("currency", {"abbr": "none", "value": 3}), ("currency", {"value": 3}), ("currency", {"abbr": "gp", "value": "3"}),
             ("currency", {"abbr": "gp"})]
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for variant in variants:
            for path, body in calls:
                ids = [_pc(**variant) for _ in range(2)]
                before = [_row("player_characters", i) for i in ids]
                ra = d.post(f"/api/characters/{ids[0]}/{path}", json=body)
                rb = via.post(f"/api/characters/{ids[1]}/{path}", json=body)
                _same_answer(ra, rb)
                _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1])
                assert _logs(ids[0]) == _logs(ids[1]), (path, body, _logs(ids[0]), _logs(ids[1]))
                served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 50, served


def _pc_forms():
    full = [("name", "  Kira  "), ("player_name", " Pat "), ("race", "Elf"), ("char_class", "Rogue"), ("level", "25"), ("xp", "-4"), ("backstory", "b\r\nx"),
            ("notes", ""), ("race_id", " elf "), ("max_hp", "0"), ("current_hp", ""), ("shock_max", "3"), ("pp_current", "-2"), ("minor_edge", " Quick "),
            ("minor_edge_count", "2"), ("sheet_template_id", ""), ("stats_json", json.dumps([{"id": "str", "value": 4}, {"id": "dex", "value": 3}])),
            ("currency_json", ""), ("custom_fields_json", '{"a": 1}'), ("equipment_json", "{bad")]
    return [full, [("name", "")], [], [("level", "x")], [("level", " 3")], [("current_hp", "7"), ("max_hp", "12")], [("sheet_template_id", "abc"), ("name", "T")],
            [("stats_json", "NaN")], [("conditions_json", '["Burn"]'), ("app_extra_json", "")], [("name", "Ünï ✓"), ("level", "")]]


def test_character_create_edit_owner_delete_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import CharacterLog, Party, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.max_characters_per_player = 100
        db.commit()
    finally:
        db.close()
    w = seed.world_a.id
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for form in _pc_forms():
            for how in ("urlencoded", "multipart"):
                ra = _send_file(d, "/characters/new", form, how)
                na = _newest("player_characters")
                if ra.status_code == 303:
                    _drop_pc(na["id"])   # a player's character limit must see the same count for both requests
                rb = _send_file(via, "/characters/new", form, how)
                nb = _newest("player_characters")
                created = ra.status_code == 303 and ra.headers["location"].startswith("/characters/")
                _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
                if created:
                    _same_rows(na, nb)
                    _drop_pc(nb["id"])
                served += rb.headers.get("x-nd-served-by") == "gateway"
                ids = [_pc(world_id=w, owner_user_id=seed.player_a.id, name="Old", level=4, xp=10, race_id=None, max_hp=9, current_hp=3,
                           stats_json=json.dumps([{"id": "str", "value": 1}])) for _ in range(2)]
                before = [_row("player_characters", i) for i in ids]
                ra = _send_file(d, f"/characters/{ids[0]}/edit", form, how)
                rb = _send_file(via, f"/characters/{ids[1]}/edit", form, how)
                _same_answer(ra, rb, ids[0], ids[1])
                _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1])
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for form in ({"owner_user_id": str(seed.player_a.id)}, {"owner_user_id": ""}, {"owner_user_id": str(seed.player_b.id)}, {"owner_user_id": str(seed.gm.id)},
                     {"owner_user_id": "x"}, {"owner_user_id": " 99999 "}, {}):
            for start_name in ("", "Kept"):
                ids = [_pc(world_id=w, owner_user_id=None, name="O", player_name=start_name) for _ in range(2)]
                before = [_row("player_characters", i) for i in ids]
                ra = d.post(f"/characters/{ids[0]}/owner", data=form, follow_redirects=False)
                rb = via.post(f"/characters/{ids[1]}/owner", data=form, follow_redirects=False)
                _same_answer(ra, rb, ids[0], ids[1])
                _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1])
                served += rb.headers.get("x-nd-served-by") == "gateway"
        for owner in (seed.player_a.id, seed.player_b.id):
            ids = [_pc(world_id=w, owner_user_id=owner, name="Del") for _ in range(2)]
            parties = []
            for i in ids:
                other = _pc(world_id=w, owner_user_id=owner, name="Stay")
                loot = json.dumps([{"lid": "a1", "name": "Rope", "qty": 1, "notes": "", "claimed_by": [i, other]}, {"lid": "a2", "name": "x", "claimed_by": []}])
                parties.append(_party(world_id=w, name="P", member_pc_ids_json=json.dumps([other, i]), loot_json=loot))
                db = SessionLocal()
                try:
                    db.add(CharacterLog(world_id=w, character_id=i, actor="a", kind="k", text="t")); db.commit()
                finally:
                    db.close()
            before = [_row("parties", p) for p in parties]
            ra = d.post(f"/characters/{ids[0]}/delete", follow_redirects=False)
            rb = via.post(f"/characters/{ids[1]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]))
            pa, pb = _row("parties", parties[0]), _row("parties", parties[1])
            _same_rows(pa, pb, before[0], before[1], ignore=("member_pc_ids_json", "loot_json"))
            assert (pa["member_pc_ids_json"] == before[0]["member_pc_ids_json"]) == (pb["member_pc_ids_json"] == before[1]["member_pc_ids_json"])
            assert (pa["loot_json"] == before[0]["loot_json"]) == (pb["loot_json"] == before[1]["loot_json"])
            assert len(_logs(ids[0])) == len(_logs(ids[1]))
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 20, served


def _send_file(client, url, form, how):
    """The character form: urlencoded, or multipart as a browser sends it with no portrait chosen"""
    if how == "urlencoded":
        return client.post(url, content=urlencode(form), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    b = "----WebKitFormBoundaryNd7x"
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v.encode() + b"\r\n" for k, v in form]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="portrait"; filename=""\r\nContent-Type: application/octet-stream\r\n\r\n\r\n'.encode())
    return client.post(url, content=b"".join(parts) + f"--{b}--\r\n".encode(), headers={"content-type": f"multipart/form-data; boundary={b}"}, follow_redirects=False)


def _drop_pc(pc_id):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM player_characters WHERE id = :i"), {"i": pc_id})
        db.commit()
    finally:
        db.close()


def test_character_level_feats_equipment_and_carry_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import CharacterPref
    base = str(client.base_url)
    w, o = seed.world_a.id, seed.player_a.id
    gear = json.dumps([{"name": "Rope", "qty": 2, "weight": 1.5, "equipped": False, "notes": ""}, {"name": "Stim", "qty": 0, "weight": 0.1}, "junk",
                       {"name": "Kit", "weight": None}])
    stats = json.dumps([{"id": "str", "value": 3}, {"id": "bod", "value": 2.5}])
    feats = json.dumps([{"name": "Dash", "type": "Minor", "rank": "1", "notes": ""}, {"name": "Grit"}])
    variants = [dict(world_id=w, owner_user_id=o, name="A", level=2, xp=900, stats_json=stats, equipment_json=gear, feats_json=feats),
                dict(world_id=w, owner_user_id=o, name="B", level=20, xp=999999, stats_json="[]", equipment_json="[]", feats_json="[]"),
                dict(world_id=w, owner_user_id=o, name="C", level=1, xp=10, stats_json=stats, equipment_json="[1, 2", feats_json="[]"),
                dict(world_id=w, owner_user_id=o, name="D", level=3, xp=2700, stats_json=json.dumps([{"id": "str", "value": "4"}]), equipment_json=gear)]
    calls = [("level-up", None), ("feats", {"item": {"name": "  Leap  ", "type": "Major", "id": 7}}), ("feats", {"item": {"name": ""}}), ("feats", {"item": {"name": "x", "rank": 3}}),
             ("feats", {"action": "remove", "index": 1}), ("feats", {"action": "remove", "index": 9}), ("feats", {"action": "remove", "index": True}), ("feats", {"action": "x"}),
             ("equipment", {"item": {"name": " Sword ", "qty": 2.7, "weight": 3, "equipped": [], "cost": "5gp", "category": None}}), ("equipment", {"item": {"name": "Gem"}}),
             ("equipment", {"item": {"name": "Gem", "weight": "1"}}), ("equipment", {"action": "remove", "index": 0}), ("equipment", {"action": "qty", "index": 0, "delta": -5}),
             ("equipment", {"action": "qty", "index": 3, "delta": 1}), ("equipment", {"action": "weight", "index": 0, "value": 2.25}),
             ("equipment", {"action": "weight", "index": 0, "value": 1e9}), ("equipment", {"action": "weight", "index": 0, "value": -3}),
             ("equipment", {"action": "weight", "index": 0}), ("equipment", {"action": "weight", "index": 2, "value": 1}), ("equipment", {"action": "x"}),
             ("carry-limit", {"value": 40}), ("carry-limit", {"value": 0}), ("carry-limit", {"value": 10 ** 6}), ("carry-limit", {"value": "5"})]
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for variant in variants:
            for prefs in (None, '{"carry_limit": 12, "x": [1]}'):
                for path, body in calls:
                    ids = [_pc(**variant) for _ in range(2)]
                    if prefs:
                        db = SessionLocal()
                        try:
                            db.add_all([CharacterPref(world_id=w, character_id=i, prefs_json=prefs) for i in ids]); db.commit()
                        finally:
                            db.close()
                    before = [_row("player_characters", i) for i in ids]
                    ra = d.post(f"/api/characters/{ids[0]}/{path}", json=body) if body is not None else d.post(f"/api/characters/{ids[0]}/{path}")
                    rb = via.post(f"/api/characters/{ids[1]}/{path}", json=body) if body is not None else via.post(f"/api/characters/{ids[1]}/{path}")
                    _same_answer(ra, rb)
                    _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1])
                    assert _logs(ids[0]) == _logs(ids[1]), (path, body, _logs(ids[0]), _logs(ids[1]))
                    assert _prefs(ids[0]) == _prefs(ids[1]), (path, body)
                    served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 60, served


def _prefs(pc_id):
    from app.database import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        r = db.execute(text("SELECT prefs_json FROM character_prefs WHERE character_id = :c"), {"c": pc_id}).first()
        return r[0] if r else None
    finally:
        db.close()


def test_character_sync_retire_and_portrait_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import ImageJob, World
    base = str(client.base_url)
    w, o = seed.world_a.id, seed.player_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.max_characters_per_player = 1000
        job = ImageJob(world_id=w, created_by_user_id=o, result_urls_json=json.dumps(["/uploads/a.png", "/uploads/b.png"]))
        other_job = ImageJob(world_id=w, created_by_user_id=seed.player_b.id, result_urls_json=json.dumps(["/uploads/c.png"]))
        db.add_all([job, other_job]); db.commit()
        jid, ojid = job.id, other_job.id
    finally:
        db.close()
    sync_bodies = [{"name": " Kira ", "level": 30, "xp": -1, "max_hp": 12, "stats_json": [{"id": "str", "value": 1.5}], "custom_fields_json": {"b": [1, 2.0, None]},
                    "currency_json": "nope", "sheet_template_id": 0, "minor_edge": None, "notes": "Ünï"},
                   {}, {"name": 5}, {"level": "3"}, {"level": 2.5}, {"sheet_template_id": "4"}, {"sheet_template_id": 4, "current_hp": None},
                   {"equipment_json": [{"w": 1e-7}], "app_extra_json": [1]}]
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for body in sync_bodies:
            ids = [_pc(world_id=w, owner_user_id=o, name="S", level=4, xp=10, race_id="elf", minor_edge_count=2) for _ in range(2)]
            before = [_row("player_characters", i) for i in ids]
            ra = d.put(f"/api/characters/{ids[0]}/sync", json=body)
            rb = via.put(f"/api/characters/{ids[1]}/sync", json=body)
            assert ra.status_code == rb.status_code, (body, ra.text, rb.text)
            if ra.status_code == 200:
                ja, jb = ra.json(), rb.json()
                for k in ("id", "updated_at"):
                    ja.pop(k), jb.pop(k)
                assert ja == jb, (ja, jb)
            _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
            ra = d.post(f"/api/worlds/{w}/characters/sync", json=body)
            na = _newest("player_characters")
            rb = via.post(f"/api/worlds/{w}/characters/sync", json=body)
            nb = _newest("player_characters")
            assert ra.status_code == rb.status_code, (body, ra.text, rb.text)
            if ra.status_code == 200:
                _same_rows(na, nb)
                ja, jb = ra.json(), rb.json()
                for k in ("id", "updated_at"):
                    ja.pop(k), jb.pop(k)
                assert ja == jb
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for body in ({"job_id": jid, "url": " /uploads/b.png "}, {"job_id": jid, "url": "/uploads/x.png"}, {"job_id": ojid, "url": "/uploads/c.png"}, {"job_id": "1", "url": "x"}, {}):
            ids = [_pc(world_id=w, owner_user_id=o, name="P") for _ in range(2)]
            before = [_row("player_characters", i) for i in ids]
            ra = d.post(f"/api/characters/{ids[0]}/portrait-from-url", json=body)
            rb = via.post(f"/api/characters/{ids[1]}/portrait-from-url", json=body)
            _same_answer(ra, rb)
            _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for extra in (dict(char_class="Rogue", race="Elf", level=3, background="Urchin", alignment="CN", personality_traits="Sly", backstory="Born...",
                           player_name="Pat", portrait_url="/uploads/p.png", stats_json=json.dumps([{"id": "str", "value": 2}])),
                      dict(level=0, notes="n")):
            ids = [_pc(world_id=w, owner_user_id=o, name="Ret", **extra) for _ in range(2)]
            ra = d.post(f"/characters/{ids[0]}/retire-to-npc", follow_redirects=False)
            na = _newest("entities")
            rb = via.post(f"/characters/{ids[1]}/retire-to-npc", follow_redirects=False)
            nb = _newest("entities")
            created = ra.status_code == 303 and "/entity/" in ra.headers.get("location", "")
            _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
            if created:
                _same_rows(na, nb)
            _same_rows(_row("player_characters", ids[0]), _row("player_characters", ids[1]))
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 15, served


def test_session_delete_publish_loot_and_xp_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import Fact, SheetTemplate, World
    import os
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"sessions": {"player": "edit", "assistant": "edit"}})
        t = SheetTemplate(name="Cust2", slug="cust2-gw-test", sheet_mode="custom", world_id=w)
        db.add(t); db.commit()
        tpl = t.id
    finally:
        db.close()
    live_root = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads" / "live"
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for media in ("", json.dumps([{"kind": "image", "url": "/uploads/x.png", "own": True}]), json.dumps([{"kind": "image", "url": "/uploads/x.png"}])):
            ids = [_gsession(world_id=w, title="D", recap_media_json=media) for _ in range(2)]
            for i in ids:
                (live_root / str(i) / "rec").mkdir(parents=True, exist_ok=True)
                (live_root / str(i) / "rec" / "0001.webm").write_bytes(b"x")
                db = SessionLocal()
                try:
                    db.add(Fact(world_id=w, content="f", game_session_id=i)); db.commit()
                finally:
                    db.close()
            ra = d.post(f"/sessions/{ids[0]}/delete", follow_redirects=False)
            rb = via.post(f"/sessions/{ids[1]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            _same_rows(_row("game_sessions", ids[0]), _row("game_sessions", ids[1]))
            assert (live_root / str(ids[0])).exists() == (live_root / str(ids[1])).exists()
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for form in ({"player_summary": " Recap ", "publish": "1"}, {"player_summary": "  ", "publish": "1"}, {"player_summary": "x", "publish": ""}, {}):
            ids = [_gsession(world_id=w, title="R", player_summary="old", player_summary_published=True) for _ in range(2)]
            before = [_row("game_sessions", i) for i in ids]
            ra = d.post(f"/sessions/{ids[0]}/recap-publish", data=form, follow_redirects=False)
            rb = via.post(f"/sessions/{ids[1]}/recap-publish", data=form, follow_redirects=False)
            _same_answer(ra, rb, ids[0], ids[1])
            _same_rows(_row("game_sessions", ids[0]), _row("game_sessions", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
        ids = [_gsession(world_id=w, title="L", live_transcript="words", live_transcript_segments_json="[1]") for _ in range(2)]
        before = [_row("game_sessions", i) for i in ids]
        ra, rb = d.post(f"/api/sessions/{ids[0]}/live-transcript/clear"), via.post(f"/api/sessions/{ids[1]}/live-transcript/clear")
        _same_answer(ra, rb)
        _same_rows(_row("game_sessions", ids[0]), _row("game_sessions", ids[1]), before[0], before[1])
        served += rb.headers.get("x-nd-served-by") == "gateway"
        for body in ({"action": "add", "name": "Gem", "qty": 3, "notes": "n"}, {"action": "add"}, {"action": "add", "qty": "2"}, {"action": "add", "qty": 0, "name": ["x"]},
                     {"action": "transfer", "index": 0}, {"action": "remove", "index": 1}, {"action": "transfer", "index": 9}, {"index": "0"}):
            parties = [_party(world_id=w, name="P", loot_json=json.dumps([{"lid": "z", "name": "Old"}])) for _ in range(2)]
            ids = [_gsession(world_id=w, title="T", party_id=p, loot_json=json.dumps([{"name": "Rope", "qty": 1, "notes": ""}, {"name": "Gold", "qty": 5.5}]))
                   for p in parties]
            before = [_row("game_sessions", i) for i in ids]
            ra = d.post(f"/api/sessions/{ids[0]}/loot/transfer", json=body)
            rb = via.post(f"/api/sessions/{ids[1]}/loot/transfer", json=body)
            _same_answer(ra, rb)
            _same_rows(_row("game_sessions", ids[0]), _row("game_sessions", ids[1]), before[0], before[1], ignore=("party_id",))
            _same_rows(_row("parties", parties[0]), _row("parties", parties[1]))
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for body, custom in (({"delta": 100}, False), ({"delta": -50, "pc_ids": "pcs"}, False), ({"delta": 10}, True), ({"delta": "5"}, False), ({}, False),
                             ({"delta": 20, "pcs": True}, False)):
            sets = []
            for _ in range(2):
                a = _pc(world_id=w, name="Ann", xp=40)
                b = _pc(world_id=w, name="Ann", xp=0, sheet_template_id=tpl if custom else None)
                p = _party(world_id=w, name="P", member_pc_ids_json=json.dumps([b, a]), xp_json=json.dumps([{"ts": "x"}] * 100))
                sets.append((a, b, p, _gsession(world_id=w, title="X", party_id=p, xp_awarded=5)))
            reqs = []
            for a, b, p, g in sets:
                bb = dict(body)
                if bb.get("pc_ids") == "pcs":
                    bb["pc_ids"] = [b, a, b]
                if bb.pop("pcs", None):
                    bb["pc_ids"] = [a]
                reqs.append(bb)
            ra = d.post(f"/api/sessions/{sets[0][3]}/xp", json=reqs[0])
            rb = via.post(f"/api/sessions/{sets[1][3]}/xp", json=reqs[1])
            _same_answer(ra, rb)
            for k in range(3):
                _same_rows(_row(("player_characters", "player_characters", "parties")[k], sets[0][k]), _row(("player_characters", "player_characters", "parties")[k], sets[1][k]),
                           ignore=("xp_json", "member_pc_ids_json"))
            la, lb = json.loads(_row("parties", sets[0][2])["xp_json"]), json.loads(_row("parties", sets[1][2])["xp_json"])
            assert [{**e, "ts": 0, "session_id": 0} for e in la] == [{**e, "ts": 0, "session_id": 0} for e in lb]
            assert len(la[-1]["ts"]) == len(lb[-1]["ts"])
            _same_rows(_row("game_sessions", sets[0][3]), _row("game_sessions", sets[1][3]), ignore=("party_id",))
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 15, served


def test_calendar_config_save_is_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World, WorldCalendar
    from sqlalchemy import text
    base = str(client.base_url)
    w = seed.world_a.id
    config = {"era_name": "Ash", "current_day": 400, "months": [{"name": "Frost", "days": 30, "color": "#abc"}, {"name": "Thaw", "days": 20}],
              "moons": [{"name": "Pale", "cycle_days": 28, "offset": 0}], "days_per_week": 5, "weekday_names": ["A", "B"],
              "holidays": [{"name": "Feast", "month": 1, "day": 19}, {"name": "Old", "month": 0, "day": 3}], "extra": {"k": 1.5}}
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"calendar": {"player": "edit", "assistant": "edit"}})
        db.add(WorldCalendar(world_id=w, config_json=json.dumps(config)))
        db.commit()
    finally:
        db.close()

    def cal():
        db = SessionLocal()
        try:
            return db.execute(text("SELECT config_json FROM world_calendars WHERE world_id = :w"), {"w": w}).scalar()
        finally:
            db.close()

    def set_cal(t):
        db = SessionLocal()
        try:
            db.query(WorldCalendar).filter(WorldCalendar.world_id == w).update({"config_json": t})
            db.commit()
        finally:
            db.close()

    months = json.dumps([{"name": " Wolf ", "days": "31", "season": "Winter", "color": "#123456"}, {"name": "", "days": 2.9, "color": "red"}, {"days": 5000}])
    forms = [
        {"era_name": "  Age of Ash  ", "current_day": "50", "days_per_week": "6", "months_json": months, "moons_json": json.dumps([{"name": "", "cycle_days": "0", "color": None}]),
         "holidays_json": json.dumps([{"name": " Yule ", "month": "wolf", "day": 99}, {"name": "x", "month": 7}, {"name": "", "month": 0}, {"name": "B", "month": "1", "day": "2"},
                                      {"name": "A", "month": 1, "day": 1, "notes": " n ", "color": "#fff"}, "junk"]),
         "year_format": " {year} {era} ", "weekday_names": "Mon, Tue;; Wed\nThu"},
        {},
        {"months_json": "{bad", "current_day": "x"},
        {"months_json": json.dumps([{"name": "M", "days": "abc"}]), "moons_json": json.dumps(["x"]), "days_per_week": "99", "weekday_names": ""},
        {"months_json": "[]", "holidays_json": "", "year_format": "no year"},
        {"holidays_json": json.dumps([{"name": "Q", "month": "Ünï"}])},
        {"months_json": json.dumps([{"name": 5, "days": 3}])},
        {"current_day": " +7_0 ", "days_per_week": "-3", "months_json": json.dumps([{"name": "Solo", "days": 10}]), "moons_json": "[]"},
        {"weekday_names": "only"},
    ]
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for start in (json.dumps(config), "{}", json.dumps({**config, "holidays": [{"name": "H", "month": 0, "day": "zz"}]}),
                      json.dumps({**config, "weekday_names": 5})):
            for form in forms:
                set_cal(start)
                ra = d.post("/calendar/config", data=form, follow_redirects=False)
                after_a = cal()
                set_cal(start)
                rb = via.post("/calendar/config", data=form, follow_redirects=False)
                after_b = cal()
                _same_answer(ra, rb)
                assert after_a == after_b, (form, start, after_a, after_b)
                served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 15, served


def test_calendar_icon_delete_is_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import CalendarDayIcon
    import os
    from pathlib import Path
    base = str(client.base_url)
    up = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads"
    (up / "calendar_icons").mkdir(parents=True, exist_ok=True)
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for kind in ("file", "escape", "missing", "other-world"):
            ids, files = [], []
            for side in ("a", "b"):
                name = f"gw-{kind}-{side}-{email[:3]}.png"
                f = up / "calendar_icons" / name
                f.write_bytes(b"png")
                url = {"file": f"/uploads/calendar_icons/{name}", "escape": f"/uploads/../{name}", "missing": "/uploads/calendar_icons/none.png",
                       "other-world": f"/uploads/calendar_icons/{name}"}[kind]
                db = SessionLocal()
                try:
                    icon = CalendarDayIcon(world_id=seed.world_b.id if kind == "other-world" else seed.world_a.id, day=3, image_url=url, label="l")
                    db.add(icon); db.commit(); ids.append(icon.id)
                finally:
                    db.close()
                files.append(f)
            ra = d.post(f"/api/calendar/icons/{ids[0]}/delete")
            rb = via.post(f"/api/calendar/icons/{ids[1]}/delete")
            _same_answer(ra, rb)
            assert (_row("calendar_day_icons", ids[0]) is None) == (_row("calendar_day_icons", ids[1]) is None)
            assert files[0].exists() == files[1].exists(), kind
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 2, served


def test_facts_bulk_is_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import AudioJob, Fact, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"facts": {"player": "edit", "assistant": "edit"}})
        jobs = [AudioJob(world_id=w, purpose="facts_parse"), AudioJob(world_id=w, purpose="facts_parse"), AudioJob(world_id=seed.world_b.id, purpose="facts_parse")]
        db.add_all(jobs); db.commit()
        j1, j2, jb_ = (j.id for j in jobs)
    finally:
        db.close()

    def facts_now():
        from sqlalchemy import text
        db = SessionLocal()
        try:
            return [dict(r) for r in db.execute(text("SELECT game_session_id, content, visible_to_players, tags, author_id FROM facts WHERE world_id = :w ORDER BY id"),
                                                 {"w": w}).mappings()]
        finally:
            db.close()

    def reset(extra=()):
        from sqlalchemy import text
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM facts WHERE world_id = :w"), {"w": w})
            db.add_all([Fact(world_id=w, content="The party met Elyra.")] + [Fact(world_id=w, content=x) for x in extra])
            db.execute(text("UPDATE audio_jobs SET draft_consumed = NULL"))
            db.commit()
        finally:
            db.close()

    bodies = [
        ({"facts": [{"content": " the  party met ELYRA!! "}, {"content": "New\tthing", "tags": ["a", " ", "b "], "visible_to_players": 0},
                    {"content": "new thing."}, "junk", {"content": ""}, {"content": "Tagged", "tags": " x, y "}], "game_session_id": "4", "job_id": "JOB1"}, ()),
        ({"facts": []}, ()), ({"facts": "x"}, ()), ({"facts": [{"content": 5}]}, ()), ({"facts": [{"content": "Ünï"}]}, ()), ({"facts": [{"content": "ok"}]}, ("Ärger",)),
        ({"facts": [{"content": "j"}], "job_id": "JOBB"}, ()), ({"facts": [{"content": "j"}], "job_id": True, "game_session_id": 0}, ()),
        ({"facts": [{"content": "k", "tags": [1]}]}, ()),
    ]
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for body, extra in bodies:
            body = json.loads(json.dumps(body).replace('"JOB1"', str(j1)).replace('"JOBB"', str(jb_)))
            reset(extra)
            ra = d.post("/api/facts/bulk", json=body)
            fa = facts_now()
            ja = [(_row("audio_jobs", j) or {}).get("draft_consumed") for j in (j1, j2, jb_)]
            reset(extra)
            rb = via.post("/api/facts/bulk", json=body)
            fb = facts_now()
            jb2 = [(_row("audio_jobs", j) or {}).get("draft_consumed") for j in (j1, j2, jb_)]
            _same_answer(ra, rb)
            assert fa == fb, (body, fa, fb)
            assert ja == jb2
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 3, served


def test_tables_import_and_quests_apply_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    from sqlalchemy import text
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"tables": {"player": "edit", "assistant": "edit"}})
        db.commit()
    finally:
        db.close()

    def tables_now():
        db = SessionLocal()
        try:
            return [dict(r) for r in db.execute(text("SELECT name, slug, category, description, is_builtin, entries_json, created_by_user_id FROM random_tables "
                                                     "WHERE world_id = :w ORDER BY id"), {"w": w}).mappings()]
        finally:
            db.close()

    def drop_tables():
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM random_tables WHERE world_id = :w"), {"w": w})
            db.execute(text("INSERT INTO random_tables (world_id, name, slug, category, description, is_builtin, entries_json) "
                            "VALUES (:w, 'Loot', 'loot', 'general', '', 0, '[]')"), {"w": w})
            db.commit()
        finally:
            db.close()

    def upload(client, payload, field="file"):
        b = "----WebKitFormBoundaryTbl"
        body = (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="t.json"\r\nContent-Type: application/json\r\n\r\n'.encode() + payload
                + f"\r\n--{b}--\r\n".encode())
        return client.post("/tables/import", content=body, headers={"content-type": f"multipart/form-data; boundary={b}"}, follow_redirects=False)

    payloads = [json.dumps([{"name": " Loot ", "category": "treasure", "entries": [{"roll": 1, "text": "Gem", "w": 0.5}]}, {"description": "d"}, {"name": "Keep"}]).encode(),
                json.dumps([{"name": "Twin"}, {"name": "twin"}]).encode(), b"{bad", json.dumps({"x": 1}).encode(), json.dumps([{"name": 5}]).encode(),
                json.dumps(["x"]).encode(), json.dumps([{"name": "Ünï"}]).encode(), "﻿[]".encode(), json.dumps([]).encode()]
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for payload in payloads:
            drop_tables()
            ra = upload(d, payload)
            ta = tables_now()
            drop_tables()
            rb = upload(via, payload)
            tb = tables_now()
            _same_answer(ra, rb)
            assert ta == tb, (payload, ta, tb)
            served += rb.headers.get("x-nd-served-by") == "gateway"
        # quests apply
        party_a = _party(world_id=w, name="P")
        party_b = _party(world_id=seed.world_b.id, name="Far")
        for body in ({"new_quests": [{"title": "  Find the key ", "status": "secret", "category": "main", "summary": " s ", "body": "b", "assigned_party_id": "PA",
                                      "visible_to_players": 0}, {"title": ""}, "x", {"title": "t", "status": "weird", "assigned_party_id": "PB"},
                                     {"title": "u", "assigned_party_id": True}],
                      "quest_updates": [{"quest_id": "Q", "status": "complete", "note": "  done  "}, {"quest_id": "QB", "status": "failed"}, {"quest_id": "Q", "note": " "}]},
                     {}, {"new_quests": "x"}, {"new_quests": [{"title": 5}]}, {"quest_updates": [{"quest_id": True}]}, {"quest_updates": [{"quest_id": "Q", "note": 5}]}):
            qa = [_quest(world_id=w, title="Q", status="active", summary="old") for _ in range(2)]
            qb = _quest(world_id=seed.world_b.id, title="Far")
            res = []
            for side, qid in ((d, qa[0]), (via, qa[1])):
                bb = json.loads(json.dumps(body).replace('"PA"', str(party_a)).replace('"PB"', str(party_b)).replace('"QB"', str(qb)).replace('"Q"', str(qid)))
                before_max = (_newest("quests") or {}).get("id", 0)
                r = side.post("/api/quests/apply", json=bb)
                db = SessionLocal()
                try:
                    new = [dict(x) for x in db.execute(text("SELECT title, status, category, summary, body, visible_to_players, assigned_party_id, created_by_user_id "
                                                            "FROM quests WHERE id > :m ORDER BY id"), {"m": before_max}).mappings()]
                finally:
                    db.close()
                res.append((r, new, _row("quests", qid)))
            _same_answer(res[0][0], res[1][0])
            assert res[0][1] == res[1][1], (body, res[0][1], res[1][1])
            _same_rows(res[0][2], res[1][2])
            served += res[1][0].headers.get("x-nd-served-by") == "gateway"
    assert served >= 4, served


# ── media libraries ───────────────────────────────────────────────────────────

_LIBS = {"audio": ("audio_albums", "audio_clips", "AudioAlbum", "AudioClip"), "video": ("video_albums", "video_clips", "VideoAlbum", "VideoClip"),
         "pages": ("page_albums", "page_docs", "PageAlbum", "PageDoc")}


def _add(model_name, **kw):
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        o = getattr(models, model_name)(**kw)
        db.add(o)
        db.commit()
        return o.id
    finally:
        db.close()


def test_media_library_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    import os
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads"
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({s: {"player": "edit", "assistant": "edit"} for s in ("audio", "video", "pages")})
        db.commit()
    finally:
        db.close()
    [ent] = _entities(w, 1)
    [far_ent] = _entities(seed.world_b.id, 1)
    served = 0
    n = [0]

    def item(lib, album_id, **kw):
        n[0] += 1
        (up / lib).mkdir(parents=True, exist_ok=True)
        f = up / lib / f"gw-{n[0]}.bin"
        f.write_bytes(b"x")
        extra = {"poster_url": f"/uploads/{lib}/gw-{n[0]}.bin.jpg"} if lib == "video" else {}
        if lib == "video":
            (up / lib / f"gw-{n[0]}.bin.jpg").write_bytes(b"p")
        return _add(_LIBS[lib][3], world_id=w, name="I", file_url=f"/uploads/{lib}/gw-{n[0]}.bin", album_id=album_id, **extra, **kw), f

    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for lib, (atab, itab, amodel, imodel) in _LIBS.items():
            parent = _add(amodel, world_id=w, name="Parent")
            far = _add(amodel, world_id=seed.world_b.id, name="Far")
            for form in ({"name": "  Songs  "}, {}, {"name": "x" * 200, "parent_id": str(parent)}, {"parent_id": str(far)}, {"parent_id": "abc"}, {"parent_id": " 99999"}):
                ra = d.post(f"/{lib}/albums/new", data=form, follow_redirects=False)
                na = _newest(atab)
                rb = via.post(f"/{lib}/albums/new", data=form, follow_redirects=False)
                nb = _newest(atab)
                created = ra.status_code == 303 and "/albums/" in ra.headers.get("location", "")
                _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
                if created:
                    _same_rows(na, nb)
                served += rb.headers.get("x-nd-served-by") == "gateway"
            for form in ({"name": " New "}, {"name": "  "}):
                ids = [_add(amodel, world_id=w, name="Old") for _ in range(2)]
                ra = d.post(f"/{lib}/albums/{ids[0]}/rename", data=form, follow_redirects=False)
                rb = via.post(f"/{lib}/albums/{ids[1]}/rename", data=form, follow_redirects=False)
                _same_answer(ra, rb, ids[0], ids[1])
                _same_rows(_row(atab, ids[0]), _row(atab, ids[1]))
                served += rb.headers.get("x-nd-served-by") == "gateway"
            # album delete: a nested tree with items and their files
            trees = []
            for _ in range(2):
                top = _add(amodel, world_id=w, name="Top", parent_id=parent)
                sub = _add(amodel, world_id=w, name="Sub", parent_id=top)
                items = [item(lib, top), item(lib, sub)]
                trees.append((top, sub, items))
            ra = d.post(f"/{lib}/albums/{trees[0][0]}/delete", follow_redirects=False)
            rb = via.post(f"/{lib}/albums/{trees[1][0]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            for k in range(2):
                assert (_row(atab, trees[0][1]) is None) == (_row(atab, trees[1][1]) is None)
                assert (_row(itab, trees[0][2][k][0]) is None) == (_row(itab, trees[1][2][k][0]) is None)
                assert trees[0][2][k][1].exists() == trees[1][2][k][1].exists()
            served += rb.headers.get("x-nd-served-by") == "gateway"
            # item edit / delete
            forms = [{"name": " Renamed ", "description": " d ", "visible_to_players": "on", "album_id": str(parent), "entity_id": str(ent),
                      "is_character_sheet_template": "1"},
                     {}, {"album_id": str(far)}, {"entity_id": str(far_ent), "album_id": "x"}, {"name": "   ", "visible_to_players": ""}, {"entity_id": "٣"}]
            for form in forms:
                ids = [item(lib, None, visible_to_players=True)[0] for _ in range(2)]
                before = [_row(itab, i) for i in ids]
                ra = d.post(f"/{lib}/{ids[0]}/edit", data=form, follow_redirects=False)
                rb = via.post(f"/{lib}/{ids[1]}/edit", data=form, follow_redirects=False)
                _same_answer(ra, rb)
                _same_rows(_row(itab, ids[0]), _row(itab, ids[1]), before[0], before[1], ignore=("file_url", "poster_url"))
                served += rb.headers.get("x-nd-served-by") == "gateway"
            pairs = [item(lib, parent) for _ in range(2)]
            ra = d.post(f"/{lib}/{pairs[0][0]}/delete", follow_redirects=False)
            rb = via.post(f"/{lib}/{pairs[1][0]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            assert (_row(itab, pairs[0][0]) is None) == (_row(itab, pairs[1][0]) is None)
            assert pairs[0][1].exists() == pairs[1][1].exists()
            served += rb.headers.get("x-nd-served-by") == "gateway"
        # audio: hiding or deleting the clip players are hearing ends the broadcast (Python's, both ways)
        if email == seed.gm.email:
            for action in ("edit", "delete"):
                for side in (d, via):
                    cid, _f = item("audio", None)
                    db = SessionLocal()
                    try:
                        wa = db.query(World).filter(World.id == w).first()
                        wa.now_playing_url = _row("audio_clips", cid)["file_url"]
                        db.commit()
                    finally:
                        db.close()
                    r = side.post(f"/audio/{cid}/{action}", data={"visible_to_players": ""}, follow_redirects=False)
                    assert r.status_code == 303
                    assert _row("worlds", w)["now_playing_url"] is None, (action, side)
        # a page still used as a character-sheet template cannot be deleted (alone or in its album)
        if email == seed.gm.email:
            for target in ("doc", "album"):
                res = []
                for side in (d, via):
                    alb = _add("PageAlbum", world_id=w, name="Sheets")
                    doc, _f = item("pages", alb)
                    _add("CharacterSheet", world_id=w, template_id=doc, owner_user_id=seed.player_a.id, name="S")
                    url = f"/pages/{doc}/delete" if target == "doc" else f"/pages/albums/{alb}/delete"
                    res.append((side.post(url, follow_redirects=False), doc))
                _same_answer(res[0][0], res[1][0])
                assert res[0][0].status_code == 400
                assert _row("page_docs", res[0][1]) is not None and _row("page_docs", res[1][1]) is not None
    assert served >= 30, served


def test_image_album_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"images": {"player": "edit", "assistant": "edit"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        parent = _add("ImageAlbum", world_id=w, name="P", image_urls_json="[]")
        far = _add("ImageAlbum", world_id=seed.world_b.id, name="F", image_urls_json="[]")
        for form in ({"name": " Maps "}, {}, {"parent_id": str(parent)}, {"parent_id": str(far)}, {"parent_id": "٣"}):
            ra = d.post("/images/albums/new", data=form, follow_redirects=False)
            na = _newest("image_albums")
            rb = via.post("/images/albums/new", data=form, follow_redirects=False)
            nb = _newest("image_albums")
            created = ra.status_code == 303 and "/albums/" in ra.headers.get("location", "")
            _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
            if created:
                _same_rows(na, nb)
            served += rb.headers.get("x-nd-served-by") == "gateway"
        for path, form in (("rename", {"name": " R "}), ("rename", {"name": ""}), ("move", {"parent_id": str(parent)}), ("move", {"parent_id": ""}),
                           ("move", {"parent_id": "SELF"}), ("move", {"parent_id": "CHILD"}), ("move", {"parent_id": "x"}), ("move", {"parent_id": str(far)}), ("delete", {})):
            pairs = []
            for _ in range(2):
                a = _add("ImageAlbum", world_id=w, name="A", image_urls_json="[]", parent_id=parent)
                ch = _add("ImageAlbum", world_id=w, name="C", image_urls_json="[]", parent_id=a)
                pairs.append((a, ch))
            res = []
            for side, (a, ch) in ((d, pairs[0]), (via, pairs[1])):
                ff = {k: v.replace("SELF", str(a)).replace("CHILD", str(ch)) for k, v in form.items()}
                res.append(side.post(f"/images/albums/{a}/{path}", data=ff, follow_redirects=False))
            _same_answer(res[0], res[1], pairs[0][0], pairs[1][0])
            for k in range(2):
                _same_rows(_row("image_albums", pairs[0][k]), _row("image_albums", pairs[1][k]), ignore=("parent_id",))
            served += res[1].headers.get("x-nd-served-by") == "gateway"
        start = json.dumps(["/uploads/a.png", "/uploads/b.png", None])
        for path, body in (("add", {"urls": ["/uploads/c.png", "/uploads/a.png", "", 5, "/uploads/c.png"]}), ("add", {"urls": "x"}), ("add", {}),
                           ("remove", {"url": "/uploads/a.png"}), ("remove", {"urls": ["/uploads/b.png", "/uploads/zz.png"]}), ("remove", {}),
                           ("remove", {"urls": [{"x": 1}]}), ("remove", {"urls": [5]})):
            ids = [_add("ImageAlbum", world_id=w, name="U", image_urls_json=start) for _ in range(2)]
            before = [_row("image_albums", i) for i in ids]
            ra = d.post(f"/images/albums/{ids[0]}/{path}", json=body)
            rb = via.post(f"/images/albums/{ids[1]}/{path}", json=body)
            _same_answer(ra, rb)
            _same_rows(_row("image_albums", ids[0]), _row("image_albums", ids[1]), before[0], before[1])
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 10, served   # the GM's


def test_session_media_writes_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"sessions": {"player": "edit", "assistant": "edit"}})
        db.commit()
    finally:
        db.close()
    alb = _add("ImageAlbum", world_id=w, name="Pics", image_urls_json=json.dumps(["/uploads/gallery/pic-a.png"] + [f"/uploads/gallery/pic-{i}.png" for i in range(200)]))

    def setup():
        a1 = _add("AudioClip", world_id=w, name="A1", file_url="/uploads/audio/a1.mp3", visible_to_players=False)
        a2 = _add("AudioClip", world_id=w, name="A2", file_url="/uploads/audio/a2.mp3")
        v1 = _add("VideoClip", world_id=w, name="V1", file_url="/uploads/video/v1.mp4")
        v2 = _add("VideoClip", world_id=w, name="V2", file_url="/uploads/video/v2.mp4", poster_url="/uploads/video/v2.jpg")
        media = [{"uid": "img1", "kind": "image", "url": "/uploads/gallery/pic-a.png", "caption": "A", "visible": 1, "own": False},
                 {"uid": "own1", "kind": "image", "url": "/uploads/session_media/x.png", "own": True},
                 {"uid": "vid1", "kind": "video", "clip_id": v1}, {"kind": "image", "url": "/nope"}, "junk"]
        g = _gsession(world_id=w, title="M", recap_audio_json=json.dumps([a1, a1, True, "x"]), recap_media_json=json.dumps(media))
        return g, {"A1": a1, "A2": a2, "V1": v1, "V2": v2}

    def norm(resp, ids):
        if not resp.headers.get("content-type", "").startswith("application/json"):
            return resp.status_code
        d = resp.json()
        if isinstance(d, dict) and "library" in d:
            amap = {ids["A1"]: "A1", ids["A2"]: "A2"}
            vmap = {ids["V1"]: "V1", ids["V2"]: "V2"}
            for x in d["clips"]:
                x["id"] = amap.get(x["id"], x["id"])
            for x in d["videos"]:
                x["id"] = vmap.get(x["id"], x["id"])
                if x["uid"] not in ("vid1",):
                    x["uid"] = "<new>"
            for x in d["images"]:
                if x["uid"] not in ("img1", "own1"):
                    x["uid"] = "<new>"
                if x["url"] == ids.get("PIC"):
                    x["url"], x["caption"] = "PIC", x["caption"].replace(ids["PIC"].rsplit("/", 1)[1], "PICNAME")
            d["library"] = sorted(amap[x["id"]] for x in d["library"] if x["id"] in amap)
            d["video_library"] = sorted(vmap[x["id"]] for x in d["video_library"] if x["id"] in vmap)
        return resp.status_code, d

    calls = [("recap-audio", {"clip_id": "A2", "visible_to_players": True}), ("recap-audio", {"clip_id": "A1"}), ("recap-audio", {"clip_id": True}), ("recap-audio", "bad"),
             ("recap-audio/A1/visibility", {"visible_to_players": True}), ("recap-audio/A2/visibility", {"visible_to_players": True}),
             ("recap-audio/A1/visibility", {"visible_to_players": 1}), ("recap-audio/A1/remove", None), ("recap-audio/A2/remove", None),
             ("media/attach", {"kind": "video", "id": "V2", "visible_to_players": False}), ("media/attach", {"kind": "video", "url": " /uploads/video/v1.mp4 "}),
             ("media/attach", {"kind": "audio", "url": "/uploads/audio/a2.mp3"}), ("media/attach", {"kind": "image", "url": "PIC", "visible_to_players": True}),
             ("media/attach", {"kind": "image", "url": "/uploads/gallery/pic-a.png"}), ("media/attach", {"kind": "image", "url": "/uploads/x"}), ("media/attach", {"kind": "z"}),
             ("media/audio/A1/visibility", {"visible_to_players": True}), ("media/video/V1/visibility", {"visible_to_players": False}),
             ("media/video/V2/visibility", {"visible_to_players": False}), ("media/image/img1/visibility", {"visible_to_players": False}),
             ("media/image/nope/visibility", {"visible_to_players": False}), ("media/audio/A1/rename", {"name": "  New\\tname "}), ("media/video/V1/rename", {"name": " "}),
             ("media/image/img1/rename", {"name": " Cap "}), ("media/image/img1/rename", {"name": 5}), ("media/audio/A1/remove", None), ("media/video/V1/remove", None),
             ("media/image/img1/remove", None), ("media/image/own1/remove", None), ("media/audio/A2/remove", None)]
    served = 0
    pic_n = [0]
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path, body in calls:
            out = []
            for side in (d, via):
                g, ids = setup()
                pic_n[0] += 1
                ids_pic = f"/uploads/gallery/pic-{pic_n[0]}.png"
                ids["PIC"] = ids_pic
                p = path
                for k, v in ids.items():
                    p = p.replace(f"/{k}/", f"/{v}/")
                if isinstance(body, dict):
                    bb = json.loads(json.dumps(body).replace('"A1"', str(ids["A1"])).replace('"A2"', str(ids["A2"])).replace('"V1"', str(ids["V1"])).replace('"V2"', str(ids["V2"])).replace('"PIC"', json.dumps(ids_pic)))
                    r = side.post(f"/api/sessions/{g}/{p}", json=bb)
                elif body == "bad":
                    r = side.post(f"/api/sessions/{g}/{p}", content=b"{bad", headers={"content-type": "application/json"})
                else:
                    r = side.post(f"/api/sessions/{g}/{p}")
                nr = norm(r, ids)
                row = _row("game_sessions", g)
                media = json.loads(row["recap_media_json"] or "[]")
                vmap = {ids["V1"]: "V1", ids["V2"]: "V2"}
                amap = {ids["A1"]: "A1", ids["A2"]: "A2"}
                ids = {k: v for k, v in ids.items() if k != "PIC"}
                for m in media:
                    if isinstance(m, dict):
                        if m.get("uid") not in ("img1", "own1", "vid1"):
                            m["uid"] = "<new>"
                        if "clip_id" in m:
                            m["clip_id"] = vmap.get(m["clip_id"], m["clip_id"])
                        if m.get("url") == ids_pic:
                            m["url"], m["caption"] = "PIC", m.get("caption", "").replace(ids_pic.rsplit("/", 1)[1], "PICNAME")
                audio = [amap.get(x, x) for x in json.loads(row["recap_audio_json"] or "[]")]
                text = (media, audio)
                clips = [(_row("audio_clips", ids[k]) or {}).get("visible_to_players") for k in ("A1", "A2")] + \
                        [(_row("video_clips", ids[k]) or {}).get("visible_to_players") for k in ("V1", "V2")] + [(_row("audio_clips", ids["A1"]) or {}).get("name")]
                out.append((nr, text, clips, r.headers.get("x-nd-served-by")))
            assert out[0][:3] == out[1][:3], (path, body, out[0], out[1])
            served += out[1][3] == "gateway"
    assert served >= 15, served


# ── uploads ───────────────────────────────────────────────────────────────────

def _multipart(fields, files):
    """A browser-shaped multipart body: fields [(name, value)], files [(name, filename, bytes)]"""
    b = "----WebKitFormBoundaryUp1"
    out = b""
    for k, v in fields:
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v.encode() + b"\r\n"
    for k, fn, data in files:
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), {"content-type": f"multipart/form-data; boundary={b}"}


def _masked_url(url):
    """/uploads/audio/<12 hex>-slug.mp3 -> /uploads/audio/<hex>-slug.mp3 (the random prefix differs per upload)"""
    import re
    return re.sub(r"/([0-9a-f]{12}-|[0-9a-f]{32})", lambda m: "/<hex>-" if m.group(1).endswith("-") else "/<hex32>", url or "")


def test_library_uploads_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import World
    import os
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads"
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({s: {"player": "edit", "assistant": "edit"} for s in ("audio", "video", "pages")})
        db.commit()
    finally:
        db.close()
    big = os.urandom(300 * 1024) + b"\r\n--not-a-boundary\r\n" + os.urandom(1000)
    served = 0
    for email, pw, world in _people(seed):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for lib, ext, table in (("audio", ".mp3", "audio_clips"), ("pages", ".html", "page_docs"), ("video", ".mp4", "video_clips")):
            album = _add(_LIBS[lib][2], world_id=w, name="Al")
            for case in ("ok", "bad-id", "bad-index", "neg", "two-files", "nofile"):
                res = []
                for side in (d, via):
                    uid = os.urandom(16).hex()
                    fields = [("upload_id", uid if case != "bad-id" else "XYZ"), ("chunk_index", {"bad-index": "x", "neg": "-1"}.get(case, "0"))]
                    files = [] if case == "nofile" else [("file", "blob", big)] * (2 if case == "two-files" else 1)
                    body, hdr = _multipart(fields, files)
                    r = side.post(f"/{lib}/upload/chunk", content=body, headers=hdr)
                    part = up / lib / "_chunks" / uid / "000000.part"
                    res.append((r, part.exists() and part.read_bytes() == big))
                _same_answer(res[0][0], res[1][0])
                assert res[0][1] == res[1][1], (lib, case)
                served += res[1][0].headers.get("x-nd-served-by") == "gateway"
            if lib == "video":
                continue
            # complete: two parts, then the clip / page
            for case in ("ok", "missing", "badext", "album"):
                res = []
                for side in (d, via):
                    uid = os.urandom(16).hex()
                    for i, data in enumerate((b"first-", b"second")):
                        if case == "missing" and i == 1:
                            continue
                        body, hdr = _multipart([("upload_id", uid), ("chunk_index", str(i))], [("file", "blob", data)])
                        side.post(f"/{lib}/upload/chunk", content=body, headers=hdr)
                    form = {"upload_id": uid, "filename": f"My Song (Live){ext if case != 'badext' else '.exe'}", "total_chunks": "2", "name": "",
                            "description": " d ", "visible_to_players": "on"}
                    if case == "album":
                        form["album_id"] = str(album)
                    before = (_newest(table) or {}).get("id")
                    r = side.post(f"/{lib}/upload/complete", data=form)
                    new = _newest(table)
                    row = new if new and new.get("id") != before else None
                    content = (up / row["file_url"][len("/uploads/"):]).read_bytes() if row else None
                    res.append((r, row, content, (up / lib / "_chunks" / uid).exists()))
                _same_answer(res[0][0], res[1][0])
                if res[0][1]:
                    assert _masked_url(res[0][1]["file_url"]) == _masked_url(res[1][1]["file_url"]), (res[0][1]["file_url"], res[1][1]["file_url"])
                    _same_rows(res[0][1], res[1][1], ignore=("file_url",))
                else:
                    assert res[1][1] is None
                assert res[0][2] == res[1][2] and res[0][3] == res[1][3], (lib, case)
                served += res[1][0].headers.get("x-nd-served-by") == "gateway"
            # direct upload
            for fn, extra in ((f"Ünï cödé{ext}", [("entity_id", "1")]), (f"plain{ext}", [("album_id", str(album)), ("name", " Named ")]), ("x.exe", []), (f".{ext[1:]}", []),
                              (f"{ext}", [])):
                res = []
                for side in (d, via):
                    body, hdr = _multipart([("description", "x")] + extra, [("file", fn, big)])
                    before = (_newest(table) or {}).get("id")
                    r = side.post(f"/{lib}/upload", content=body, headers=hdr, follow_redirects=False)
                    new = _newest(table)
                    row = new if new and new.get("id") != before else None
                    content = (up / row["file_url"][len("/uploads/"):]).read_bytes() if row else None
                    res.append((r, row, content))
                _same_answer(res[0][0], res[1][0])
                if res[0][1]:
                    assert _masked_url(res[0][1]["file_url"]) == _masked_url(res[1][1]["file_url"]), (res[0][1]["file_url"], res[1][1]["file_url"])
                    _same_rows(res[0][1], res[1][1], ignore=("file_url",))
                    assert res[0][2] == res[1][2] == big
                else:
                    assert res[1][1] is None
                served += res[1][0].headers.get("x-nd-served-by") == "gateway"
    assert served >= 10, served   # the GM's


def _pictures():
    """Test pictures as bytes, by name"""
    import io
    from PIL import Image
    out = {}
    im = Image.new("RGB", (1000, 600))
    for x in range(0, 1000, 50):
        im.paste((x % 255, 80, 200 - x % 200), (x, 0, x + 50, 600))
    b = io.BytesIO(); im.save(b, "JPEG", quality=85); out["Photo One.jpg"] = b.getvalue()
    im = Image.new("RGBA", (300, 200), (10, 200, 30, 120))
    b = io.BytesIO(); im.save(b, "PNG"); out["small alpha.png"] = b.getvalue()
    im = Image.new("L", (900, 2000), 128)
    b = io.BytesIO(); im.save(b, "PNG"); out["tall-grey.png"] = b.getvalue()
    frames = [Image.new("RGB", (64, 64), c) for c in ((255, 0, 0), (0, 255, 0))]
    b = io.BytesIO(); frames[0].save(b, "WEBP", save_all=True, append_images=frames[1:], duration=100); out["anim.webp"] = b.getvalue()
    b = io.BytesIO(); Image.new("P", (40, 40)).save(b, "GIF"); out["still.gif"] = b.getvalue()
    out["broken.png"] = b"\x89PNG\r\n\x1a\n" + b"garbage" * 50
    return out


def _picture_facts(up, url):
    """(masked url, format, size, mode) of the stored picture and of its thumbnail; whether the original upload is gone"""
    from PIL import Image
    if not url:
        return None
    p = up / url[len("/uploads/"):]
    facts = [_masked_url(url)]
    try:
        with Image.open(p) as im:
            facts += [im.format, im.size, im.mode]
    except Exception:
        facts += ["unreadable", p.exists()]
    for suf in ("_thumb.avif", "_thumb.webp"):
        t = p.with_name(p.stem + suf)
        if t.exists():
            with Image.open(t) as im:
                facts += [suf, im.format, im.size, im.mode]
    return facts


def test_gallery_uploads_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import AppSettings, World
    import os
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads"
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"images": {"player": "edit", "assistant": "edit"}})
        db.commit()
    finally:
        db.close()

    def set_format(fmt):
        db = SessionLocal()
        try:
            s = db.query(AppSettings).first()
            if s is None:
                s = AppSettings(id=1); db.add(s)
            s.static_format = fmt
            db.commit()
        finally:
            db.close()

    pics = _pictures()
    served = 0
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    for fmt in ("avif", "webp", "none", "jpg", "png"):
        set_format(fmt)
        for name, data in pics.items():
            if fmt != "avif" and name not in ("Photo One.jpg", "small alpha.png"):
                continue
            res = []
            for side in (d, via):
                alb = _add("ImageAlbum", world_id=w, name="Up", image_urls_json="[]")
                body, hdr = _multipart([], [("file", name, data)])
                r = side.post(f"/images/albums/{alb}/upload", content=body, headers=hdr, follow_redirects=False)
                urls = json.loads(_row("image_albums", alb)["image_urls_json"])
                res.append((r, [_picture_facts(up, u) for u in urls], r.headers.get("x-nd-served-by"), alb))
            _same_answer(res[0][0], res[1][0], res[0][3], res[1][3])
            assert res[0][1] == res[1][1], (fmt, name, res[0][1], res[1][1])
            served += res[1][2] == "gateway"
    # chunked: two parts, then complete
    set_format("avif")
    for name in ("Photo One.jpg", "small alpha.png", "x.txt"):
        data = pics.get(name, b"text")
        res = []
        for side in (d, via):
            alb = _add("ImageAlbum", world_id=w, name="Up", image_urls_json="[]")
            uid = os.urandom(16).hex()
            for i, chunk in enumerate((data[: len(data) // 2], data[len(data) // 2:])):
                body, hdr = _multipart([("upload_id", uid), ("chunk_index", str(i))], [("file", "blob", chunk)])
                side.post("/images/albums/upload/chunk", content=body, headers=hdr)
            r = side.post("/images/albums/upload/complete", data={"upload_id": uid, "filename": name, "total_chunks": "2", "album_id": str(alb)})
            urls = json.loads(_row("image_albums", alb)["image_urls_json"])
            ans = r.json() if r.status_code == 200 else r.text
            if isinstance(ans, dict):
                ans = {"url": _masked_url(ans["url"])}
            res.append((r.status_code, ans, [_picture_facts(up, u) for u in urls], (up / "gallery" / "_chunks" / uid).exists()))
            served += r.headers.get("x-nd-served-by") == "gateway"
        assert res[0] == res[1], (name, res[0], res[1])
    assert served >= 8, served


def test_picture_form_uploads_are_pythons(client, seed):
    import io
    import os
    from pathlib import Path
    from PIL import Image
    from app.database import SessionLocal
    from app.models import World, WorldCalendar
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads"
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"calendar": {"player": "edit", "assistant": "edit"}})
        wa.max_characters_per_player = 1000
        db.add(WorldCalendar(world_id=w, config_json=json.dumps({"months": [{"name": "M", "days": 30}], "current_day": 1})))
        db.commit()
    finally:
        db.close()
    pics = _pictures()
    noisy = Image.effect_noise((900, 700), 90).convert("RGB")
    b = io.BytesIO(); noisy.save(b, "PNG"); pics["big noisy.png"] = b.getvalue()      # over 256 KB: streamed to disk
    assert len(pics["big noisy.png"]) > 1024 * 1024                                      # over MAX_UPLOAD_BYTES: the 413
    mid = Image.effect_noise((520, 420), 90).convert("RGB")
    b = io.BytesIO(); mid.save(b, "PNG"); pics["mid noisy.png"] = b.getvalue()
    assert 300 * 1024 < len(pics["mid noisy.png"]) < 1024 * 1024                          # streamed, under the limit
    served = 0
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    for name in ("Photo One.jpg", "small alpha.png", "mid noisy.png", "big noisy.png", "broken.png", "anim.webp", "notes.txt"):
        data = pics.get(name, b"plain text")
        # entity create / edit
        for path in ("/new", "EDIT"):
            res = []
            for side in (d, via):
                url = path
                if path == "EDIT":
                    [eid] = _entities(w, 1)
                    url = f"/entity/{eid}/edit"
                body, hdr = _multipart([("kind", "npc"), ("name", "Pic"), ("image_url", "")], [("image_file", name, data)])
                before = (_newest("entities") or {}).get("id")
                r = side.post(url, content=body, headers=hdr, follow_redirects=False)
                row = _newest("entities") if path == "/new" else _row("entities", eid)
                if path == "/new" and row and row["id"] == before:
                    row = None
                res.append((r.status_code, _picture_facts(up, row and row["image_url"]), r.headers.get("x-nd-served-by")))
            assert res[0][:2] == res[1][:2], (name, path, res)
            served += res[1][2] == "gateway"
        # character portrait
        res = []
        for side in (d, via):
            body, hdr = _multipart([("name", "Por")], [("portrait", name, data)])
            r = side.post("/characters/new", content=body, headers=hdr, follow_redirects=False)
            row = _newest("player_characters")
            res.append((r.status_code, _picture_facts(up, row["portrait_url"]), r.headers.get("x-nd-served-by")))
        assert res[0][:2] == res[1][:2], (name, "portrait", res)
        served += res[1][2] == "gateway"
        # calendar day icon (converted, no thumbnail)
        res = []
        for side in (d, via):
            body, hdr = _multipart([("label", "  Feast  ")], [("file", name, data)])
            r = side.post("/api/calendar/days/45/icons", content=body, headers=hdr)
            ans = r.json() if r.status_code == 200 else None
            facts = None
            if ans:
                facts = _picture_facts(up, ans["image_url"])
                ans = {**ans, "id": 0, "image_url": _masked_url(ans["image_url"])}
            res.append((r.status_code, ans, facts, r.headers.get("x-nd-served-by")))
        assert res[0][:3] == res[1][:3], (name, "icon", res)
        served += res[1][3] == "gateway"
    assert served >= 10, served


def test_folders_templates_and_entity_image_are_pythons(client, seed):
    from app.database import SessionLocal
    from app.models import Entity, EntityTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    served = 0
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)

    def folders(tag):
        db = SessionLocal()
        try:
            return sorted((e.name, e.folder) for e in db.query(Entity).filter(Entity.world_id == w, Entity.name.like(f"{tag}%")).all())
        finally:
            db.close()

    for form in ({"kind": "npc", "old_path": " Town ", "new_path": " City/Old "}, {"kind": "npc", "old_path": "Town", "new_path": ""}, {"kind": "npc", "old_path": "T_wn"},
                 {"kind": "npc", "old_path": "town"}, {"kind": "location", "old_path": "Town", "new_path": "X"}, {"kind": "npc", "old_path": " "}, {"old_path": "Town"}):
        res = []
        for side, tag in ((d, "fa"), (via, "fb")):
            db = SessionLocal()
            try:
                db.add_all([Entity(world_id=w, kind="npc", name=f"{tag}{i}", folder=fo) for i, fo in enumerate(("Town", "Town/Inn", "Townhall", "TOWN/x", "Tewn/y", None))])
                db.add(Entity(world_id=w, kind="location", name=f"{tag}L", folder="Town"))
                db.commit()
            finally:
                db.close()
            r = side.post("/folders/rename", data=form, follow_redirects=False)
            res.append((r, [(n[len(tag):], f) for n, f in folders(tag)]))
            db = SessionLocal()
            try:
                db.query(Entity).filter(Entity.name.like(f"{tag}%")).delete(synchronize_session=False); db.commit()
            finally:
                db.close()
        _same_answer(res[0][0], res[1][0])
        assert res[0][1] == res[1][1], (form, res[0][1], res[1][1])
        served += res[1][0].headers.get("x-nd-served-by") == "gateway"
    for form in ({"name": "  Shop Keeper ", "description": " d ", "kind": "npc", "fields_json": '[{"key": "hp"}]'}, {"name": "", "kind": "nope"}, {"fields_json": "{bad"},
                 {"name": "Ünï Tëmplate", "kind": ""}, {"name": "Shop Keeper"}):
        ra = d.post("/entity-templates/new", data=form, follow_redirects=False)
        na = _newest("entity_templates")
        rb = via.post("/entity-templates/new", data=form, follow_redirects=False)
        nb = _newest("entity_templates")
        created = ra.status_code == 303
        _same_answer(ra, rb, na["id"] if created else None, nb["id"] if created else None)
        if created:
            assert na["slug"].rstrip("-0123456789") == nb["slug"].rstrip("-0123456789")
            _same_rows(na, nb, ignore=("slug",))
        served += rb.headers.get("x-nd-served-by") == "gateway"
    for builtin in (False, True):
        for form in ({"name": " Renamed ", "kind": "location", "fields_json": "[]"}, {"name": "", "kind": "zzz"}, {"fields_json": "NaN"}, {}):
            ids = []
            for _ in range(2):
                db = SessionLocal()
                try:
                    t = EntityTemplate(world_id=w, name="T", slug=f"gwt-{os_random_hex()}", kind="npc", description="old", is_builtin=builtin, fields_json='[1]')
                    db.add(t); db.commit(); ids.append(t.id)
                finally:
                    db.close()
            before = [_row("entity_templates", i) for i in ids]
            ra = d.post(f"/entity-templates/{ids[0]}/edit", data=form, follow_redirects=False)
            rb = via.post(f"/entity-templates/{ids[1]}/edit", data=form, follow_redirects=False)
            _same_answer(ra, rb, ids[0], ids[1])
            _same_rows(_row("entity_templates", ids[0]), _row("entity_templates", ids[1]), before[0], before[1], ignore=("slug",))
            served += rb.headers.get("x-nd-served-by") == "gateway"
        ids, ents = [], []
        for _ in range(2):
            db = SessionLocal()
            try:
                t = EntityTemplate(world_id=w, name="T", slug=f"gwt-{os_random_hex()}", is_builtin=builtin, fields_json="[]")
                db.add(t); db.commit()
                e = Entity(world_id=w, kind="npc", name="E", template_id=t.id); db.add(e); db.commit()
                ids.append(t.id); ents.append(e.id)
            finally:
                db.close()
        ra = d.post(f"/entity-templates/{ids[0]}/delete", follow_redirects=False)
        rb = via.post(f"/entity-templates/{ids[1]}/delete", follow_redirects=False)
        _same_answer(ra, rb)
        _same_rows(_row("entity_templates", ids[0]), _row("entity_templates", ids[1]), ignore=("slug",))
        assert (_row("entities", ents[0])["template_id"] is None) == (_row("entities", ents[1])["template_id"] is None)
        served += rb.headers.get("x-nd-served-by") == "gateway"
    [other] = _entities(seed.world_b.id, 1)
    for body in ({"image_url": " /uploads/x.png "}, {"image_url": " "}, {"image_url": 5}, {}):
        ea, eb = _entities(w, 2)
        for ta, tb in ((ea, eb), (other, other)):
            ra = d.post(f"/api/entity/{ta}/image", json=body)
            rb = via.post(f"/api/entity/{tb}/image", json=body)
            _same_answer(ra, rb)
            served += rb.headers.get("x-nd-served-by") == "gateway"
        _same_rows(_row("entities", ea), _row("entities", eb))
    assert served >= 15, served


def os_random_hex():
    import os
    return os.urandom(6).hex()


def test_race_and_profession_writes_are_pythons(client, seed):
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import Entity, EntityRelation
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads"
    pics = _pictures()
    served = 0
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    for kind, path in (("race", "races"), ("profession", "professions")):
        for fields, file in (([("name", "  Elf  "), ("tier", "advanced"), ("summary", " s "), ("body", "")], None), ([("name", "X"), ("tier", "godly")], None),
                             ([("name", "")], None), ([("name", "Pic")], ("file", "Photo One.jpg", pics["Photo One.jpg"])),
                             ([("name", "Txt")], ("file", "a.txt", b"x")), ([("name", "Bad")], ("file", "broken.png", pics["broken.png"]))):
            res = []
            for side in (d, via):
                body, hdr = _multipart(fields, [file] if file else [("file", "", b"")])
                before = (_newest("entities") or {}).get("id")
                r = side.post(f"/{path}/new", content=body, headers=hdr, follow_redirects=False)
                row = _newest("entities")
                row = row if row and row["id"] != before else None
                res.append((r, row, _picture_facts(up, row and row["image_url"])))
            _same_answer(res[0][0], res[1][0])
            _same_rows(res[0][1], res[1][1], ignore=("image_url",))
            assert res[0][2] == res[1][2], (kind, fields, res[0][2], res[1][2])
            served += res[1][0].headers.get("x-nd-served-by") == "gateway"
        [other] = _entities(seed.world_b.id, 1)
        for target in ("own", "wrong-kind", "other"):
            ids = []
            for _ in range(2):
                db = SessionLocal()
                try:
                    e = Entity(world_id=w, kind=kind if target == "own" else "npc", name="R")
                    db.add(e); db.commit()
                    db.add(EntityRelation(world_id=w, source_id=e.id, target_id=e.id, label="x") if hasattr(EntityRelation, "label") else EntityRelation(world_id=w, source_id=e.id, target_id=e.id))
                    db.commit()
                    ids.append(e.id if target != "other" else other)
                finally:
                    db.close()
            ra = d.post(f"/{path}/{ids[0]}/delete", follow_redirects=False)
            rb = via.post(f"/{path}/{ids[1]}/delete", follow_redirects=False)
            _same_answer(ra, rb)
            _same_rows(_row("entities", ids[0]), _row("entities", ids[1]))
            served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 8, served


def test_view_pref_writes_are_pythons(client, seed):
    from sqlalchemy import text
    from app.database import SessionLocal
    base = str(client.base_url)
    served = 0

    def state(uid):
        db = SessionLocal()
        try:
            prefs = [tuple(r) for r in db.execute(text("SELECT section, prefs_json FROM user_view_prefs WHERE user_id = :u ORDER BY section"), {"u": uid})]
            views = [tuple(r) for r in db.execute(text("SELECT section, name, prefs_json FROM saved_views WHERE user_id = :u ORDER BY section, name"), {"u": uid})]
            return prefs, views
        finally:
            db.close()

    def wipe(uid):
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM user_view_prefs WHERE user_id = :u"), {"u": uid})
            db.execute(text("DELETE FROM saved_views WHERE user_id = :u"), {"u": uid})
            db.commit()
        finally:
            db.close()

    calls = [("post", "/api/view-prefs/images", {"mode": "masonry", "size": "3", "sort": "-uses", "group": "usage", "filters": ["unused", "unused", "x"]}),
             ("post", "/api/view-prefs/images", {"mode": "nope", "size": 2.9, "sort": 5, "group": "", "filters": "x"}),
             ("post", "/api/view-prefs/home", {"mode": "night", "order": ["a", "a", "B", "c_d", "x" * 41], "hidden": ["z"], "size": True}),
             ("post", "/api/view-prefs/hub", {"mode": "play", "filters": ["sheet", "notes", "log", "world", "quests"]}),
             ("post", "/api/view-prefs/tables", {"group": None}), ("post", "/api/view-prefs/images", [1, 2]), ("post", "/api/view-prefs/nosuch", {}),
             ("post", "/api/view-prefs/site", {"options": {"nav": "rail"}}), ("post", "/api/view-prefs/images", {"size": " 3 "}),
             ("delete", "/api/view-prefs/images", None), ("delete", "/api/view-prefs/nosuch", None),
             ("post", "/api/saved-views/images", {"name": "  My   view ", "state": {"mode": "list"}}), ("post", "/api/saved-views/images", {"name": "My view", "state": {"mode": "grid"}}),
             ("post", "/api/saved-views/images", {"name": " "}), ("post", "/api/saved-views/images", {"name": 5}), ("post", "/api/saved-views/nosuch", {"name": "x"}),
             ("delete", "/api/saved-views/images/FIRST", None), ("delete", "/api/saved-views/images/999999", None)]
    for email, pw, world in _people(seed):
        from app.models import User
        db = SessionLocal()
        try:
            uid = db.query(User).filter(User.email == email).first().id
        finally:
            db.close()
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        results = []
        for side in (d, via):
            wipe(uid)
            out = []
            for method, path, body in calls:
                if "FIRST" in path:
                    db = SessionLocal()
                    try:
                        first = db.execute(text("SELECT id FROM saved_views WHERE user_id = :u ORDER BY id LIMIT 1"), {"u": uid}).scalar() or 0
                    finally:
                        db.close()
                    path = path.replace("FIRST", str(first))
                r = side.request(method.upper(), path, json=body) if body is not None else side.request(method.upper(), path)
                ans = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.status_code
                if isinstance(ans, dict) and "views" in ans:
                    ans = {"views": [{**v, "id": 0} for v in ans["views"]]}
                out.append((r.status_code, ans, r.text if r.status_code == 200 else ""))
                if side is via:
                    served += r.headers.get("x-nd-served-by") == "gateway"
            out.append(state(uid))
            results.append(out)
        for a, b_, call in zip(results[0], results[1], calls + [("state",)]):
            assert a[:2] == b_[:2] if len(a) > 2 else a == b_, (call, a, b_)
    assert served >= 25, served
