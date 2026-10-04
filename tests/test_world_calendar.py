"""World calendars: every world can have its own calendar - weekday names, seasons, yearly holidays, how a year is
written - instead of only month names (app/calendar_config.py, app/routers/calendar.py, calendar/*.html).

The shape is cleaned in one place (clean_calendar_config and the clean_* parts) whether it comes from the settings
form, a preset, an AI draft or world creation.
"""
import json
import re

import pytest

from app import ai as ai_module
from app import calendar_config as cc
from app.database import SessionLocal
from app.models import CalendarEvent, World, WorldCalendar
from app.routers import calendar as cal

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _config(world_id):
    db = SessionLocal()
    try:
        c = db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first()
        return json.loads(c.config_json) if c else None
    finally:
        db.close()


def _save(client, **fields):
    data = {"era_name": "Year 1", "current_day": "1", "days_per_week": "7", "months_json": "[]"}
    data.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in fields.items()})
    r = client.post("/calendar/config", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text[:200]


def _set_config(world_id, **cfg):
    db = SessionLocal()
    try:
        c = db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first()
        base = {**cal._default_config(), **cfg}
        if c:
            c.config_json = json.dumps(base)
        else:
            db.add(WorldCalendar(world_id=world_id, config_json=json.dumps(base)))
        db.commit()
    finally:
        db.close()


WINTER_SPRING = [
    {"name": "Frost", "days": 10, "season": "Winter", "color": "#7aa7d9"},
    {"name": "Bloom", "days": 12, "season": "Spring", "color": "#6fbf73"},
    {"name": "Plain", "days": 8},
]


# ── the cleaners ──────────────────────────────────────────────────────────────────────────────

def test_clean_months_trims_names_clamps_days_and_keeps_valid_season_and_colour():
    out = cc.clean_months([
        {"name": "  Frost  ", "days": 0, "season": " Winter ", "color": "#abc"},
        {"name": "", "days": 5000, "season": "", "color": "javascript:alert(1)"},
        {"name": "x" * 99, "days": "12"},
    ])
    assert out[0] == {"name": "Frost", "days": 1, "season": "Winter", "color": "#abc"}
    assert out[1] == {"name": "Month 2", "days": cc.MAX_MONTH_DAYS}          # a bad colour is dropped, not kept
    assert len(out[2]["name"]) == 40 and out[2]["days"] == 12
    assert len(cc.clean_months([{"name": "m"}] * 80)) == cc.MAX_MONTHS


def test_clean_months_rejects_what_is_not_a_clean_number_or_list():
    with pytest.raises(ValueError):
        cc.clean_months([{"name": "m", "days": "many"}])
    with pytest.raises(ValueError):
        cc.clean_months("nope")
    with pytest.raises(ValueError):
        cc.clean_months(["not an object"])


def test_clean_weekday_names_from_text_or_list_fits_days_per_week():
    assert cc.clean_weekday_names("Mon, Tue, Wed", 3) == ["Mon", "Tue", "Wed"]
    assert cc.clean_weekday_names("A\nB\nC\nD", 3) == ["A", "B", "C"]                # extras dropped
    assert cc.clean_weekday_names("A, B", 4) == ["A", "B", "Day 3", "Day 4"]         # missing ones numbered
    assert cc.clean_weekday_names("A,,C", 3) == ["A", "Day 2", "C"]
    assert cc.clean_weekday_names(["Sol", "Luna"], 2) == ["Sol", "Luna"]
    assert cc.clean_weekday_names("", 5) == [] and cc.clean_weekday_names(" , ,", 5) == []
    assert len(cc.clean_weekday_names("x" * 80, 1)[0]) == 24
    with pytest.raises(ValueError):
        cc.clean_weekday_names(42, 5)


def test_clean_holidays_maps_names_to_months_clamps_days_and_sorts():
    months = cc.clean_months(WINTER_SPRING)
    out = cc.clean_holidays([
        {"name": "Spring Fair", "month": "bloom", "day": 99, "notes": "stalls", "color": "#ff0"},     # by name, day clamped
        {"name": "First Frost", "month": 0, "day": 3},
        {"name": "Bad month", "month": 7, "day": 1},                                                 # no such month
        {"name": "", "month": 0, "day": 1},                                                          # no name
        {"name": "Numeric text", "month": "2", "day": 0},
        {"name": "Bool month", "month": True, "day": 1},
        "junk",
    ], months)
    assert [h["name"] for h in out] == ["First Frost", "Spring Fair", "Numeric text"]
    assert out[1] == {"name": "Spring Fair", "month": 1, "day": 12, "notes": "stalls", "color": "#ff0"}
    assert out[2]["day"] == 1
    assert len(cc.clean_holidays([{"name": "h", "month": 0, "day": 1}] * 150, months)) == cc.MAX_HOLIDAYS


def test_clean_year_format_needs_the_year_placeholder():
    assert cc.clean_year_format("{year} {era}") == "{year} {era}"
    assert cc.clean_year_format("Year of the Dragon") == cc.DEFAULT_YEAR_FORMAT
    assert cc.clean_year_format("") == cc.DEFAULT_YEAR_FORMAT
    assert cc.clean_year_format(None) == cc.DEFAULT_YEAR_FORMAT
    assert len(cc.clean_year_format("{year} " + "x" * 80)) <= 40 or cc.clean_year_format("{year} " + "x" * 80) == cc.DEFAULT_YEAR_FORMAT


def test_format_year():
    assert cc.format_year({}, 427) == "Year 427"
    assert cc.format_year({"year_format": "{year} {era}", "era_name": "Y.S.F."}, 427) == "427 Y.S.F."
    assert cc.format_year({"year_format": "Year {year} of {era}", "era_name": "the Embers"}, 3) == "Year 3 of the Embers"
    assert cc.format_year({"year_format": "{year} {era}", "era_name": ""}, 9) == "9"
    assert cc.format_year({"year_format": "{year} {unknown}"}, 9) == "9 {unknown}"          # nothing else is substituted


def test_clean_calendar_config_gives_every_key_and_drops_what_is_unusable():
    out = cc.clean_calendar_config({
        "era_name": "  Age of Ash ", "days_per_week": "5", "year_format": "{year} AA", "surprise": "dropped",
        "weekday_names": "A,B,C,D,E", "months": WINTER_SPRING,
        "moons": [{"name": "Moon", "cycle_days": "not a number"}],                  # unusable -> no moons, not a crash
        "holidays": [{"name": "Fair", "month": "Frost", "day": 4}],
    })
    assert set(out) == {"era_name", "year_format", "days_per_week", "weekday_names", "months", "moons", "holidays"}
    assert out["era_name"] == "Age of Ash" and out["days_per_week"] == 5 and out["moons"] == []
    assert out["weekday_names"] == ["A", "B", "C", "D", "E"]
    assert out["holidays"] == [{"name": "Fair", "month": 0, "day": 4}]
    empty = cc.clean_calendar_config(None)
    assert empty["months"] == cc.DEFAULT_MONTHS and empty["days_per_week"] == 7 and empty["holidays"] == []
    assert cc.clean_calendar_config({"months": "oops"})["months"] == cc.DEFAULT_MONTHS


def test_holiday_index_and_weekday_names_of_read_old_configs_without_the_new_keys():
    assert cc.holiday_index({"months": WINTER_SPRING}) == {}
    assert cc.weekday_names_of({"days_per_week": 5}) == []
    cfg = {"months": WINTER_SPRING, "holidays": [{"name": "Fair", "month": 1, "day": 4}], "days_per_week": 3,
           "weekday_names": ["a", "b"]}
    assert cc.holiday_index(cfg)[(1, 4)][0]["name"] == "Fair"
    assert cc.weekday_names_of(cfg) == ["a", "b", "Day 3"]


# ── presets ───────────────────────────────────────────────────────────────────────────────────

def test_every_preset_is_a_valid_calendar_and_cleaning_it_twice_changes_nothing():
    assert {"hunt_in_the_moonlight", "earth_like", "fantasy_classic", "lunar_13x28"} <= set(cc.CALENDAR_PRESETS)
    for key, preset in cc.CALENDAR_PRESETS.items():
        once = cc.clean_calendar_config(preset)
        assert cc.clean_calendar_config(once) == once, key
        assert preset["label"], key
        if once["weekday_names"]:
            assert len(once["weekday_names"]) == once["days_per_week"], key
        assert once["moons"], key
        for h in once["holidays"]:
            assert 0 <= h["month"] < len(once["months"]), key


def test_earth_like_preset_is_a_gregorian_style_year():
    p = cc.clean_calendar_config(cc.CALENDAR_PRESETS["earth_like"])
    assert [m["days"] for m in p["months"]] == [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    assert sum(m["days"] for m in p["months"]) == 365
    assert p["weekday_names"][0] == "Monday" and len(p["weekday_names"]) == 7
    assert {m["season"] for m in p["months"]} == {"Winter", "Spring", "Summer", "Autumn"}


def test_lunar_and_classic_presets():
    lunar = cc.clean_calendar_config(cc.CALENDAR_PRESETS["lunar_13x28"])
    assert len(lunar["months"]) == 13 and all(m["days"] == 28 for m in lunar["months"])
    assert lunar["moons"][0]["cycle_days"] == 28
    classic = cc.clean_calendar_config(cc.CALENDAR_PRESETS["fantasy_classic"])
    assert sum(m["days"] for m in classic["months"]) == 360


def test_the_hunt_in_the_moonlight_preset_invents_no_canon():
    p = cc.CALENDAR_PRESETS["hunt_in_the_moonlight"]
    assert not p.get("weekday_names") and not p.get("holidays")
    assert not any("season" in m for m in p["months"])


def test_config_page_lists_all_presets_and_the_new_fields(client, seed):
    _gm(client, seed)
    page = client.get("/calendar/config").text
    for key in cc.CALENDAR_PRESETS:
        assert f'value="{key}"' in page
    for needle in ('name="weekday_names"', 'name="year_format"', 'name="holidays_json"', 'id="holidays-list"',
                   'id="cal-ai-design"'):
        assert needle in page, needle


# ── config save ────────────────────────────────────────────────────────────────────────────────

def test_save_stores_weekday_names_seasons_holidays_and_year_format(client, seed):
    _gm(client, seed)
    _save(client, days_per_week="3", months_json=WINTER_SPRING, weekday_names="Mon, Tue, Wed",
          holidays_json=[{"name": "Fair", "month": 1, "day": 4}], year_format="{year} {era}", era_name="Y.S.F.")
    c = _config(seed.world_a.id)
    assert c["weekday_names"] == ["Mon", "Tue", "Wed"]
    assert c["months"][0] == {"name": "Frost", "days": 10, "season": "Winter", "color": "#7aa7d9"}
    assert c["months"][2] == {"name": "Plain", "days": 8}
    assert c["holidays"] == [{"name": "Fair", "month": 1, "day": 4}]
    assert c["year_format"] == "{year} {era}" and c["era_name"] == "Y.S.F."


def test_save_without_the_new_fields_keeps_what_is_stored(client, seed):
    """An older form / API client that knows nothing about weekdays or holidays must not wipe them."""
    _gm(client, seed)
    _save(client, days_per_week="3", months_json=WINTER_SPRING, weekday_names="Mon, Tue, Wed",
          holidays_json=[{"name": "Fair", "month": 1, "day": 4}], year_format="{year} AA")
    _save(client, days_per_week="3", months_json=WINTER_SPRING)
    c = _config(seed.world_a.id)
    assert c["weekday_names"] == ["Mon", "Tue", "Wed"] and c["year_format"] == "{year} AA"
    assert c["holidays"] == [{"name": "Fair", "month": 1, "day": 4}]


def test_save_can_clear_weekday_names_and_holidays(client, seed):
    _gm(client, seed)
    _save(client, days_per_week="3", months_json=WINTER_SPRING, weekday_names="Mon, Tue, Wed",
          holidays_json=[{"name": "Fair", "month": 1, "day": 4}])
    _save(client, days_per_week="3", months_json=WINTER_SPRING, weekday_names="", holidays_json="[]")
    c = _config(seed.world_a.id)
    assert c["weekday_names"] == [] and c["holidays"] == []


def test_garbage_in_one_new_field_skips_only_that_field(client, seed):
    _gm(client, seed)
    _save(client, days_per_week="3", months_json=WINTER_SPRING, weekday_names="Mon, Tue, Wed",
          holidays_json=[{"name": "Fair", "month": 1, "day": 4}])
    _save(client, days_per_week="3", months_json=WINTER_SPRING, holidays_json="{not json", year_format="{year} X",
          weekday_names="Sun, Moon, Star")
    c = _config(seed.world_a.id)
    assert c["holidays"] == [{"name": "Fair", "month": 1, "day": 4}]                  # kept
    assert c["year_format"] == "{year} X" and c["weekday_names"] == ["Sun", "Moon", "Star"]


def test_changing_days_per_week_refits_weekday_names(client, seed):
    _gm(client, seed)
    _save(client, days_per_week="3", months_json=WINTER_SPRING, weekday_names="Mon, Tue, Wed")
    _save(client, days_per_week="5", months_json=WINTER_SPRING)
    assert _config(seed.world_a.id)["weekday_names"] == ["Mon", "Tue", "Wed", "Day 4", "Day 5"]
    _save(client, days_per_week="2", months_json=WINTER_SPRING)
    assert _config(seed.world_a.id)["weekday_names"] == ["Mon", "Tue"]


def test_removing_months_drops_or_pulls_back_holidays(client, seed):
    _gm(client, seed)
    _save(client, months_json=WINTER_SPRING, holidays_json=[
        {"name": "Late", "month": 2, "day": 8}, {"name": "Mid", "month": 1, "day": 12}])
    _save(client, months_json=WINTER_SPRING[:2])                                    # the third month is gone
    assert [h["name"] for h in _config(seed.world_a.id)["holidays"]] == ["Mid"]
    _save(client, months_json=[{"name": "Frost", "days": 10}, {"name": "Bloom", "days": 5}])
    assert _config(seed.world_a.id)["holidays"] == [{"name": "Mid", "month": 1, "day": 5}]


def test_a_player_cannot_save_the_calendar_config(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/calendar/config", data={"year_format": "{year} hacked"}, follow_redirects=False)
    assert r.status_code == 403


# ── month view ─────────────────────────────────────────────────────────────────────────────────

def test_month_view_shows_a_weekday_header_row_in_order(client, seed):
    _gm(client, seed)
    _set_config(seed.world_a.id, days_per_week=3, weekday_names=["Sol", "Luna", "Terra"], months=WINTER_SPRING)
    page = client.get("/calendar?year=1&month=0").text
    head = re.search(r'<div class="cal-weekdays"[^>]*>(.*?)</div>\s*<div class="cal-grid"', page, re.S)
    assert head, "no weekday header"
    assert re.findall(r'class="cal-weekday-full">([^<]+)<', head.group(1)) == ["Sol", "Luna", "Terra"]
    assert "grid-template-columns:repeat(3, 1fr)" in head.group(0)


def test_month_view_has_no_weekday_row_when_none_are_named(client, seed):
    _gm(client, seed)
    assert 'class="cal-weekdays"' not in client.get("/calendar").text


def test_month_view_shows_the_season_of_the_month(client, seed):
    _gm(client, seed)
    _set_config(seed.world_a.id, months=WINTER_SPRING)
    page = client.get("/calendar?year=1&month=1").text
    m = re.search(r'id="cal-season"[^>]*>(.*?)</div>', page, re.S)
    assert m and "Spring" in m.group(1)
    assert "#6fbf73" in page
    assert 'id="cal-season"' not in client.get("/calendar?year=1&month=2").text            # a month with no season


def test_holidays_come_round_every_year_on_the_same_day(client, seed):
    _gm(client, seed)
    _set_config(seed.world_a.id, months=WINTER_SPRING, holidays=[{"name": "Founders Day", "month": 1, "day": 4, "notes": "Bells"}])
    for year in (1, 2, 40):
        page = client.get(f"/calendar?year={year}&month=1").text
        assert len(re.findall(r'class="cal-holiday"', page)) == 1, year
        cell = re.search(r'<div class="cal-cell[^"]*" data-day="(\d+)"[^>]*>(?:(?!<div class="cal-cell).)*?cal-holiday', page, re.S)
        assert cell and int(cell.group(1)) == (year - 1) * 30 + 10 + 4, year
        assert "Founders Day" in page
    assert 'class="cal-holiday"' not in client.get("/calendar?year=1&month=0").text


def test_holidays_are_in_the_day_panel_data(client, seed):
    _gm(client, seed)
    _set_config(seed.world_a.id, months=WINTER_SPRING, holidays=[{"name": "Founders Day", "month": 0, "day": 2, "notes": "Bells"}])
    page = client.get("/calendar?year=1&month=0").text
    data = json.loads(re.search(r"const DAYS_DATA = (.*?);\n", page).group(1))
    assert data[1]["holidays"][0]["name"] == "Founders Day" and data[1]["holidays"][0]["notes"] == "Bells"
    assert data[0]["holidays"] == []


def test_year_format_is_used_everywhere_a_date_is_written(client, seed):
    _gm(client, seed)
    _set_config(seed.world_a.id, months=WINTER_SPRING, era_name="Y.S.F.", year_format="{year} {era}", current_day=15)
    client.post("/api/calendar/events", json={"day": 3, "title": "Parade"})
    month = client.get("/calendar?year=7&month=1").text
    assert re.search(r'id="cal-title"[^>]*>\s*Bloom, 7 Y\.S\.F\.\s*<', month)
    assert "Today: Bloom 5, 1 Y.S.F." in month
    assert "Year 7" not in month
    assert "1 Y.S.F." in client.get("/calendar/year?year=1").text and "Year 1" not in client.get("/calendar/year?year=1").text
    assert "Frost 3, 1 Y.S.F." in client.get("/calendar/agenda").text
    hit = client.get("/api/calendar/search?q=parade").json()["results"][0]
    assert hit["label"] == "Frost 3, 1 Y.S.F."


def test_the_default_format_is_unchanged(client, seed):
    _gm(client, seed)
    assert re.search(r'id="cal-title"[^>]*>\s*Frostwake, Year 1\s*<', client.get("/calendar").text)


def test_year_overview_marks_holidays_and_shows_weekday_initials(client, seed):
    _gm(client, seed)
    _set_config(seed.world_a.id, months=WINTER_SPRING, days_per_week=3, weekday_names=["Sol", "Luna", "Terra"],
                holidays=[{"name": "Founders Day", "month": 0, "day": 2}])
    page = client.get("/calendar/year?year=3").text
    assert re.search(r'class="[^"]*\bholiday\b[^"]*"[^>]*data-day="%d"' % (2 * 30 + 2), page)
    assert 'title="Sol"' in page and 'title="Terra"' in page
    assert "Spring" in page


def test_hub_schedule_writes_dates_in_the_world_format(client, seed):
    from app.models import PlayerCharacter
    _set_config(seed.world_a.id, months=WINTER_SPRING, era_name="Y.S.F.", year_format="{year} {era}", current_day=15)
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Ryn", owner_user_id=seed.player_a.id)
        db.add(pc)
        db.commit()
        pc_id = pc.id
        db.add(CalendarEvent(world_id=seed.world_a.id, day=20, title="Duel", character_id=pc_id))
        w = db.get(World, seed.world_a.id)
        from app.deps import world_section_access
        access = world_section_access(w)
        access["calendar"]["player"] = "read"
        w.section_access_json = json.dumps(access)
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    data = client.get(f"/api/characters/{pc_id}/hub/schedule").json()
    assert data["today"] == "Bloom 5, 1 Y.S.F." and data["events"][0]["date"] == "Bloom 10, 1 Y.S.F."


# ── a calendar for a new world ────────────────────────────────────────────────────────────────────

def test_worlds_page_offers_a_calendar_choice(client, seed):
    _gm(client, seed)
    page = client.get("/worlds").text
    assert 'name="calendar_preset"' in page
    assert "Earth-like" in page and "Thirteen moons" in page


def test_creating_a_world_with_a_calendar_preset_starts_from_it(client, seed):
    _gm(client, seed)
    r = client.post("/worlds/new", data={"name": "Gilded Coast", "calendar_preset": "earth_like"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        world = db.query(World).filter(World.slug == "gilded-coast").first()
        row = db.query(WorldCalendar).filter(WorldCalendar.world_id == world.id).first()
        cfg = json.loads(row.config_json)
    finally:
        db.close()
    assert cfg["current_day"] == 1 and cfg["era_name"] == "Common Era"
    assert cfg["weekday_names"][0] == "Monday" and len(cfg["months"]) == 12
    assert cfg["holidays"], "the preset's holidays came along"


@pytest.mark.parametrize("value", ["", "no_such_preset", "../../etc"])
def test_creating_a_world_without_a_known_preset_makes_no_calendar_yet(client, seed, value):
    _gm(client, seed)
    assert client.post("/worlds/new", data={"name": "Plain World", "calendar_preset": value}, follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        world = db.query(World).filter(World.slug == "plain-world").first()
        assert db.query(WorldCalendar).filter(WorldCalendar.world_id == world.id).count() == 0
    finally:
        db.close()


# ── AI: design a calendar for this world ────────────────────────────────────────────────────────

def _mock_design(monkeypatch, payload, calls=None):
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")

    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        if calls is not None:
            calls.append({"messages": messages, "system": system})
        return payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(ai_module, "generate_chat", _gen)


DRAFT = {
    "era_name": "Age of Ash", "year_format": "{year} AA", "days_per_week": 5,
    "weekday_names": ["Cindrel", "Embris", "Soot", "Pyre", "Dusk"],
    "months": [{"name": "Kindling", "days": 25, "season": "Dry"}, {"name": "Smolder", "days": 25, "season": "Dry"},
               {"name": "Downpour", "days": 20, "season": "Wet", "color": "#4488ff"}],
    "moons": [{"name": "Ember", "cycle_days": 25, "color": "#ff8844"}],
    "holidays": [{"name": "Ashfall Vigil", "month": "Smolder", "day": 10, "notes": "Candles in every window"}],
}


def test_ai_design_returns_a_cleaned_draft_and_saves_nothing(client, seed, monkeypatch):
    calls = []
    _mock_design(monkeypatch, {**DRAFT, "surprise": 1, "holidays": DRAFT["holidays"] + [{"name": "Lost", "month": "Nowhere", "day": 1}]}, calls)
    _gm(client, seed)
    before = _config(seed.world_a.id)
    r = client.post("/calendar/ai-design", data={"brief": "a world of ash and long dry seasons"})
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    assert cfg["weekday_names"] == DRAFT["weekday_names"] and cfg["days_per_week"] == 5
    assert cfg["months"][2] == {"name": "Downpour", "days": 20, "season": "Wet", "color": "#4488ff"}
    assert cfg["holidays"] == [{"name": "Ashfall Vigil", "month": 1, "day": 10, "notes": "Candles in every window"}]
    assert "surprise" not in cfg and cfg["year_format"] == "{year} AA"
    assert _config(seed.world_a.id) == before, "a draft is for review - nothing is saved"
    prompt = calls[0]["messages"][0]["content"] + calls[0]["system"]
    assert "ash and long dry seasons" in prompt and "World A" in prompt


def test_ai_design_tolerates_fenced_json_and_rejects_an_unusable_reply(client, seed, monkeypatch):
    _gm(client, seed)
    _mock_design(monkeypatch, "```json\n" + json.dumps(DRAFT) + "\n```")
    assert client.post("/calendar/ai-design", data={"brief": ""}).status_code == 200
    _mock_design(monkeypatch, "I would suggest a calendar with many months.")
    assert client.post("/calendar/ai-design", data={"brief": "x"}).status_code == 502
    _mock_design(monkeypatch, {"era_name": "Only a name"})
    assert client.post("/calendar/ai-design", data={"brief": "x"}).status_code == 502           # no months -> nothing to review


def test_ai_design_needs_a_gm_and_a_configured_backend(client, seed, monkeypatch):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/calendar/ai-design", data={"brief": "x"}).status_code == 403
    client.cookies.clear()
    _gm(client, seed)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "")
    assert client.post("/calendar/ai-design", data={"brief": "x"}).status_code == 400


def test_ai_design_is_a_background_ai_task():
    from app import ai_background
    assert ai_background.TASK_PATHS.match("/calendar/ai-design")
    assert ai_background.label_for("/calendar/ai-design") == "Calendar design"
