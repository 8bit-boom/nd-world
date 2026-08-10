"""Tests for the UX-audit simplification batch: the consolidated Export &
Backup hub (replacing scattered nav/world-card export buttons), the
light-touch nav regroup (Boards into Tools, a new AI Tools dropdown), the
empty-world onboarding hint, and the context-aware "+ New" nav button.
Nothing here removes functionality — every underlying route still works,
just reorganized/labeled more clearly.
"""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


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
    """Boards moved into the Tools dropdown, and AI/Image Studio/Content
    Editor moved into a new AI Tools dropdown — confirm the links (and thus
    the underlying pages) are still present in the rendered nav, just
    regrouped, not removed."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    for href in ("/boards", "/ai", "/imagestudio", "/editor", "/export", "/settings"):
        assert f'data-ql-ref="{href}"' in r.text, f"{href} missing from nav"
    assert "🎯 Tools" in r.text
    assert "🤖 AI Tools" in r.text
    # The routes themselves are unaffected regardless of nav placement.
    assert client.get("/boards").status_code == 200


def test_player_nav_unaffected_by_gm_only_regroup(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "🎯 Tools" not in r.text
    assert "🤖 AI Tools" not in r.text


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
