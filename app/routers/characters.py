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
    ND_DEFAULT_STATS, ND_DEFAULT_SKILLS, ND_DEFAULT_CURRENCY,
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
    stats    = json.loads(pc.stats_json    or "[]")
    skills   = json.loads(pc.skills_json   or "[]")
    currency = json.loads(pc.currency_json or "[]")
    equipment = json.loads(pc.equipment_json or "[]")
    feats     = json.loads(pc.feats_json     or "[]")
    attacks   = json.loads(pc.attacks_json   or "[]")

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

    # Build stat lookup for skill display
    stat_map = {s["id"]: s for s in stats}

    # Annotate skills with stat label for display
    annotated_skills = []
    for sk in skills:
        stat = stat_map.get(sk.get("stat_id", ""), {})
        annotated_skills.append({
            **sk,
            "stat_abbr": stat.get("abbr", sk.get("stat_id", "").upper()),
            "stat_label": stat.get("label", ""),
        })

    secondary = None
    if getattr(pc, "secondary_resource_name", ""):
        secondary = {
            "name": pc.secondary_resource_name,
            "max": pc.secondary_resource_max,
            "current": pc.secondary_resource_current,
        }

    return {
        "stats": stats,
        "skills": annotated_skills,
        "currency": currency,
        "secondary": secondary,
        "xp_lo": xp_lo, "xp_hi": xp_hi, "xp_pct": xp_pct,
        "equipment": equipment, "feats": feats, "attacks": attacks,
        "total_weight": total_weight,
    }


def _apply_form(pc: PlayerCharacter, data: dict):
    def gi(k, d=0):  return int(data.get(k) or d)
    def gs(k, d=""): return str(data.get(k) or d).strip()

    pc.name        = gs("name") or "Unnamed"
    pc.player_name = gs("player_name")
    pc.race        = gs("race")
    pc.char_class  = gs("char_class")
    pc.subclass    = gs("subclass")
    pc.level       = max(1, min(20, gi("level", 1)))
    pc.xp          = max(0, gi("xp"))
    pc.background  = gs("background")
    pc.alignment   = gs("alignment")
    pc.max_hp      = max(1, gi("max_hp", 10))
    pc.current_hp  = gi("current_hp", pc.max_hp)
    pc.temp_hp     = max(0, gi("temp_hp"))
    pc.armor_class = max(0, gi("armor_class", 10))
    pc.speed       = max(0, gi("speed", 30))
    pc.hit_dice    = gs("hit_dice", "1d8")
    pc.armor_profs  = gs("armor_profs")
    pc.weapon_profs = gs("weapon_profs")
    pc.tool_profs   = gs("tool_profs")
    pc.languages    = gs("languages")
    pc.personality_traits = gs("personality_traits")
    pc.ideals       = gs("ideals")
    pc.bonds        = gs("bonds")
    pc.flaws        = gs("flaws")
    pc.backstory    = gs("backstory")
    pc.notes        = gs("notes")
    pc.age          = gs("age")
    pc.height       = gs("height")
    pc.weight_app   = gs("weight_app")
    pc.eyes         = gs("eyes")
    pc.skin         = gs("skin")
    pc.hair         = gs("hair")

    # Secondary resource
    pc.secondary_resource_name    = gs("secondary_resource_name")
    pc.secondary_resource_max     = max(0, gi("secondary_resource_max"))
    pc.secondary_resource_current = max(0, gi("secondary_resource_current"))

    # JSON fields
    for field in ("stats_json", "skills_json", "currency_json",
                  "equipment_json", "feats_json", "attacks_json"):
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
        "nd_default_skills": ND_DEFAULT_SKILLS,
        "nd_default_currency": ND_DEFAULT_CURRENCY,
        "stats": [], "skills": [], "currency": [],
        "equipment": [], "feats": [], "attacks": [],
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
        "nd_default_skills": ND_DEFAULT_SKILLS,
        "nd_default_currency": ND_DEFAULT_CURRENCY,
        "stats":     json.loads(pc.stats_json    or "[]"),
        "skills":    json.loads(pc.skills_json   or "[]"),
        "currency":  json.loads(pc.currency_json or "[]"),
        "equipment": json.loads(pc.equipment_json or "[]"),
        "feats":     json.loads(pc.feats_json     or "[]"),
        "attacks":   json.loads(pc.attacks_json   or "[]"),
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
    if action == "delta":
        pc.current_hp = max(0, min(pc.max_hp + pc.temp_hp, pc.current_hp + val))
    elif action == "temp":
        pc.temp_hp = max(0, val)
    elif action == "max":
        pc.max_hp = max(1, val)
        pc.current_hp = min(pc.current_hp, pc.max_hp)
    elif action == "death_success":
        pc.death_saves_success = max(0, min(3, pc.death_saves_success + val))
    elif action == "death_failure":
        pc.death_saves_failure = max(0, min(3, pc.death_saves_failure + val))
    elif action == "secondary_set":
        pc.secondary_resource_current = max(0, min(pc.secondary_resource_max, val))
    elif action == "secondary_delta":
        pc.secondary_resource_current = max(0, min(pc.secondary_resource_max, pc.secondary_resource_current + val))
    else:
        pc.current_hp = max(0, min(pc.max_hp + pc.temp_hp, val))
    db.commit()
    return {
        "current_hp": pc.current_hp, "max_hp": pc.max_hp,
        "temp_hp": pc.temp_hp,
        "death_success": pc.death_saves_success,
        "death_failure": pc.death_saves_failure,
        "secondary_current": getattr(pc, "secondary_resource_current", 0),
    }


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
