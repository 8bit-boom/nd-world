import json
import math
import os
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..database import get_app_settings, get_db
from ..deps import (
    can_edit_content, get_world_ctx, with_world, world_can_edit_row, world_can_edit_section, world_can_view_section,
)
from ..imaging import convert_image
from ..models import CalendarDayIcon, CalendarEvent, Entity, GameSession, Party, PlayerCharacter, World, WorldCalendar
from ..templating import templates
from ..uploads import MAX_UPLOAD_BYTES, copy_upload_bounded, effective_upload_bytes, unique_upload_filename

router = APIRouter()


def _current_user_id(request: Request):
    user = getattr(request.state, "user", None)
    return user.id if user else None


def _can_manage_calendar(request: Request, world) -> bool:
    """True for a GM, or an assistant with calendar:edit — the tier
    allowed to touch the calendar's own configuration (era/months/days-
    per-week/moons/presets), advance/set the current day, and manage day
    icons. None of that is something a plain "edit"-level player gets
    even under the new permission matrix — those are structural/
    world-shaping actions, unlike CalendarEvent, where a player's own
    "edit" grant lets them add/remove events they created (see
    calendar_event_add/calendar_event_delete)."""
    if not world_can_edit_section(request, world, "calendar"):
        return False
    user = getattr(request.state, "user", None)
    return bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))

DEFAULT_MONTHS = [
    {"name": n, "days": 30} for n in [
        "Frostwake", "Thawmoon", "Greentide", "Sunhigh", "Longlight", "Harvestfall",
        "Amberfall", "Duskmere", "Stormtide", "Coldreach", "Deepwinter", "Yearsend",
    ]
]
DEFAULT_DAYS_PER_WEEK = 7

# Built-in calendar presets a GM can one-click apply from the config page
# (app/templates/calendar/config.html's "Apply preset" control) instead of
# hand-typing months/moons — same "starter, not a straitjacket" spirit as
# the Rules page's "Auto-build tabs from headings" button: applying a
# preset only fills in the form fields, the GM still reviews and clicks
# Save themselves.
#
# "hunt_in_the_moonlight" is the Moonfall Calendar from the bundled Hunt in
# the Moonlight rules (ch. 31): 13 months of 25 days each (325-day year),
# five 5-day weeks per month, and a single Moon whose 25-day shape cycle
# is fixed to line up exactly with the month length (the source's own
# canon note on this: "This calendar fixes the lunar cycle at exactly 25
# days"). The Moon's rich Color mechanic (ch. 32-33 — Red/Violet/Green/
# Blue/White/Black/Pink/Teal/Amber/Grey/Yellow, per-month scheduled
# Moonshifts, phase-based intensity, Bleeding Moon) is GM/dice-table
# content this calendar widget has no mechanism to encode (moons[] here
# has one static display color, not a day-by-day color schedule) — the
# preset's color is left at Grey, which happens to double as the source's
# own "ordinary, nothing-special" default state.
CALENDAR_PRESETS = {
    "hunt_in_the_moonlight": {
        "label": "Hunt in the Moonlight — Moonfall Calendar",
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
}
# 1..60 — a week of 1 degenerates to "every day starts a new row" (still
# renders fine), and 60 is generous headroom past any real-world calendar
# convention while still keeping the week-row grid a sane width.
_MIN_DAYS_PER_WEEK, _MAX_DAYS_PER_WEEK = 1, 60

# Icon uploads are small "sticker" images, same size/format contract as a
# portrait or entity art image elsewhere in this app (see main.py's own
# ALLOWED_EXTS) — duplicated locally rather than imported from main.py,
# since main.py imports this router and the reverse would be circular (same
# rationale as pages.py's/video.py's own local _UPLOADS_DIR copy).
_ICON_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ICON_SUBDIR = "calendar_icons"
_MAX_ICONS_PER_DAY = 24



# Moons are opt-in worldbuilding flavor (unlike months, a calendar works
# fine with zero configured) — empty by default, unlike DEFAULT_MONTHS.
_MOON_PHASE_NAMES = (
    "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
    "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
)
_DEFAULT_MOON_COLOR = "#cccccc"
_DEFAULT_MOON_CYCLE_DAYS = 29


def _default_config() -> dict:
    return {
        "era_name": "Year 1", "current_day": 1, "months": DEFAULT_MONTHS,
        "days_per_week": DEFAULT_DAYS_PER_WEEK, "moons": [],
    }


def _days_per_week(config: dict) -> int:
    try:
        dpw = int(config.get("days_per_week", DEFAULT_DAYS_PER_WEEK))
    except (TypeError, ValueError):
        return DEFAULT_DAYS_PER_WEEK
    return max(_MIN_DAYS_PER_WEEK, min(_MAX_DAYS_PER_WEEK, dpw)) or DEFAULT_DAYS_PER_WEEK


def _delete_icon_file(icon: CalendarDayIcon) -> None:
    """Each CalendarDayIcon row owns exactly one file under its own
    dedicated subdir (never a shared/flat portrait-style path), so — like
    AudioClip/VideoClip/PageDoc elsewhere — it's always safe to delete on
    row removal without risking another row's file."""
    root = _UPLOADS_DIR.resolve()
    if not icon.image_url or not icon.image_url.startswith("/uploads/"):
        return
    try:
        path = (root / icon.image_url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return
    if path.is_relative_to(root) and path.is_file():
        path.unlink()


def _get_or_create_calendar(db: Session, world_id: int) -> WorldCalendar:
    cal = db.query(WorldCalendar).filter(WorldCalendar.world_id == world_id).first()
    if not cal:
        cal = WorldCalendar(world_id=world_id, config_json=json.dumps(_default_config()))
        db.add(cal)
        db.commit()
        db.refresh(cal)
    return cal


def _months_of(config: dict) -> list:
    return config.get("months") or DEFAULT_MONTHS


def _moons_of(config: dict) -> list:
    moons = config.get("moons")
    return moons if isinstance(moons, list) else []


def _moon_phase_for_day(moon: dict, day_num: int) -> dict:
    """One moon's phase on one absolute day — a pure function of day_num
    (the calendar's linear day-count, see WorldCalendar's own docstring)
    and the moon's own cycle_days/offset, deliberately decoupled from the
    custom month structure the same way current_day itself already is:
    a moon's cycle rarely lines up evenly with a GM's custom month
    lengths, so anchoring it to months would drift the phase in ways a
    GM can't reason about. `offset` shifts which day counts as this
    moon's "day zero" (new moon) — lets a GM line up a specific campaign
    day with a full moon without renumbering the whole calendar.

    illum: 0.0 (new) -> 1.0 (full) -> 0.0 (new), via the standard
    sinusoidal approximation (real orbital mechanics aren't the point
    here — a smooth, symmetric waxing/waning curve is). `dark_pct` and
    `lit_offset_pct` are handed to the template as CSS knobs so the moon
    swatch renders as an actual crescent/gibbous shape (two same-size
    circles — a dark one and a lit one — overlapping inside a clipped
    circular frame, the same trick most flat-icon moon-phase glyphs use)
    instead of a diagonal color split, without the template needing to
    know any of this math itself. lit_offset_pct is how far (as a % of
    the swatch's own width) the lit circle is slid sideways off the dark
    one: 0% = fully overlapping (full moon), ±100% = no overlap at all
    (new moon) — positive slides right (waxing, lit crescent grows on
    the right), negative slides left (waning, lit crescent shrinks on
    the left)."""
    cycle = max(1, int(moon.get("cycle_days") or _DEFAULT_MOON_CYCLE_DAYS))
    offset = int(moon.get("offset") or 0)
    t = ((day_num - 1 - offset) % cycle) / cycle
    illum = (1 - math.cos(2 * math.pi * t)) / 2
    phase_idx = round(t * 8) % 8
    waxing = t < 0.5
    dark_pct = round((1 - illum) * 100)
    return {
        "name": moon.get("name") or "Moon",
        "color": moon.get("color") or _DEFAULT_MOON_COLOR,
        "phase_name": _MOON_PHASE_NAMES[phase_idx],
        "illum_pct": round(illum * 100),
        "dark_pct": dark_pct,
        "waxing": waxing,
        "lit_offset_pct": dark_pct if waxing else -dark_pct,
    }


def _resolve_date(config: dict, day_num: int):
    """Returns (year, month_idx, day_of_month) for an absolute day number (1-indexed)."""
    months = _months_of(config)
    total = sum(m["days"] for m in months) or 1
    idx0 = (day_num - 1) % total
    year = (day_num - 1) // total + 1
    remaining = idx0
    month_idx = len(months) - 1
    for i, m in enumerate(months):
        if remaining < m["days"]:
            month_idx = i
            break
        remaining -= m["days"]
    return year, month_idx, remaining + 1


_MAX_CALENDAR_PICKER_ROWS = 500

# The Agenda view (all days with content, across the whole calendar — see
# calendar_agenda) queries events/icons unbounded by month, unlike every
# other query in this file. Capped for the same reason
# _MAX_CALENDAR_PICKER_ROWS is: a mature, decades-long campaign can rack up
# thousands of rows, and loading all of them on every visit would make the
# one page meant to make a huge calendar navigable slow to load itself.
_MAX_AGENDA_ROWS = 1000


def _month_start_day(months: list, year: int, month_idx: int) -> int:
    total = sum(m["days"] for m in months) or 1
    return (year - 1) * total + sum(m["days"] for m in months[:month_idx]) + 1


# Nobody's campaign runs a hundred thousand years, but a typed-in year must not be able to push a day number past
# what SQLite stores in one integer column (a 500 on every later page view of the calendar).
MAX_YEAR = 99_999


def _year_length(months: list) -> int:
    return sum(m["days"] for m in months) or 1


def _max_day(months: list) -> int:
    return MAX_YEAR * _year_length(months)


def _clamp_day(months: list, day) -> int:
    return max(1, min(_max_day(months), day))


def _normalise_month(months: list, year: int, month_idx: int):
    """(year, month_idx) for any year / month pair a URL can carry: a month index past either end rolls into the
    neighbouring year(s), and nothing goes before year 1 / month 0 or past MAX_YEAR."""
    n = max(1, len(months))
    year = max(1, min(MAX_YEAR, year))
    absolute = max(0, min(MAX_YEAR * n - 1, (year - 1) * n + month_idx))
    return absolute // n + 1, absolute % n


def _step_month(months: list, year: int, month_idx: int, delta: int):
    return _normalise_month(months, year, month_idx + delta)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _span_label(total_months: int, per_year: int) -> str:
    years, months = divmod(total_months, max(1, per_year))
    parts = []
    if years:
        parts.append(_plural(years, "year"))
    if months:
        parts.append(_plural(months, "month"))
    return ", ".join(parts)


def _relative_months_label(delta: int, per_year: int) -> str:
    """How far a viewed month is from the current one: "this month", "last month", "3 months ago", "in 2 years, 2 months"."""
    if delta == 0:
        return "this month"
    if abs(delta) == 1 and per_year > 1:
        return "next month" if delta > 0 else "last month"
    span = _span_label(abs(delta), per_year)
    return f"in {span}" if delta > 0 else f"{span} ago"


def _relative_days_label(delta: int, year_days: int, months_per_year: int = 12) -> str:
    """The same for a day: "today", "5 days ago", "in 3 months" (months at the calendar's average month length)."""
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if abs(delta) < 60:
        span = _plural(abs(delta), "day")
    else:
        month_len = max(1.0, year_days / max(1, months_per_year))
        span = _span_label(round(abs(delta) / month_len), months_per_year) or _plural(abs(delta), "day")
    return f"in {span}" if delta > 0 else f"{span} ago"


def date_label(config: dict, day_num: int) -> str:
    """One absolute day as the calendar writes it: "Palevigil 7, Year 427"."""
    months = _months_of(config)
    year, month_idx, dom = _resolve_date(config, max(1, day_num))
    return f"{months[month_idx]['name']} {dom}, Year {year}"


def _month_href(year: int, month_idx: int, day: int = 0) -> str:
    return f"/calendar?year={year}&month={month_idx}" + (f"&day={day}" if day else "")


def _like_escape(text: str) -> str:
    """A user's text as a LIKE pattern body: %, _ and the escape character itself mean themselves."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _safe_int(value, default: int) -> int:
    """int(value), falling back to `default` on anything that isn't a clean
    integer — a bare int(...) on a query param or form field 500s the whole
    request on a malformed value (e.g. /calendar?year=x) instead of just
    ignoring it, which every other route in this app treats as a 400 at
    worst, never a 500."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _event_or_404(db: Session, world_id: int, event_id: int) -> CalendarEvent:
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id, CalendarEvent.world_id == world_id).first()
    if not ev:
        raise HTTPException(404)
    return ev


def _icon_or_404(db: Session, world_id: int, icon_id: int) -> CalendarDayIcon:
    icon = db.query(CalendarDayIcon).filter(CalendarDayIcon.id == icon_id, CalendarDayIcon.world_id == world_id).first()
    if not icon:
        raise HTTPException(404)
    return icon


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "calendar"):
        raise HTTPException(403)
    world_id = world.id
    cal = _get_or_create_calendar(db, world_id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    months = _months_of(config)
    current_day = _clamp_day(months, _safe_int(config.get("current_day"), 1))
    cur_year, cur_month_idx, cur_dom = _resolve_date(config, current_day)

    year, month_idx = _normalise_month(
        months, _safe_int(request.query_params.get("year"), cur_year),
        _safe_int(request.query_params.get("month"), cur_month_idx),
    )

    month = months[month_idx]
    month_start = _month_start_day(months, year, month_idx)
    month_end = month_start + month["days"] - 1

    events = db.query(CalendarEvent).options(
        joinedload(CalendarEvent.entity), joinedload(CalendarEvent.session),
        joinedload(CalendarEvent.character), joinedload(CalendarEvent.party),
    ).filter(
        CalendarEvent.world_id == world_id, CalendarEvent.day >= month_start, CalendarEvent.day <= month_end
    ).order_by(CalendarEvent.day).all()
    events_by_day: dict = {}
    for e in events:
        events_by_day.setdefault(e.day, []).append({
            "id": e.id, "title": e.title, "notes": e.notes, "color": e.color,
            "entity_id": e.entity_id, "entity_label": (e.entity.name if e.entity else None),
            "session_id": e.session_id,
            "session_label": (f"#{e.session.session_num} {e.session.title}" if e.session else None),
            "character_id": e.character_id, "character_label": (e.character.name if e.character else None),
            "party_id": e.party_id, "party_label": (e.party.name if e.party else None),
            "created_by_user_id": e.created_by_user_id,
        })

    icons = db.query(CalendarDayIcon).filter(
        CalendarDayIcon.world_id == world_id, CalendarDayIcon.day >= month_start, CalendarDayIcon.day <= month_end
    ).order_by(CalendarDayIcon.created_at).all()
    icons_by_day: dict = {}
    for ic in icons:
        icons_by_day.setdefault(ic.day, []).append({"id": ic.id, "image_url": ic.image_url, "label": ic.label})

    moons = _moons_of(config)
    days = [{"day_num": month_start + i, "dom": i + 1, "is_current": (month_start + i) == current_day,
             "events": events_by_day.get(month_start + i, []),
             "icons": icons_by_day.get(month_start + i, []),
             "moons": [_moon_phase_for_day(m, month_start + i) for m in moons]} for i in range(month["days"])]

    # Weeks run continuously across the whole calendar (day 1 always starts
    # week-column 0), not reset per month — a month's first day lands
    # wherever that ongoing week cycle puts it, same as a real calendar
    # where e.g. March 1st doesn't have to fall on a Sunday. lead_pad blank
    # cells shift it into the right column of the week grid below.
    days_per_week = _days_per_week(config)
    lead_pad = (month_start - 1) % days_per_week

    # Navigation: every control is a plain link (None = nothing further in that direction), so browsing the calendar
    # never touches the campaign's current day.
    n_months = len(months)
    first = (year, month_idx) == (1, 0)
    last = (year, month_idx) == (MAX_YEAR, n_months - 1)
    prev_year, prev_month = _step_month(months, year, month_idx, -1)
    next_year, next_month = _step_month(months, year, month_idx, 1)
    nav = {
        "prev_month": None if first else _month_href(prev_year, prev_month),
        "next_month": None if last else _month_href(next_year, next_month),
        "prev_year": None if year <= 1 else _month_href(year - 1, month_idx),
        "next_year": None if year >= MAX_YEAR else _month_href(year + 1, month_idx),
        "prev_decade": None if year <= 1 else _month_href(max(1, year - 10), month_idx),
        "next_decade": None if year >= MAX_YEAR else _month_href(min(MAX_YEAR, year + 10), month_idx),
        "today": _month_href(cur_year, cur_month_idx),
        "year": f"/calendar/year?year={year}",
        "prev_event": f"/calendar/event-jump?dir=prev&from={month_start}",
        "next_event": f"/calendar/event-jump?dir=next&from={month_end}",
    }
    offset_label = _relative_months_label(
        ((year - 1) * n_months + month_idx) - ((cur_year - 1) * n_months + cur_month_idx), n_months,
    )
    none_notice = {"next": "No later events on the calendar.", "prev": "No earlier events on the calendar."}.get(
        request.query_params.get("none", ""))

    # Populate the "link this event to…" dropdowns — capped rather than
    # loading every row in the world on every month view (a mature campaign's
    # entity list alone can run into the thousands). A world past this cap
    # just can't pick one of the overflow rows when creating a NEW event;
    # existing events' linked-item labels are unaffected, since those come
    # from the joinedload above, not from these lists.
    entities = [
        {"id": e.id, "name": e.name}
        for e in db.query(Entity).filter(Entity.world_id == world_id)
        .order_by(Entity.name).limit(_MAX_CALENDAR_PICKER_ROWS).all()
    ]
    sessions = [
        {"id": s.id, "label": f"#{s.session_num} {s.title}"}
        for s in db.query(GameSession).filter(GameSession.world_id == world_id)
        .order_by(GameSession.session_num.desc()).limit(_MAX_CALENDAR_PICKER_ROWS).all()
    ]
    characters = [
        {"id": c.id, "name": c.name}
        for c in db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id)
        .order_by(PlayerCharacter.name).limit(_MAX_CALENDAR_PICKER_ROWS).all()
    ]
    parties = [
        {"id": p.id, "name": p.name}
        for p in db.query(Party).filter(Party.world_id == world_id)
        .order_by(Party.name).limit(_MAX_CALENDAR_PICKER_ROWS).all()
    ]

    return templates.TemplateResponse("calendar/month.html", {
        "request": request, "world": world, "worlds": worlds,
        "config": config, "months": months, "month": month, "month_idx": month_idx, "year": year,
        "days": days, "era_name": config.get("era_name", "Year"),
        "days_per_week": days_per_week, "lead_pad": range(lead_pad),
        "current_day": current_day, "cur_year": cur_year, "cur_month_idx": cur_month_idx, "cur_dom": cur_dom,
        "prev_month": prev_month, "next_month": next_month, "nav": nav,
        "offset_label": offset_label, "none_notice": none_notice, "max_year": MAX_YEAR, "month_start": month_start,
        "entities": entities, "sessions": sessions, "characters": characters, "parties": parties,
        "can_manage": _can_manage_calendar(request, world),
        "can_add_event": world_can_edit_section(request, world, "calendar"),
        "current_user_id": _current_user_id(request),
    })


@router.get("/calendar/agenda", response_class=HTMLResponse)
def calendar_agenda(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Every day in the whole calendar that has an event or icon pinned to
    it, sorted chronologically — the answer to "a 427-year calendar can't
    be browsed month by month to find what's on it." Each row links back
    into the month view (?year=&month=&day=), which auto-opens that day's
    panel on load (see month.html's own script)."""
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "calendar"):
        raise HTTPException(403)
    world_id = world.id
    cal = _get_or_create_calendar(db, world_id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    months = _months_of(config)

    events = db.query(CalendarEvent).options(
        joinedload(CalendarEvent.entity), joinedload(CalendarEvent.session),
        joinedload(CalendarEvent.character), joinedload(CalendarEvent.party),
    ).filter(CalendarEvent.world_id == world_id).order_by(CalendarEvent.day).limit(_MAX_AGENDA_ROWS).all()
    icons = db.query(CalendarDayIcon).filter(
        CalendarDayIcon.world_id == world_id
    ).order_by(CalendarDayIcon.day).limit(_MAX_AGENDA_ROWS).all()

    by_day: dict = {}
    for e in events:
        by_day.setdefault(e.day, {"events": [], "icons": []})["events"].append({
            "id": e.id, "title": e.title, "notes": e.notes, "color": e.color,
            "entity_label": (e.entity.name if e.entity else None),
            "session_label": (f"#{e.session.session_num} {e.session.title}" if e.session else None),
            "character_label": (e.character.name if e.character else None),
            "party_label": (e.party.name if e.party else None),
        })
    for ic in icons:
        by_day.setdefault(ic.day, {"events": [], "icons": []})["icons"].append(
            {"image_url": ic.image_url, "label": ic.label}
        )

    rows = []
    for day_num in sorted(by_day.keys()):
        year, month_idx, dom = _resolve_date(config, day_num)
        rows.append({
            "day_num": day_num, "year": year, "month_idx": month_idx,
            "month_name": months[month_idx]["name"], "dom": dom,
            "events": by_day[day_num]["events"], "icons": by_day[day_num]["icons"],
        })

    return templates.TemplateResponse("calendar/agenda.html", {
        "request": request, "world": world, "worlds": worlds,
        "era_name": config.get("era_name", "Year"), "rows": rows,
        "truncated": len(events) >= _MAX_AGENDA_ROWS or len(icons) >= _MAX_AGENDA_ROWS,
    })


def _calendar_ctx(request: Request, db: Session, active_world):
    """The viewer's world, its calendar config, and the gate every read-only calendar page and API shares."""
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "calendar"):
        raise HTTPException(403)
    cal = _get_or_create_calendar(db, world.id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    months = _months_of(config)
    current_day = _clamp_day(months, _safe_int(config.get("current_day"), 1))
    return world, worlds, config, months, current_day


@router.get("/calendar/event-jump")
def calendar_event_jump(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Previous / next event from a day — "what happened before this?" without scrolling month by month and without
    moving the campaign's current day. ?dir=next|prev&from=<absolute day> (default: the current day); redirects to the
    month view with the day opened (`day=`), or back to where the user was with `none=` when nothing is left that way."""
    world, _, config, months, current_day = _calendar_ctx(request, db, active_world)
    direction = request.query_params.get("dir")
    if direction not in ("next", "prev"):
        raise HTTPException(400, "dir must be 'next' or 'prev'")
    frm = _clamp_day(months, _safe_int(request.query_params.get("from"), current_day))
    q = db.query(CalendarEvent).filter(CalendarEvent.world_id == world.id)
    if direction == "next":
        ev = q.filter(CalendarEvent.day > frm).order_by(CalendarEvent.day, CalendarEvent.id).first()
    else:
        ev = q.filter(CalendarEvent.day < frm).order_by(CalendarEvent.day.desc(), CalendarEvent.id).first()
    if ev:
        year, month_idx, _ = _resolve_date(config, ev.day)
        target = _month_href(year, month_idx, ev.day)
    else:
        year, month_idx, _ = _resolve_date(config, frm)
        target = _month_href(year, month_idx) + f"&none={direction}"
    return RedirectResponse(with_world(target, world), status_code=303)


_SEARCH_DEFAULT_LIMIT, _SEARCH_MAX_LIMIT = 20, 50


@router.get("/api/calendar/search")
def calendar_search(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Find events by title, notes or the name of what they are linked to, nearest to a day first (?q=, ?near=<day>,
    default the current day, ?limit=). Each hit says how far it is from TODAY (the campaign's current day), whatever
    `near` is. Read-only, same audience as the calendar page itself."""
    world, _, config, months, current_day = _calendar_ctx(request, db, active_world)
    text = (request.query_params.get("q") or "").strip()[:100]
    if not text:
        return {"results": [], "truncated": False}
    limit = max(1, min(_SEARCH_MAX_LIMIT, _safe_int(request.query_params.get("limit"), _SEARCH_DEFAULT_LIMIT)))
    near = _clamp_day(months, _safe_int(request.query_params.get("near"), current_day))
    pattern = f"%{_like_escape(text)}%"

    def like(col):
        return col.ilike(pattern, escape="\\")

    # What an event is linked to is searchable too — but a player must not be able to probe the names of entities the
    # GM keeps hidden from them by seeing which events a search turns up.
    entity_match = CalendarEvent.entity.has(like(Entity.name))
    if not can_edit_content(request):
        entity_match = CalendarEvent.entity.has((Entity.visible_to_players.is_(True)) & like(Entity.name))
    rows = db.query(CalendarEvent).options(joinedload(CalendarEvent.entity)).filter(
        CalendarEvent.world_id == world.id,
        or_(
            like(CalendarEvent.title), like(CalendarEvent.notes), entity_match,
            CalendarEvent.character.has(like(PlayerCharacter.name)),
            CalendarEvent.party.has(like(Party.name)),
            CalendarEvent.session.has(like(GameSession.title)),
        ),
    ).order_by(func.abs(CalendarEvent.day - near), CalendarEvent.day, CalendarEvent.id).limit(limit + 1).all()

    year_days = _year_length(months)
    results = []
    for ev in rows[:limit]:
        year, month_idx, _ = _resolve_date(config, ev.day)
        results.append({
            "id": ev.id, "day": ev.day, "title": ev.title, "notes": (ev.notes or "")[:140], "color": ev.color,
            "label": date_label(config, ev.day), "delta_days": ev.day - current_day,
            "when": _relative_days_label(ev.day - current_day, year_days, len(months)),
            "href": with_world(_month_href(year, month_idx, ev.day), world),
        })
    return {"results": results, "truncated": len(rows) > limit}


@router.get("/calendar/year", response_class=HTMLResponse)
def calendar_year(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """One year at a glance: every month as a small grid, days with events marked, a count per month — the way to spot
    where in a decades-long calendar things happened. Links into the month view."""
    world, worlds, config, months, current_day = _calendar_ctx(request, db, active_world)
    cur_year, cur_month_idx, _ = _resolve_date(config, current_day)
    year = max(1, min(MAX_YEAR, _safe_int(request.query_params.get("year"), cur_year)))
    year_start = _month_start_day(months, year, 0)
    year_end = year_start + _year_length(months) - 1

    titles_by_day: dict = {}
    rows = db.query(CalendarEvent.day, CalendarEvent.title).filter(
        CalendarEvent.world_id == world.id, CalendarEvent.day >= year_start, CalendarEvent.day <= year_end,
    ).order_by(CalendarEvent.day, CalendarEvent.id).limit(_MAX_AGENDA_ROWS * 5).all()
    for day, title in rows:
        titles_by_day.setdefault(day, []).append(title)
    icon_days = {d for (d,) in db.query(CalendarDayIcon.day).filter(
        CalendarDayIcon.world_id == world.id, CalendarDayIcon.day >= year_start, CalendarDayIcon.day <= year_end,
    ).distinct().all()}

    days_per_week = _days_per_week(config)
    grid = []
    for idx, month in enumerate(months):
        start = _month_start_day(months, year, idx)
        days = []
        for i in range(month["days"]):
            day_num = start + i
            titles = titles_by_day.get(day_num, [])
            days.append({
                "day_num": day_num, "dom": i + 1, "is_current": day_num == current_day,
                "has_content": bool(titles) or day_num in icon_days,
                "title": "; ".join(titles[:4]) + (" …" if len(titles) > 4 else ""),
            })
        grid.append({
            "idx": idx, "name": month["name"], "days": days, "lead_pad": range((start - 1) % days_per_week),
            "events": sum(len(titles_by_day.get(start + i, [])) for i in range(month["days"])),
            "href": _month_href(year, idx),
            "is_current": (year, idx) == (cur_year, cur_month_idx),
        })

    nav = {
        "prev_year": None if year <= 1 else f"/calendar/year?year={year - 1}",
        "next_year": None if year >= MAX_YEAR else f"/calendar/year?year={year + 1}",
        "prev_decade": None if year <= 1 else f"/calendar/year?year={max(1, year - 10)}",
        "next_decade": None if year >= MAX_YEAR else f"/calendar/year?year={min(MAX_YEAR, year + 10)}",
        "today": f"/calendar/year?year={cur_year}",
        "month_view": _month_href(year, cur_month_idx if year == cur_year else 0),
    }
    return templates.TemplateResponse("calendar/year.html", {
        "request": request, "world": world, "worlds": worlds, "era_name": config.get("era_name", "Year"),
        "year": year, "cur_year": cur_year, "max_year": MAX_YEAR, "grid": grid, "days_per_week": days_per_week, "nav": nav,
        "offset_label": _span_label(abs(year - cur_year) * len(months), len(months)),
        "year_delta": year - cur_year,
    })


@router.get("/calendar/config", response_class=HTMLResponse)
def calendar_config_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _can_manage_calendar(request, world):
        raise HTTPException(403)
    world_id = world.id
    cal = _get_or_create_calendar(db, world_id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    current_day = max(1, _safe_int(config.get("current_day"), 1))
    cur_year, cur_month_idx, cur_dom = _resolve_date(config, current_day)
    return templates.TemplateResponse("calendar/config.html", {
        "request": request, "world": world, "worlds": worlds, "config": config,
        "calendar_presets": CALENDAR_PRESETS,
        "cur_year": cur_year, "cur_month_idx": cur_month_idx, "cur_dom": cur_dom,
    })


@router.post("/calendar/config")
async def calendar_config_save(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _can_manage_calendar(request, world):
        raise HTTPException(403)
    world_id = world.id
    cal = _get_or_create_calendar(db, world_id)
    form = await request.form()
    config = json.loads(cal.config_json or "{}") or _default_config()
    config["era_name"] = str(form.get("era_name", "")).strip() or "Year"
    config["current_day"] = max(1, _safe_int(form.get("current_day"), 1))
    config["days_per_week"] = _days_per_week({"days_per_week": form.get("days_per_week")})
    raw_months = str(form.get("months_json", "[]") or "[]")
    try:
        months = json.loads(raw_months)
        if isinstance(months, list) and months:
            config["months"] = [{"name": m.get("name") or "Month", "days": max(1, int(m.get("days") or 1))} for m in months]
    except Exception:
        pass
    raw_moons = str(form.get("moons_json", "[]") or "[]")
    try:
        moons = json.loads(raw_moons)
        if isinstance(moons, list):
            # Unlike months, an empty list is valid here — a GM removing
            # every moon they'd added should actually clear them, not fall
            # back to keeping the previous ones.
            config["moons"] = [
                {
                    "name": (m.get("name") or "Moon").strip()[:64] or "Moon",
                    "cycle_days": max(1, int(m.get("cycle_days") or _DEFAULT_MOON_CYCLE_DAYS)),
                    "offset": int(m.get("offset") or 0),
                    "color": str(m.get("color") or _DEFAULT_MOON_COLOR)[:16],
                }
                for m in moons
            ]
    except Exception:
        pass
    config["current_day"] = _clamp_day(_months_of(config), config["current_day"])
    cal.config_json = json.dumps(config)
    db.commit()
    return RedirectResponse("/calendar?saved=1", status_code=303)


@router.post("/api/calendar/events")
async def calendar_event_add(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_edit_section(request, world, "calendar"):
        raise HTTPException(403)
    world_id = world.id
    body = await request.json()
    # A plain player adding under their own "calendar: edit" grant owns the
    # row (see CalendarEvent.created_by_user_id) and may only ever delete
    # this one going forward — a GM or assistant adding one leaves it
    # GM-authored (None), same as every event before this column existed,
    # since both already have unrestricted rights on every event here.
    user = getattr(request.state, "user", None)
    is_gm_or_assistant = bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))
    months = _months_of(json.loads(_get_or_create_calendar(db, world_id).config_json or "{}"))
    ev = CalendarEvent(
        world_id=world_id, day=_clamp_day(months, _safe_int(body.get("day"), 1)),
        title=str(body.get("title", "")).strip() or "Event",
        notes=str(body.get("notes", "")),
        entity_id=int(body["entity_id"]) if body.get("entity_id") else None,
        session_id=int(body["session_id"]) if body.get("session_id") else None,
        character_id=int(body["character_id"]) if body.get("character_id") else None,
        party_id=int(body["party_id"]) if body.get("party_id") else None,
        color=str(body.get("color", "#4488ff")),
        created_by_user_id=None if is_gm_or_assistant else _current_user_id(request),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return {"id": ev.id, "day": ev.day, "title": ev.title}


@router.post("/api/calendar/events/{event_id}/delete")
def calendar_event_delete(
    event_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    ev = _event_or_404(db, world.id, event_id)
    if not world_can_edit_row(request, world, "calendar", ev.created_by_user_id):
        raise HTTPException(403)
    db.delete(ev)
    db.commit()
    return {"ok": True}


@router.post("/api/calendar/advance")
async def calendar_advance(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _can_manage_calendar(request, world):
        raise HTTPException(403)
    world_id = world.id
    cal = _get_or_create_calendar(db, world_id)
    body = await request.json()
    delta = _safe_int(body.get("days"), 1)
    config = json.loads(cal.config_json or "{}") or _default_config()
    config["current_day"] = _clamp_day(_months_of(config), _safe_int(config.get("current_day"), 1) + delta)
    cal.config_json = json.dumps(config)
    db.commit()
    year, month_idx, dom = _resolve_date(config, config["current_day"])
    return {"current_day": config["current_day"], "year": year, "month_idx": month_idx, "dom": dom}


@router.post("/api/calendar/set-date")
async def calendar_set_date(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Set the campaign's current day directly from a (year, month, day-of-
    month) triple instead of an absolute day count — the friendly
    counterpart to /api/calendar/advance's relative move. Nobody can hold
    "day 140658" in their head, but "Palevigil 8, Year 433" is exactly what
    the calendar already displays, so this is the inverse of _resolve_date:
    given the triple a GM actually thinks in, computes the absolute day
    _resolve_date would derive it back into. Out-of-range input is clamped
    rather than rejected — same "never 500 on a stray value" spirit as
    _safe_int — since a slightly-off month/day picked via a stale <select>
    is a UI hiccup, not something worth a hard error."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _can_manage_calendar(request, world):
        raise HTTPException(403)
    world_id = world.id
    cal = _get_or_create_calendar(db, world_id)
    body = await request.json()
    config = json.loads(cal.config_json or "{}") or _default_config()
    months = _months_of(config)
    year = max(1, min(MAX_YEAR, _safe_int(body.get("year"), 1)))
    month_idx = max(0, min(len(months) - 1, _safe_int(body.get("month_idx"), 0)))
    dom = max(1, min(months[month_idx]["days"], _safe_int(body.get("dom"), 1)))
    config["current_day"] = _month_start_day(months, year, month_idx) + dom - 1
    cal.config_json = json.dumps(config)
    db.commit()
    return {"current_day": config["current_day"], "year": year, "month_idx": month_idx, "dom": dom}


@router.post("/api/calendar/days/{day}/icons")
async def calendar_day_icon_add(
    day: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
    file: UploadFile = File(...), label: str = Form(""),
):
    """Pin a small uploaded image to a calendar day — several can stack on
    the same day, rendered like emoji stickers on the month grid (see
    _ICON_ALLOWED_EXTS/day.icons in calendar/month.html)."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _can_manage_calendar(request, world):
        raise HTTPException(403)
    world_id = world.id
    day = _clamp_day(_months_of(json.loads(_get_or_create_calendar(db, world_id).config_json or "{}")), day)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ICON_ALLOWED_EXTS:
        raise HTTPException(400, "Unsupported image type")
    existing = db.query(CalendarDayIcon).filter(
        CalendarDayIcon.world_id == world_id, CalendarDayIcon.day == day
    ).count()
    if existing >= _MAX_ICONS_PER_DAY:
        raise HTTPException(400, f"Max {_MAX_ICONS_PER_DAY} icons per day")
    target_dir = _UPLOADS_DIR / _ICON_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    # One settings row feeds both the size cap (AppSettings.max_upload_mb,
    # Settings > System's "Upload limits" — blank = MAX_UPLOAD_BYTES env
    # default; see effective_upload_bytes in app/uploads.py) and the
    # image-format conversion below.
    settings = get_app_settings(db)
    copy_upload_bounded(file, dest, max_bytes=effective_upload_bytes(getattr(settings, "max_upload_mb", None), MAX_UPLOAD_BYTES))
    dest = convert_image(dest, static_format=settings.static_format, animated_format=settings.animated_format)
    icon = CalendarDayIcon(
        world_id=world_id, day=day, image_url=f"/uploads/{_ICON_SUBDIR}/{dest.name}",
        label=str(label or "").strip()[:120],
    )
    db.add(icon)
    db.commit()
    db.refresh(icon)
    return {"id": icon.id, "day": icon.day, "image_url": icon.image_url, "label": icon.label}


@router.post("/api/calendar/icons/{icon_id}/delete")
def calendar_day_icon_delete(
    icon_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _can_manage_calendar(request, world):
        raise HTTPException(403)
    icon = _icon_or_404(db, world.id, icon_id)
    _delete_icon_file(icon)
    db.delete(icon)
    db.commit()
    return {"ok": True}


# ── AI day flavor ────────────────────────────────────────────────────────────
# Proposes weather/omen/sensory texture for a calendar day the GM can copy
# into an event or just read aloud. The calendar itself stays fully
# deterministic — this is a flavor generator, not a scheduler.

@router.post("/calendar/ai-day")
async def calendar_ai_day(request: Request, db: Session = Depends(get_db),
                          active_world: str = Cookie(None),
                          date_label: str = Form(""), context: str = Form("")):
    """Generate a day's sensory texture (weather, an omen, three sights)
    for the given date label. GM-only; returns text, writes nothing."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    from .. import ai as _ai
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System)")
    date_label = (date_label or "").strip() or "an unspecified day"
    context = (context or "").strip()[:500]

    system = (
        "You are a TTRPG session-prep assistant generating one day's atmospheric texture. "
        "Reply ONLY with JSON, no fences:\n"
        '{"weather": str (one sentence), "omen": str (one subtle portent, one sentence), '
        '"sights": [str, str, str] (three short sensory details of the world that day)}\n'
        "Match the setting's tone from the context when given; otherwise stay dark "
        "cyberpunk-fantasy. Never name real places or people."
    )
    try:
        raw = await _ai.generate_chat(
            [{"role": "user", "content": f"Date: {date_label}"
                                         + (f"\nContext: {context}" if context else "")}],
            system=system, options={"num_predict": 400}, think=False,
        )
    except Exception as exc:
        raise HTTPException(502, f"AI call failed: {exc}") from exc
    if _ai.is_failure_sentinel(raw or ""):
        raise HTTPException(502, raw)
    from .boards_generate import _extract_json_object
    data = _extract_json_object(raw) or {}
    out = {
        "weather": str(data.get("weather") or "")[:240],
        "omen": str(data.get("omen") or "")[:240],
        "sights": [str(s)[:160] for s in data.get("sights", []) if str(s).strip()][:3],
    }
    if not (out["weather"] or out["omen"] or out["sights"]):
        raise HTTPException(502, "The AI reply wasn't usable — try again.")
    return out
