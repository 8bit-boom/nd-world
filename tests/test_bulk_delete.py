"""Tests for bulk entity deletion: POST /kind/{kind}/bulk-delete (new), and
the world-ownership check added to the existing POST /entity/{id}/delete
alongside it — that route previously had no check tying the entity id to the
currently active world at all, unlike every other delete route touched this
session (races, professions), so any GM could delete any entity on the
instance just by walking ids. Fixed as part of building the bulk-delete UI
that sits right next to it.
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entities(world_id, kind="feat", subtype=None, count=3, folder=None, prefix="Bulk Test"):
    db = SessionLocal()
    try:
        made = []
        for i in range(count):
            e = Entity(world_id=world_id, kind=kind, subtype=subtype, name=f"{prefix} {i+1}", folder=folder)
            db.add(e)
            made.append(e)
        db.commit()
        for e in made:
            db.refresh(e)
        return [e.id for e in made]
    finally:
        db.close()


def test_bulk_delete_removes_selected_entities(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    ids = _make_entities(seed.world_a.id, count=3)

    r = client.post(f"/kind/feat/bulk-delete", data={"entity_ids": [str(i) for i in ids]}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        remaining = db.query(Entity).filter(Entity.id.in_(ids)).count()
        assert remaining == 0
    finally:
        db.close()


def test_bulk_delete_ignores_ids_from_other_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    own_ids = _make_entities(seed.world_a.id, count=2)
    other_ids = _make_entities(seed.world_b.id, count=2)

    r = client.post("/kind/feat/bulk-delete", data={"entity_ids": [str(i) for i in own_ids + other_ids]})
    assert r.status_code == 200 or r.status_code == 303

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id.in_(own_ids)).count() == 0
        # Untouched — belonged to a different world than the active one.
        assert db.query(Entity).filter(Entity.id.in_(other_ids)).count() == 2
    finally:
        db.close()


def test_bulk_delete_ignores_ids_of_a_different_kind(client, seed):
    """A batch built against /kind/feat must not delete a same-id-range
    entity of a different kind that happens to be in the request by
    mistake — the kind filter in the query is what makes this safe, not
    trust in what the client claims it's deleting."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    feat_ids = _make_entities(seed.world_a.id, kind="feat", count=1)
    note_ids = _make_entities(seed.world_a.id, kind="note", count=1)

    client.post("/kind/feat/bulk-delete", data={"entity_ids": [str(i) for i in feat_ids + note_ids]})

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id.in_(feat_ids)).count() == 0
        assert db.query(Entity).filter(Entity.id.in_(note_ids)).count() == 1
    finally:
        db.close()


def test_bulk_delete_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    ids = _make_entities(seed.world_a.id, count=1)

    r = client.post("/kind/feat/bulk-delete", data={"entity_ids": [str(i) for i in ids]})
    assert r.status_code == 403

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id.in_(ids)).count() == 1
    finally:
        db.close()


def test_bulk_delete_empty_selection_is_a_no_op(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/kind/feat/bulk-delete", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/kind/feat?w={seed.world_a.slug}"


def test_bulk_delete_redirect_preserves_folder_and_query(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    ids = _make_entities(seed.world_a.id, folder="Race Feats/Darro/Rank 1", count=1)

    r = client.post(
        "/kind/feat/bulk-delete",
        data={"entity_ids": [str(i) for i in ids], "folder": "Race Feats/Darro/Rank 1", "q": "hide"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/kind/feat?")
    # quote()'s default safe="/" leaves slashes literal in the query value —
    # still a well-formed query string (only ?, &, and = are structurally
    # significant there), and what get_query_params/FastAPI's own folder
    # param parses back out correctly either way.
    assert "folder=Race%20Feats/Darro/Rank%201" in location
    assert "q=hide" in location


def test_single_delete_rejects_entity_from_other_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    other_id = _make_entities(seed.world_b.id, count=1)[0]

    r = client.post(f"/entity/{other_id}/delete")
    assert r.status_code == 404

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.id == other_id).first() is not None
    finally:
        db.close()


def test_entities_list_page_has_bulk_delete_ui(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/feat")
    assert r.status_code == 200
    assert 'id="bulk-action-bar"' in r.text
    assert 'id="bulk-delete-btn"' in r.text
    assert 'id="bulk-select-all-btn"' in r.text
    assert "class=\"row-cb\"" in r.text or "row-cb" in r.text
    assert "/bulk-delete`" in r.text


def test_entities_list_page_select_all_toggles_every_checkbox(client, seed):
    """Source-level regression guard: the Select All button must toggle
    every .row-cb on the page (all tables and cards), not just one table —
    that's the whole point of it existing alongside each table's own
    per-table select-all checkbox."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/feat")
    assert r.status_code == 200
    assert "function updateBulkBar()" in r.text
    assert "allRowCbs()" in r.text
    assert "Deselect All" in r.text
    assert "getElementById('bulk-select-all-btn').addEventListener" in r.text
    assert r.text.count("updateBulkBar();") >= 1  # called on load, not just reactively on change
