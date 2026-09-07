"""Tests for the race catalog (app/routers/races.py): the bundled markdown
race library, one-click add into a world as Entity(kind="race") rows, and
that races are otherwise just a normal entity kind (GM-only writes, player-safe
reads, no cross-world reach).
"""
from app.database import SessionLocal
from app.models import Entity
from app.routers import races as races_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add_race(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="race", name=kw.pop("name", "Secret Race"),
                    subtype=kw.pop("subtype", "standard"), body=kw.pop("body", ""), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_hidden_race_not_shown_to_players(client, seed):
    """Regression: races_page queried Entity with no visibility filter at
    all, so a race the GM marked hidden was still fully inlined (name +
    rendered body) into the player-safe /races page's JS object
    (docs/AUDIT_PLAN_NEXT.md item 3)."""
    _add_race(seed.world_a.id, name="Secret Deep Ones", body="Forbidden lore text",
              visible_to_players=False)
    _add_race(seed.world_a.id, name="Common Folk", body="Ordinary lore", visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/races")
    assert r.status_code == 200
    assert "Secret Deep Ones" not in r.text
    assert "Forbidden lore text" not in r.text
    assert "Common Folk" in r.text

    # The GM still sees both.
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/races")
    assert "Secret Deep Ones" in r.text
    assert "Common Folk" in r.text


def test_races_page_lists_builtin_catalog(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/races")
    assert r.status_code == 200
    assert "Dwarf" in r.text
    assert "Dragonblooded" in r.text


def test_races_page_is_player_safe(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/races")
    assert r.status_code == 200


def test_add_builtin_race_creates_entity_in_active_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race", Entity.name.ilike("%dwarf%")
        ).first()
        assert ent is not None
        assert ent.subtype == "standard"
    finally:
        db.close()


def test_add_builtin_race_response_carries_entity_card_data(client, seed):
    """The response's `entity` payload is what races.html's own addBuiltin()
    JS uses to insert a card into the tier grid without a page reload — see
    the route's own docstring for why {"ok": true} alone isn't enough."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})
    assert r.status_code == 200
    data = r.json()["entity"]
    assert "dwarf" in data["name"].lower()
    assert data["tier"] == "standard"
    assert isinstance(data["id"], int)
    assert "body_html" in data

    # Re-adding the same (already-added) race returns the SAME entity's
    # data rather than erroring or creating a duplicate.
    r2 = client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})
    assert r2.json()["entity"]["id"] == data["id"]


def test_races_page_race_card_links_to_generic_entity_edit(client, seed):
    """A race becomes a normal Entity(kind="race") row — the catalog page
    itself must offer a way to edit it, not just view (the read-only modal)
    or delete it. Reuses the generic /entity/{id}/edit form rather than a
    race-specific one, same as every other entity kind."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race", Entity.name.ilike("%dwarf%")
        ).first()
        race_id = ent.id
    finally:
        db.close()

    r = client.get("/races")
    assert r.status_code == 200
    assert f"/entity/{race_id}/edit" in r.text

    # And the generic edit route actually works for a race entity.
    r2 = client.get(f"/entity/{race_id}/edit")
    assert r2.status_code == 200


def test_add_builtin_race_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})
    assert r.status_code == 403


def test_add_all_builtin_adds_every_race_once(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/races/add-all-builtin", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        count_first = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race"
        ).count()
        assert count_first >= 17
    finally:
        db.close()

    # Re-running must not create duplicates.
    client.post("/races/add-all-builtin", follow_redirects=False)
    db = SessionLocal()
    try:
        count_second = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "race"
        ).count()
        assert count_second == count_first
    finally:
        db.close()


def test_race_kind_appears_in_generic_kind_browsing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/races/add-builtin", json={"slug": "dwarf", "tier": "standard"})

    r = client.get("/kind/race")
    assert r.status_code == 200
    assert "Dwarf" in r.text


def test_race_delete_rejects_entity_from_other_world(client, seed):
    """Mirrors this session's established cross-world-leak hardening: a race
    id from a different world must not be deletable via the active world's
    session, even though the original NeonDragonsWorld implementation this
    was ported from didn't check world ownership at all."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    other_world_race = Entity(world_id=seed.world_b.id, kind="race", subtype="standard", name="Foreign Race")
    db = SessionLocal()
    try:
        db.add(other_world_race)
        db.commit()
        db.refresh(other_world_race)
        race_id = other_world_race.id
    finally:
        db.close()

    r = client.post(f"/races/{race_id}/delete")
    assert r.status_code == 404

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == race_id).first() is not None
    finally:
        db.close()


def test_builtin_race_catalog_is_memoized(client, seed):
    """docs/AUDIT_PLAN_NEXT.md item 14: re-reading and re-rendering all 17+
    bundled markdown files on every /races view is pure waste for content
    that's byte-identical for the life of the process."""
    races_module._load_builtin_races.cache_clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    client.get("/races")
    assert races_module._load_builtin_races.cache_info().misses == 1
    client.get("/races")
    info = races_module._load_builtin_races.cache_info()
    assert info.misses == 1, "second /races view re-read the catalog from disk"
    assert info.hits >= 1
