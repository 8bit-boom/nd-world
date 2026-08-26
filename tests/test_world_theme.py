"""Tests for per-world visual themes (World.theme_json): the
_sanitize_theme() whitelist in app/main.py, the import/clear routes it
guards, and base.html's rendering of the resulting CSS variable overrides.

See docs/world-theme-gothic-moonlight.json for a real example file (derived
from a GM-supplied HTML rules document's own <style> block) and
World.theme_json's docstring in app/models.py for the recognized shape.
"""
import io
import json
from pathlib import Path

from app.database import SessionLocal
from app.main import _sanitize_theme
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_EXAMPLE_THEME_PATH = Path(__file__).parent.parent / "docs" / "world-theme-gothic-moonlight.json"


# ── _sanitize_theme() — pure validation logic ───────────────────────────────

def test_example_theme_file_round_trips_with_every_field_intact():
    """The actual shipped example (docs/world-theme-gothic-moonlight.json)
    must survive _sanitize_theme() unchanged, field for field — a
    regression here means a real GM-facing example file silently loses
    part of its own theme on import."""
    data = json.loads(_EXAMPLE_THEME_PATH.read_text())
    cleaned = _sanitize_theme(data)
    assert cleaned == data

def test_sanitize_theme_accepts_valid_hex_colors():
    cleaned = _sanitize_theme({"accent": "#c9a25c", "bg": "#0b0a10", "neon2": "#b3455a"})
    assert cleaned == {"accent": "#c9a25c", "bg": "#0b0a10", "neon2": "#b3455a"}


def test_sanitize_theme_accepts_3_digit_hex():
    cleaned = _sanitize_theme({"bg": "#0af"})
    assert cleaned == {"bg": "#0af"}


def test_sanitize_theme_drops_invalid_color_values():
    # Not a plain hex color — CSS injection attempt via bg, ignored rather
    # than propagated into base.html's <style> block.
    cleaned = _sanitize_theme({"bg": "red} body{display:none", "border": "not-a-color"})
    assert cleaned == {}


def test_sanitize_theme_accepts_valid_font_strings():
    cleaned = _sanitize_theme({"font": "'EB Garamond', Georgia, serif", "font_heading": "'Cinzel', serif"})
    assert cleaned == {"font": "'EB Garamond', Georgia, serif", "font_heading": "'Cinzel', serif"}


def test_sanitize_theme_drops_font_with_disallowed_characters():
    cleaned = _sanitize_theme({"font": "serif; } body { background: url(javascript:alert(1))"})
    assert cleaned == {}


def test_sanitize_theme_accepts_google_fonts_url():
    url = "https://fonts.googleapis.com/css2?family=Cinzel:wght@700&display=swap"
    cleaned = _sanitize_theme({"google_fonts_url": url})
    assert cleaned == {"google_fonts_url": url}


def test_sanitize_theme_rejects_non_google_fonts_url():
    cleaned = _sanitize_theme({"google_fonts_url": "https://evil.example.com/steal.css"})
    assert cleaned == {}


def test_sanitize_theme_truncates_long_name():
    cleaned = _sanitize_theme({"name": "x" * 500})
    assert len(cleaned["name"]) == 120


def test_sanitize_theme_drops_unrecognized_keys():
    cleaned = _sanitize_theme({"bg": "#111111", "onload": "alert(1)", "arbitrary": "value"})
    assert cleaned == {"bg": "#111111"}


def test_sanitize_theme_empty_input_returns_empty():
    assert _sanitize_theme({}) == {}
    assert _sanitize_theme({"unknown_key": "whatever"}) == {}


# ── POST /worlds/{id}/theme/import ──────────────────────────────────────────

_VALID_THEME = {
    "name": "Hunt in the Moonlight",
    "accent": "#c9a25c",
    "bg": "#0b0a10",
    "bg2": "#131019",
    "border": "#2c2536",
    "neon2": "#b3455a",
    "neon3": "#8b6fb0",
    "text": "#d9d2c9",
    "font": "'EB Garamond', Georgia, serif",
    "font_heading": "'Cinzel', Georgia, serif",
    "google_fonts_url": "https://fonts.googleapis.com/css2?family=Cinzel&display=swap",
}


def _upload_theme(client, world_id, payload):
    return client.post(
        f"/worlds/{world_id}/theme/import",
        files={"file": ("theme.json", io.BytesIO(json.dumps(payload).encode()), "application/json")},
        follow_redirects=False,
    )


def test_theme_import_round_trip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _upload_theme(client, seed.world_a.id, _VALID_THEME)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.accent == "#c9a25c"
        stored = json.loads(w.theme_json)
        assert stored["name"] == "Hunt in the Moonlight"
        assert stored["bg"] == "#0b0a10"
        # accent lives on World.accent, not duplicated inside theme_json
        assert "accent" not in stored
    finally:
        db.close()


def test_theme_import_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = _upload_theme(client, seed.world_a.id, _VALID_THEME)
    assert r.status_code == 403


def test_theme_import_rejects_invalid_json(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/theme/import",
        files={"file": ("theme.json", io.BytesIO(b"not json"), "application/json")},
    )
    assert r.status_code == 400


def test_theme_import_rejects_non_object_json(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/theme/import",
        files={"file": ("theme.json", io.BytesIO(b"[1, 2, 3]"), "application/json")},
    )
    assert r.status_code == 400


def test_theme_import_rejects_when_nothing_recognized(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _upload_theme(client, seed.world_a.id, {"totally": "unrelated", "shape": True})
    assert r.status_code == 400


def test_theme_import_drops_bad_fields_but_keeps_good_ones(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _upload_theme(client, seed.world_a.id, {"bg": "#111111", "text": "not-a-color"})
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        stored = json.loads(w.theme_json)
        assert stored == {"bg": "#111111"}
    finally:
        db.close()


def test_theme_import_unknown_world_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _upload_theme(client, 999999, _VALID_THEME)
    assert r.status_code == 404


# ── POST /worlds/{id}/theme/clear ───────────────────────────────────────────

def test_theme_clear_removes_theme(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _upload_theme(client, seed.world_a.id, _VALID_THEME)
    r = client.post(f"/worlds/{seed.world_a.id}/theme/clear", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.theme_json is None
        # Clearing the theme doesn't revert the accent color it set earlier.
        assert w.accent == "#c9a25c"
    finally:
        db.close()


def test_theme_clear_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/theme/clear")
    assert r.status_code == 403


def test_theme_clear_unknown_world_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/worlds/999999/theme/clear")
    assert r.status_code == 404


# ── base.html renders the theme's CSS variable overrides ───────────────────

def test_page_renders_theme_css_overrides_when_set(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _upload_theme(client, seed.world_a.id, _VALID_THEME)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/")
    assert r.status_code == 200
    assert "--bg: #0b0a10 !important" in r.text
    assert "--bg2: #131019 !important" in r.text
    assert "--border: #2c2536 !important" in r.text
    assert "--neon2: #b3455a !important" in r.text
    assert "--neon3: #8b6fb0 !important" in r.text
    assert "--text: #d9d2c9 !important" in r.text
    assert "--font: 'EB Garamond', Georgia, serif !important" in r.text
    assert "--font-heading: 'Cinzel', Georgia, serif !important" in r.text
    # & is HTML-entity-escaped in an href attribute (correct, standard
    # behavior — browsers decode it back to a literal "&" when parsing).
    assert "https://fonts.googleapis.com/css2?family=Cinzel&amp;display=swap" in r.text
    # The plain accent field still drives --neon exactly as before.
    assert "--neon: #c9a25c !important" in r.text


def test_page_omits_theme_overrides_when_no_theme_set(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--bg:" not in r.text
    assert "--font-heading:" not in r.text
    # The accent override (unrelated to theme_json) is unaffected.
    assert "--neon:" in r.text


def test_theme_visible_to_players_reading_the_world(client, seed):
    # Rendering the palette is not a GM-only concern — every visitor to a
    # themed world (including players) should see the reskin.
    login(client, seed.gm.email, GM_PASSWORD)
    _upload_theme(client, seed.world_a.id, _VALID_THEME)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--bg: #0b0a10 !important" in r.text
