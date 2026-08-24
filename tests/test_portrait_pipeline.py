"""Tests for the Image Studio portrait pipeline's backend surface:
GET /api/entities/picker (the lightweight {id,name,kind,folder} listing
behind the "🎭 Pick entity…"/"📎 Attach…" picker overlay, see
static/js/entity-picker.js and ai_chat.html's igOpenEntityPicker) and
POST /api/entity/{id}/image (sets Entity.image_url directly — "Set as
portrait"/"Attach" write the entity's image without round-tripping through
the full entity edit form). Both are GM-only, matching Image Studio's own
access level; the client-side prompt-building step (parse_entity's own
summary/body/custom_fields into an image prompt via /api/ai/chat) needs no
new backend route since it's just a system-prompted /api/ai/chat call —
covered indirectly via the existing chat tests.
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


# ── GET /api/entities/picker ────────────────────────────────────────────────

def test_picker_lists_every_kind_not_just_characters(client, seed):
    _make_entity(seed.world_a.id, kind="character", name="Elena", folder="NPCs")
    _make_entity(seed.world_a.id, kind="location", name="The Bazaar", folder="")
    _make_entity(seed.world_a.id, kind="item", name="Rusty Blade", folder="Loot")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/entities/picker")
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["entities"]}
    assert names == {"Elena", "The Bazaar", "Rusty Blade"}


def test_picker_carries_folder_data(client, seed):
    eid = _make_entity(seed.world_a.id, name="Elena", folder="NPCs/Bazaar")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entities = client.get("/api/entities/picker").json()["entities"]
    matching = [e for e in entities if e["id"] == eid]
    assert matching[0]["folder"] == "NPCs/Bazaar"


def test_picker_includes_hidden_entities_since_its_gm_only(client, seed):
    _make_entity(seed.world_a.id, name="Secret Villain", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    names = {e["name"] for e in client.get("/api/entities/picker").json()["entities"]}
    assert "Secret Villain" in names


def test_picker_cross_world_isolation(client, seed):
    _make_entity(seed.world_b.id, name="World B Only")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    names = {e["name"] for e in client.get("/api/entities/picker").json()["entities"]}
    assert "World B Only" not in names


def test_picker_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/entities/picker").status_code == 403


# ── POST /api/entity/{id}/image ─────────────────────────────────────────────

def test_set_image_updates_entity(client, seed):
    eid = _make_entity(seed.world_a.id, name="Elena")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/entity/{eid}/image", json={"image_url": "/uploads/imagegen/portrait123.png"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        assert e.image_url == "/uploads/imagegen/portrait123.png"
    finally:
        db.close()


def test_set_image_rejects_blank_url(client, seed):
    eid = _make_entity(seed.world_a.id, name="Elena")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/entity/{eid}/image", json={"image_url": "  "})
    assert r.status_code == 400


def test_set_image_404s_for_nonexistent_entity(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/entity/999999/image", json={"image_url": "/uploads/x.png"})
    assert r.status_code == 404


def test_set_image_404s_across_worlds(client, seed):
    """A GM whose active world is world_a can't overwrite an entity that
    belongs to world_b just by knowing its id — same ownership check every
    other entity-mutating route in main.py enforces."""
    eid = _make_entity(seed.world_b.id, name="World B Entity")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/entity/{eid}/image", json={"image_url": "/uploads/x.png"})
    assert r.status_code == 404

    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        assert e.image_url is None
    finally:
        db.close()


def test_set_image_requires_gm(client, seed):
    eid = _make_entity(seed.world_a.id, name="Elena", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/entity/{eid}/image", json={"image_url": "/uploads/x.png"})
    assert r.status_code == 403
