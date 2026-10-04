"""What a world's calendar is made of, and the one place that cleans it up.

A world calendar is plain JSON (WorldCalendar.config_json): an era name, a year format, days per week with optional
weekday names, months (each with an optional season and colour), moons, and holidays that come round every year. It is
written from the Calendar settings form, from a preset, from an AI draft and when a world is created — all four go
through `clean_calendar_config` / the `clean_*` parts, so a stored calendar always has the same shape and sane limits.

A leaf module (no router, model or database imports) so the calendar router, the character hub and world creation can
all use it without an import cycle.

The part cleaners raise ValueError for a value that is not a clean number (a form that posts garbage must not half
apply it - the calendar route skips just that part); `clean_calendar_config`, which takes a whole loose dict (an AI
draft, a preset), drops the part instead.
"""
import re
from typing import Optional

DEFAULT_MONTHS = [
    {"name": n, "days": 30} for n in [
        "Frostwake", "Thawmoon", "Greentide", "Sunhigh", "Longlight", "Harvestfall",
        "Amberfall", "Duskmere", "Stormtide", "Coldreach", "Deepwinter", "Yearsend",
    ]
]
DEFAULT_DAYS_PER_WEEK = 7
# 1..60 - a week of 1 degenerates to "every day starts a new row" (still renders fine), and 60 is generous headroom
# past any real-world calendar convention while still keeping the week-row grid a sane width.
MIN_DAYS_PER_WEEK, MAX_DAYS_PER_WEEK = 1, 60
DEFAULT_YEAR_FORMAT = "Year {year}"
DEFAULT_MOON_COLOR = "#cccccc"
DEFAULT_MOON_CYCLE_DAYS = 29

MAX_MONTHS, MAX_MONTH_DAYS, MAX_MOONS, MAX_HOLIDAYS = 60, 1000, 8, 100
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# ── built-in presets ──────────────────────────────────────────────────────────────────────────────────

_SEASON_COLORS = {"Winter": "#7aa7d9", "Spring": "#6fbf73", "Summer": "#e6b84a", "Autumn": "#d9824a"}


def _months(rows) -> list:
    """[(name, days, season_or_None)] -> month dicts, coloured by season."""
    out = []
    for name, days, season in rows:
        m = {"name": name, "days": days}
        if season:
            m["season"] = season
            m["color"] = _SEASON_COLORS[season]
        out.append(m)
    return out


# Built-in calendar presets a GM can one-click apply from the config page (or pick when creating a world) instead of
# hand-typing months/moons. A preset only fills in the form fields - the GM still reviews and clicks Save.
#
# "hunt_in_the_moonlight" is the Moonfall Calendar from the bundled Hunt in the Moonlight rules (ch. 31): 13 months of
# 25 days each (325-day year), five 5-day weeks per month, and a single Moon whose 25-day shape cycle is fixed to line
# up exactly with the month length (the source's own canon note on this: "This calendar fixes the lunar cycle at
# exactly 25 days"). The Moon's rich Color mechanic (ch. 32-33) is GM/dice-table content this widget has no mechanism
# to encode - the preset's colour is left at Grey, the source's own "ordinary, nothing-special" default. The rules name
# no weekdays, seasons or holidays, so none are invented here.
#
# The others are original, generic starting points for a world of your own.
CALENDAR_PRESETS = {
    "hunt_in_the_moonlight": {
        "label": "Hunt in the Moonlight — Moonfall Calendar (13 × 25, 5-day weeks)",
        "era_name": "After the Last Flight",
        "days_per_week": 5,
        "months": [
            {"name": n, "days": 25} for n in [
                "Ashwake", "Rainscar", "Blackbloom", "Cinderfast", "Hollowtide",
                "Gildfall", "Veilwane", "Glassfrost", "Emberwane", "Nightroot",
                "Palevigil", "Thawblood", "Last Lantern",
            ]
        ],
        "moons": [{"name": "The Moon", "cycle_days": 25, "offset": 0, "color": "#888888"}],
    },
    "earth_like": {
        "label": "Earth-like — 12 months, 365 days, Monday–Sunday",
        "era_name": "Common Era",
        "days_per_week": 7,
        "weekday_names": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": _months([
            ("January", 31, "Winter"), ("February", 28, "Winter"), ("March", 31, "Spring"),
            ("April", 30, "Spring"), ("May", 31, "Spring"), ("June", 30, "Summer"),
            ("July", 31, "Summer"), ("August", 31, "Summer"), ("September", 30, "Autumn"),
            ("October", 31, "Autumn"), ("November", 30, "Autumn"), ("December", 31, "Winter"),
        ]),
        "moons": [{"name": "Moon", "cycle_days": 29, "offset": 0, "color": "#d8d8c8"}],
        "holidays": [
            {"name": "New Year's Day", "month": 0, "day": 1},
            {"name": "Midsummer", "month": 5, "day": 21},
            {"name": "Midwinter", "month": 11, "day": 21},
        ],
    },
    "fantasy_classic": {
        "label": "Fantasy classic — 12 months × 30 days, 7-day weeks",
        "era_name": "Age of Embers",
        "days_per_week": 7,
        "weekday_names": ["Starday", "Moonday", "Fireday", "Waveday", "Windday", "Stoneday", "Restday"],
        "months": _months([
            ("Frostwake", 30, "Winter"), ("Thawmoon", 30, "Spring"), ("Greentide", 30, "Spring"),
            ("Sunhigh", 30, "Spring"), ("Longlight", 30, "Summer"), ("Harvestfall", 30, "Summer"),
            ("Amberfall", 30, "Summer"), ("Duskmere", 30, "Autumn"), ("Stormtide", 30, "Autumn"),
            ("Coldreach", 30, "Autumn"), ("Deepwinter", 30, "Winter"), ("Yearsend", 30, "Winter"),
        ]),
        "moons": [{"name": "Moon", "cycle_days": 30, "offset": 0, "color": "#c9d4e6"}],
        "holidays": [
            {"name": "Midsummer Feast", "month": 4, "day": 15},
            {"name": "Year's End Vigil", "month": 11, "day": 30},
        ],
    },
    "lunar_13x28": {
        "label": "Thirteen moons — 13 months × 28 days, 7-day weeks",
        "era_name": "Year of the Moons",
        "days_per_week": 7,
        "weekday_names": ["Starday", "Moonday", "Fireday", "Waveday", "Windday", "Stoneday", "Restday"],
        "months": _months([
            ("Wolf Moon", 28, "Winter"), ("Snow Moon", 28, "Winter"), ("Worm Moon", 28, "Spring"),
            ("Pink Moon", 28, "Spring"), ("Flower Moon", 28, "Spring"), ("Strawberry Moon", 28, "Summer"),
            ("Buck Moon", 28, "Summer"), ("Sturgeon Moon", 28, "Summer"), ("Harvest Moon", 28, "Autumn"),
            ("Hunter's Moon", 28, "Autumn"), ("Beaver Moon", 28, "Autumn"), ("Cold Moon", 28, "Winter"),
            ("Blue Moon", 28, "Winter"),
        ]),
        "moons": [{"name": "The Moon", "cycle_days": 28, "offset": 0, "color": "#d6dcec"}],
    },
}


# ── cleaning ──────────────────────────────────────────────────────────────────────────────────────────

def clean_color(value) -> str:
    """A #rgb / #rrggbb colour, or "" for anything else (it ends up in a style attribute)."""
    text = str(value or "").strip()
    return text if _HEX.match(text) else ""


def clean_era_name(value) -> str:
    return str(value or "").strip()[:64] or "Year"


def clean_days_per_week(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DAYS_PER_WEEK
    return max(MIN_DAYS_PER_WEEK, min(MAX_DAYS_PER_WEEK, n)) or DEFAULT_DAYS_PER_WEEK


def clean_months(raw) -> list:
    if not isinstance(raw, list):
        raise ValueError("months must be a list")
    out = []
    for i, m in enumerate(raw[:MAX_MONTHS]):
        if not isinstance(m, dict):
            raise ValueError("a month must be an object")
        entry = {
            "name": str(m.get("name") or "").strip()[:40] or f"Month {i + 1}",
            "days": max(1, min(MAX_MONTH_DAYS, int(m.get("days") or 1))),
        }
        season = str(m.get("season") or "").strip()[:40]
        if season:
            entry["season"] = season
        color = clean_color(m.get("color"))
        if color:
            entry["color"] = color
        out.append(entry)
    return out


def clean_moons(raw) -> list:
    if not isinstance(raw, list):
        raise ValueError("moons must be a list")
    return [
        {
            "name": str(m.get("name") or "Moon").strip()[:64] or "Moon",
            "cycle_days": max(1, int(m.get("cycle_days") or DEFAULT_MOON_CYCLE_DAYS)),
            "offset": int(m.get("offset") or 0),
            "color": str(m.get("color") or DEFAULT_MOON_COLOR)[:16],
        }
        for m in raw[:MAX_MOONS]
    ]


def clean_weekday_names(raw, days_per_week: int) -> list:
    """The names of the days of the week - a list, or one comma / line separated string (the settings form). Exactly
    `days_per_week` of them (extra ones dropped, missing ones "Day N"), or [] when none is named at all."""
    if isinstance(raw, str):
        parts = re.split(r"[,;\n]", raw)
    elif isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        raise ValueError("weekday names must be a list or text")
    names = [p.strip()[:24] for p in parts]
    if not any(names):
        return []
    names = names[:days_per_week]
    names += [""] * (days_per_week - len(names))
    return [n or f"Day {i + 1}" for i, n in enumerate(names)]


def month_index(value, months: list) -> Optional[int]:
    """A month given as its 0-based index, a numeric string, or its name -> the index, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < len(months) else None
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        i = int(text)
        return i if 0 <= i < len(months) else None
    for i, m in enumerate(months):
        if m["name"].strip().lower() == text.lower():
            return i
    return None


def clean_holidays(raw, months: list) -> list:
    """Holidays that come round every year: [{name, month, day, notes?, color?}], month a 0-based index into `months`.
    One naming no real month is dropped, a day past the month's end is pulled back to it; sorted by date."""
    if not isinstance(raw, list):
        raise ValueError("holidays must be a list")
    out = []
    for h in raw[:MAX_HOLIDAYS]:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()[:60]
        idx = month_index(h.get("month"), months)
        if not name or idx is None:
            continue
        entry = {"name": name, "month": idx, "day": max(1, min(months[idx]["days"], int(h.get("day") or 1)))}
        notes = str(h.get("notes") or "").strip()[:200]
        if notes:
            entry["notes"] = notes
        color = clean_color(h.get("color"))
        if color:
            entry["color"] = color
        out.append(entry)
    out.sort(key=lambda e: (e["month"], e["day"]))
    return out


def clean_year_format(value) -> str:
    """How a year is written: text containing {year} (and optionally {era}), like "Year {year}" or "{year} {era}"."""
    text = str(value or "").strip()[:40]
    return text if "{year}" in text else DEFAULT_YEAR_FORMAT


def format_year(config: dict, year: int) -> str:
    """"Year 427" - or, with year_format "{year} {era}" and era_name "Y.S.F.", "427 Y.S.F."."""
    text = clean_year_format(config.get("year_format")).replace("{year}", str(year))
    text = text.replace("{era}", str(config.get("era_name") or "").strip())
    return " ".join(text.split())


def weekday_names_of(config: dict) -> list:
    """The configured weekday names, fitted to days_per_week ([] = none set)."""
    try:
        return clean_weekday_names(config.get("weekday_names") or [], clean_days_per_week(config.get("days_per_week")))
    except ValueError:
        return []


def holidays_of(config: dict) -> list:
    try:
        return clean_holidays(config.get("holidays") or [], config.get("months") or DEFAULT_MONTHS)
    except ValueError:
        return []


def holiday_index(config: dict) -> dict:
    """{(month_idx, day_of_month): [holiday, ...]} for looking up a calendar day."""
    index: dict = {}
    for h in holidays_of(config):
        index.setdefault((h["month"], h["day"]), []).append(h)
    return index


def clean_calendar_config(raw) -> dict:
    """A whole calendar from a loose dict (a preset, an AI draft): every key present and within limits. A part that is
    unusable is dropped, never raised. `current_day` is not part of it - callers keep their own."""
    raw = raw if isinstance(raw, dict) else {}

    def part(fn, *args, default):
        try:
            return fn(*args)
        except (ValueError, TypeError, OverflowError):
            return default

    dpw = clean_days_per_week(raw.get("days_per_week"))
    months = part(clean_months, raw.get("months"), default=[]) or [dict(m) for m in DEFAULT_MONTHS]
    return {
        "era_name": clean_era_name(raw.get("era_name")),
        "year_format": clean_year_format(raw.get("year_format")),
        "days_per_week": dpw,
        "weekday_names": part(clean_weekday_names, raw.get("weekday_names") or [], dpw, default=[]),
        "months": months,
        "moons": part(clean_moons, raw.get("moons") or [], default=[]),
        "holidays": part(clean_holidays, raw.get("holidays") or [], months, default=[]),
    }
