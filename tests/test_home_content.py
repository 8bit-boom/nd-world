"""Tests for GM-customizable home page content (app/routers/home_content.py +
app/main.py's _resolve_home_sections/_resolve_home_link_href): a welcome
blurb plus GM-defined tabs/sections of curated Quick Links.
"""
import json

from app.database import SessionLocal
from app.models import Entity, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, name="Linked Thing", kind="character"):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, name=name)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _sections_payload(*, label="My Link", target_type="entity", target_ref, visible=True, section_visible=True):
    return json.dumps([{
        "name": "Quick Links",
        "visible_to_players": section_visible,
        "links": [{
            "label": label, "icon": "🔗", "target_type": target_type,
            "target_ref": str(target_ref), "visible_to_players": visible,
        }],
    }])


def test_home_edit_form_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/worlds/{seed.world_a.id}/home/edit")
    assert r.status_code == 403


def test_home_edit_form_renders_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/worlds/{seed.world_a.id}/home/edit")
    assert r.status_code == 200
    assert "Quick Link Sections" in r.text


def test_save_round_trip_persists_blurb_and_link(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    eid = _make_entity(seed.world_a.id)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={"home_welcome_md": "**Session 12** is next Tuesday.", "home_sections_json": _sections_payload(target_ref=eid)},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.home_welcome_md == "**Session 12** is next Tuesday."
        sections = json.loads(w.home_sections_json)
        assert len(sections) == 1
        assert sections[0]["links"][0]["target_ref"] == str(eid)
        assert sections[0]["links"][0]["label"] == "My Link"
    finally:
        db.close()


def test_malformed_sections_json_falls_back_to_empty(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={"home_welcome_md": "", "home_sections_json": "not json{{{"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.home_sections_json) == []
    finally:
        db.close()


def test_javascript_uri_link_is_dropped_on_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = json.dumps([{
        "name": "Section", "visible_to_players": True,
        "links": [{"label": "Bad", "icon": "", "target_type": "url",
                   "target_ref": "javascript:alert(1)", "visible_to_players": True}],
    }])
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={"home_welcome_md": "", "home_sections_json": payload},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        sections = json.loads(w.home_sections_json)
        assert sections[0]["links"] == []
    finally:
        db.close()


def test_home_page_renders_welcome_blurb_as_markdown(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_welcome_md = "**bold announcement**"
        db.commit()
    finally:
        db.close()
    r = client.get("/")
    assert r.status_code == 200
    assert "<strong>bold announcement</strong>" in r.text


def test_home_page_omits_quick_links_section_when_empty(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "quick-links-section" not in r.text


def test_deleted_entity_link_disappears_from_home_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    eid = _make_entity(seed.world_a.id, name="Ephemeral NPC")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = _sections_payload(label="Ephemeral NPC Link", target_ref=eid)
        db.commit()
    finally:
        db.close()

    r = client.get("/")
    assert "Ephemeral NPC Link" in r.text

    db = SessionLocal()
    try:
        db.query(Entity).filter(Entity.id == eid).delete()
        db.commit()
    finally:
        db.close()

    r = client.get("/")
    assert "Ephemeral NPC Link" not in r.text


def test_link_visibility_hides_from_players_not_gm(client, seed):
    eid = _make_entity(seed.world_a.id, name="Hidden Link Target")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = _sections_payload(label="Secret GM Link", target_ref=eid, visible=False)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Secret GM Link" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Secret GM Link" not in r.text


def test_cross_world_link_resolves_to_nothing(client, seed):
    """A link stored in world_a's home_sections_json pointing at an entity
    that actually belongs to world_b must not resolve or leak anything about
    world_b's content — mirrors the world-membership gate every other
    entity-touching route already enforces."""
    other_world_entity_id = _make_entity(seed.world_b.id, name="World B Secret")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = _sections_payload(label="Cross World Link", target_ref=other_world_entity_id)
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Cross World Link" not in r.text


def test_kind_link_resolves_to_kind_page(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = json.dumps([{
            "name": "Browse", "visible_to_players": True,
            "links": [{"label": "All Characters", "icon": "👤", "target_type": "kind",
                       "target_ref": "character", "visible_to_players": True}],
        }])
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'href="/kind/character"' in r.text
    assert "All Characters" in r.text


def test_invalid_kind_link_is_dropped_on_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = json.dumps([{
        "name": "Section", "visible_to_players": True,
        "links": [{"label": "Bad Kind", "icon": "", "target_type": "kind",
                   "target_ref": "not-a-real-kind", "visible_to_players": True}],
    }])
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={"home_welcome_md": "", "home_sections_json": payload},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.home_sections_json)[0]["links"] == []
    finally:
        db.close()
