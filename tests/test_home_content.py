"""Tests for GM-customizable home page content (app/routers/home_content.py +
app/main.py's _resolve_home_sections/_resolve_home_link_href): a welcome
blurb plus GM-defined tabs/sections of curated Quick Links, plus the hero
title/subtitle/background-image and pinned-dashboard-tile customization
covered further below.
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


def test_home_page_shows_empty_state_dropzone_for_gm_only(client, seed):
    """With zero Quick Link sections configured, a GM sees the drag-a-tab-
    here empty-state placeholder (so there's a drop target for the
    drag-and-drop feature); a player — who never gets a drop affordance —
    sees nothing there at all."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="ql-empty-dropzone"' in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
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
    assert f'href="/kind/character?w={seed.world_a.slug}"' in r.text
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


# ── Drag-a-nav-tab-onto-the-home-page quick-add ─────────────────────────────


def test_quick_link_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": None, "label": "Characters", "target_type": "kind", "target_ref": "character"},
    )
    assert r.status_code == 403


def test_quick_link_appends_to_given_section(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = json.dumps([
            {"name": "First", "visible_to_players": True, "links": []},
            {"name": "Second", "visible_to_players": True, "links": []},
        ])
        db.commit()
    finally:
        db.close()

    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": 1, "label": "Characters", "target_type": "kind", "target_ref": "character"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "section_index": 1}

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        sections = json.loads(w.home_sections_json)
        assert sections[0]["links"] == []
        assert sections[1]["links"][0]["target_ref"] == "character"
    finally:
        db.close()


def test_quick_link_missing_section_index_falls_back_to_first_or_creates_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    # No sections at all yet — should create a default "Quick Links" section.
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": None, "label": "Race Catalog", "target_type": "url", "target_ref": "/races"},
    )
    assert r.status_code == 200
    assert r.json()["section_index"] == 0

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        sections = json.loads(w.home_sections_json)
        assert len(sections) == 1
        assert sections[0]["links"][0]["target_ref"] == "/races"
    finally:
        db.close()

    # An out-of-range index falls back to section 0 rather than erroring.
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": 99, "label": "Boards", "target_type": "url", "target_ref": "/boards"},
    )
    assert r.status_code == 200
    assert r.json()["section_index"] == 0


def test_quick_link_rejects_full_section(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    full_links = [
        {"label": f"L{i}", "icon": "", "target_type": "url", "target_ref": f"/l{i}", "visible_to_players": True}
        for i in range(50)
    ]
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = json.dumps([{"name": "Full", "visible_to_players": True, "links": full_links}])
        db.commit()
    finally:
        db.close()

    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": 0, "label": "One More", "target_type": "url", "target_ref": "/one-more"},
    )
    assert r.status_code == 400


def test_quick_link_rejects_invalid_target_type(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/quick-link",
        json={"section_index": None, "label": "Bad", "target_type": "not-a-real-type", "target_ref": "whatever"},
    )
    assert r.status_code == 400

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.home_sections_json or "[]") == []
    finally:
        db.close()


def test_empty_named_section_visible_to_gm_but_not_player(client, seed):
    """A section a GM has named via the edit page but hasn't filled in yet
    should still show up in the GM's own resolved home_sections (so it's a
    usable drop target for the drag-a-nav-tab feature), but stays absent
    for players — nothing useful to show them for an empty section."""
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_sections_json = json.dumps([{"name": "Empty For Now", "visible_to_players": True, "links": []}])
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Empty For Now" in r.text
    assert 'id="ql-tab-0"' in r.text or 'id="ql-pane-0"' in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Empty For Now" not in r.text


# ── Hero title / subtitle / background image ────────────────────────────────


def test_home_page_falls_back_to_default_title_when_unset(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "WORLD" in r.text and "DATABASE" in r.text
    assert "Neon &amp; Dragons worldbuilding codex" in r.text or "Neon & Dragons worldbuilding codex" in r.text


def test_home_page_renders_custom_title_and_subtitle(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_title = "SABLE ROW ARCHIVE"
        w.home_subtitle = "a custom worldbuilding codex"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "SABLE ROW ARCHIVE" in r.text
    assert "a custom worldbuilding codex" in r.text
    assert "hero-custom" in r.text


def test_home_title_is_html_escaped_not_rendered_raw(client, seed):
    """home_title/home_subtitle are plain user text rendered inside the
    hero's <h1>/<p> — unlike home_welcome_md they never go through the `md`
    filter or |safe, so a GM typing a literal '<script>' must come back
    escaped, not as a live tag."""
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_title = "<script>alert(1)</script>"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_home_edit_save_persists_title_subtitle(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]", "home_pinned_tiles_json": "[]",
            "home_title": "  New Title  ", "home_subtitle": "New Subtitle",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.home_title == "New Title"
        assert w.home_subtitle == "New Subtitle"
    finally:
        db.close()


def test_home_edit_blank_title_clears_to_default(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_title = "Old Title"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={"home_welcome_md": "", "home_sections_json": "[]", "home_pinned_tiles_json": "[]", "home_title": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.home_title is None
    finally:
        db.close()


def test_home_background_url_renders_in_hero_style(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_background_url = "/uploads/home/test-bg.avif"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "background-image" in r.text
    assert "/uploads/home/test-bg.avif" in r.text


def test_home_background_pasted_relative_path_is_accepted(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]", "home_pinned_tiles_json": "[]",
            "home_background_url": "/uploads/home/pasted.png",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.home_background_url == "/uploads/home/pasted.png"
    finally:
        db.close()


def test_home_background_javascript_uri_is_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]", "home_pinned_tiles_json": "[]",
            "home_background_url": "javascript:alert(1)",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.home_background_url is None
    finally:
        db.close()


def test_home_background_remove_checkbox_clears_it(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_background_url = "/uploads/home/old.png"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]", "home_pinned_tiles_json": "[]",
            "remove_home_background": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.home_background_url is None
    finally:
        db.close()


def test_home_edit_form_is_multipart_capable_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={"home_welcome_md": "", "home_sections_json": "[]", "home_pinned_tiles_json": "[]"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# ── Pinned Dashboard Tiles (drag-a-nav-tab-onto-the-dashboard) ──────────────


def _pinned_payload(*, label="My Tile", target_type="url", target_ref="/maps", visible=True):
    return json.dumps([{
        "label": label, "icon": "", "target_type": target_type,
        "target_ref": str(target_ref), "visible_to_players": visible,
    }])


def test_pinned_tile_edit_save_round_trips(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]",
            "home_pinned_tiles_json": _pinned_payload(label="Maps Tile", target_ref="/maps"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        tiles = json.loads(w.home_pinned_tiles_json)
        assert len(tiles) == 1
        assert tiles[0]["label"] == "Maps Tile"
        assert tiles[0]["target_ref"] == "/maps"
    finally:
        db.close()


def test_pinned_tile_javascript_uri_dropped_on_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]",
            "home_pinned_tiles_json": _pinned_payload(target_type="url", target_ref="javascript:alert(1)"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.home_pinned_tiles_json) == []
    finally:
        db.close()


def test_pinned_tile_invalid_kind_dropped_on_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/home/edit",
        data={
            "home_welcome_md": "", "home_sections_json": "[]",
            "home_pinned_tiles_json": _pinned_payload(target_type="kind", target_ref="not-a-real-kind"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.home_pinned_tiles_json) == []
    finally:
        db.close()


def test_pinned_tile_api_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/pinned-tile",
        json={"label": "Maps", "target_type": "url", "target_ref": "/maps"},
    )
    assert r.status_code == 403


def test_pinned_tile_api_appends_and_renders_on_dashboard(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/pinned-tile",
        json={"label": "Chronicler", "target_type": "url", "target_ref": "/chronicler"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "dash-card-pinned" in r.text
    assert "Chronicler" in r.text
    assert f'href="/chronicler?w={seed.world_a.slug}"' in r.text


def test_pinned_tile_kind_type_shows_a_count(client, seed):
    _make_entity(seed.world_a.id, name="Counted One")
    _make_entity(seed.world_a.id, name="Counted Two")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/pinned-tile",
        json={"label": "All Characters", "target_type": "kind", "target_ref": "character"},
    )
    assert r.status_code == 200

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "All Characters" in r.text
    # Two seeded characters above; exact count assertion via the pinned
    # card's markup — dash-count only prints for kind-type tiles.
    idx = r.text.index("All Characters")
    surrounding = r.text[max(0, idx - 400):idx]
    assert "dash-count" in surrounding


def test_pinned_tile_visibility_hides_from_players_not_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/pinned-tile",
        json={"label": "Secret Tile", "target_type": "url", "target_ref": "/rules"},
    )
    assert r.status_code == 200
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        tiles = json.loads(w.home_pinned_tiles_json)
        tiles[0]["visible_to_players"] = False
        w.home_pinned_tiles_json = json.dumps(tiles)
        db.commit()
    finally:
        db.close()

    client.cookies.set("active_world", seed.world_a.slug)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Secret Tile" in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Secret Tile" not in r.text


def test_pinned_tile_rejects_full_dashboard(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    full_tiles = [
        {"label": f"T{i}", "icon": "", "target_type": "url", "target_ref": f"/t{i}", "visible_to_players": True}
        for i in range(24)
    ]
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_pinned_tiles_json = json.dumps(full_tiles)
        db.commit()
    finally:
        db.close()

    r = client.post(
        f"/api/worlds/{seed.world_a.id}/home/pinned-tile",
        json={"label": "One More", "target_type": "url", "target_ref": "/one-more"},
    )
    assert r.status_code == 400


def test_cross_world_pinned_entity_tile_resolves_to_nothing(client, seed):
    other_world_entity_id = _make_entity(seed.world_b.id, name="World B Secret Pin")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.home_pinned_tiles_json = json.dumps([{
            "label": "Cross World Pin", "icon": "", "target_type": "entity",
            "target_ref": str(other_world_entity_id), "visible_to_players": True,
        }])
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Cross World Pin" not in r.text
