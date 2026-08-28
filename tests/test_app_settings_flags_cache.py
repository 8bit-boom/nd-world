"""Tests for app.database.get_app_settings_flags_cached (plan item Speed
4.5) — the read-only, short-TTL cache over the two AppSettings booleans
app.templating._kinds_context_processor reads on every single templated
page render. Mirrors tests/test_gallery.py's own spotlight-cache test in
shape: pins the actual caching mechanism (a direct DB write bypassing the
real save route must NOT be visible until invalidated), not just its
externally-visible effect (already covered by tests/test_lore_extras.py).
"""
from app.database import SessionLocal, clear_app_settings_flags_cache, get_app_settings_flags_cached
from app.models import AppSettings

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _get_settings_row():
    db = SessionLocal()
    try:
        s = db.query(AppSettings).first()
        if not s:
            s = AppSettings(id=1)
            db.add(s)
            db.commit()
            db.refresh(s)
        return s
    finally:
        db.close()


def test_cache_serves_a_hit_within_ttl(client, seed):
    db = SessionLocal()
    try:
        flags1 = get_app_settings_flags_cached(db)
        # Bypass the cache entirely, write directly to the DB — the cache
        # should still return the stale value it already has.
        s = db.query(AppSettings).first()
        s.dreamlands_enabled = not flags1["dreamlands_enabled"]
        db.commit()
        flags2 = get_app_settings_flags_cached(db)
        assert flags2 == flags1
    finally:
        db.close()


def test_clear_cache_forces_a_fresh_read(client, seed):
    db = SessionLocal()
    try:
        flags1 = get_app_settings_flags_cached(db)
        s = db.query(AppSettings).first()
        s.dreamlands_enabled = not flags1["dreamlands_enabled"]
        db.commit()
        clear_app_settings_flags_cache()
        flags2 = get_app_settings_flags_cached(db)
        assert flags2["dreamlands_enabled"] == (not flags1["dreamlands_enabled"])
    finally:
        db.close()


def test_settings_system_save_route_invalidates_the_cache(client, seed):
    """The real end-to-end path: a GM saves Settings > System with the
    optional-extras checkboxes on, and the very next page render must
    already reflect it — not up to _APP_SETTINGS_FLAGS_CACHE_TTL seconds
    later."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    page = client.get("/")
    assert "/dreamlands" not in page.text

    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "android_emulator_url": "", "editor_external_url": "", "whisper_url": "",
        "dreamlands_enabled": "on", "king_in_yellow_enabled": "on",
        "ollama_keep_alive": "", "ollama_use_mmap": "",
    })
    assert r.status_code in (200, 303)

    page2 = client.get("/")
    assert "/dreamlands" in page2.text
    assert "/king-in-yellow" in page2.text
