"""Shared dice roller + roll log (world-scoped).

Notation is system-agnostic on purpose — nd-world also hosts non-N&D
worlds via Sheet Templates, so the roller accepts plain dice expressions
(``2d6+3``, ``1d20-1``, ``4d8+2d6+1``) rather than hardcoding one game's
dice-pool math. The breakdown it stores is enough for any system's GM to
apply their own rules on top.

Player-safe by design: rolling dice and seeing the table's roll history is
a whole-table activity, so GET/POST /dice and the two /api/dice/ endpoints
are listed in _is_player_safe (app/main.py) — world membership (via
get_world_ctx) is the only gate.
"""
import json
import random
import re

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import DiceRoll
from ..templating import templates

router = APIRouter()

MAX_DICE_PER_TERM = 50
MAX_SIDES = 1000
MAX_TERMS = 8

# One dice term or flat modifier: "2d6", "d20", "+3", "-1"
_TERM = r"[+-]?\s*(?:\d{0,3}d\d{1,4}|\d{1,6})"
_NOTATION_RE = re.compile(rf"^{_TERM}(?:\s*{_TERM})*$", re.IGNORECASE)
_TERM_RE = re.compile(r"([+-]?)\s*(?:(\d{0,3})d(\d{1,4})|(\d{1,6}))", re.IGNORECASE)

HISTORY_LIMIT = 50


def parse_and_roll(notation: str) -> tuple:
    """Roll a notation string. Returns (breakdown, total) where breakdown is
    a JSON-serializable list of per-term results. Raises ValueError with a
    user-presentable message on anything the grammar doesn't accept."""
    text = (notation or "").strip()
    if not text:
        raise ValueError("Enter a dice notation like 2d6+3")
    if len(text) > 120:
        raise ValueError("Notation is too long")
    if not _NOTATION_RE.match(text):
        raise ValueError("Use dice notation like 2d6+3, d20-1, or 4d8+2d6+1")

    breakdown = []
    total = 0
    terms = _TERM_RE.findall(text)
    if len(terms) > MAX_TERMS:
        raise ValueError(f"At most {MAX_TERMS} terms per roll")
    for sign, count, sides, flat in terms:
        negative = sign == "-"
        if flat:
            value = int(flat)
            term_total = -value if negative else value
            breakdown.append({"term": ("-" if negative else "+") + flat, "sum": term_total})
        else:
            n = int(count) if count else 1
            sides = int(sides)
            if n < 1:
                raise ValueError("Roll at least one die")
            if n > MAX_DICE_PER_TERM:
                raise ValueError(f"At most {MAX_DICE_PER_TERM} dice per term")
            if sides < 2:
                raise ValueError("Dice need at least 2 sides")
            if sides > MAX_SIDES:
                raise ValueError(f"Dice can have at most {MAX_SIDES} sides")
            rolls = [random.randint(1, sides) for _ in range(n)]
            term_total = sum(rolls) * (-1 if negative else 1)
            breakdown.append({
                "term": ("-" if negative else "+") + f"{n}d{sides}",
                "rolls": rolls,
                "sum": term_total,
            })
        total += term_total
    return breakdown, total


def _roll_response(roll: DiceRoll) -> dict:
    return {
        "id": roll.id,
        "user_name": roll.user_name,
        "notation": roll.notation,
        "breakdown": json.loads(roll.breakdown or "[]"),
        "total": roll.total,
        "created_at": roll.created_at.isoformat() + "Z" if roll.created_at else None,
    }


def _store_roll(db: Session, request: Request, world, notation: str) -> DiceRoll:
    breakdown, total = parse_and_roll(notation)
    user = getattr(request.state, "user", None)
    roll = DiceRoll(
        world_id=world.id,
        user_id=user.id if user else None,
        user_name=(user.display_name if user and user.display_name else "Someone"),
        notation=(notation or "").strip(),
        breakdown=json.dumps(breakdown),
        total=total,
    )
    db.add(roll)
    db.commit()
    return roll


@router.get("/dice", response_class=HTMLResponse)
def dice_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    rolls = []
    if world:
        rows = (
            db.query(DiceRoll)
            .filter(DiceRoll.world_id == world.id)
            .order_by(DiceRoll.created_at.desc(), DiceRoll.id.desc())
            .limit(HISTORY_LIMIT)
            .all()
        )
        rolls = [_roll_response(r) for r in rows]
    return templates.TemplateResponse("dice.html", {
        "request": request, "world": world, "worlds": worlds,
        "rolls": rolls,
        "error": request.query_params.get("error", ""),
        "last_notation": request.query_params.get("n", ""),
    })


@router.post("/dice")
async def dice_roll_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    form = await request.form()
    notation = str(form.get("notation", ""))
    try:
        _store_roll(db, request, world, notation)
    except ValueError as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/dice?error={quote(str(exc))}&n={quote(notation)}", status_code=303)
    return RedirectResponse("/dice", status_code=303)


class RollBody(BaseModel):
    notation: str


@router.post("/api/dice/roll")
async def api_dice_roll(body: RollBody, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    try:
        roll = _store_roll(db, request, world, body.notation)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _roll_response(roll)


@router.get("/api/dice/history")
def api_dice_history(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    rows = (
        db.query(DiceRoll)
        .filter(DiceRoll.world_id == world.id)
        .order_by(DiceRoll.created_at.desc(), DiceRoll.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    return {"rolls": [_roll_response(r) for r in rows]}
