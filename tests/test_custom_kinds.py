"""Tests for GM-defined custom entity kinds/categories (app/routers/
kinds_admin.py + app/deps.py's load_custom_kinds/effective_kinds + the
app/templating.py context processor that makes them world-scoped
everywhere): a GM can add a per-world content category that behaves like a
built-in kind (nav tab, home stat tile, entity create/import support), on
top of the fixed built-ins in app/constants.py.
"""
import json

from app.database import SessionLocal
from app.models import Entity, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_custom_kinds(world_id, kinds):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.custom_kinds_json = json.dumps(kinds)
        db.commit()
    finally:
        db.close()


def _get_custom_kinds(world_id):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return json.loads(w.custom_kinds_json or "[]")
    finally:
        db.close()


def _make_entity(world_id, kind, name="Thing"):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, name=name)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


# ── Access control ──────────────────────────────────────────────────────────

def test_kinds_edit_form_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/kinds/edit")
    assert r.status_code == 403


def test_kinds_edit_form_renders_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/kinds/edit")
    assert r.status_code == 200
    assert "Manage Kinds" in r.text


def test_kinds_new_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    assert r.status_code == 403
    assert _get_custom_kinds(seed.world_a.id) == []


# ── Create / rename / reorder / delete round-trip ───────────────────────────

def test_create_kind_round_trips(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/kinds/new",
        data={"label": "Vehicles", "icon": "🚗", "subtypes": "Car, Bike, Bike"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    kinds = _get_custom_kinds(seed.world_a.id)
    assert len(kinds) == 1
    assert kinds[0]["id"] == "custom_vehicles"
    assert kinds[0]["label"] == "Vehicles"
    assert kinds[0]["icon"] == "🚗"
    assert kinds[0]["subtypes"] == ["Car", "Bike"]  # de-duplicated


def test_slugify_and_id_dedup_on_collision(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles!!"})
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles!!"})
    kinds = _get_custom_kinds(seed.world_a.id)
    ids = [k["id"] for k in kinds]
    assert ids == ["custom_vehicles", "custom_vehicles_2"]


def test_max_custom_kinds_enforced(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _set_custom_kinds(seed.world_a.id, [
        {"id": f"custom_k{i}", "label": f"K{i}", "icon": "🏷", "subtypes": [], "created_at": ""}
        for i in range(25)
    ])
    r = client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "One Too Many"})
    assert r.status_code == 400
    assert len(_get_custom_kinds(seed.world_a.id)) == 25


def test_rename_reorder_via_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Deities"})
    kinds = _get_custom_kinds(seed.world_a.id)
    assert [k["label"] for k in kinds] == ["Vehicles", "Deities"]

    # Rename "Vehicles" -> "Cars" and swap order (ids are immutable).
    reordered = [
        {"id": kinds[1]["id"], "label": "Deities", "icon": "🪨", "subtypes": []},
        {"id": kinds[0]["id"], "label": "Cars", "icon": "🚗", "subtypes": ["Car"]},
    ]
    r = client.post(
        f"/worlds/{seed.world_a.id}/kinds/edit",
        data={"custom_kinds_json": json.dumps(reordered)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    saved = _get_custom_kinds(seed.world_a.id)
    assert [k["label"] for k in saved] == ["Deities", "Cars"]
    assert saved[1]["id"] == kinds[0]["id"]
    assert saved[1]["subtypes"] == ["Car"]


def test_save_cannot_smuggle_new_id_or_steal_another_worlds_id(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    _set_custom_kinds(seed.world_b.id, [
        {"id": "custom_deities", "label": "Deities", "icon": "🪨", "subtypes": [], "created_at": ""}
    ])
    smuggled = [
        {"id": "custom_vehicles", "label": "Vehicles Renamed", "icon": "🚗", "subtypes": []},
        {"id": "custom_hacked", "label": "Hacked In", "icon": "💀", "subtypes": []},
        {"id": "custom_deities", "label": "Stolen From World B", "icon": "🪨", "subtypes": []},
    ]
    r = client.post(
        f"/worlds/{seed.world_a.id}/kinds/edit",
        data={"custom_kinds_json": json.dumps(smuggled)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    saved = _get_custom_kinds(seed.world_a.id)
    assert [k["id"] for k in saved] == ["custom_vehicles"]
    assert saved[0]["label"] == "Vehicles Renamed"
    # World B's own kind is untouched.
    assert _get_custom_kinds(seed.world_b.id)[0]["label"] == "Deities"


def test_delete_blocked_while_entities_use_it_then_succeeds_once_empty(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]
    eid = _make_entity(seed.world_a.id, kind_id, name="Motorbike")

    r = client.post(f"/worlds/{seed.world_a.id}/kinds/{kind_id}/delete", follow_redirects=False)
    assert r.status_code == 400
    assert len(_get_custom_kinds(seed.world_a.id)) == 1

    db = SessionLocal()
    try:
        db.query(Entity).filter(Entity.id == eid).delete()
        db.commit()
    finally:
        db.close()

    r = client.post(f"/worlds/{seed.world_a.id}/kinds/{kind_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _get_custom_kinds(seed.world_a.id) == []


def test_builtin_kind_can_never_be_deleted(client, seed):
    """There's no route to delete a built-in kind at all — the delete route
    only ever operates on ids loaded from this world's custom_kinds_json,
    and a built-in id (e.g. "character") can never appear there since
    load_custom_kinds() drops any entry without the custom_ prefix."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/kinds/character/delete", follow_redirects=False)
    assert r.status_code == 404


# ── Nav tab + home stat tile ────────────────────────────────────────────────

def test_new_kind_appears_in_nav_and_home_with_zero_count(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles", "icon": "🚗"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    r = client.get("/")
    assert r.status_code == 200
    assert f'data-ql-ref="{kind_id}"' in r.text
    assert f'class="dash-card kind-{kind_id}"' in r.text

    _make_entity(seed.world_a.id, kind_id, name="Motorbike")
    r = client.get("/")
    assert f'class="dash-card kind-{kind_id}"' in r.text


def test_entity_create_form_lists_custom_kind_and_entity_creation_works(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    r = client.get("/new")
    assert f'value="{kind_id}"' in r.text or f">{kind_id}" in r.text.lower() or kind_id in r.text

    r = client.post(
        "/new",
        data={"kind": kind_id, "subtype": "", "name": "Motorbike", "folder": "", "tags": "",
              "image_url": "", "summary": "", "body": "", "visibility_mode": "everyone"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.kind == kind_id).first()
        assert ent is not None
        assert ent.name == "Motorbike"
    finally:
        db.close()


# ── Import pipeline ──────────────────────────────────────────────────────────

def test_bulk_json_import_accepts_custom_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    payload = json.dumps([{"kind": kind_id, "name": "Dune Buggy"}])
    r = client.post("/api/import/execute", json={"json_text": payload})
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.kind == kind_id).first()
        assert ent is not None
        assert ent.name == "Dune Buggy"
    finally:
        db.close()


def test_import_detect_recognizes_custom_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    payload = json.dumps({"kind": kind_id, "name": "Dune Buggy"})
    r = client.post("/api/import/detect", json={"json_text": payload})
    assert r.status_code == 200
    assert r.json()["kind"] == "entity_single"


def test_legacy_api_import_accepts_custom_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    r = client.post("/api/import", json={
        "world_id": seed.world_a.id,
        "entities": [{"name": "Dune Buggy", "kind": kind_id}],
    })
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.kind == kind_id).first()
        assert ent is not None
    finally:
        db.close()


# ── Home Quick Link targeting a custom kind ─────────────────────────────────

def test_quick_link_can_target_custom_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": None, "label": "All Vehicles", "target_type": "kind", "target_ref": kind_id},
    )
    assert r.status_code == 200

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        sections = json.loads(w.home_sections_json)
        assert sections[0]["links"][0]["target_ref"] == kind_id
    finally:
        db.close()

    r = client.get("/")
    assert f'href="/kind/{kind_id}?w={seed.world_a.slug}"' in r.text


# ── World scoping ────────────────────────────────────────────────────────────

def test_custom_kind_is_scoped_to_its_own_world(client, seed):
    """World B must never see World A's custom kind — not in the nav, not in
    the home stat tiles, not in the entity-create form, and not on its own
    /kinds/edit management page."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]
    assert _get_custom_kinds(seed.world_b.id) == []

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert kind_id not in r.text

    r = client.get(f"/worlds/{seed.world_b.id}/kinds/edit")
    assert r.status_code == 200
    assert kind_id not in r.text

    r = client.get("/new")
    assert kind_id not in r.text


def test_entity_template_cannot_use_another_worlds_custom_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/kinds/new", data={"label": "Vehicles"})
    kind_id = _get_custom_kinds(seed.world_a.id)[0]["id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.post(
        "/entity-templates/new",
        data={"name": "Bogus Template", "description": "", "kind": kind_id, "fields_json": "[]"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # world_id in this request is resolved from the active_world cookie (World B),
    # so a kind that only exists as a custom kind in World A must not validate.
    from app.models import EntityTemplate
    db = SessionLocal()
    try:
        tpl = db.query(EntityTemplate).filter(EntityTemplate.name == "Bogus Template").first()
        assert tpl is not None
        assert tpl.kind is None
    finally:
        db.close()
