"""Session planner: the table agrees on WHEN to play.

Anyone in the world starts a plan and proposes time slots, everybody marks each slot Yes / Maybe / No, and a GM (or an
assistant / owner) confirms one. After that the votes on the chosen slot are the RSVPs. A confirmed plan can create the
session log, appears on the in-world calendar page and in each player's hub Schedule tab, and downloads as .ics.

Open to every member of the world (and the GM) - it is a table function, not world content, so it does not go through
the per-section Players / Assistants matrix. Everyone only ever votes as themselves; managing (confirm / reopen / edit or
remove anyone's plan or slot) is GM / assistant / owner, and a creator manages their own plan and slots.

Times are stored in UTC (naive datetimes, like every DateTime column here) and shown in the viewer's own time zone by the
browser; the page sends ISO-8601 with an offset.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import ical, live
from ..database import get_db
from ..deps import get_world_ctx, with_world
from ..models import GameSession, SessionPlan, SessionPlanSlot, SessionPlanVote, User, WorldMembership
from ..templating import templates

router = APIRouter()

CHOICES = ("yes", "maybe", "no")
MAX_SLOTS_PER_PLAN = 20
MAX_PLANS_PER_WORLD = 200
DEFAULT_DURATION_MIN, MIN_DURATION_MIN, MAX_DURATION_MIN = 180, 15, 1440
_MIN_YEAR, _MAX_YEAR = 2000, 2100


# ── small helpers ────────────────────────────────────────────────────────────────────────────────────────

def _ctx(request: Request, db: Session, active_world):
    """(user, world, is_manager) for the viewer - 401 without a login, 404 without a world."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    is_manager = bool(user.is_gm or getattr(request.state, "is_assistant", False))
    return user, world, is_manager


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Send a JSON body.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Send a JSON object.")
    return data


def _parse_time(value) -> datetime:
    """An ISO-8601 time (with an offset - "Z" or +02:00; without one it is taken as UTC) -> naive UTC, whole minutes."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a time is required")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if not (_MIN_YEAR <= dt.year <= _MAX_YEAR):
        raise ValueError("that year is out of range")
    return dt.replace(second=0, microsecond=0)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _duration(value, default: int = DEFAULT_DURATION_MIN) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_DURATION_MIN, min(MAX_DURATION_MIN, n))


def _name(user: Optional[User]) -> str:
    if not user:
        return "Someone"
    return user.display_name or (user.email or "").split("@")[0] or "Someone"


def _participants(db: Session, world) -> list:
    """Everyone who answers for this world: the GM(s) and the world's members, GMs first."""
    gms = db.query(User).filter(User.is_gm.is_(True)).order_by(User.id).all()
    members = db.query(WorldMembership).filter(WorldMembership.world_id == world.id).all()
    users = {u.id: u for u in db.query(User).filter(User.id.in_([m.user_id for m in members] or [0])).all()}
    out = [{"id": g.id, "name": _name(g), "role": "gm"} for g in gms]
    seen = {g.id for g in gms}
    rest = []
    for m in members:
        u = users.get(m.user_id)
        if u and u.id not in seen:
            seen.add(u.id)
            rest.append({"id": u.id, "name": _name(u), "role": getattr(m, "role", None) or "player"})
    rest.sort(key=lambda p: ({"owner": 0, "assistant": 1}.get(p["role"], 2), p["name"].lower()))
    return out + rest


def _plan_or_404(db: Session, world, plan_id: int) -> SessionPlan:
    plan = db.query(SessionPlan).filter(SessionPlan.id == plan_id, SessionPlan.world_id == world.id).first()
    if not plan:
        raise HTTPException(404)
    return plan


def _slot_or_404(db: Session, world, slot_id: int) -> SessionPlanSlot:
    slot = db.query(SessionPlanSlot).filter(SessionPlanSlot.id == slot_id, SessionPlanSlot.world_id == world.id).first()
    if not slot:
        raise HTTPException(404)
    return slot


def _can_touch(user, is_manager: bool, owner_id: Optional[int]) -> bool:
    return bool(is_manager or (owner_id is not None and owner_id == user.id))


# ── state ───────────────────────────────────────────────────────────────────────────────────────────────────

def build_state(db: Session, world, user, is_manager: bool) -> dict:
    now = datetime.utcnow()
    people = _participants(db, world)
    ids = {p["id"] for p in people}
    names = {p["id"]: p["name"] for p in people}
    plans = db.query(SessionPlan).filter(SessionPlan.world_id == world.id).all()
    slots = db.query(SessionPlanSlot).filter(SessionPlanSlot.world_id == world.id).order_by(SessionPlanSlot.starts_at, SessionPlanSlot.id).all()
    votes = db.query(SessionPlanVote).filter(SessionPlanVote.world_id == world.id).all()
    author_ids = {p.created_by_user_id for p in plans} | {s.proposed_by_user_id for s in slots}
    authors = {u.id: u for u in db.query(User).filter(User.id.in_([i for i in author_ids if i] or [0])).all()}

    def who(uid):
        return {"id": uid, "name": names.get(uid) or _name(authors.get(uid))} if uid else None

    by_slot: dict = {}
    for v in votes:
        if v.user_id in ids and v.choice in CHOICES:
            by_slot.setdefault(v.slot_id, {})[v.user_id] = v.choice
    slots_by_plan: dict = {}
    for s in slots:
        slots_by_plan.setdefault(s.plan_id, []).append(s)

    out = []
    for p in plans:
        mine_slots = slots_by_plan.get(p.id, [])
        chosen = next((s for s in mine_slots if s.id == p.confirmed_slot_id), None) if p.status == "confirmed" else None
        end = chosen.starts_at + timedelta(minutes=p.duration_min or DEFAULT_DURATION_MIN) if chosen else None
        rows = []
        answered = False
        for s in mine_slots:
            votes_here = by_slot.get(s.id, {})
            answered = answered or user.id in votes_here
            rows.append({
                "id": s.id, "starts_at": _iso(s.starts_at),
                "ends_at": _iso(s.starts_at + timedelta(minutes=p.duration_min or DEFAULT_DURATION_MIN)),
                "proposed_by": who(s.proposed_by_user_id),
                "can_delete": _can_touch(user, is_manager, s.proposed_by_user_id),
                "counts": {c: sum(1 for v in votes_here.values() if v == c) for c in CHOICES},
                "votes": {str(uid): c for uid, c in votes_here.items()},
                "mine": votes_here.get(user.id),
            })
        out.append({
            "id": p.id, "title": p.title, "notes": p.notes or "", "location": p.location or "",
            "duration_min": p.duration_min or DEFAULT_DURATION_MIN, "status": p.status or "open",
            "created_by": who(p.created_by_user_id),
            "can_edit": _can_touch(user, is_manager, p.created_by_user_id),
            "confirmed_slot_id": p.confirmed_slot_id if chosen else None,
            "session_id": p.session_id,
            "session_href": with_world(f"/sessions/{p.session_id}", world) if p.session_id else None,
            "past": bool(end and end < now), "my_answered": answered,
            "slots": rows,
        })
    order = {"open": 0, "confirmed": 1, "cancelled": 2}
    far = "9999"
    out.sort(key=lambda p: (order.get(p["status"], 3),
                            (next((s["starts_at"] for s in p["slots"] if s["id"] == p["confirmed_slot_id"]), far)
                             if p["status"] == "confirmed" else (p["slots"][0]["starts_at"] if p["slots"] else far)),
                            p["id"]))
    return {
        "me": {"id": user.id, "name": _name(user), "can_manage": is_manager},
        "participants": people, "plans": out, "now": _iso(now.replace(microsecond=0)),
    }


def next_confirmed(db: Session, world_id: int):
    """(plan, slot) of the next confirmed session that has not ended yet, or None."""
    now = datetime.utcnow()
    best = None
    for plan in db.query(SessionPlan).filter(SessionPlan.world_id == world_id, SessionPlan.status == "confirmed").all():
        slot = db.get(SessionPlanSlot, plan.confirmed_slot_id) if plan.confirmed_slot_id else None
        if not slot or slot.plan_id != plan.id:
            continue
        if slot.starts_at + timedelta(minutes=plan.duration_min or DEFAULT_DURATION_MIN) < now:
            continue
        if best is None or slot.starts_at < best[1].starts_at:
            best = (plan, slot)
    return best


def polls_waiting(db: Session, world_id: int, user_id: int) -> int:
    """Open polls with at least one slot where this person has not answered any slot yet."""
    n = 0
    for plan in db.query(SessionPlan).filter(SessionPlan.world_id == world_id, SessionPlan.status == "open").all():
        slot_ids = [s.id for s in db.query(SessionPlanSlot.id).filter(SessionPlanSlot.plan_id == plan.id).all()]
        if not slot_ids:
            continue
        if not db.query(SessionPlanVote).filter(SessionPlanVote.slot_id.in_(slot_ids), SessionPlanVote.user_id == user_id).first():
            n += 1
    return n


# ── pages ───────────────────────────────────────────────────────────────────────────────────────────────────

@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    _, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("schedule/index.html", {
        "request": request, "world": world, "worlds": worlds,
        "state": build_state(db, world, user, is_manager),
    })


@router.get("/api/schedule/state")
def schedule_state(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    return build_state(db, world, user, is_manager)


# ── plans ───────────────────────────────────────────────────────────────────────────────────────────────────

def _parse_slots(raw) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, "slots must be a list of times.")
    out = []
    for item in raw:
        try:
            dt = _parse_time(item)
        except (ValueError, TypeError):
            raise HTTPException(400, f"Not a valid time: {item!r}")
        if dt not in out:
            out.append(dt)
    if len(out) > MAX_SLOTS_PER_PLAN:
        raise HTTPException(400, f"At most {MAX_SLOTS_PER_PLAN} time slots per plan.")
    return out


@router.post("/api/schedule/plans")
async def plan_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, _ = _ctx(request, db, active_world)
    body = await _body(request)
    title = _text(body.get("title"), 160)
    if not title:
        raise HTTPException(400, "Give the session a title.")
    times = _parse_slots(body.get("slots"))
    if db.query(SessionPlan).filter(SessionPlan.world_id == world.id).count() >= MAX_PLANS_PER_WORLD:
        raise HTTPException(400, "Too many plans - delete some old ones first.")
    plan = SessionPlan(
        world_id=world.id, title=title, notes=_text(body.get("notes"), 1500), location=_text(body.get("location"), 200),
        duration_min=_duration(body.get("duration_min")), status="open", created_by_user_id=user.id,
    )
    db.add(plan)
    db.flush()
    for dt in times:
        db.add(SessionPlanSlot(plan_id=plan.id, world_id=world.id, starts_at=dt, proposed_by_user_id=user.id))
    db.commit()
    live.touch(world.id)
    return {"id": plan.id}


@router.post("/api/schedule/plans/{plan_id}/edit")
async def plan_edit(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    plan = _plan_or_404(db, world, plan_id)
    if not _can_touch(user, is_manager, plan.created_by_user_id):
        raise HTTPException(403)
    body = await _body(request)
    if "title" in body:
        title = _text(body.get("title"), 160)
        if not title:
            raise HTTPException(400, "A plan needs a title.")
        plan.title = title
    if "notes" in body:
        plan.notes = _text(body.get("notes"), 1500)
    if "location" in body:
        plan.location = _text(body.get("location"), 200)
    if "duration_min" in body:
        plan.duration_min = _duration(body.get("duration_min"), plan.duration_min or DEFAULT_DURATION_MIN)
    db.commit()
    live.touch(world.id)
    return {"ok": True}


@router.post("/api/schedule/plans/{plan_id}/slots")
async def slot_add(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, _ = _ctx(request, db, active_world)
    plan = _plan_or_404(db, world, plan_id)
    if plan.status != "open":
        raise HTTPException(400, "This plan is no longer open for new times.")
    body = await _body(request)
    try:
        dt = _parse_time(body.get("starts_at"))
    except (ValueError, TypeError):
        raise HTTPException(400, "Not a valid time.")
    existing = db.query(SessionPlanSlot).filter(SessionPlanSlot.plan_id == plan.id).all()
    same = next((s for s in existing if s.starts_at == dt), None)
    if same:
        return {"id": same.id}
    if len(existing) >= MAX_SLOTS_PER_PLAN:
        raise HTTPException(400, f"At most {MAX_SLOTS_PER_PLAN} time slots per plan.")
    slot = SessionPlanSlot(plan_id=plan.id, world_id=world.id, starts_at=dt, proposed_by_user_id=user.id)
    db.add(slot)
    db.commit()
    live.touch(world.id)
    return {"id": slot.id}


@router.post("/api/schedule/slots/{slot_id}/delete")
def slot_delete(slot_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    slot = _slot_or_404(db, world, slot_id)
    if not _can_touch(user, is_manager, slot.proposed_by_user_id):
        raise HTTPException(403)
    plan = db.get(SessionPlan, slot.plan_id)
    if plan and plan.status == "confirmed" and plan.confirmed_slot_id == slot.id:
        raise HTTPException(400, "Reopen the plan before removing the chosen time.")
    db.query(SessionPlanVote).filter(SessionPlanVote.slot_id == slot.id).delete()
    db.delete(slot)
    db.commit()
    live.touch(world.id)
    return {"ok": True}


@router.post("/api/schedule/slots/{slot_id}/vote")
async def slot_vote(slot_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, _ = _ctx(request, db, active_world)
    slot = _slot_or_404(db, world, slot_id)
    plan = db.get(SessionPlan, slot.plan_id)
    body = await _body(request)
    choice = body.get("choice")
    if choice not in CHOICES + ("clear",):
        raise HTTPException(400, "choice must be yes, maybe, no or clear.")
    if not plan or plan.status == "cancelled":
        raise HTTPException(400, "This plan was cancelled.")
    if plan.status == "confirmed" and plan.confirmed_slot_id != slot.id:
        raise HTTPException(400, "A time has been chosen - only that one takes answers now.")
    # always the caller's own vote, whatever the body says
    row = db.query(SessionPlanVote).filter(SessionPlanVote.slot_id == slot.id, SessionPlanVote.user_id == user.id).first()
    if choice == "clear":
        if row:
            db.delete(row)
    elif row:
        row.choice, row.updated_at = choice, datetime.utcnow()
    else:
        db.add(SessionPlanVote(plan_id=plan.id, slot_id=slot.id, world_id=world.id, user_id=user.id, choice=choice))
    db.commit()
    live.touch(world.id)
    return {"ok": True, "mine": None if choice == "clear" else choice}


# ── confirm / reopen / cancel / delete ───────────────────────────────────────────────────────────────────────────

def _valid_date(value) -> Optional[str]:
    """YYYY-MM-DD (the browser's local date of the chosen time) or None."""
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


@router.post("/api/schedule/plans/{plan_id}/confirm")
async def plan_confirm(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    if not is_manager:
        raise HTTPException(403)
    plan = _plan_or_404(db, world, plan_id)
    body = await _body(request)
    try:
        slot_id = int(body.get("slot_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Pick one of the plan's times.")
    if plan.status != "open":
        raise HTTPException(400, "This plan is not open - reopen it to choose a different time." if plan.status == "confirmed" else "This plan was cancelled.")
    slot = db.query(SessionPlanSlot).filter(SessionPlanSlot.id == slot_id, SessionPlanSlot.plan_id == plan.id).first()
    if not slot:
        raise HTTPException(400, "That time is not one of this plan's.")
    plan.status, plan.confirmed_slot_id = "confirmed", slot.id
    if body.get("create_session") and not (plan.session_id and db.get(GameSession, plan.session_id)):
        last = db.query(GameSession).filter(GameSession.world_id == world.id).order_by(GameSession.session_num.desc()).first()
        gs = GameSession(
            world_id=world.id, title=plan.title, session_num=((last.session_num or 0) + 1) if last else 1,
            session_date=_valid_date(body.get("session_date")) or slot.starts_at.strftime("%Y-%m-%d"),
        )
        db.add(gs)
        db.flush()
        plan.session_id = gs.id
    db.commit()
    live.touch(world.id)
    return {"ok": True, "session_id": plan.session_id}


@router.post("/api/schedule/plans/{plan_id}/reopen")
def plan_reopen(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    if not is_manager:
        raise HTTPException(403)
    plan = _plan_or_404(db, world, plan_id)
    if plan.status == "open":
        raise HTTPException(400, "This plan is already open.")
    plan.status, plan.confirmed_slot_id = "open", None
    db.commit()
    live.touch(world.id)
    return {"ok": True}


@router.post("/api/schedule/plans/{plan_id}/cancel")
def plan_cancel(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    plan = _plan_or_404(db, world, plan_id)
    if not _can_touch(user, is_manager, plan.created_by_user_id):
        raise HTTPException(403)
    plan.status, plan.confirmed_slot_id = "cancelled", None
    db.commit()
    live.touch(world.id)
    return {"ok": True}


@router.post("/api/schedule/plans/{plan_id}/delete")
def plan_delete(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, is_manager = _ctx(request, db, active_world)
    plan = _plan_or_404(db, world, plan_id)
    if not _can_touch(user, is_manager, plan.created_by_user_id):
        raise HTTPException(403)
    db.query(SessionPlanVote).filter(SessionPlanVote.plan_id == plan.id).delete()
    db.query(SessionPlanSlot).filter(SessionPlanSlot.plan_id == plan.id).delete()
    db.delete(plan)
    db.commit()
    live.touch(world.id)
    return {"ok": True}


# ── .ics ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _event_for(plan: SessionPlan, slot: SessionPlanSlot, world) -> dict:
    start = slot.starts_at
    return {
        "uid": f"plan-{plan.id}-{world.id}@nd-world",
        "start": start, "end": start + timedelta(minutes=plan.duration_min or DEFAULT_DURATION_MIN),
        "summary": plan.title, "description": plan.notes or "", "location": plan.location or "",
    }


def _ics_response(text: str, filename: str) -> Response:
    return Response(text, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/schedule/plans/{plan_id:int}.ics")
def plan_ics(plan_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    user, world, _ = _ctx(request, db, active_world)
    plan = _plan_or_404(db, world, plan_id)
    slot = db.get(SessionPlanSlot, plan.confirmed_slot_id) if plan.confirmed_slot_id else None
    if plan.status != "confirmed" or not slot or slot.plan_id != plan.id:
        raise HTTPException(404, "No time has been chosen for this plan yet.")
    return _ics_response(ical.build_calendar([_event_for(plan, slot, world)], name=world.name), f"session-{plan.id}.ics")


@router.get("/schedule/confirmed.ics")
def confirmed_ics(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Every confirmed session from yesterday on, as one file to import into a calendar app."""
    user, world, _ = _ctx(request, db, active_world)
    cutoff = datetime.utcnow() - timedelta(days=1)
    events = []
    for plan in db.query(SessionPlan).filter(SessionPlan.world_id == world.id, SessionPlan.status == "confirmed").all():
        slot = db.get(SessionPlanSlot, plan.confirmed_slot_id) if plan.confirmed_slot_id else None
        if slot and slot.plan_id == plan.id and slot.starts_at >= cutoff:
            events.append(_event_for(plan, slot, world))
    events.sort(key=lambda e: e["start"])
    return _ics_response(ical.build_calendar(events, name=f"{world.name} sessions"), "sessions.ics")
