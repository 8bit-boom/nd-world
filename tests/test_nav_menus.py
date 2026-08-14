"""Tests for GM-manageable top-nav dropdown menus (app/nav_menus.py's
catalog/default/sanitizer/resolver, app/routers/nav_menus_admin.py's save
endpoint, and the Navigation tab on /settings): grouping the GM-only tool
pages (Boards, Quests, AI Chat, ...) into dropdown menus, editable per
world, defaulting to the shipped Tools/AI Tools grouping until a GM
explicitly customizes it."""
import json

from app.database import SessionLocal
from app.models import World
from app.nav_menus import build_catalog, load_nav_menus, resolve_nav_menus, sanitize_nav_menus

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_nav_menus(world_id, menus):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.nav_menus_json = json.dumps(menus)
        db.commit()
    finally:
        db.close()


def _get_nav_menus_json(world_id):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return w.nav_menus_json
    finally:
        db.close()


# ── Default grouping (unset world) ──────────────────────────────────────────

def test_fresh_world_defaults_to_tools_and_ai_tools_grouping(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "🎯 Tools" in r.text
    assert "🤖 AI Tools" in r.text
    assert 'data-ql-ref="/boards"' in r.text
    assert 'data-ql-ref="/ai"' in r.text


def test_load_nav_menus_null_column_falls_back_to_default(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.nav_menus_json is None  # never customized
        menus = load_nav_menus(w)
    finally:
        db.close()
    assert [m["id"] for m in menus] == ["menu_tools", "menu_ai_tools"]

    # No world at all (e.g. a GM logged in with nothing active) hits the
    # same "never customized" path as a real, un-customized world.
    menus, ungrouped = resolve_nav_menus(None, dreamlands_enabled=False, king_in_yellow_enabled=False, is_gm=True)
    assert [m["id"] for m in menus] == ["menu_tools", "menu_ai_tools"]


def test_explicit_empty_list_is_honored_as_all_flat(client, seed):
    """An explicitly-saved [] (GM wants everything flat) must NOT be
    treated the same as an unset/NULL column (which means 'use the
    default')."""
    _set_nav_menus(seed.world_a.id, [])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "🎯 Tools" not in r.text
    assert "🤖 AI Tools" not in r.text
    assert 'data-ql-ref="/boards"' in r.text  # still reachable, just flat now
    assert '"nav-kind ' in r.text or 'class="nav-kind' in r.text


# ── Rendering: custom grouping ───────────────────────────────────────────────

def test_custom_menu_moves_item_out_of_default_grouping(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_tools", "label": "Tools", "icon": "🎯",
         "item_ids": ["boards", "tables", "combat", "parties", "sessions",
                      "facts", "calendar", "images", "import", "export"]},
        {"id": "menu_session", "label": "Session Tools", "icon": "🗂", "item_ids": ["quests"]},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Session Tools" in r.text
    assert r.text.count('data-ql-ref="/quests"') == 1


def test_dreamlands_item_hidden_entirely_when_setting_disabled(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_tools", "label": "Tools", "icon": "🎯", "item_ids": ["dreamlands", "boards"]},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/dreamlands"' not in r.text  # setting off by default


# ── Access control ───────────────────────────────────────────────────────────

def test_nav_menus_save_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={"nav_menus_json": "[]"})
    assert r.status_code == 403


def test_settings_navigation_tab_hidden_content_not_leaked_to_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/settings?tab=navigation")
    assert r.status_code == 403


# ── Save endpoint + sanitizer ────────────────────────────────────────────────

def test_save_round_trips_a_custom_menu(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = json.dumps([{"id": "menu_x", "label": "My Menu", "icon": "🗂", "item_ids": ["ai", "editor"]}])
    r = client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={"nav_menus_json": payload}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?tab=navigation"

    saved = json.loads(_get_nav_menus_json(seed.world_a.id))
    assert len(saved) == 1
    assert saved[0]["label"] == "My Menu"
    assert saved[0]["item_ids"] == ["ai", "editor"]


def test_save_drops_invalid_item_ids(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = json.dumps([{"id": "menu_x", "label": "My Menu", "icon": "🗂",
                            "item_ids": ["ai", "not_a_real_item", "editor"]}])
    client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={"nav_menus_json": payload})
    saved = json.loads(_get_nav_menus_json(seed.world_a.id))
    assert saved[0]["item_ids"] == ["ai", "editor"]


def test_save_drops_menu_with_empty_label(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = json.dumps([
        {"id": "menu_x", "label": "", "icon": "🗂", "item_ids": ["ai"]},
        {"id": "menu_y", "label": "Kept", "icon": "🗂", "item_ids": ["editor"]},
    ])
    client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={"nav_menus_json": payload})
    saved = json.loads(_get_nav_menus_json(seed.world_a.id))
    assert len(saved) == 1
    assert saved[0]["label"] == "Kept"


def test_save_caps_max_menus(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = json.dumps([{"id": f"menu_{i}", "label": f"Menu {i}", "icon": "🗂", "item_ids": []} for i in range(20)])
    client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={"nav_menus_json": payload})
    saved = json.loads(_get_nav_menus_json(seed.world_a.id))
    assert len(saved) == 12  # MAX_NAV_MENUS


def test_save_keeps_each_item_in_at_most_one_menu(client, seed):
    """A single item id appearing in two menus in the raw payload (can't
    happen through the Settings UI's per-item single-select, but the
    endpoint shouldn't trust the client) — first claim wins."""
    payload = json.dumps([
        {"id": "menu_a", "label": "A", "icon": "🗂", "item_ids": ["ai"]},
        {"id": "menu_b", "label": "B", "icon": "🗂", "item_ids": ["ai", "editor"]},
    ])
    sanitized = sanitize_nav_menus(payload)
    all_ids = [iid for m in sanitized for iid in m["item_ids"]]
    assert all_ids.count("ai") == 1
    assert sanitized[0]["item_ids"] == ["ai"]
    assert sanitized[1]["item_ids"] == ["editor"]


def test_deleting_a_menu_falls_items_back_to_flat_tabs(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_x", "label": "My Menu", "icon": "🗂", "item_ids": ["ai", "editor"]},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "My Menu" in r.text

    # Save an empty list — the menu is gone, but the routes/links remain reachable.
    client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={"nav_menus_json": "[]"})
    r = client.get("/")
    assert "My Menu" not in r.text
    assert 'data-ql-ref="/ai"' in r.text
    assert 'data-ql-ref="/editor"' in r.text


# ── Cross-world isolation ────────────────────────────────────────────────────

def test_nav_menus_are_world_scoped(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_a_only", "label": "World A Only", "icon": "🗂", "item_ids": ["ai"]},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/")
    assert "World A Only" not in r.text


# ── Settings > Navigation tab ────────────────────────────────────────────────

def test_navigation_tab_renders_catalog_and_current_menus(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/settings?tab=navigation")
    assert r.status_code == 200
    assert 'id="tab-navigation" class="settings-tab active"' in r.text or "tab-navigation" in r.text
    assert "nav-menus-list" in r.text
    for item in build_catalog(seed.world_a):
        assert item["id"] in r.text


# ── Extended catalog: kinds + always-flat player-visible tabs ───────────────
# The Navigation tab originally only covered the 16 GM-only tool pages that
# used to live in the hardcoded Tools/AI Tools dropdowns — a GM reported
# "a lot of top tabs are missing (Characters, Locations, Creatures...)",
# since the per-world entity kinds and the other always-present tabs (Maps,
# Race/Profession Catalog, Chronicler, Session Log, Rules, Player
# Characters, Android App) weren't manageable at all. build_catalog(world)
# now includes all of those too, each tagged gm_only so a player never sees
# more than they already could.

def test_catalog_includes_kinds_and_flat_tabs_not_just_gm_tools(client, seed):
    catalog_by_id = {item["id"]: item for item in build_catalog(seed.world_a)}
    for kind_id in ("character", "location", "organization", "creature", "race", "profession"):
        item = catalog_by_id[f"kind_{kind_id}"]
        assert item["gm_only"] is False
        assert item["href"] == f"/kind/{kind_id}"
    for item_id, href in (
        ("maps", "/maps"), ("races", "/races"), ("professions", "/professions"),
        ("chronicler", "/chronicler"), ("session_log", "/session-log"), ("rules", "/rules"),
        ("characters", "/characters"), ("androidapp", "/androidapp"),
    ):
        assert catalog_by_id[item_id]["gm_only"] is False
        assert catalog_by_id[item_id]["href"] == href
    # The original 16 GM-tool items are still gm_only.
    assert catalog_by_id["boards"]["gm_only"] is True
    assert catalog_by_id["ai"]["gm_only"] is True


def test_grouping_a_player_visible_item_shows_it_to_players_too(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_pc", "label": "Party Stuff", "icon": "🛡", "item_ids": ["characters"]},
    ])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "Party Stuff" in r.text
    assert 'data-ql-ref="/characters"' in r.text


def test_menu_mixing_gm_only_and_player_items_filters_per_viewer(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_mix", "label": "Mixed", "icon": "🗂", "item_ids": ["boards", "characters"]},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Mixed" in r.text
    assert 'data-ql-ref="/boards"' in r.text
    assert 'data-ql-ref="/characters"' in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/")
    assert "Mixed" in r.text  # menu survives — it still has a visible item
    assert 'data-ql-ref="/boards"' not in r.text  # GM-only item dropped
    assert 'data-ql-ref="/characters"' in r.text


def test_kind_item_keeps_kind_drag_type_when_grouped(client, seed):
    _set_nav_menus(seed.world_a.id, [
        {"id": "menu_lore", "label": "Lore", "icon": "📚", "item_ids": ["kind_character"]},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Lore" in r.text
    assert 'data-ql-type="kind" data-ql-ref="character"' in r.text
