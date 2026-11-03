"""Player groups: several tables play one world; an entity limited to some groups is visible to the players of those groups only (and the GM)."""
import json
import os

import httpx
import pytest

from app import auth
from app.database import SessionLocal
from app.models import Entity, PlayerGroup, User, WorldMembership, entity_player_access

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

DIRECT = os.environ.get("ND_GOLDEN_DIRECT_URL", "")


@pytest.fixture()
def table(seed):
    """Three more players in world A besides seed.player_a: ids by name."""
    db = SessionLocal()
    try:
        out = {"a": seed.player_a.id}
        pw = auth.hash_password(PLAYER_PASSWORD)
        for n in ("b", "c", "d"):
            u = User(email=f"grp-{n}@test.local", password_hash=pw, display_name=f"Grp {n}")
            db.add(u)
            db.flush()
            db.add(WorldMembership(world_id=seed.world_a.id, user_id=u.id))
            out[n] = u.id
        db.commit()
        return out
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    return client


def _player(client, seed, email):
    login(client, email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    return client


def _groups(seed):
    db = SessionLocal()
    try:
        return {g.name: g.id for g in db.query(PlayerGroup).filter(PlayerGroup.world_id == seed.world_a.id).all()}
    finally:
        db.close()


def _setup(client, seed, table):
    gm = _gm(client, seed)
    for name in ("Tuesday", "Newcomers"):
        assert gm.post("/groups/new", data={"name": name}, follow_redirects=False).status_code == 303
    g = _groups(seed)
    # a, b -> Tuesday; c -> Newcomers; d -> no group
    for key, grp in (("a", "Tuesday"), ("b", "Tuesday"), ("c", "Newcomers")):
        assert gm.post(f"/groups/members/{table[key]}", data={"group_id": str(g[grp])}, follow_redirects=False).status_code == 303
    return gm, g


def _entity(gm, **form):
    data = {"kind": "location", "name": "Tuesday's cellar", "visibility_mode": "groups", **form}
    r = gm.post("/new", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rstrip("/").split("/")[-1].split("?")[0])


def _sees(client, seed, email, eid):
    _player(client, seed, email)
    return client.get(f"/entity/{eid}").status_code == 200


def test_an_entity_limited_to_a_group_is_seen_by_that_group_only(client, seed, table):
    gm, g = _setup(client, seed, table)
    eid = _entity(gm, allowed_group_ids=[str(g["Tuesday"])])
    assert gm.get(f"/entity/{eid}").status_code == 200
    seen = {n: _sees(client, seed, f"grp-{n}@test.local" if n != "a" else seed.player_a.email, eid) for n in "abcd"}
    assert seen == {"a": True, "b": True, "c": False, "d": False}
    # the lists and the search follow
    _player(client, seed, f"grp-c@test.local")
    assert "Tuesday&#39;s cellar" not in client.get("/kind/location").text and "Tuesday&#39;s cellar" not in client.get("/search?q=cellar").text
    _player(client, seed, f"grp-b@test.local")
    assert "Tuesday&#39;s cellar" in client.get("/kind/location").text and "Tuesday&#39;s cellar" in client.get("/search?q=cellar").text


def test_moving_a_player_or_deleting_a_group_follows(client, seed, table):
    gm, g = _setup(client, seed, table)
    eid = _entity(gm, allowed_group_ids=[str(g["Tuesday"])])
    gm = _gm(client, seed)
    gm.post(f"/groups/members/{table['c']}", data={"group_id": str(g["Tuesday"])}, follow_redirects=False)
    assert _sees(client, seed, "grp-c@test.local", eid)
    _gm(client, seed).post(f"/groups/members/{table['a']}", data={"group_id": str(g["Newcomers"])}, follow_redirects=False)
    assert not _sees(client, seed, seed.player_a.email, eid)
    _gm(client, seed).post(f"/groups/members/{table['d']}", data={"group_id": str(g["Tuesday"])}, follow_redirects=False)
    assert _sees(client, seed, "grp-d@test.local", eid)
    # the group goes: the entity must not open up to everyone, it becomes GM-only
    assert _gm(client, seed).post(f"/groups/{g['Tuesday']}/delete", follow_redirects=False).status_code == 303
    assert not any(_sees(client, seed, e, eid) for e in ("grp-b@test.local", "grp-c@test.local", "grp-d@test.local"))
    assert _gm(client, seed).get(f"/entity/{eid}").status_code == 200
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        assert e.visible_to_players is False and not e.group_ids_json
        assert db.query(entity_player_access).filter(entity_player_access.c.entity_id == eid).count() == 0
    finally:
        db.close()


def test_editing_an_entity_back_to_everyone_or_gm_only_clears_the_groups(client, seed, table):
    gm, g = _setup(client, seed, table)
    eid = _entity(gm, allowed_group_ids=[str(g["Tuesday"])])
    form = {"kind": "location", "name": "Tuesday's cellar", "visibility_mode": "everyone"}
    assert _gm(client, seed).post(f"/entity/{eid}/edit", data=form, follow_redirects=False).status_code == 303
    assert all(_sees(client, seed, e, eid) for e in ("grp-c@test.local", "grp-d@test.local"))
    db = SessionLocal()
    try:
        assert not db.get(Entity, eid).group_ids_json
    finally:
        db.close()
    assert _gm(client, seed).post(f"/entity/{eid}/edit", data={**form, "visibility_mode": "groups"}, follow_redirects=False).status_code == 303   # groups, none picked
    assert not any(_sees(client, seed, e, eid) for e in ("grp-b@test.local", "grp-c@test.local"))


def test_the_form_offers_groups_and_other_groups_are_not_accepted(client, seed, table):
    gm = _gm(client, seed)
    assert "Only for these player groups" not in gm.get("/new?kind=location").text      # no groups yet: nothing to choose
    gm, g = _setup(client, seed, table)
    assert "Only for these player groups" in gm.get("/new?kind=location").text
    eid = _entity(gm, allowed_group_ids=[str(g["Tuesday"]), "99999"])                    # an unknown id is dropped
    h = _gm(client, seed).get(f"/entity/{eid}/edit").text
    assert f'id="group-{g["Tuesday"]}"' in h and 'name="allowed_group_ids"' in h and "checked" in h.split(f'id="group-{g["Tuesday"]}"')[1][:200]
    db = SessionLocal()
    try:
        assert json.loads(db.get(Entity, eid).group_ids_json) == [g["Tuesday"]]
    finally:
        db.close()


def test_only_the_gm_manages_groups(client, seed, table):
    _setup(client, seed, table)
    p = _player(client, seed, seed.player_a.email)
    assert p.get("/groups").status_code in (403, 404)
    assert p.post("/groups/new", data={"name": "Mine"}, follow_redirects=False).status_code in (403, 404)
    assert "Mine" not in _gm(client, seed).get("/groups").text
    page = _gm(client, seed).get("/groups").text
    assert "Tuesday" in page and "Newcomers" in page and "Grp b" in page


def test_bulk_visibility_by_group(client, seed, table):
    gm, g = _setup(client, seed, table)
    eid = _entity(gm, visibility_mode="everyone")
    r = _gm(client, seed).post("/api/entities/bulk-visibility", json={"entity_ids": [eid], "visibility_mode": "groups", "group_ids": [g["Newcomers"]]})
    assert r.status_code == 200 and r.json() == {"updated": 1}
    seen = {n: _sees(client, seed, f"grp-{n}@test.local", eid) for n in "bcd"}
    assert seen == {"b": False, "c": True, "d": False}


@pytest.mark.skipif(not DIRECT or not os.environ.get("ND_TEST_BASE_URL"), reason="needs a gateway and the app's own address")
def test_the_gateway_does_the_same_as_the_app(client, seed, table):
    gm, g = _setup(client, seed, table)
    via = str(client.base_url)
    results = []
    for base in (DIRECT, via):
        c = httpx.Client(base_url=base, timeout=60)
        assert c.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False).status_code == 303
        c.cookies.set("active_world", seed.world_a.slug)
        r = c.post("/new", data={"kind": "location", "name": f"Cellar {len(results)}", "visibility_mode": "groups", "allowed_group_ids": [str(g["Tuesday"])]},
                   follow_redirects=False)
        assert r.status_code == 303
        eid = int(r.headers["location"].rstrip("/").split("/")[-1].split("?")[0])
        db = SessionLocal()
        try:
            e = db.get(Entity, eid)
            access = sorted(x[0] for x in db.query(entity_player_access.c.user_id).filter(entity_player_access.c.entity_id == eid).all())
            results.append((e.visible_to_players, e.group_ids_json, access))
        finally:
            db.close()
        assert c.post(f"/groups/members/{table['d']}", data={"group_id": str(g["Tuesday"])}, follow_redirects=False).status_code == 303
        db = SessionLocal()
        try:
            results.append(sorted(x[0] for x in db.query(entity_player_access.c.user_id).filter(entity_player_access.c.entity_id == eid).all()))
        finally:
            db.close()
        c.post(f"/groups/members/{table['d']}", data={"group_id": ""}, follow_redirects=False)
        # the edit form and the groups page are the same pages
        results.append((c.get(f"/entity/{eid}/edit").text.count("group-"), c.get("/groups").status_code))
    assert results[:3] == results[3:], results
