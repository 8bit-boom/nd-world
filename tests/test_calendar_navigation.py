"""Calendar navigation (app/routers/calendar.py, calendar/month.html, calendar/year.html):

A long-running campaign's calendar can't be browsed with "previous month / next month" alone, so the month view also has
  - a jump bar (month + year), year / decade steps and a Today link that never touches the campaign's current day,
  - an offset label ("3 months ago") so you always know how far from today you are,
  - previous / next event buttons (GET /calendar/event-jump) that land on the day with the next thing pinned to it,
  - a search over event titles, notes and linked names (GET /api/calendar/search),
  - a whole-year overview (GET /calendar/year).
Everything here only READS — the current day is changed by "Set as Today" / "Advance Days" and nothing else.
"""
import json
import re

import pytest

from app.database import SessionLocal
from app.models import CalendarEvent, Entity, World, WorldCalendar
from app.routers import calendar as cal

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

# the default calendar: 12 months x 30 days, current day 1 (year 1, month 0)
MONTHS12 = [{"name": f"M{i}", "days": 30} for i in range(12)]


def _plain_calendar(world_id):
    """Twelve 30-day months with short names (M0..M11), current day 1 - easy to do sums with."""
    db = SessionLocal()
    try:
        c = db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first()
        cfg = {**cal._default_config(), "months": MONTHS12}
        if c:
            c.config_json = json.dumps(cfg)
        else:
            db.add(WorldCalendar(world_id=world_id, config_json=json.dumps(cfg)))
        db.commit()
    finally:
        db.close()


def _gm(client, seed, world=None):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)
    _plain_calendar((world or seed.world_a).id)


def _event(world_id, day, title, **kw):
    db = SessionLocal()
    try:
        ev = CalendarEvent(world_id=world_id, day=day, title=title, notes=kw.get("notes", ""),
                           entity_id=kw.get("entity_id"), color=kw.get("color", "#4488ff"))
        db.add(ev)
        db.commit()
        return ev.id
    finally:
        db.close()


def _entity(world_id, name):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="location", name=name)
        db.add(e)
        db.commit()
        return e.id
    finally:
        db.close()


def _set_current_day(world_id, day):
    db = SessionLocal()
    try:
        c = db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first()
        if not c:
            c = WorldCalendar(world_id=world_id, config_json=json.dumps(cal._default_config()))
            db.add(c)
        cfg = json.loads(c.config_json)
        cfg["current_day"] = day
        c.config_json = json.dumps(cfg)
        db.commit()
    finally:
        db.close()


def _current_day(world_id):
    db = SessionLocal()
    try:
        return json.loads(db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first().config_json)["current_day"]
    finally:
        db.close()


def _nav(page, name):
    """The <a> / <span> carrying data-cal-nav="name": (tag text, href or None)."""
    m = re.search(rf'<(a|span)\b[^>]*data-cal-nav="{re.escape(name)}"[^>]*>', page)
    assert m, f"no nav control {name!r} in the page"
    href = re.search(r'href="([^"]*)"', m.group(0))
    return m.group(0), (href.group(1).replace("&amp;", "&") if href else None)


def _heading(page):
    m = re.search(r'<h2[^>]*id="cal-title"[^>]*>(.*?)</h2>', page, re.S)
    assert m, "no month title"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()


# ── pure helpers ────────────────────────────────────────────────────────────────

def test_normalise_month_wraps_both_ways():
    n = cal._normalise_month
    assert n(MONTHS12, 1, 0) == (1, 0)
    assert n(MONTHS12, 3, -1) == (2, 11)
    assert n(MONTHS12, 1, 12) == (2, 0)
    assert n(MONTHS12, 1, 25) == (3, 1)
    assert n(MONTHS12, 5, -30) == (2, 6)       # several years back in one go


def test_normalise_month_never_goes_before_the_first_month_or_past_the_last_year():
    n = cal._normalise_month
    assert n(MONTHS12, 1, -1) == (1, 0)
    assert n(MONTHS12, 0, 5) == (1, 5)
    assert n(MONTHS12, -40, 3) == (1, 3)           # a year before the first one is the first one
    assert n(MONTHS12, cal.MAX_YEAR, 12) == (cal.MAX_YEAR, 11)
    assert n(MONTHS12, 10 ** 40, 0) == (cal.MAX_YEAR, 0)


def test_step_month_moves_across_year_boundaries():
    assert cal._step_month(MONTHS12, 3, 0, -1) == (2, 11)
    assert cal._step_month(MONTHS12, 3, 11, 1) == (4, 0)
    assert cal._step_month(MONTHS12, 1, 0, -1) == (1, 0)          # nothing before year 1, month 1
    assert cal._step_month(MONTHS12, 2, 4, 12) == (3, 4)          # a year is len(months) steps
    assert cal._step_month(MONTHS12, 2, 4, -120) == (1, 0)


@pytest.mark.parametrize("delta,per_year,expected", [
    (0, 12, "this month"), (1, 12, "next month"), (-1, 12, "last month"),
    (5, 12, "in 5 months"), (-5, 12, "5 months ago"),
    (12, 12, "in 1 year"), (-12, 12, "1 year ago"),
    (14, 12, "in 1 year, 2 months"), (-26, 12, "2 years, 2 months ago"),
    (13, 13, "in 1 year"), (26, 13, "in 2 years"), (-1, 1, "1 year ago"),
])
def test_relative_months_label(delta, per_year, expected):
    assert cal._relative_months_label(delta, per_year) == expected


@pytest.mark.parametrize("delta,expected", [
    (0, "today"), (1, "tomorrow"), (-1, "yesterday"), (5, "in 5 days"), (-5, "5 days ago"),
    (59, "in 59 days"), (-59, "59 days ago"),
])
def test_relative_days_label_short_range(delta, expected):
    assert cal._relative_days_label(delta, 360) == expected


def test_relative_days_label_long_range_uses_months_and_years():
    assert cal._relative_days_label(90, 360) == "in 3 months"
    assert cal._relative_days_label(-90, 360) == "3 months ago"
    assert cal._relative_days_label(720, 360) == "in 2 years"
    assert cal._relative_days_label(-360, 360) == "1 year ago"


def test_date_label_names_month_day_and_year():
    cfg = {"months": MONTHS12, "era_name": "Year 1", "current_day": 1}
    assert cal.date_label(cfg, 1) == "M0 1, Year 1"
    assert cal.date_label(cfg, 31) == "M1 1, Year 1"
    assert cal.date_label(cfg, 361) == "M0 1, Year 2"


# ── month view: jump bar, steps, offset, Today ────────────────────────────────────────

def test_month_view_has_jump_bar_with_month_select_and_year_input(client, seed):
    _gm(client, seed)
    page = client.get("/calendar").text
    form = re.search(r'<form[^>]*id="cal-jump"[^>]*>(.*?)</form>', page, re.S)
    assert form, "no jump form"
    assert 'method="get"' in form.group(0).lower()
    assert re.search(r'<select[^>]*name="month"', form.group(1))
    assert re.search(r'<input[^>]*name="year"', form.group(1))
    # all twelve months are choosable, the viewed month is preselected
    assert form.group(1).count("<option") == 12
    assert re.search(r'<option value="0"\s+selected', form.group(1))


def test_jump_form_keeps_the_world_pinned(client, seed):
    _gm(client, seed)
    form = re.search(r'<form[^>]*id="cal-jump"[^>]*>(.*?)</form>', client.get("/calendar").text, re.S)
    assert re.search(r'<input[^>]*type="hidden"[^>]*name="w"[^>]*value="world-a"', form.group(1))


def test_year_and_decade_steps_keep_the_month(client, seed):
    _gm(client, seed)
    page = client.get("/calendar?year=30&month=4").text
    assert _nav(page, "prev-year")[1].startswith("/calendar?year=29&month=4")
    assert _nav(page, "next-year")[1].startswith("/calendar?year=31&month=4")
    assert _nav(page, "prev-decade")[1].startswith("/calendar?year=20&month=4")
    assert _nav(page, "next-decade")[1].startswith("/calendar?year=40&month=4")
    assert _nav(page, "prev-month")[1].startswith("/calendar?year=30&month=3")
    assert _nav(page, "next-month")[1].startswith("/calendar?year=30&month=5")
    assert "w=world-a" in _nav(page, "next-year")[1]


def test_month_steps_cross_the_year_boundary(client, seed):
    _gm(client, seed)
    page = client.get("/calendar?year=3&month=0").text
    assert _nav(page, "prev-month")[1].startswith("/calendar?year=2&month=11")
    page = client.get("/calendar?year=3&month=11").text
    assert _nav(page, "next-month")[1].startswith("/calendar?year=4&month=0")


def test_first_month_has_no_previous_and_year_steps_are_disabled_near_the_start(client, seed):
    _gm(client, seed)
    page = client.get("/calendar?year=1&month=0").text
    for name in ("prev-month", "prev-year", "prev-decade"):
        tag, href = _nav(page, name)
        assert href is None and "disabled" in tag, name
    assert _nav(page, "next-month")[1] is not None
    page = client.get("/calendar?year=5&month=2").text
    assert _nav(page, "prev-year")[1] is not None
    assert _nav(page, "prev-decade")[1].startswith("/calendar?year=1&month=2")      # 5 - 10 -> clamped to year 1


def test_today_link_returns_to_the_current_month_without_changing_it(client, seed):
    _gm(client, seed)
    _set_current_day(seed.world_a.id, 40 + 360 * 426)      # year 427, month 1, day 10
    page = client.get("/calendar?year=3&month=7").text
    tag, href = _nav(page, "today")
    assert href.startswith("/calendar?year=427&month=1")
    assert _current_day(seed.world_a.id) == 40 + 360 * 426
    assert "Year 427" in page and "Today:" in page


def test_offset_label_is_relative_to_the_current_day(client, seed):
    _gm(client, seed)
    _set_current_day(seed.world_a.id, 360 * 4 + 31)        # year 5, month 1
    off = lambda q: re.sub(r"\s+", " ", re.search(r'id="cal-offset"[^>]*>(.*?)</', client.get(q).text, re.S).group(1)).strip()
    assert off("/calendar?year=5&month=1") == "this month"
    assert off("/calendar?year=5&month=2") == "next month"
    assert off("/calendar?year=5&month=0") == "last month"
    assert off("/calendar?year=1&month=1") == "4 years ago"
    assert off("/calendar?year=7&month=3") == "in 2 years, 2 months"


def test_browsing_never_changes_the_current_day(client, seed):
    _gm(client, seed)
    _set_current_day(seed.world_a.id, 77)
    for q in ("/calendar?year=9&month=3", "/calendar/year?year=9", "/calendar/event-jump?dir=next&from=5",
              "/api/calendar/search?q=x", "/calendar?year=2&month=1&day=40"):
        client.get(q, follow_redirects=True)
    assert _current_day(seed.world_a.id) == 77


# ── month view: clamping of the query values ─────────────────────────────────────────────

@pytest.mark.parametrize("query,title", [
    ("year=-5&month=0", "M0, Year 1"),
    ("year=0&month=0", "M0, Year 1"),
    ("year=1&month=-1", "M0, Year 1"),
    ("year=2&month=-1", "M11, Year 1"),
    ("year=1&month=12", "M0, Year 2"),
    ("year=1&month=25", "M1, Year 3"),
    ("year=4&month=-13", "M11, Year 2"),
])
def test_month_view_clamps_and_wraps_year_and_month(client, seed, query, title):
    _gm(client, seed)
    r = client.get(f"/calendar?{query}")
    assert r.status_code == 200
    assert _heading(r.text) == title


def test_month_view_survives_an_absurd_year(client, seed):
    _gm(client, seed)
    r = client.get(f"/calendar?year={10 ** 30}&month=3")
    assert r.status_code == 200
    assert f"Year {cal.MAX_YEAR}" in r.text
    assert client.get("/calendar?year=1e9&month=x").status_code == 200


def test_set_date_and_advance_cannot_overflow_the_day_counter(client, seed):
    _gm(client, seed)
    r = client.post("/api/calendar/set-date", json={"year": 10 ** 30, "month_idx": 2, "dom": 3})
    assert r.status_code == 200 and r.json()["year"] == cal.MAX_YEAR
    assert client.get("/calendar").status_code == 200
    r = client.post("/api/calendar/advance", json={"days": 10 ** 40})
    assert r.status_code == 200
    assert client.get("/calendar").status_code == 200
    assert _current_day(seed.world_a.id) <= cal.MAX_YEAR * 360


# ── previous / next event ───────────────────────────────────────────────────────────────────

def _jump(client, direction, frm=None):
    q = f"/calendar/event-jump?dir={direction}" + (f"&from={frm}" if frm is not None else "")
    r = client.get(q, follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    return r.headers["location"]


def test_event_jump_next_and_prev_land_on_the_day_of_the_event(client, seed):
    _gm(client, seed)
    for day, title in ((5, "Founding"), (40, "Harvest"), (400, "Far future")):
        _event(seed.world_a.id, day, title)
    loc = _jump(client, "next", 10)
    assert "year=1" in loc and "month=1" in loc and "day=40" in loc and "w=world-a" in loc
    loc = _jump(client, "next", 40)                     # strictly after the day it is called from
    assert "year=2" in loc and "month=1" in loc and "day=400" in loc
    loc = _jump(client, "prev", 40)
    assert "year=1" in loc and "month=0" in loc and "day=5" in loc
    loc = _jump(client, "prev", 1000)
    assert "day=400" in loc


def test_event_jump_defaults_to_the_current_day(client, seed):
    _gm(client, seed)
    _set_current_day(seed.world_a.id, 100)
    _event(seed.world_a.id, 50, "Before")
    _event(seed.world_a.id, 150, "After")
    assert "day=150" in _jump(client, "next")
    assert "day=50" in _jump(client, "prev")
    assert "day=150" in _jump(client, "next", "not-a-number")


def test_event_jump_reports_when_nothing_is_left_in_that_direction(client, seed):
    _gm(client, seed)
    _event(seed.world_a.id, 40, "Only one")
    loc = _jump(client, "next", 40)
    assert "none=next" in loc and "day=" not in loc
    assert "year=1" in loc and "month=1" in loc              # it stays where the user was
    assert "none=prev" in _jump(client, "prev", 40)
    page = client.get(loc).text
    assert 'id="cal-notice"' in page and "No later event" in page
    page = client.get("/calendar?none=prev").text
    assert "No earlier event" in page
    assert 'id="cal-notice"' not in client.get("/calendar").text


def test_event_jump_ignores_other_worlds_and_rejects_a_bad_direction(client, seed):
    _gm(client, seed)
    _event(seed.world_b.id, 40, "Elsewhere")
    assert "none=next" in _jump(client, "next", 1)
    assert client.get("/calendar/event-jump?dir=sideways", follow_redirects=False).status_code == 400
    assert client.get("/calendar/event-jump", follow_redirects=False).status_code == 400


def test_month_view_links_previous_and_next_event_from_the_edges_of_the_viewed_month(client, seed):
    _gm(client, seed)
    page = client.get("/calendar?year=1&month=1").text           # days 31..60
    assert "dir=prev" in _nav(page, "prev-event")[1] and "from=31" in _nav(page, "prev-event")[1]
    assert "dir=next" in _nav(page, "next-event")[1] and "from=60" in _nav(page, "next-event")[1]


# ── search ────────────────────────────────────────────────────────────────────────────────────

def _search(client, q, **kw):
    params = "&".join([f"q={q}"] + [f"{k}={v}" for k, v in kw.items()])
    r = client.get(f"/api/calendar/search?{params}")
    assert r.status_code == 200, r.text[:200]
    return r.json()


def test_search_matches_title_notes_and_linked_entity_name(client, seed):
    _gm(client, seed)
    market = _entity(seed.world_a.id, "Neon Market")
    _event(seed.world_a.id, 10, "Ambush in the rain")
    _event(seed.world_a.id, 20, "Quiet", notes="an ambush is planned")
    _event(seed.world_a.id, 30, "Auction", entity_id=market)
    _event(seed.world_a.id, 40, "Unrelated")
    got = _search(client, "ambush")["results"]
    assert sorted(r["day"] for r in got) == [10, 20]
    assert [r["day"] for r in _search(client, "neon%20market")["results"]] == [30]
    assert _search(client, "AMBUSH")["results"], "case-insensitive"


def test_search_orders_nearest_first_and_labels_each_hit(client, seed):
    _gm(client, seed)
    for day in (5, 95, 120, 400):
        _event(seed.world_a.id, day, f"Feast {day}")
    out = _search(client, "feast", near=100)
    assert [r["day"] for r in out["results"]] == [95, 120, 5, 400]
    first = out["results"][0]
    assert first["title"] == "Feast 95" and first["label"] == "M3 5, Year 1"
    assert first["when"] == "in 3 months" and first["delta_days"] == 94       # measured from today (day 1), not from `near`
    assert "year=1" in first["href"] and "month=3" in first["href"] and "day=95" in first["href"]
    assert "w=world-a" in first["href"]
    # the default is to measure from the campaign's current day
    _set_current_day(seed.world_a.id, 400)
    top = _search(client, "feast")["results"][0]
    assert top["day"] == 400 and top["when"] == "today"
    assert _search(client, "feast", near=5)["results"][-1]["when"] == "today"


def test_search_treats_wildcards_as_text(client, seed):
    _gm(client, seed)
    _event(seed.world_a.id, 1, "50% off")
    _event(seed.world_a.id, 2, "Fifty percent")
    _event(seed.world_a.id, 3, "a_b")
    _event(seed.world_a.id, 4, "axb")
    assert [r["day"] for r in _search(client, "%25")["results"]] == [1]
    assert [r["day"] for r in _search(client, "a_b")["results"]] == [3]
    assert [r["day"] for r in _search(client, "%5C")["results"]] == []


def test_search_limit_empty_query_and_world_scope(client, seed):
    _gm(client, seed)
    for day in range(1, 40):
        _event(seed.world_a.id, day, f"Tournament {day}")
    _event(seed.world_b.id, 5, "Tournament elsewhere")
    out = _search(client, "tournament")
    assert len(out["results"]) == 20 and out["truncated"] is True
    assert len(_search(client, "tournament", limit=5)["results"]) == 5
    assert len(_search(client, "tournament", limit=9999)["results"]) <= 50
    assert _search(client, "tournament", limit="x")["results"]
    assert _search(client, "")["results"] == []
    assert _search(client, "%20%20")["results"] == []
    assert all("elsewhere" not in r["title"] for r in _search(client, "tournament", limit=50)["results"])


def test_search_tolerates_a_bad_near(client, seed):
    _gm(client, seed)
    _event(seed.world_a.id, 7, "Wedding")
    assert _search(client, "wedding", near="oops")["results"][0]["day"] == 7


# ── year overview ───────────────────────────────────────────────────────────────────────────────

def test_year_overview_lists_every_month_and_links_into_it(client, seed):
    _gm(client, seed)
    r = client.get("/calendar/year?year=2")
    assert r.status_code == 200
    for i in range(12):
        assert f"year=2&month={i}" in r.text.replace("&amp;", "&")
    assert "Year 2" in r.text


def test_year_overview_marks_days_with_events_and_counts_per_month(client, seed):
    _gm(client, seed)
    _event(seed.world_a.id, 5, "Founding")            # year 1, month 0
    _event(seed.world_a.id, 6, "Second")
    _event(seed.world_a.id, 40, "Harvest")           # year 1, month 1
    _event(seed.world_a.id, 400, "Next year")        # year 2
    page = client.get("/calendar/year?year=1").text
    assert re.search(r'data-month="0"[^>]*data-events="2"', page)
    assert re.search(r'data-month="1"[^>]*data-events="1"', page)
    assert re.search(r'data-month="2"[^>]*data-events="0"', page)
    assert re.search(r'class="[^"]*\bhas-content\b[^"]*"[^>]*data-day="5"', page)
    assert re.search(r'class="[^"]*\bhas-content\b[^"]*"[^>]*data-day="40"', page)
    assert not re.search(r'has-content[^>]*data-day="400"', page)
    assert 'data-day="400"' not in page
    assert re.search(r'data-day="361"', client.get("/calendar/year?year=2").text)


def test_year_overview_marks_today_and_steps_between_years(client, seed):
    _gm(client, seed)
    _set_current_day(seed.world_a.id, 361)           # year 2, month 0, day 1
    page = client.get("/calendar/year?year=2").text
    assert re.search(r'class="[^"]*\btoday\b[^"]*"[^>]*data-day="361"', page)
    assert "/calendar/year?year=1" in _nav(page, "prev-year")[1]
    assert "/calendar/year?year=3" in _nav(page, "next-year")[1]
    assert "/calendar/year?year=2" in _nav(page, "today")[1]
    page = client.get("/calendar/year?year=1").text
    assert _nav(page, "prev-year")[1] is None


def test_year_overview_clamps_and_survives_garbage(client, seed):
    _gm(client, seed)
    assert "Year 1" in client.get("/calendar/year?year=-3").text
    assert "Year 1" in client.get("/calendar/year?year=abc").text
    assert f"Year {cal.MAX_YEAR}" in client.get(f"/calendar/year?year={10 ** 30}").text
    assert "Year 1" in client.get("/calendar/year").text            # default = the current year


def test_year_overview_is_linked_from_the_month_view(client, seed):
    _gm(client, seed)
    page = client.get("/calendar?year=3&month=2").text
    assert "/calendar/year?year=3" in _nav(page, "year")[1]


def test_year_overview_is_world_scoped(client, seed):
    _gm(client, seed)
    _event(seed.world_b.id, 5, "Other world")
    page = client.get("/calendar/year?year=1").text
    assert re.search(r'data-month="0"[^>]*data-events="0"', page)


# ── who may use it ─────────────────────────────────────────────────────────────────────────────────

def _allow(world_id, level):
    from app.deps import world_section_access
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        access = world_section_access(w)
        access["calendar"]["player"] = level
        w.section_access_json = json.dumps(access)
        db.commit()
    finally:
        db.close()


def test_players_without_calendar_access_get_403_everywhere(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/calendar/year?year=1", "/calendar/event-jump?dir=next&from=1", "/api/calendar/search?q=x"):
        assert client.get(path, follow_redirects=False).status_code == 403, path


def test_players_with_read_access_can_navigate(client, seed):
    _plain_calendar(seed.world_a.id)
    _allow(seed.world_a.id, "read")
    _event(seed.world_a.id, 40, "Harvest feast")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/calendar/year?year=1").status_code == 200
    assert "day=40" in client.get("/calendar/event-jump?dir=next&from=1", follow_redirects=False).headers["location"]
    assert _search(client, "harvest")["results"][0]["day"] == 40
    page = client.get("/calendar?year=1&month=1").text
    assert _nav(page, "next-event")[1] and _nav(page, "year")[1]


def test_players_cannot_reach_other_worlds_events_through_search(client, seed):
    _allow(seed.world_a.id, "read")
    _event(seed.world_b.id, 5, "Secret of world B")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _search(client, "secret")["results"] == []
    client.cookies.set("active_world", seed.world_b.slug)         # a world this player is not in
    r = client.get("/api/calendar/search?q=secret")
    assert r.status_code in (403, 404) or r.json()["results"] == []
