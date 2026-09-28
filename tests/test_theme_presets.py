"""Built-in genre theme presets (app/theme_presets.py) and the route that
applies one, POST /worlds/{world_id}/theme/preset — the one-click sibling
of the existing /theme/import file-upload flow, sharing the same
_sanitize_theme() validation and World.accent/theme_json storage shape
(see tests/test_world_theme.py for that route's own coverage).
"""
import json

from app.database import SessionLocal
from app.main import _sanitize_theme
from app.models import World
from app.theme_presets import THEME_PRESETS

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── Every preset must be a valid, complete theme object ─────────────────────

def test_every_preset_round_trips_through_sanitize_theme():
    """A preset that fails this has a typo somewhere (a bad hex digit, a
    font string with a disallowed character, an unrecognized hero_graphic
    value, ...) that _sanitize_theme() would otherwise silently drop —
    catch it here rather than shipping a genre preset that quietly loses
    part of itself the moment a GM clicks it."""
    for key, preset in THEME_PRESETS.items():
        cleaned = _sanitize_theme(preset)
        assert cleaned == preset, f"preset {key!r} lost fields: {set(preset) - set(cleaned)}"


def test_every_preset_has_a_name():
    for key, preset in THEME_PRESETS.items():
        assert preset.get("name"), f"preset {key!r} has no name"


# ── POST /worlds/{id}/theme/preset ──────────────────────────────────────────

def test_theme_preset_applies_and_sets_accent_separately(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/theme/preset",
        data={"preset": "lovecraftian"}, follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.accent == THEME_PRESETS["lovecraftian"]["accent"]
        stored = json.loads(w.theme_json)
        assert stored["name"] == THEME_PRESETS["lovecraftian"]["name"]
        assert stored["bg"] == THEME_PRESETS["lovecraftian"]["bg"]
        # accent lives on World.accent, not duplicated inside theme_json —
        # same contract as a manually-imported theme file.
        assert "accent" not in stored
    finally:
        db.close()


def test_theme_preset_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/theme/preset", data={"preset": "gothic"})
    assert r.status_code == 403


def test_theme_preset_unknown_key_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/theme/preset", data={"preset": "not-a-real-preset"})
    assert r.status_code == 404


def test_theme_preset_unknown_world_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/worlds/999999/theme/preset", data={"preset": "gothic"})
    assert r.status_code == 404


def test_theme_preset_overwrites_a_previously_imported_theme(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/theme/preset", data={"preset": "sci_fi"})
    client.post(f"/worlds/{seed.world_a.id}/theme/preset", data={"preset": "victorian"}, follow_redirects=False)

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        stored = json.loads(w.theme_json)
        assert stored["name"] == THEME_PRESETS["victorian"]["name"]
        assert w.accent == THEME_PRESETS["victorian"]["accent"]
    finally:
        db.close()


def test_page_renders_a_preset_theme_css_overrides(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/worlds/{seed.world_a.id}/theme/preset", data={"preset": "pirate"})
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/")
    assert r.status_code == 200
    assert f"--bg: {THEME_PRESETS['pirate']['bg']} !important" in r.text
    assert f"--neon: {THEME_PRESETS['pirate']['accent']} !important" in r.text
