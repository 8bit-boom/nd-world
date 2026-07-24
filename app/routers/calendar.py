import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CalendarEvent, Entity, World, WorldCalendar

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []

DEFAULT_MONTHS = [
    {"name": n, "days": 30} for n in [
        "Frostwake", "Thawmoon", "Greentide", "Sunhigh", "Longlight", "Harvestfall",
        "Amberfall", "Duskmere", "Stormtide", "Coldreach", "Deepwinter", "Yearsend",
    ]
]


def _get_world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


def _default_config() -> dict:
    return {"era_name": "Year 1", "current_day": 1, "months": DEFAULT_MONTHS}


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
    world, worlds = _get_world_ctx(db, active_world)
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
        events_by_day.setdefault(e.day, []).append(
            {"id": e.id, "title": e.title, "notes": e.notes, "color": e.color, "entity_id": e.entity_id}
        )

    days = [{"day_num": month_start + i, "dom": i + 1, "is_current": (month_start + i) == current_day,
             "events": events_by_day.get(month_start + i, [])} for i in range(month["days"])]

    prev_month = month_idx - 1
    prev_year = year if prev_month >= 0 else year - 1
    prev_month = prev_month if prev_month >= 0 else len(months) - 1
    next_month = month_idx + 1
    next_year = year if next_month < len(months) else year + 1
    next_month = next_month if next_month < len(months) else 0

    entities = [{"id": e.id, "name": e.name} for e in db.query(Entity).filter(Entity.world_id == world_id).order_by(Entity.name).all()]

    return templates.TemplateResponse("calendar/month.html", {
        "request": request, "world": world, "worlds": worlds,
        "config": config, "months": months, "month": month, "month_idx": month_idx, "year": year,
        "days": days, "era_name": config.get("era_name", "Year"),
        "current_day": current_day, "cur_year": cur_year, "cur_month_idx": cur_month_idx, "cur_dom": cur_dom,
        "prev_month": prev_month, "prev_year": prev_year, "next_month": next_month, "next_year": next_year,
        "entities": entities,
    })


@router.get("/calendar/config", response_class=HTMLResponse)
def calendar_config_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    world_id = world.id if world else 1
    cal = _get_or_create_calendar(db, world_id)
    config = json.loads(cal.config_json or "{}") or _default_config()
    return templates.TemplateResponse("calendar/config.html", {
        "request": request, "world": world, "worlds": worlds, "config": config,
    })


@router.post("/calendar/config")
async def calendar_config_save(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    world_id = world.id if world else 1
    cal = _get_or_create_calendar(db, world_id)
    form = await request.form()
    config = json.loads(cal.config_json or "{}") or _default_config()
    config["era_name"] = str(form.get("era_name", "")).strip() or "Year"
    config["current_day"] = max(1, int(form.get("current_day") or 1))
    raw_months = str(form.get("months_json", "[]") or "[]")
    try:
        months = json.loads(raw_months)
        if isinstance(months, list) and months:
            config["months"] = [{"name": m.get("name") or "Month", "days": max(1, int(m.get("days") or 1))} for m in months]
    except Exception:
        pass
    cal.config_json = json.dumps(config)
    db.commit()
    return RedirectResponse("/calendar?saved=1", status_code=303)


@router.post("/api/calendar/events")
async def calendar_event_add(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    world_id = world.id if world else 1
    body = await request.json()
    ev = CalendarEvent(
        world_id=world_id, day=int(body.get("day", 1)),
        title=str(body.get("title", "")).strip() or "Event",
        notes=str(body.get("notes", "")),
        entity_id=int(body["entity_id"]) if body.get("entity_id") else None,
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
    world, _ = _get_world_ctx(db, active_world)
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
