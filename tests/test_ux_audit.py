"""Tests for the UX-audit simplification batch: the consolidated Export &
Backup hub (replacing scattered nav/world-card export buttons), the
light-touch nav regroup (Boards into Tools, a new AI Tools dropdown), the
empty-world onboarding hint, and the context-aware "+ New" nav button.
Nothing here removes functionality — every underlying route still works,
just reorganized/labeled more clearly.

Also covers two follow-up visual-bug fixes reported from real screenshots
(folder names in the entity list sidebar getting clipped past readability,
and the AI page's RAG Entities/Notes slider value labels getting hard-clipped
by an overflow:hidden ancestor because the <input type="range"> couldn't
shrink below its intrinsic width as a flex child) plus the new UI-scale
preference.
"""
from pathlib import Path

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_STYLE_CSS = (Path(__file__).parent.parent / "static" / "style.css").read_text()


# ── Export & Backup hub ──────────────────────────────────────────────────────

def test_export_hub_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/export").status_code == 403


def test_export_hub_lists_all_options_for_active_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export")
    assert r.status_code == 200
    assert "/admin/backup.zip" in r.text
    assert "/export/book.zip" in r.text
    assert f"/worlds/{seed.world_a.id}/export" in r.text
    assert f"/worlds/{seed.world_a.id}/export/split" in r.text
    assert f"/worlds/{seed.world_a.id}/import" in r.text


def test_book_export_moved_to_book_zip_path(client, seed):
    """The old bare GET /export (direct zip download) now lives at
    /export/book.zip — /export itself is the new hub page."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/export/book.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"


def test_world_import_redirects_to_export_hub_with_notice(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    payload = {"entities": [{"name": "Imported NPC", "kind": "character"}]}
    import io, json
    f = io.BytesIO(json.dumps(payload).encode())
    r = client.post(
        f"/worlds/{seed.world_a.id}/import",
        files={"file": ("world.json", f, "application/json")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/export?w={seed.world_a.slug}&imported=1&updated=0"

    r2 = client.get(r.headers["location"])
    assert "Import complete" in r2.text
    assert "1 created" in r2.text


def test_worlds_page_has_single_export_link_not_scattered_buttons(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/worlds")
    assert r.status_code == 200
    assert f"/export?w={seed.world_a.slug}" in r.text
    # The old per-card duplicated controls are gone.
    assert f"/worlds/{seed.world_a.id}/export/split" not in r.text
    assert "Export (separate files)" not in r.text
    assert f"/worlds/{seed.world_a.id}/import" not in r.text


# ── Nav regroup (light touch) ────────────────────────────────────────────────

def test_gm_nav_still_reaches_every_relocated_page(client, seed):
    """Boards/AI/Image Studio/Content Editor once lived in click-to-open
    Tools/AI Tools dropdown menus; those were later flattened into ordinary
    top-level tabs per an explicit UX request (dropdowns are harder to scan
    than a flat row of tabs). This just confirms the links — and thus the
    underlying pages — are still present in the rendered nav regardless of
    how they're grouped/labeled."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    for href in ("/boards", "/ai", "/imagestudio", "/editor", "/export", "/settings"):
        assert f'data-ql-ref="{href}"' in r.text, f"{href} missing from nav"
    # The routes themselves are unaffected regardless of nav placement.
    assert client.get("/boards").status_code == 200


def test_player_nav_unaffected_by_gm_only_regroup(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    for href in ("/boards", "/ai", "/imagestudio", "/editor", "/export", "/settings"):
        assert f'data-ql-ref="{href}"' not in r.text, f"{href} unexpectedly visible to a player"


# ── Empty-world onboarding hint ──────────────────────────────────────────────

def test_onboarding_hint_shown_for_gm_on_empty_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Get started with World A" in r.text


def test_onboarding_hint_hidden_once_world_has_content(client, seed):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Someone"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Get started with World A" not in r.text


def test_onboarding_hint_hidden_for_players(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "Get started with World A" not in r.text


# ── Context-aware "+ New" nav button ─────────────────────────────────────────

def test_new_button_defaults_to_character_on_home(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert "/new?kind=character" in r.text


def test_new_button_matches_current_kind_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/location")
    assert r.status_code == 200
    assert "/new?kind=location" in r.text


# ── Folder-sidebar truncation (screenshot: "Player C..." instead of the
#    full name) — was a fixed 200px sidebar too narrow for realistic folder
#    names once the count badge ate its share of the space. ─────────────────

def test_folder_sidebar_widened_and_scales_with_root_font_size():
    """Locks in the regression: the sidebar must not go back to a fixed px
    width, since px widths don't grow with the new UI-scale setting (or
    browser zoom) while their rem-sized contents do — which is what caused
    the clipping in the first place."""
    assert ".folder-sidebar {" in _STYLE_CSS
    block = _STYLE_CSS.split(".folder-sidebar {", 1)[1].split("}", 1)[0]
    assert "200px" not in block
    assert "rem" in block


def test_folder_names_render_in_full_in_the_dom(client, seed):
    """The ellipsis is CSS-only truncation for pathological cases — the
    actual folder name must always be the real, untruncated text in the
    rendered HTML (a screen reader / copy-paste / browser search must see
    "Player Characters", not "Player C...")."""
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Bob", folder="Player Characters"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character")
    assert r.status_code == 200
    assert "Player Characters" in r.text


# ── RAG slider value clipping (screenshot: "20" showing as "2") ─────────────

def test_rag_sliders_can_shrink_to_make_room_for_their_value_label(client, seed):
    """The <input type="range"> siblings must have min-width:0 so they can
    actually shrink as flex children — without it a range input's intrinsic
    width can push the value span past the sidebar's right edge, where
    .ai-sidebar's overflow-x:hidden silently clips it instead of the ellipsis
    CSS visibly kicking in (there's no ellipsis on these — a hard clip)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    for input_id in ("ctx-limit", "notes-limit", "ctx-limit-mob", "notes-limit-mob"):
        needle = f'id="{input_id}"'
        idx = r.text.index(needle)
        # The style attribute immediately follows the id on these inputs.
        tag_end = r.text.index(">", idx)
        assert "min-width:0" in r.text[idx:tag_end], f"#{input_id} can't shrink — will clip its value label"
    for val_id in ("ctx-limit-val", "notes-limit-val", "ctx-limit-mob-val", "notes-limit-mob-val"):
        needle = f'id="{val_id}"'
        idx = r.text.index(needle)
        tag_end = r.text.index(">", idx)
        assert "min-width:2.4rem" in r.text[idx:tag_end]


def test_ai_sidebar_width_scales_with_root_font_size():
    with open(Path(__file__).parent.parent / "app" / "templates" / "ai_chat.html") as f:
        content = f.read()
    block = content.split(".ai-sidebar {", 1)[1].split("}", 1)[0]
    assert "260px" not in block
    assert "rem" in block


# ── UI scale preference ──────────────────────────────────────────────────────

def test_ui_scale_css_rules_present():
    for pct in ("90", "110", "125", "150"):
        assert f'html[data-scale="{pct}"]' in _STYLE_CSS


def test_ui_scale_picker_available_to_gm_and_players(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'id="scale-select"' in r.text

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r2 = client.get("/")
    assert 'id="scale-select"' in r2.text


def test_ui_scale_control_also_in_settings_options_tab(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'id="ui-scale-settings"' in r.text
    assert 'class="nd-ui-scale-select"' in r.text
