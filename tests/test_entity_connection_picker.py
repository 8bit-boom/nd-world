"""Tests for plan item UI 2.3: entity detail's "Add connection" panel used
to have detail() load every other visible entity in the world into the
template and render one <form> per entity — GET /entity/{id} loading
O(world entity count) rows on every single page view. Replaced with the
shared entity-picker (static/js/entity-picker.js) lazy-fetching
GET /api/entities/picker?exclude_id=<id> only once the panel is opened."""
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


def test_detail_page_no_longer_embeds_every_entity(client, seed):
    eid = _make_entity(seed.world_a.id, name="Viewed Entity")
    for i in range(20):
        _make_entity(seed.world_a.id, name=f"Other {i}")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    # None of the 20 other entities' names should be pre-rendered into the
    # page — they're only reachable via the lazy JSON fetch now.
    assert "Other 0" not in r.text
    assert "Other 19" not in r.text
    # The panel and its lazy-load wiring are present.
    assert 'id="add-link-panel"' in r.text
    assert "/api/entities/picker?exclude_id=" in r.text
    assert "entity-picker.js" in r.text


def test_picker_excludes_the_viewed_entity_itself(client, seed):
    eid = _make_entity(seed.world_a.id, name="Self")
    other = _make_entity(seed.world_a.id, name="Other")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/api/entities/picker?exclude_id={eid}")
    assert r.status_code == 200
    ids = {e["id"] for e in r.json()["entities"]}
    assert eid not in ids
    assert other in ids


def test_player_picker_omits_gm_only_entities_for_linking(client, seed):
    eid = _make_entity(seed.world_a.id, name="Self", visible_to_players=True)
    _make_entity(seed.world_a.id, name="Hidden Villain", visible_to_players=False)
    _make_entity(seed.world_a.id, name="Visible NPC", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get(f"/api/entities/picker?exclude_id={eid}")
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["entities"]}
    assert names == {"Visible NPC"}


def test_linking_still_works_end_to_end_via_the_picker_backed_route(client, seed):
    src = _make_entity(seed.world_a.id, name="Source")
    tgt = _make_entity(seed.world_a.id, name="Target")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/entity/{src}/link/{tgt}", follow_redirects=False)
    assert r.status_code == 303

    detail = client.get(f"/entity/{src}")
    assert "Target" in detail.text
