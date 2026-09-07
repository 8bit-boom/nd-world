"""Tests for the profession catalog (app/routers/professions.py): the bundled
markdown profession library, one-click add into a world as Entity(kind="profession")
rows, and that professions are otherwise just a normal entity kind (GM-only
writes, player-safe reads, no cross-world reach). Mirrors test_races.py.
"""
from app.database import SessionLocal
from app.models import Entity
from app.routers import professions as professions_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add_profession(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="profession", name=kw.pop("name", "Secret Profession"),
                    subtype=kw.pop("subtype", "standard"), body=kw.pop("body", ""), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_hidden_profession_not_shown_to_players(client, seed):
    """Regression: professions_page queried Entity with no visibility filter
    at all (mirrors test_races.py's races equivalent — see
    docs/AUDIT_PLAN_NEXT.md item 3)."""
    _add_profession(seed.world_a.id, name="Secret Assassin", body="Forbidden lore text",
                     visible_to_players=False)
    _add_profession(seed.world_a.id, name="Common Laborer", body="Ordinary lore", visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/professions")
    assert r.status_code == 200
    assert "Secret Assassin" not in r.text
    assert "Forbidden lore text" not in r.text
    assert "Common Laborer" in r.text

    # The GM still sees both.
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/professions")
    assert "Secret Assassin" in r.text
    assert "Common Laborer" in r.text


def test_professions_page_lists_builtin_catalog(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/professions")
    assert r.status_code == 200
    assert "Street Fighter" in r.text
    assert "Psyonic" in r.text


def test_professions_page_is_player_safe(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/professions")
    assert r.status_code == 200


def test_add_builtin_profession_creates_entity_in_active_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/professions/add-builtin", json={"slug": "merc", "tier": "standard"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "profession", Entity.name.ilike("%merc%")
        ).first()
        assert ent is not None
        assert ent.subtype == "standard"
    finally:
        db.close()


def test_add_builtin_profession_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/professions/add-builtin", json={"slug": "merc", "tier": "standard"})
    assert r.status_code == 403


def test_add_all_builtin_adds_every_profession_once(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/professions/add-all-builtin", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        count_first = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "profession"
        ).count()
        assert count_first >= 6
    finally:
        db.close()

    # Re-running must not create duplicates.
    client.post("/professions/add-all-builtin", follow_redirects=False)
    db = SessionLocal()
    try:
        count_second = db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.kind == "profession"
        ).count()
        assert count_second == count_first
    finally:
        db.close()


def test_profession_kind_appears_in_generic_kind_browsing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/professions/add-builtin", json={"slug": "merc", "tier": "standard"})

    r = client.get("/kind/profession")
    assert r.status_code == 200
    assert "Merc" in r.text


def test_profession_delete_rejects_entity_from_other_world(client, seed):
    """Mirrors this session's established cross-world-leak hardening: a
    profession id from a different world must not be deletable via the
    active world's session."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    other_world_profession = Entity(world_id=seed.world_b.id, kind="profession", subtype="standard", name="Foreign Profession")
    db = SessionLocal()
    try:
        db.add(other_world_profession)
        db.commit()
        db.refresh(other_world_profession)
        profession_id = other_world_profession.id
    finally:
        db.close()

    r = client.post(f"/professions/{profession_id}/delete")
    assert r.status_code == 404

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == profession_id).first() is not None
    finally:
        db.close()


def test_builtin_profession_catalog_is_memoized(client, seed):
    """docs/AUDIT_PLAN_NEXT.md item 14: re-reading and re-rendering every
    bundled markdown file on every /professions view is pure waste for
    content that's byte-identical for the life of the process."""
    professions_module._load_builtin_professions.cache_clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    client.get("/professions")
    assert professions_module._load_builtin_professions.cache_info().misses == 1
    client.get("/professions")
    info = professions_module._load_builtin_professions.cache_info()
    assert info.misses == 1, "second /professions view re-read the catalog from disk"
    assert info.hits >= 1


def test_player_never_sees_gm_only_profession_controls(client, seed):
    """docs/AUDIT_PLAN_NEXT.md item 20: the page rendered a full GM toolbar
    (New Profession, per-profession delete, built-in Add buttons) with no
    can_edit(request) guard — every button 403s for a player who clicks it.
    Cosmetic only (the server-side gate already denies the POSTs), but the
    controls must not render at all."""
    prof_id = _add_profession(seed.world_a.id, name="Common Trade", visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/professions")
    assert r.status_code == 200
    assert "+ New Profession" not in r.text
    assert f'/professions/{prof_id}/delete' not in r.text
    assert "Add built-in professions" not in r.text
    assert "/professions/add-all-builtin" not in r.text

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/professions")
    assert "+ New Profession" in r.text
    assert f'/professions/{prof_id}/delete' in r.text
    assert "Add built-in professions" in r.text
    assert "/professions/add-all-builtin" in r.text
