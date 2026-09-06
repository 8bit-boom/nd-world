"""Tests for the /calendar in-world calendar (app/routers/calendar.py):

- Configurable days-per-week (config.days_per_week, defaulting to 7,
  clamped to [1, 60]) — drives the month grid's column count and the
  leading blank-cell padding that aligns each month's first day into the
  right week-column, continuing the week cycle across month boundaries
  rather than resetting it per month.
- CalendarDayIcon: small uploaded images ("stickers") a GM can pin to a
  day, several per day, rendered on the month grid and manageable from the
  day panel — same upload/delete/world-scoping shape as every other
  file-backed model in this app (see tests/test_gallery.py's own
  _png_file for the fake-image convention this mirrors).
- A "has-content" marker class on any day cell with events or icons, plus
  a client-side "only show days with content" filter toggle that dims
  (never removes, to keep the week-grid alignment intact) the rest.
- Moons: config.moons ([{name, cycle_days, offset, color}], empty by
  default — opt-in). _moon_phase_for_day derives a phase purely from the
  absolute day number (decoupled from the calendar's own custom month
  structure, same as current_day itself), rendered on the month grid as
  a small circle whose lit portion is tinted with the moon's own color.

The whole /calendar surface is GM-only (not listed in main.py's
_is_player_safe at all — every route here, GET included, 403s a player),
same as before this feature existed.
"""
import io
import json
import re

from app.database import SessionLocal
from app.models import CalendarDayIcon, CalendarEvent, Entity, GameSession, Party, PlayerCharacter, WorldCalendar
from app.routers.calendar import (
    _days_per_week, _DEFAULT_MOON_COLOR, _DEFAULT_MOON_CYCLE_DAYS, _moon_phase_for_day, DEFAULT_DAYS_PER_WEEK,
    CALENDAR_PRESETS,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _cell_class_for_day(page: str, day_num: int) -> str:
    m = re.search(rf'<div class="([^"]*)" data-day="{day_num}">', page)
    assert m, f"day {day_num} cell not found in page"
    return m.group(1)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000


def _png_file(name="icon.png"):
    return {"file": (name, io.BytesIO(_PNG_BYTES), "image/png")}


def _get_config(world_id):
    db = SessionLocal()
    try:
        cal = db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first()
        return json.loads(cal.config_json) if cal else None
    finally:
        db.close()


# ── GM-only gating ───────────────────────────────────────────────────────

def test_calendar_view_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/calendar").status_code == 403


def test_calendar_config_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/calendar/config").status_code == 403
    assert client.post("/calendar/config", data={}).status_code == 403


def test_calendar_icon_routes_are_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/calendar/days/1/icons", files=_png_file())
    assert r.status_code == 403
    assert client.post("/api/calendar/icons/1/delete").status_code == 403


# ── days_per_week: pure function ─────────────────────────────────────────

def test_days_per_week_defaults_to_seven():
    assert _days_per_week({}) == DEFAULT_DAYS_PER_WEEK == 7


def test_days_per_week_clamps_out_of_range():
    assert _days_per_week({"days_per_week": 0}) == 1
    assert _days_per_week({"days_per_week": -5}) == 1
    assert _days_per_week({"days_per_week": 9999}) == 60


def test_days_per_week_ignores_garbage_value():
    assert _days_per_week({"days_per_week": "not a number"}) == DEFAULT_DAYS_PER_WEEK
    assert _days_per_week({"days_per_week": None}) == DEFAULT_DAYS_PER_WEEK


# ── days_per_week: config save/round-trip ────────────────────────────────

def test_calendar_config_save_persists_days_per_week(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "5", "months_json": "[]",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_config(seed.world_a.id)["days_per_week"] == 5


def test_calendar_config_save_clamps_days_per_week(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "500", "months_json": "[]",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_config(seed.world_a.id)["days_per_week"] == 60


def test_calendar_config_save_missing_days_per_week_falls_back_to_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "months_json": "[]",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_config(seed.world_a.id)["days_per_week"] == DEFAULT_DAYS_PER_WEEK


# ── days_per_week: month-grid rendering ──────────────────────────────────

def test_calendar_month_grid_uses_configured_days_per_week(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "5", "months_json": "[]",
    })
    page = client.get("/calendar").text
    assert "grid-template-columns:repeat(5, 1fr)" in page


def test_calendar_month_grid_pads_first_day_into_correct_week_column(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    # Default months (12 x 30 days) + days_per_week=7: the first month
    # starts on absolute day 1, so lead_pad = (1-1) % 7 == 0 — no padding
    # on month 0. Month index 1 starts on day 31 → lead_pad = 30 % 7 == 2.
    page = client.get("/calendar?year=1&month=1").text
    assert page.count('class="cal-cell cal-cell-pad"') == 2


def test_calendar_month_grid_no_padding_when_month_starts_a_new_week(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/calendar?year=1&month=0").text
    assert page.count('class="cal-cell cal-cell-pad"') == 0


# ── CalendarDayIcon: upload/render/delete ────────────────────────────────

def test_calendar_icon_upload_and_render(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/days/3/icons", files=_png_file(), data={"label": "Holiday"})
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == 3
    assert body["label"] == "Holiday"
    assert body["image_url"].startswith("/uploads/calendar_icons/")

    db = SessionLocal()
    try:
        icon = db.query(CalendarDayIcon).filter(CalendarDayIcon.world_id == seed.world_a.id).first()
        assert icon is not None
        assert icon.day == 3
        assert icon.label == "Holiday"
    finally:
        db.close()

    page = client.get("/calendar?year=1&month=0").text
    assert body["image_url"] in page
    assert 'class="cal-icon"' in page


def test_calendar_icon_upload_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/days/1/icons",
                     files={"file": ("notes.txt", io.BytesIO(b"hi"), "text/plain")})
    assert r.status_code == 400


def test_calendar_icon_upload_rejects_svg(client, seed):
    """Regression guard: calendar icons render via a bare <img> tag, and
    main.py's serve_upload forces Content-Disposition: attachment for any
    .svg (it can carry <script>) — which would silently break the <img>
    display. Simplest fix is not allowing .svg here at all."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/days/1/icons",
                     files={"file": ("icon.svg", io.BytesIO(b"<svg></svg>"), "image/svg+xml")})
    assert r.status_code == 400


def test_calendar_icon_upload_enforces_per_day_cap(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        for i in range(24):
            db.add(CalendarDayIcon(world_id=seed.world_a.id, day=7, image_url=f"/uploads/calendar_icons/x{i}.png"))
        db.commit()
    finally:
        db.close()
    r = client.post("/api/calendar/days/7/icons", files=_png_file())
    assert r.status_code == 400


def test_calendar_icon_delete_removes_row_and_file(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/days/2/icons", files=_png_file())
    icon_id = r.json()["id"]
    image_url = r.json()["image_url"]

    from app.main import UPLOADS_DIR
    saved_path = UPLOADS_DIR / image_url[len("/uploads/"):]
    assert saved_path.is_file()

    r2 = client.post(f"/api/calendar/icons/{icon_id}/delete")
    assert r2.status_code == 200
    assert not saved_path.exists()
    db = SessionLocal()
    try:
        assert db.get(CalendarDayIcon, icon_id) is None
    finally:
        db.close()


def test_calendar_icon_delete_unknown_id_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post("/api/calendar/icons/999999/delete").status_code == 404


# ── CalendarEvent: linking to entity/session/character/party ────────────

def test_calendar_event_add_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/calendar/events", json={"day": 1, "title": "Ambush"})
    assert r.status_code == 403


def test_calendar_event_add_plain(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/events", json={"day": 5, "title": "Founding Day"})
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == 5
    assert body["title"] == "Founding Day"


def test_calendar_event_add_links_entity_session_character_party(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        entity = Entity(world_id=seed.world_a.id, kind="location", name="Neon Market")
        session = GameSession(world_id=seed.world_a.id, title="The Heist", session_num=3)
        character = PlayerCharacter(world_id=seed.world_a.id, name="Ryn Cutter")
        party = Party(world_id=seed.world_a.id, name="The Wire Runners")
        db.add_all([entity, session, character, party])
        db.commit()
        for obj in (entity, session, character, party):
            db.refresh(obj)
        entity_id, session_id, character_id, party_id = entity.id, session.id, character.id, party.id
    finally:
        db.close()

    r = client.post("/api/calendar/events", json={
        "day": 10, "title": "Big Score",
        "entity_id": entity_id, "session_id": session_id,
        "character_id": character_id, "party_id": party_id,
    })
    assert r.status_code == 200
    event_id = r.json()["id"]

    db = SessionLocal()
    try:
        ev = db.get(CalendarEvent, event_id)
        assert ev.entity_id == entity_id
        assert ev.session_id == session_id
        assert ev.character_id == character_id
        assert ev.party_id == party_id
    finally:
        db.close()

    page = client.get("/calendar?year=1&month=0").text
    assert "Neon Market" in page
    assert "#3 The Heist" in page
    assert "Ryn Cutter" in page
    assert "The Wire Runners" in page


def test_calendar_event_add_without_links_leaves_them_null(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/events", json={"day": 2, "title": "Quiet Day"})
    event_id = r.json()["id"]
    db = SessionLocal()
    try:
        ev = db.get(CalendarEvent, event_id)
        assert ev.entity_id is None
        assert ev.session_id is None
        assert ev.character_id is None
        assert ev.party_id is None
    finally:
        db.close()


def test_calendar_view_offers_sessions_characters_parties_as_link_options(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        db.add_all([
            GameSession(world_id=seed.world_a.id, title="Prologue", session_num=1),
            PlayerCharacter(world_id=seed.world_a.id, name="Vex"),
            Party(world_id=seed.world_a.id, name="Chrome Fangs"),
        ])
        db.commit()
    finally:
        db.close()
    page = client.get("/calendar").text
    assert "#1 Prologue" in page
    assert "Vex" in page
    assert "Chrome Fangs" in page


def test_calendar_event_delete(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/events", json={"day": 1, "title": "Temp"})
    event_id = r.json()["id"]
    r2 = client.post(f"/api/calendar/events/{event_id}/delete")
    assert r2.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(CalendarEvent, event_id) is None
    finally:
        db.close()


def test_calendar_event_delete_unknown_id_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post("/api/calendar/events/999999/delete").status_code == 404


def test_calendar_icons_are_world_scoped(client, seed):
    """A day icon pinned in one world must never leak into another world's
    calendar view for the same day number."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    client.post("/api/calendar/days/4/icons", files=_png_file())

    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/calendar?year=1&month=0").text
    assert 'class="cal-icon"' not in page


# ── "Which days have content" indicator + filter toggle ─────────────────

def test_calendar_month_grid_marks_days_with_content(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/calendar/events", json={"day": 5, "title": "Ambush"})
    page = client.get("/calendar?year=1&month=0").text
    assert "has-content" in _cell_class_for_day(page, 5)
    assert "has-content" not in _cell_class_for_day(page, 6)


def test_calendar_month_grid_marks_days_with_icons_too(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/calendar/days/8/icons", files=_png_file())
    page = client.get("/calendar?year=1&month=0").text
    assert "has-content" in _cell_class_for_day(page, 8)


def test_calendar_month_grid_has_filter_toggle(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    page = client.get("/calendar").text
    assert 'id="filter-empty-cb"' in page
    assert 'id="cal-grid"' in page
    assert "filter-empty" in page  # the CSS rule that dims non-content cells


# ── Moon phases: pure function ───────────────────────────────────────────
#
# cycle_days=8 gives exact, floating-point-friendly quarter points (t = 0,
# .25, .5, .75) instead of needing near-equality assertions.

def test_moon_phase_new_moon_at_cycle_start():
    phase = _moon_phase_for_day({"cycle_days": 8, "offset": 0}, day_num=1)
    assert phase["phase_name"] == "New Moon"
    assert phase["illum_pct"] == 0
    assert phase["waxing"] is True


def test_moon_phase_first_quarter_is_waxing():
    phase = _moon_phase_for_day({"cycle_days": 8, "offset": 0}, day_num=3)
    assert phase["phase_name"] == "First Quarter"
    assert phase["illum_pct"] == 50
    assert phase["waxing"] is True


def test_moon_phase_full_moon_at_cycle_midpoint():
    phase = _moon_phase_for_day({"cycle_days": 8, "offset": 0}, day_num=5)
    assert phase["phase_name"] == "Full Moon"
    assert phase["illum_pct"] == 100


def test_moon_phase_last_quarter_is_waning():
    phase = _moon_phase_for_day({"cycle_days": 8, "offset": 0}, day_num=7)
    assert phase["phase_name"] == "Last Quarter"
    assert phase["illum_pct"] == 50
    assert phase["waxing"] is False


def test_moon_phase_wraps_around_to_a_new_cycle():
    phase = _moon_phase_for_day({"cycle_days": 8, "offset": 0}, day_num=9)
    assert phase["phase_name"] == "New Moon"


def test_moon_phase_offset_shifts_which_day_is_new_moon():
    # Same math a day-5/offset-0 full moon would produce, shifted 4 days
    # earlier so day 5 itself lands back on New Moon.
    phase = _moon_phase_for_day({"cycle_days": 8, "offset": 4}, day_num=5)
    assert phase["phase_name"] == "New Moon"


def test_moon_phase_defaults_when_fields_missing():
    phase = _moon_phase_for_day({}, day_num=1)
    assert phase["name"] == "Moon"
    assert phase["color"] == _DEFAULT_MOON_COLOR


def test_moon_phase_clamps_nonpositive_cycle_days():
    """cycle_days <= 0 must not raise (mod-by-zero) or explode the day
    grid — clamped to 1, which degenerates to "always new moon" instead of
    crashing the whole calendar page."""
    phase = _moon_phase_for_day({"cycle_days": -5}, day_num=42)
    assert phase["illum_pct"] == 0


# ── Moon phases: config save/round-trip ──────────────────────────────────

def test_calendar_config_save_persists_moons(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
        "moons_json": json.dumps([{"name": "Silversheen", "cycle_days": 30, "offset": 2, "color": "#aabbcc"}]),
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_config(seed.world_a.id)["moons"] == [
        {"name": "Silversheen", "cycle_days": 30, "offset": 2, "color": "#aabbcc"},
    ]


def test_calendar_config_save_missing_moons_json_defaults_to_empty(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_config(seed.world_a.id)["moons"] == []


def test_calendar_config_save_empty_moons_json_clears_existing_moons(client, seed):
    """Unlike months (which can never go to zero — the month editor's own
    Remove button refuses to drop below one row), a GM CAN remove every
    moon they've added, so an explicit empty save has to actually clear
    them rather than being treated as "no moons field submitted, leave
    the old ones alone"."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
        "moons_json": json.dumps([{"name": "Moon", "cycle_days": 29, "offset": 0, "color": "#cccccc"}]),
    })
    assert len(_get_config(seed.world_a.id)["moons"]) == 1

    client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
        "moons_json": "[]",
    })
    assert _get_config(seed.world_a.id)["moons"] == []


def test_calendar_config_save_garbage_moon_value_no_ops_the_whole_update(client, seed):
    """A field that fails int() inside the list comprehension raises,
    caught by the same outer try/except months_json already relies on —
    the whole moons update is silently skipped (previous value survives)
    rather than 500ing the page or half-applying a corrupt entry. Not a
    new inconsistency: months_json has always worked this way."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    good_moon = {"name": "Moon", "cycle_days": 30, "offset": 0, "color": "#cccccc"}
    client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
        "moons_json": json.dumps([good_moon]),
    })
    r = client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
        "moons_json": json.dumps([{"name": "Moon", "cycle_days": "not-a-number"}]),
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _get_config(seed.world_a.id)["moons"] == [good_moon]


# ── Moon phases: month-grid rendering ─────────────────────────────────────

def test_calendar_month_grid_renders_moon_phase_indicator(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/calendar/config", data={
        "era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]",
        "moons_json": json.dumps([{"name": "Blood Moon", "cycle_days": 8, "offset": 0, "color": "#ff0000"}]),
    })
    page = client.get("/calendar?year=1&month=0").text
    assert 'class="cal-moon"' in page
    assert "#ff0000" in page
    assert "Blood Moon:" in page  # the swatch's title tooltip names the moon + phase


def test_calendar_month_grid_no_moons_configured_renders_no_moon_swatches(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    page = client.get("/calendar").text
    assert 'class="cal-moon"' not in page


# ── Built-in calendar presets ────────────────────────────────────────────

def test_calendar_config_page_lists_builtin_presets(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/calendar/config").text
    assert 'id="cal-preset-select"' in page
    assert "Hunt in the Moonlight — Moonfall Calendar" in page
    assert "hunt_in_the_moonlight" in page


def test_hunt_in_the_moonlight_preset_shape():
    """The preset's own data — the canon 13-months-of-25-days, five 5-day
    weeks, moon cycle fixed to 25 days (ch. 31's own canon note)."""
    preset = CALENDAR_PRESETS["hunt_in_the_moonlight"]
    assert preset["days_per_week"] == 5
    assert len(preset["months"]) == 13
    assert all(m["days"] == 25 for m in preset["months"])
    assert sum(m["days"] for m in preset["months"]) == 325
    assert preset["months"][0]["name"] == "Ashwake"
    assert preset["months"][-1]["name"] == "Last Lantern"
    assert len(preset["moons"]) == 1
    assert preset["moons"][0]["cycle_days"] == 25


def test_applying_hunt_in_the_moonlight_preset_via_config_save(client, seed):
    """Simulates what the page's "Apply preset" JS produces — POSTing the
    preset's own months/moons/era_name/days_per_week — and confirms it
    round-trips through the save route exactly as any hand-typed config
    would (no special-casing needed; the preset just fills the same
    fields a GM could type by hand)."""
    preset = CALENDAR_PRESETS["hunt_in_the_moonlight"]
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={
        "era_name": preset["era_name"], "current_day": "1",
        "days_per_week": str(preset["days_per_week"]),
        "months_json": json.dumps(preset["months"]),
        "moons_json": json.dumps(preset["moons"]),
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = _get_config(seed.world_a.id)
    assert saved["era_name"] == "After the Last Flight"
    assert saved["days_per_week"] == 5
    assert len(saved["months"]) == 13
    assert sum(m["days"] for m in saved["months"]) == 325
    assert saved["moons"][0]["cycle_days"] == 25

    # And the resulting calendar actually renders the custom months/week.
    page = client.get("/calendar?year=1&month=0").text
    assert "Ashwake" in page
    assert "grid-template-columns:repeat(5, 1fr)" in page
