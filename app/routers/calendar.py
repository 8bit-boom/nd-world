import json
import math
import os
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_app_settings, get_db
from ..deps import get_world_ctx
from ..imaging import convert_image
from ..models import CalendarDayIcon, CalendarEvent, Entity, GameSession, Party, PlayerCharacter, World, WorldCalendar
from ..templating import templates
from ..uploads import MAX_UPLOAD_BYTES, copy_upload_bounded, effective_upload_bytes, unique_upload_filename

router = APIRouter()

DEFAULT_MONTHS = [
    {"name": n, "days": 30} for n in [
        "Frostwake", "Thawmoon", "Greentide", "Sunhigh", "Longlight", "Harvestfall",
        "Amberfall", "Duskmere", "Stormtide", "Coldreach", "Deepwinter", "Yearsend",
    ]
]
DEFAULT_DAYS_PER_WEEK = 7
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
    here — a smooth, symmetric waxing/waning curve is). `dark_pct` is
    handed to the template as a CSS gradient split point so the moon
    swatch's lit portion is tinted with the moon's own color instead of
    a fixed white, without the template needing to know any of this
    math itself."""
    cycle = max(1, int(moon.get("cycle_days") or _DEFAULT_MOON_CYCLE_DAYS))
    offset = int(moon.get("offset") or 0)
    t = ((day_num - 1 - offset) % cycle) / cycle
    illum = (1 - math.cos(2 * math.pi * t)) / 2
    phase_idx = round(t * 8) % 8
    return {
        "name": moon.get("name") or "Moon",
        "color": moon.get("color") or _DEFAULT_MOON_COLOR,
        "phase_name": _MOON_PHASE_NAMES[phase_idx],
        "illum_pct": round(illum * 100),
        "dark_pct": round((1 - illum) * 100),
        "waxing": t < 0.5,
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


def _month_start_day(months: list, year: int, month_idx: int) -> int:
    total = sum(m["days"] for m in months) or 1
    return (year - 1) * total + sum(m["days"] for m in months[:month_idx]) + 1


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    cal = _get_or_create_calendar(db, world_id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    months = _months_of(config)
    current_day = int(config.get("current_day", 1))
    cur_year, cur_month_idx, cur_dom = _resolve_date(config, current_day)

    year = int(request.query_params.get("year", cur_year))
    month_idx = int(request.query_params.get("month", cur_month_idx))
    if month_idx < 0:
        month_idx = len(months) - 1
        year -= 1
    elif month_idx >= len(months):
        month_idx = 0
        year += 1

    month = months[month_idx]
    month_start = _month_start_day(months, year, month_idx)
    month_end = month_start + month["days"] - 1

    events = db.query(CalendarEvent).filter(
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

    prev_month = month_idx - 1
    prev_year = year if prev_month >= 0 else year - 1
    prev_month = prev_month if prev_month >= 0 else len(months) - 1
    next_month = month_idx + 1
    next_year = year if next_month < len(months) else year + 1
    next_month = next_month if next_month < len(months) else 0

    entities = [{"id": e.id, "name": e.name} for e in db.query(Entity).filter(Entity.world_id == world_id).order_by(Entity.name).all()]
    sessions = [
        {"id": s.id, "label": f"#{s.session_num} {s.title}"}
        for s in db.query(GameSession).filter(GameSession.world_id == world_id)
        .order_by(GameSession.session_num.desc()).all()
    ]
    characters = [
        {"id": c.id, "name": c.name}
        for c in db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).order_by(PlayerCharacter.name).all()
    ]
    parties = [
        {"id": p.id, "name": p.name}
        for p in db.query(Party).filter(Party.world_id == world_id).order_by(Party.name).all()
    ]

    return templates.TemplateResponse("calendar/month.html", {
        "request": request, "world": world, "worlds": worlds,
        "config": config, "months": months, "month": month, "month_idx": month_idx, "year": year,
        "days": days, "era_name": config.get("era_name", "Year"),
        "days_per_week": days_per_week, "lead_pad": range(lead_pad),
        "current_day": current_day, "cur_year": cur_year, "cur_month_idx": cur_month_idx, "cur_dom": cur_dom,
        "prev_month": prev_month, "prev_year": prev_year, "next_month": next_month, "next_year": next_year,
        "entities": entities, "sessions": sessions, "characters": characters, "parties": parties,
    })


@router.get("/calendar/config", response_class=HTMLResponse)
def calendar_config_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    cal = _get_or_create_calendar(db, world_id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    return templates.TemplateResponse("calendar/config.html", {
        "request": request, "world": world, "worlds": worlds, "config": config,
    })


@router.post("/calendar/config")
async def calendar_config_save(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    cal = _get_or_create_calendar(db, world_id)
    form = await request.form()
    config = json.loads(cal.config_json or "{}") or _default_config()
    config["era_name"] = str(form.get("era_name", "")).strip() or "Year"
    config["current_day"] = max(1, int(form.get("current_day") or 1))
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
    cal.config_json = json.dumps(config)
    db.commit()
    return RedirectResponse("/calendar?saved=1", status_code=303)


@router.post("/api/calendar/events")
async def calendar_event_add(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    body = await request.json()
    ev = CalendarEvent(
        world_id=world_id, day=int(body.get("day", 1)),
        title=str(body.get("title", "")).strip() or "Event",
        notes=str(body.get("notes", "")),
        entity_id=int(body["entity_id"]) if body.get("entity_id") else None,
        session_id=int(body["session_id"]) if body.get("session_id") else None,
        character_id=int(body["character_id"]) if body.get("character_id") else None,
        party_id=int(body["party_id"]) if body.get("party_id") else None,
        color=str(body.get("color", "#4488ff")),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return {"id": ev.id, "day": ev.day, "title": ev.title}


@router.post("/api/calendar/events/{event_id}/delete")
def calendar_event_delete(event_id: int, db: Session = Depends(get_db)):
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(404)
    db.delete(ev)
    db.commit()
    return {"ok": True}


@router.post("/api/calendar/advance")
async def calendar_advance(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    cal = _get_or_create_calendar(db, world_id)
    body = await request.json()
    delta = int(body.get("days", 1))
    config = json.loads(cal.config_json or "{}") or _default_config()
    config["current_day"] = max(1, int(config.get("current_day", 1)) + delta)
    cal.config_json = json.dumps(config)
    db.commit()
    year, month_idx, dom = _resolve_date(config, config["current_day"])
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
    world_id = world.id if world else 1
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
def calendar_day_icon_delete(icon_id: int, db: Session = Depends(get_db)):
    icon = db.query(CalendarDayIcon).filter(CalendarDayIcon.id == icon_id).first()
    if not icon:
        raise HTTPException(404)
    _delete_icon_file(icon)
    db.delete(icon)
    db.commit()
    return {"ok": True}
