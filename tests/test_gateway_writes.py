"""Phase 3 (docs/RUST_REWRITE_PLAN.md): the writes the Rust gateway makes itself must leave the database exactly as Python leaves it.

Each case sends the same request twice - once to Python directly, once through the gateway - against two identical rows made for it, then
compares the answers (status, redirect, JSON) and the rows afterwards: every column, except ids (mapped) and timestamps (compared as
"changed or not", since the two requests run a moment apart). Refusals and odd input must reach Python unchanged, so their answers match too.
Runs only with a gateway to talk to (ND_TEST_BASE_URL) and Python directly (ND_GOLDEN_DIRECT_URL), like tests/test_gateway_golden.py.
"""
import json

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
