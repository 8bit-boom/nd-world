"""Right-click > Rename on entities: POST /api/entity/{id}/rename and the script that offers it (static/js/entity-menu.js)."""
from app.database import SessionLocal
from app.models import Entity, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make(world_id, name="Old Name"):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="character", name=name)
        db.add(e); db.commit(); db.refresh(e)
        return e.id
    finally:
        db.close()


def _name(eid):
    db = SessionLocal()
    try:
        return db.get(Entity, eid).name
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_gm_renames_an_entity_and_can_put_it_back(client, seed):
    eid = _make(seed.world_a.id)
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/rename", json={"name": "  The   Silt Lord "})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": eid, "name": "The Silt Lord", "old": "Old Name"}
    assert _name(eid) == "The Silt Lord"
    assert client.post(f"/api/entity/{eid}/rename", json={"name": "The Silt Lord"}).status_code == 422     # unchanged
    assert client.post(f"/api/entity/{eid}/rename", json={"name": "   "}).status_code == 422                 # empty
    assert client.post(f"/api/entity/{eid}/rename", json={"name": "Old Name"}).status_code == 200            # what Undo does
    assert _name(eid) == "Old Name"
    # the new name is what the page and the search show
    client.post(f"/api/entity/{eid}/rename", json={"name": "Zanzibar Quill"})
    assert "Zanzibar Quill" in client.get(f"/entity/{eid}").text
    assert "Zanzibar Quill" in client.get("/search", params={"q": "Zanzibar"}).text


def test_rename_is_limited_to_editors_and_to_the_active_world(client, seed):
    eid = _make(seed.world_a.id)
    other = _make(seed.world_b.id, "Elsewhere")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/entity/{eid}/rename", json={"name": "Hacked"}).status_code in (401, 403)
    assert _name(eid) == "Old Name"
    client.cookies.clear()
    _gm(client, seed)
    assert client.post(f"/api/entity/{other}/rename", json={"name": "Hacked"}).status_code == 404         # not in this world
    assert _name(other) == "Elsewhere"
    assert client.post("/api/entity/999999/rename", json={"name": "x"}).status_code == 404


def test_an_assistant_may_rename(client, seed):
    eid = _make(seed.world_a.id)
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter_by(world_id=seed.world_a.id, user_id=seed.player_a.id).first()
        m.role = "assistant"; db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/entity/{eid}/rename", json={"name": "By Assistant"}).status_code == 200
    assert _name(eid) == "By Assistant"


def test_the_menu_script_is_loaded_for_editors_only_and_the_page_marks_its_entity(client, seed):
    eid = _make(seed.world_a.id)
    _gm(client, seed)
    html = client.get(f"/entity/{eid}").text
    assert "entity-menu.js" in html and f'data-entity-id="{eid}"' in html
    js = client.get("/static/js/entity-menu.js").text
    for needle in ("/rename", "Rename this entity", "Rename…", "e.shiftKey", "ndRenameEntity"):
        assert needle in js, needle
    assert "ndRenameEntity" in client.get("/static/js/text-format-toolbar.js").text
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert "entity-menu.js" not in client.get("/").text
