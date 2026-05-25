import json
import os
import uuid
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import markdown2
from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..constants import (
    KIND_ICONS, KINDS, SUBTYPES, XP_THRESHOLDS,
    ND_DEFAULT_STATS, ND_DEFAULT_CURRENCY,
)
from ..database import get_db
from ..models import PlayerCharacter, World

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals.update(kinds=KINDS, subtypes=SUBTYPES, kind_icons=KIND_ICONS)
templates.env.filters["md"] = lambda t: (
    markdown2.markdown(t, extras=["fenced-code-blocks", "tables", "strike"]) if t else ""
)

UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


def _derived(pc: PlayerCharacter) -> dict:
    stats     = json.loads(pc.stats_json      or "[]")
    currency  = json.loads(pc.currency_json   or "[]")
    equipment = json.loads(pc.equipment_json  or "[]")
    feats     = json.loads(pc.feats_json      or "[]")
    attacks   = json.loads(pc.attacks_json    or "[]")
    cyberware = json.loads(getattr(pc, "cyberware_json",  None) or "[]")
    conditions = json.loads(getattr(pc, "conditions_json", None) or "[]")

    lvl = min(pc.level, 20)
    xp_lo = XP_THRESHOLDS[lvl - 1]
    xp_hi = XP_THRESHOLDS[lvl] if lvl < 20 else None
    if xp_hi and xp_hi > xp_lo:
        xp_pct = min(100, int(max(0, pc.xp - xp_lo) * 100 / (xp_hi - xp_lo)))
    else:
        xp_pct = 100

    total_weight = sum(
        float(item.get("weight", 0)) * int(item.get("qty", 1))
        for item in equipment
    )

    # N&D derived attributes
    stat_val = {s["id"]: int(s.get("value", 0)) for s in stats}
    phys = (stat_val.get("str", 0) + stat_val.get("dex", 0)
            + stat_val.get("bod", 0) + stat_val.get("per", 0))
    ment = (stat_val.get("wil", 0) + stat_val.get("int", 0)
            + stat_val.get("cha", 0) + stat_val.get("itu", 0))
    hp_max_derived    = phys + 10
    shock_max_derived = ment
    ca_derived        = stat_val.get("wil", 0) + stat_val.get("bod", 0)
    speed_derived     = stat_val.get("dex", 0) + stat_val.get("itu", 0)

    # Use stored override if non-zero, else fall back to derived value
    hp_max        = pc.max_hp if pc.max_hp > 0 else hp_max_derived
    shock_max     = (getattr(pc, "shock_max", 0) or 0) if (getattr(pc, "shock_max", 0) or 0) > 0 else shock_max_derived
    shock_current = getattr(pc, "shock_current", 0) or 0
    pp_current    = getattr(pc, "pp_current",    0) or 0
    mp_current    = getattr(pc, "mp_current",    0) or 0

    secondary = {
        "name": "Shock",
        "max": shock_max,
        "current": shock_current,
    }

    return {
        "stats": stats,
        "currency": currency,
        "secondary": secondary,
        "xp_lo": xp_lo, "xp_hi": xp_hi, "xp_pct": xp_pct,
        "equipment": equipment, "feats": feats, "attacks": attacks,
        "total_weight": total_weight,
        "cyberware": cyberware,
        "conditions": conditions,
        "phys": phys, "ment": ment,
        "hp_max_derived": hp_max_derived,
        "hp_max": hp_max,
        "shock_max_derived": shock_max_derived,
        "ca_derived": ca_derived,
        "speed_derived": speed_derived,
        "shock_max": shock_max,
        "shock_current": shock_current,
        "pp_current": pp_current,
        "mp_current": mp_current,
        "minor_edge": getattr(pc, "minor_edge", "") or "",
        "major_edge": getattr(pc, "major_edge", "") or "",
    }


def _apply_form(pc: PlayerCharacter, data: dict):
    def gi(k, d=0):  return int(data.get(k) or d)
    def gs(k, d=""): return str(data.get(k) or d).strip()

    pc.name        = gs("name") or "Unnamed"
    pc.player_name = gs("player_name")
    pc.race        = gs("race")
    pc.char_class  = gs("char_class")   # profession in N&D
    pc.level       = max(1, min(20, gi("level", 1)))
    pc.xp          = max(0, gi("xp"))
    pc.backstory   = gs("backstory")
    pc.notes       = gs("notes")

    # HP — max_hp=0 means "use auto-derived value"; store 0 so sheet uses derived
    pc.max_hp     = max(0, gi("max_hp", 0))
    pc.current_hp = gi("current_hp", pc.max_hp)

    # N&D resources
    pc.shock_max     = max(0, gi("shock_max"))
    pc.shock_current = max(0, gi("shock_current"))
    pc.pp_current    = max(0, gi("pp_current"))
    pc.mp_current    = max(0, gi("mp_current"))

    # Edges
    pc.minor_edge = gs("minor_edge")
    pc.major_edge = gs("major_edge")

    # JSON fields (no skills_json in N&D)
    for field in ("stats_json", "skills_json", "currency_json",
                  "equipment_json", "feats_json", "attacks_json",
                  "cyberware_json", "conditions_json"):
        # skills_json kept for DB compatibility but not used in N&D
        raw = data.get(field, "[]") or "[]"
        try:
            json.loads(raw)
        except Exception:
            raw = "[]"
        setattr(pc, field, raw)

    pc.updated_at = datetime.utcnow()


def _upload_portrait(file: UploadFile) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    portraits_dir = UPLOADS_DIR / "portraits"
    portraits_dir.mkdir(parents=True, exist_ok=True)
    fname = uuid.uuid4().hex + ext
    with open(portraits_dir / fname, "wb") as f:
        import shutil
        shutil.copyfileobj(file.file, f)
    return f"/uploads/portraits/{fname}"


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/characters", response_class=HTMLResponse)
def characters_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(db, active_world)
    pcs = (
        db.query(PlayerCharacter)
        .filter(PlayerCharacter.world_id == world.id)
        .order_by(PlayerCharacter.name)
        .all()
    ) if world else []
    derived = {pc.id: _derived(pc) for pc in pcs}
    return templates.TemplateResponse("characters/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "pcs": pcs, "derived": derived,
    })


# ── New ───────────────────────────────────────────────────────────────────────

@router.get("/characters/new", response_class=HTMLResponse)
def character_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(db, active_world)
    return templates.TemplateResponse("characters/form.html", {
        "request": request, "world": world, "worlds": worlds,
        "pc": None,
        "nd_default_stats": ND_DEFAULT_STATS,
        "nd_default_currency": ND_DEFAULT_CURRENCY,
        "stats": [], "currency": [],
        "equipment": [], "feats": [], "cyberware": [],
    })


@router.post("/characters/new")
async def character_create(
    request: Request,
    portrait: UploadFile = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _world_ctx(db, active_world)
    if not world:
        raise HTTPException(400, "No world selected")
    form = await request.form()
    data = dict(form)
    pc = PlayerCharacter(world_id=world.id)
    _apply_form(pc, data)
    if portrait and portrait.filename:
        url = _upload_portrait(portrait)
        if url:
            pc.portrait_url = url
    db.add(pc)
    db.commit()
    db.refresh(pc)
    return RedirectResponse(f"/characters/{pc.id}", status_code=303)


# ── Sheet ─────────────────────────────────────────────────────────────────────

@router.get("/characters/{pc_id}", response_class=HTMLResponse)
def character_sheet(pc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(db, active_world)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    d = _derived(pc)
    return templates.TemplateResponse("characters/sheet.html", {
        "request": request, "world": world, "worlds": worlds,
        "pc": pc, **d,
    })


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/characters/{pc_id}/edit", response_class=HTMLResponse)
def character_edit_form(pc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(db, active_world)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    return templates.TemplateResponse("characters/form.html", {
        "request": request, "world": world, "worlds": worlds,
        "pc": pc,
        "nd_default_stats": ND_DEFAULT_STATS,
        "nd_default_currency": ND_DEFAULT_CURRENCY,
        "stats":      json.loads(pc.stats_json      or "[]"),
        "currency":   json.loads(pc.currency_json   or "[]"),
        "equipment":  json.loads(pc.equipment_json  or "[]"),
        "feats":      json.loads(pc.feats_json       or "[]"),
        "cyberware":  json.loads(getattr(pc, "cyberware_json", None) or "[]"),
    })


@router.post("/characters/{pc_id}/edit")
async def character_update(
    pc_id: int,
    request: Request,
    portrait: UploadFile = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    form = await request.form()
    data = dict(form)
    _apply_form(pc, data)
    if portrait and portrait.filename:
        url = _upload_portrait(portrait)
        if url:
            pc.portrait_url = url
    db.commit()
    return RedirectResponse(f"/characters/{pc_id}", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/characters/{pc_id}/delete")
def character_delete(pc_id: int, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    db.delete(pc)
    db.commit()
    return RedirectResponse("/characters", status_code=303)


# ── AJAX: HP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/hp-async")
async def character_hp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    action = body.get("action", "set")
    val = int(body.get("value", 0))
    # Resolve effective HP max (0 stored = auto-derived from physical stats)
    stats = json.loads(pc.stats_json or "[]")
    stat_val = {s["id"]: int(s.get("value", 0)) for s in stats}
    phys = (stat_val.get("str", 0) + stat_val.get("dex", 0)
            + stat_val.get("bod", 0) + stat_val.get("per", 0))
    eff_max_hp = pc.max_hp if pc.max_hp > 0 else phys + 10
    temp_hp = getattr(pc, "temp_hp", 0) or 0
    if action == "delta":
        pc.current_hp = max(0, min(eff_max_hp + temp_hp, pc.current_hp + val))
    elif action == "temp":
        pc.temp_hp = max(0, val)
    elif action == "max":
        pc.max_hp = max(0, val)
        pc.current_hp = min(pc.current_hp, pc.max_hp if pc.max_hp > 0 else eff_max_hp)
    else:
        pc.current_hp = max(0, min(eff_max_hp + temp_hp, val))
    db.commit()
    return {
        "current_hp": pc.current_hp,
        "max_hp": pc.max_hp if pc.max_hp > 0 else eff_max_hp,
        "temp_hp": getattr(pc, "temp_hp", 0) or 0,
        "death_success": getattr(pc, "death_saves_success", 0) or 0,
        "death_failure": getattr(pc, "death_saves_failure", 0) or 0,
        "secondary_current": getattr(pc, "secondary_resource_current", 0),
    }


# ── AJAX: Shock ───────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/shock")
async def character_shock_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    action = body.get("action", "set")
    val = int(body.get("value", 0))
    shock_max = getattr(pc, "shock_max", 0) or 0
    shock_current = getattr(pc, "shock_current", 0) or 0
    if action == "delta":
        shock_current = max(0, min(shock_max, shock_current + val))
    elif action == "set":
        shock_current = max(0, min(shock_max, val))
    pc.shock_current = shock_current
    db.commit()
    return {"shock_current": pc.shock_current, "shock_max": pc.shock_max}


# ── AJAX: PP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/pp")
async def character_pp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    action = body.get("action", "set")
    val = int(body.get("value", 0))
    # PP max = sum of physical stats
    stats = json.loads(pc.stats_json or "[]")
    stat_val = {s["id"]: int(s.get("value", 0)) for s in stats}
    pp_max = (stat_val.get("str", 0) + stat_val.get("dex", 0)
              + stat_val.get("bod", 0) + stat_val.get("per", 0))
    pp_current = getattr(pc, "pp_current", 0) or 0
    if action == "delta":
        pp_current = max(0, min(pp_max, pp_current + val))
    elif action == "set":
        pp_current = max(0, min(pp_max, val))
    elif action == "rest":
        pp_current = min(pp_max, pp_current + pp_max // 2)
    pc.pp_current = pp_current
    db.commit()
    return {"pp_current": pc.pp_current, "pp_max": pp_max}


# ── AJAX: MP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/mp")
async def character_mp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    action = body.get("action", "set")
    val = int(body.get("value", 0))
    # MP max = sum of mental stats
    stats = json.loads(pc.stats_json or "[]")
    stat_val = {s["id"]: int(s.get("value", 0)) for s in stats}
    mp_max = (stat_val.get("wil", 0) + stat_val.get("int", 0)
              + stat_val.get("cha", 0) + stat_val.get("itu", 0))
    mp_current = getattr(pc, "mp_current", 0) or 0
    if action == "delta":
        mp_current = max(0, min(mp_max, mp_current + val))
    elif action == "set":
        mp_current = max(0, min(mp_max, val))
    elif action == "rest":
        mp_current = min(mp_max, mp_current + mp_max // 2)
    pc.mp_current = mp_current
    db.commit()
    return {"mp_current": pc.mp_current, "mp_max": mp_max}


# ── AJAX: XP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/xp")
async def character_xp(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    delta = int(body.get("delta", 0))
    pc.xp = max(0, pc.xp + delta)
    db.commit()
    lvl = min(pc.level, 20)
    xp_lo = XP_THRESHOLDS[lvl - 1]
    xp_hi = XP_THRESHOLDS[lvl] if lvl < 20 else None
    xp_pct = min(100, int(max(0, pc.xp - xp_lo) * 100 / (xp_hi - xp_lo))) if xp_hi and xp_hi > xp_lo else 100
    return {"xp": pc.xp, "xp_lo": xp_lo, "xp_hi": xp_hi, "xp_pct": xp_pct}


# ── AJAX: Dice roll ───────────────────────────────────────────────────────────

@router.post("/api/characters/roll")
async def dice_roll(request: Request):
    body = await request.json()
    expr = str(body.get("expr", "1d20")).lower().strip()
    import re
    m = re.match(r"^(\d+)d(\d+)([+-]\d+)?$", expr)
    if not m:
        return JSONResponse({"error": "Invalid dice expression"}, status_code=400)
    count = min(int(m.group(1)), 20)
    sides = int(m.group(2))
    modifier = int(m.group(3) or 0)
    if sides < 2 or sides > 1000:
        return JSONResponse({"error": "Invalid die size"}, status_code=400)
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + modifier
    return {
        "expr": expr, "rolls": rolls, "modifier": modifier,
        "total": total,
        "crit":   count == 1 and sides == 20 and rolls[0] == 20,
        "fumble": count == 1 and sides == 20 and rolls[0] == 1,
    }
