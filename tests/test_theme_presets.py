"""Built-in theme presets (app/theme_presets.py) — twelve generic genre
presets plus four tied to this app's own bundled/companion rule systems
(Asterion, Chronicles of the Worm, Neon Dragons, Hunt in the Moonlight)
— and the route that applies one, POST /worlds/{world_id}/theme/preset:
the one-click sibling of the existing /theme/import file-upload flow,
sharing the same _sanitize_theme() validation and World.accent/theme_json
storage shape (see tests/test_world_theme.py for that route's own
coverage).
"""
import json
from pathlib import Path

from app.database import SessionLocal
from app.main import _sanitize_theme
from app.models import SheetTemplate, World
from app.theme_presets import GAME_PRESET_KEYS, GENRE_PRESET_KEYS, THEME_PRESETS

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_EXAMPLE_THEME_PATH = Path(__file__).parent.parent / "docs" / "world-theme-gothic-moonlight.json"


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


def test_genre_and_game_keys_exactly_partition_theme_presets():
    """GENRE_PRESET_KEYS/GAME_PRESET_KEYS are pure UI-grouping metadata
    (see the module docstring) kept as separate tuples from THEME_PRESETS
    itself — this guards against the two ever drifting apart: a preset
    added to one without the other would either vanish from the world_edit
    gallery or 404 when clicked."""
    grouped = set(GENRE_PRESET_KEYS) | set(GAME_PRESET_KEYS)
    assert not (set(GENRE_PRESET_KEYS) & set(GAME_PRESET_KEYS)), "a key is in both groups"
    assert grouped == set(THEME_PRESETS), (
        f"missing from a group: {set(THEME_PRESETS) - grouped}; "
        f"grouped but not a real preset: {grouped - set(THEME_PRESETS)}"
    )


def test_game_preset_keys_pair_with_a_real_built_in_sheet_template():
    """asterion and hunt-in-the-moonlight are keyed to match the built-in
    SheetTemplate.slug for the same game (app/database.py's seeded rows) on
    purpose — picking the matching character sheet and the matching theme
    should feel like one decision. If the sheet template's slug is ever
    renamed without updating this preset's key, they'd silently stop
    lining up; this test catches that."""
    db = SessionLocal()
    try:
        slugs = {row[0] for row in db.query(SheetTemplate.slug).filter(SheetTemplate.world_id.is_(None)).all()}
    finally:
        db.close()
    assert "asterion" in slugs
    assert "hunt-in-the-moonlight" in slugs


def test_hunt_in_the_moonlight_preset_mirrors_the_shipped_example_file():
    """The module docstring promises this preset mirrors
    docs/world-theme-gothic-moonlight.json field for field — same game,
    same look, single source of truth. If someone edits one without the
    other, this catches the drift."""
    example = json.loads(_EXAMPLE_THEME_PATH.read_text())
    assert THEME_PRESETS["hunt-in-the-moonlight"] == example


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


def test_game_specific_preset_applies_like_any_other(client, seed):
    """The four game-specific presets (GAME_PRESET_KEYS) go through the
    exact same route and validation as the generic genre ones — no special
    casing by group."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/theme/preset",
        data={"preset": "neon_dragons"}, follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.accent == THEME_PRESETS["neon_dragons"]["accent"]
        stored = json.loads(w.theme_json)
        assert stored["name"] == THEME_PRESETS["neon_dragons"]["name"]
    finally:
        db.close()
