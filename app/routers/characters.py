import base64
import io
import json
import os
import uuid
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import markdown2
from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import auth, game_catalog
from ..constants import (
    KIND_ICONS, KINDS, SUBTYPES, XP_THRESHOLDS,
    ND_DEFAULT_STATS, ND_DEFAULT_CURRENCY,
)
from ..database import get_db, get_app_settings
from ..imaging import convert_image
from ..models import PlayerCharacter, SheetTemplate, User, World, WorldMembership

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals.update(kinds=KINDS, subtypes=SUBTYPES, kind_icons=KIND_ICONS)
templates.env.filters["md"] = lambda t: (
    markdown2.markdown(t, extras=["fenced-code-blocks", "tables", "strike"]) if t else ""
)
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []

UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _world_ctx(request: Request, db: Session, active_world: Optional[str]):
    user = getattr(request.state, "user", None)
    accessible = auth.accessible_world_ids(db, user)
    q = db.query(World)
    if accessible is not None:
        q = q.filter(World.id.in_(accessible)) if accessible else q.filter(World.id.in_([]))
    worlds = q.order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


def _num(v, default=0.0):
    """stats_json/equipment_json used to only ever be written by JS that
    already coerced numbers (parseFloat(...)||0), so this never needed to be
    defensive. The general JSON importer can write these fields directly
    from arbitrary author-supplied JSON now, so a stray "" or missing value
    must degrade to `default` instead of throwing and 500ing every page that
    lists characters."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
        _num(item.get("weight"), 0) * _num(item.get("qty"), 1)
        for item in equipment if isinstance(item, dict)
    )

    # N&D derived attributes
    stat_val = {
        s["id"]: int(_num(s.get("value"), 0))
        for s in stats if isinstance(s, dict) and s.get("id")
    }
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
    pc.race_id     = gs("race_id", pc.race_id or "")
    pc.char_class  = gs("char_class")   # profession in N&D
    pc.profession_id = gs("profession_id", pc.profession_id or "")
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
    pc.minor_edge_count = max(0, gi("minor_edge_count", pc.minor_edge_count or 0))
    pc.major_edge_count = max(0, gi("major_edge_count", pc.major_edge_count or 0))

    # Sheet template
    tpl_id = data.get("sheet_template_id")
    pc.sheet_template_id = int(tpl_id) if tpl_id and str(tpl_id).isdigit() else None

    # Custom fields (free-form JSON object)
    raw_cf = data.get("custom_fields_json", "{}") or "{}"
    try:
        json.loads(raw_cf)
    except Exception:
        raw_cf = "{}"
    pc.custom_fields_json = raw_cf

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


def _upload_portrait(file: UploadFile, db: Optional[Session] = None) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    portraits_dir = UPLOADS_DIR / "portraits"
    portraits_dir.mkdir(parents=True, exist_ok=True)
    fname = uuid.uuid4().hex + ext
    dest = portraits_dir / fname
    with open(dest, "wb") as f:
        import shutil
        shutil.copyfileobj(file.file, f)
    if db is not None:
        settings = get_app_settings(db)
        dest = convert_image(dest, static_format=settings.static_format,
                              animated_format=settings.animated_format)
    else:
        dest = convert_image(dest)
    return f"/uploads/portraits/{dest.name}"


# ── List ──────────────────────────────────────────────────────────────────────

def _templates_for_world(db: Session, world_id: Optional[int]):
    q = db.query(SheetTemplate).filter(
        (SheetTemplate.world_id == None) |
        (SheetTemplate.world_id == world_id)
    ).order_by(SheetTemplate.is_builtin.desc(), SheetTemplate.name)
    return q.all()


def _group_by_section(tpl_fields):
    """Groups template fields by their `section` label, preserving both field
    order and first-seen section order (Jinja's groupby filter re-sorts
    alphabetically, which would scramble an intentionally-ordered sheet)."""
    sections, lookup = [], {}
    for f in tpl_fields:
        sec = f.get("section") or "Custom"
        if sec not in lookup:
            lookup[sec] = []
            sections.append((sec, lookup[sec]))
        lookup[sec].append(f)
    return sections


def _current_user(request: Request):
    return getattr(request.state, "user", None)


def _own_character(db: Session, world_id: int, user_id: int) -> Optional[PlayerCharacter]:
    return db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world_id, PlayerCharacter.owner_user_id == user_id
    ).first()


def _can_manage_character(user, pc: PlayerCharacter) -> bool:
    """Full edit/delete/export access: the GM, or the player who owns this character."""
    if not user or not pc:
        return False
    return user.is_gm or pc.owner_user_id == user.id


def _can_view_character(db: Session, user, pc: PlayerCharacter, world: World) -> bool:
    if _can_manage_character(user, pc):
        return True
    # Party visibility: other players may view (read-only) party members' sheets if the GM allows it.
    return bool(world and world.players_see_party and pc.owner_user_id is not None)


@router.get("/characters", response_class=HTMLResponse)
def characters_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(request, db, active_world)
    user = _current_user(request)
    pcs = []
    if world:
        q = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id)
        if user and not user.is_gm:
            if world.players_see_party:
                q = q.filter(or_(PlayerCharacter.owner_user_id == user.id, PlayerCharacter.owner_user_id.isnot(None)))
            else:
                q = q.filter(PlayerCharacter.owner_user_id == user.id)
        pcs = q.order_by(PlayerCharacter.name).all()
    derived = {pc.id: _derived(pc) for pc in pcs}
    sheet_templates_list = _templates_for_world(db, world.id if world else None)
    custom_tpl_ids = {t.id for t in sheet_templates_list if t.sheet_mode == "custom"}
    my_character = _own_character(db, world.id, user.id) if (world and user and not user.is_gm) else None
    return templates.TemplateResponse("characters/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "pcs": pcs, "derived": derived,
        "sheet_templates": sheet_templates_list,
        "custom_tpl_ids": custom_tpl_ids,
        "user": user, "my_character": my_character,
    })


# ── New ───────────────────────────────────────────────────────────────────────

@router.get("/characters/new", response_class=HTMLResponse)
def character_new_form(
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
    template_id: Optional[int] = None,
):
    world, worlds = _world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No world selected")
    user = _current_user(request)
    if user and not user.is_gm:
        existing = _own_character(db, world.id, user.id)
        if existing:
            # One character per player per world — go straight to their sheet.
            return RedirectResponse(f"/characters/{existing.id}", status_code=303)
    # Pre-select template if given
    chosen_tpl = db.query(SheetTemplate).filter(SheetTemplate.id == template_id).first() if template_id else None
    if not chosen_tpl:
        # Default to N&D template
        chosen_tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "nd-default").first()
    if chosen_tpl and chosen_tpl.sheet_mode == "custom":
        tpl_fields = json.loads(chosen_tpl.fields_json or "[]")
        return templates.TemplateResponse("characters/custom_sheet.html", {
            "request": request, "world": world, "worlds": worlds,
            "pc": None, "can_manage": True,
            "chosen_template": chosen_tpl,
            "sections": _group_by_section(tpl_fields),
            "tpl_fields": tpl_fields,
            "custom_fields": {},
        })
    return templates.TemplateResponse("characters/wizard.html", {
        "request": request, "world": world, "worlds": worlds,
        "chosen_template": chosen_tpl,
    })


@router.get("/api/characters/catalog")
def api_characters_catalog():
    """Race/profession/feat/equipment catalog for the creation wizard frontend."""
    return game_catalog.catalog_payload()


@router.post("/characters/new")
async def character_create(
    request: Request,
    portrait: UploadFile = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No world selected")
    user = _current_user(request)
    owner_id = None
    if user and not user.is_gm:
        if _own_character(db, world.id, user.id):
            raise HTTPException(400, "You already have a character in this world.")
        owner_id = user.id
    form = await request.form()
    data = dict(form)
    pc = PlayerCharacter(world_id=world.id, owner_user_id=owner_id)
    _apply_form(pc, data)
    if portrait and portrait.filename:
        url = _upload_portrait(portrait, db=db)
        if url:
            pc.portrait_url = url
    db.add(pc)
    db.commit()
    db.refresh(pc)
    return RedirectResponse(f"/characters/{pc.id}", status_code=303)


# ── Sheet Templates (must come before /{pc_id} routes) ───────────────────────

@router.get("/characters/templates", response_class=HTMLResponse)
def template_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(request, db, active_world)
    tpls = _templates_for_world(db, world.id if world else None)
    return templates.TemplateResponse("characters/templates_list.html", {
        "request": request, "world": world, "worlds": worlds, "sheet_templates": tpls,
    })


@router.get("/characters/templates/new", response_class=HTMLResponse)
def template_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(request, db, active_world)
    return templates.TemplateResponse("characters/template_form.html", {
        "request": request, "world": world, "worlds": worlds,
        "tpl": None, "fields": [],
    })


@router.post("/characters/templates/new")
async def template_create(
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _world_ctx(request, db, active_world)
    form = await request.form()
    name = str(form.get("name", "")).strip() or "Unnamed Template"
    desc = str(form.get("description", "")).strip()
    sheet_mode = "custom" if str(form.get("sheet_mode", "nd")) == "custom" else "nd"
    raw_fields = str(form.get("fields_json", "[]") or "[]")
    try:
        json.loads(raw_fields)
    except Exception:
        raw_fields = "[]"
    base_slug = name.lower().replace(" ", "-")[:50]
    slug = base_slug
    n = 1
    while db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first():
        slug = f"{base_slug}-{n}"; n += 1
    tpl = SheetTemplate(
        world_id=world.id if world else None,
        name=name, slug=slug, description=desc,
        is_builtin=False, sheet_mode=sheet_mode, fields_json=raw_fields,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return RedirectResponse(f"/characters/templates/{tpl.id}/edit", status_code=303)


@router.get("/characters/templates/{tpl_id}/edit", response_class=HTMLResponse)
def template_edit_form(tpl_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(request, db, active_world)
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    fields = json.loads(tpl.fields_json or "[]")
    return templates.TemplateResponse("characters/template_form.html", {
        "request": request, "world": world, "worlds": worlds,
        "tpl": tpl, "fields": fields,
    })


@router.post("/characters/templates/{tpl_id}/edit")
async def template_update(
    tpl_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    form = await request.form()
    if not tpl.is_builtin:
        tpl.name = str(form.get("name", tpl.name)).strip() or tpl.name
        tpl.description = str(form.get("description", "")).strip()
        tpl.sheet_mode = "custom" if str(form.get("sheet_mode", "nd")) == "custom" else "nd"
    raw_fields = str(form.get("fields_json", "[]") or "[]")
    try:
        json.loads(raw_fields)
    except Exception:
        raw_fields = "[]"
    tpl.fields_json = raw_fields
    tpl.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/characters/templates/{tpl_id}/edit?saved=1", status_code=303)


@router.post("/characters/templates/{tpl_id}/delete")
def template_delete(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    if tpl.is_builtin:
        raise HTTPException(403, "Cannot delete built-in templates")
    db.delete(tpl)
    db.commit()
    return RedirectResponse("/characters/templates", status_code=303)


@router.get("/api/characters/templates")
def api_template_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _world_ctx(request, db, active_world)
    tpls = _templates_for_world(db, world.id if world else None)
    return [
        {"id": t.id, "name": t.name, "is_builtin": t.is_builtin,
         "fields": json.loads(t.fields_json or "[]")}
        for t in tpls
    ]


# ── Sheet ─────────────────────────────────────────────────────────────────────

@router.get("/characters/{pc_id}", response_class=HTMLResponse)
def character_sheet(pc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(request, db, active_world)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not _can_view_character(db, user, pc, db.query(World).filter(World.id == pc.world_id).first()):
        raise HTTPException(403)
    can_manage = _can_manage_character(user, pc)
    chosen_tpl = db.query(SheetTemplate).filter(
        SheetTemplate.id == pc.sheet_template_id
    ).first() if pc.sheet_template_id else None
    world_members = _assignable_members(db, pc.world_id) if user and user.is_gm else []

    if chosen_tpl and chosen_tpl.sheet_mode == "custom":
        tpl_fields = json.loads(chosen_tpl.fields_json or "[]")
        custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
        return templates.TemplateResponse("characters/custom_sheet.html", {
            "request": request, "world": world, "worlds": worlds,
            "pc": pc, "can_manage": can_manage,
            "chosen_template": chosen_tpl,
            "sections": _group_by_section(tpl_fields),
            "tpl_fields": tpl_fields,
            "custom_fields": custom_fields,
            "world_members": world_members,
        })

    d = _derived(pc)
    tpl_fields = json.loads(chosen_tpl.fields_json) if chosen_tpl else []
    custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
    return templates.TemplateResponse("characters/sheet.html", {
        "request": request, "world": world, "worlds": worlds,
        "pc": pc, **d,
        "chosen_template": chosen_tpl,
        "tpl_fields": tpl_fields,
        "custom_fields": custom_fields,
        "can_manage": can_manage,
        "world_members": world_members,
    })


def _assignable_members(db: Session, world_id: int):
    """Non-GM users invited to this world — the pool a GM can assign a
    PlayerCharacter's ownership to."""
    return (
        db.query(User)
        .join(WorldMembership, WorldMembership.user_id == User.id)
        .filter(WorldMembership.world_id == world_id, User.is_gm == False)  # noqa: E712
        .order_by(User.display_name)
        .all()
    )


@router.post("/characters/{pc_id}/owner")
def character_set_owner(
    pc_id: int, request: Request,
    owner_user_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """GM-only: link (or unlink) a PlayerCharacter to a connected player's
    account. Deliberately kept separate from _apply_form/character_update —
    that route is shared with the owning player's own self-edit, which must
    never be able to reassign ownership."""
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not user or not user.is_gm:
        raise HTTPException(403)

    owner_user_id = owner_user_id.strip()
    if not owner_user_id:
        pc.owner_user_id = None
    else:
        if not owner_user_id.isdigit():
            raise HTTPException(400, "Invalid player")
        target = db.query(User).filter(User.id == int(owner_user_id), User.is_gm == False).first()  # noqa: E712
        if not target:
            raise HTTPException(404, "Player not found")
        if not db.query(WorldMembership).filter(
            WorldMembership.world_id == pc.world_id, WorldMembership.user_id == target.id
        ).first():
            raise HTTPException(400, "That player isn't invited to this world")
        other = db.query(PlayerCharacter).filter(
            PlayerCharacter.world_id == pc.world_id,
            PlayerCharacter.owner_user_id == target.id,
            PlayerCharacter.id != pc.id,
        ).first()
        if other:
            raise HTTPException(
                400,
                f'{target.display_name or target.email} already owns "{other.name}" in this world — unassign that one first.',
            )
        pc.owner_user_id = target.id
        if not pc.player_name:
            pc.player_name = target.display_name or target.email
    db.commit()
    return RedirectResponse(f"/characters/{pc_id}", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/characters/{pc_id}/edit", response_class=HTMLResponse)
def character_edit_form(pc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _world_ctx(request, db, active_world)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    chosen_tpl = db.query(SheetTemplate).filter(
        SheetTemplate.id == pc.sheet_template_id
    ).first() if pc.sheet_template_id else None
    if chosen_tpl and chosen_tpl.sheet_mode == "custom":
        # Custom-mode sheets are always-editable in place — no separate edit page.
        return RedirectResponse(f"/characters/{pc_id}", status_code=303)
    sheet_templates_list = _templates_for_world(db, world.id if world else None)
    tpl_fields = json.loads(chosen_tpl.fields_json) if chosen_tpl else []
    custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
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
        "sheet_templates": sheet_templates_list,
        "chosen_template": chosen_tpl,
        "tpl_fields": tpl_fields,
        "custom_fields": custom_fields,
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
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    form = await request.form()
    data = dict(form)
    _apply_form(pc, data)
    if portrait and portrait.filename:
        url = _upload_portrait(portrait, db=db)
        if url:
            pc.portrait_url = url
    db.commit()
    return RedirectResponse(f"/characters/{pc_id}", status_code=303)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/characters/{pc_id}/delete")
def character_delete(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    db.delete(pc)
    db.commit()
    return RedirectResponse("/characters", status_code=303)


# ── Export (.ndc — importable by NeonDragonsApp & NeonDragonsEditor) ─────────
#
# Both apps read this exact camelCase field schema (Character.kt / models/character.py
# in the UoY-Neon-Dragons rules repo). We emit a bare JSON array of character objects:
# NeonDragonsApp's CharacterCodec.decode() accepts a bare array via its legacy
# (pre-envelope) path, and NeonDragonsEditor's data/character_io.py::load_all_characters()
# treats a top-level array as its native multi-character format — so one file
# imports cleanly into both, with no changes needed in either app.

def _pc_to_ndc_dict(pc: PlayerCharacter) -> dict:
    stats = json.loads(pc.stats_json or "[]")
    stat_val = {s["id"]: int(s.get("value", 0)) for s in stats}
    feats = json.loads(pc.feats_json or "[]")
    equipment = json.loads(pc.equipment_json or "[]")
    currency = json.loads(pc.currency_json or "[]")
    d = _derived(pc)

    selected_feats, custom_feats = [], {}
    for f in feats:
        fid = f.get("id")
        if fid:
            selected_feats.append(fid)
        else:
            name = f.get("name") or ""
            if name:
                custom_feats[name] = f.get("notes") or f.get("description") or ""

    buckets = {
        "weapons": [], "armor": [], "augments": [], "bio_augments": [],
        "drones": [], "vehicles": [], "bases": [], "husks": [], "inventory": [],
    }
    custom_equipment = {}
    for e in equipment:
        eid = e.get("id")
        cat = e.get("category") or game_catalog.EQUIPMENT_CATEGORY_OF.get(eid, "")
        if eid and cat in buckets:
            buckets[cat].append(eid)
        elif eid:
            buckets["inventory"].append(eid)
        else:
            name = e.get("name") or ""
            if name:
                custom_equipment[name] = e.get("notes") or ""

    credits = next((int(c.get("value", 0)) for c in currency if (c.get("abbr") or "").upper() == "CR"), 0)

    portrait_b64 = ""
    if pc.portrait_url and pc.portrait_url.startswith("/uploads/"):
        img_path = UPLOADS_DIR / Path(pc.portrait_url).name
        if img_path.exists():
            portrait_b64 = base64.b64encode(img_path.read_bytes()).decode()

    notes = {}
    if pc.backstory:
        notes["Backstory"] = pc.backstory
    if pc.notes:
        notes["Session Notes"] = pc.notes

    race_id = pc.race_id or ""
    max_ectoplasm = current_ectoplasm = 0
    if race_id == "banshee":
        max_ectoplasm = current_ectoplasm = d["phys"] + d["ment"]

    return {
        "id": 0,
        "name": pc.name or "",
        "raceId": race_id, "raceName": pc.race or "",
        "baseRaceId": "", "baseRaceName": "",
        "professionId": pc.profession_id or "", "professionName": pc.char_class or "",
        "strength": stat_val.get("str", 0), "dexterity": stat_val.get("dex", 0),
        "body": stat_val.get("bod", 0), "perception": stat_val.get("per", 0),
        "willpower": stat_val.get("wil", 0), "intellect": stat_val.get("int", 0),
        "charisma": stat_val.get("cha", 0), "intuition": stat_val.get("itu", 0),
        "strengthBonus": 0, "dexterityBonus": 0, "bodyBonus": 0, "perceptionBonus": 0,
        "willpowerBonus": 0, "intellectBonus": 0, "charismaBonus": 0, "intuitionBonus": 0,
        "maxHealth": d["hp_max"], "currentHealth": pc.current_hp or 0,
        "temporaryHP": getattr(pc, "temp_hp", 0) or 0, "healthBonus": 0,
        "maxShock": d["shock_max"], "currentShock": d["shock_current"],
        "temporaryShock": 0, "shockBonus": 0,
        "cyberAdaptivity": d["ca_derived"], "speed": d["speed_derived"],
        "physicalPoints": d["phys"], "currentPP": d["pp_current"], "ppBonus": 0,
        "mentalPoints": d["ment"], "currentMP": d["mp_current"], "mpBonus": 0,
        "speedBonus": 0, "caBonus": 0,
        "maxEctoplasm": max_ectoplasm, "currentEctoplasm": current_ectoplasm,
        "selectedFeats": selected_feats, "creationFeats": list(selected_feats),
        "customFeats": custom_feats,
        "psyPowerSelections": {}, "jackOfTradeSelections": {},
        "linguistLanguages": [], "masterLinguistLanguages": [],
        "infectedVirus": "", "jackOfAllTradesSelections": [], "statPickerSelections": {},
        "equippedWeapons": buckets["weapons"], "customWeapons": {}, "weaponFeats": {},
        "activeWeapons": list(buckets["weapons"]),
        "equippedArmor": buckets["armor"], "armorFeats": {}, "activeArmor": list(buckets["armor"]),
        "installedAugments": buckets["augments"], "customAugments": {}, "augmentFeats": {},
        "activeAugments": list(buckets["augments"]),
        "installedBioAugments": buckets["bio_augments"], "customBioAugments": {}, "bioAugmentFeats": {},
        "activeBioAugments": list(buckets["bio_augments"]),
        "ownedDrones": buckets["drones"], "droneFeats": {},
        "ownedVehicles": buckets["vehicles"], "vehicleFeats": {},
        "inventory": buckets["inventory"], "customEquipment": custom_equipment,
        "ownedBases": buckets["bases"], "customBases": {},
        "ownedHusks": buckets["husks"], "equippedHuskId": "",
        "huskCurrentHealth": 0, "huskCurrentPP": 0,
        "craftedItems": [], "credits": credits,
        "yellowSat": 0, "transcendPts": 0, "transcendencePts": 0, "heartPres": 0,
        "dragonBlood": "", "dragonbloodedStatGroup": "", "fleshGraftPts": 0, "flashGraftsPts": 0,
        "crimsonBoost1": "", "crimsonBoost2": "", "crimsonPenalty": "", "mentalCond": "", "ahCharges": 0,
        "currentXP": pc.xp or 0, "xpSpent": 0,
        "majorEdges": getattr(pc, "major_edge_count", 0) or 0,
        "minorEdges": getattr(pc, "minor_edge_count", 0) or 0,
        "portraitBase64": portrait_b64,
        "notes": notes,
        "featSpecialAttrValues": {}, "professionSpecialAttrValues": {}, "raceSpecialAttrValues": {},
    }


@router.get("/characters/{pc_id}/export.ndc")
def character_export_ndc(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    payload = json.dumps([_pc_to_ndc_dict(pc)], ensure_ascii=False, indent=2)
    fname = "".join(c if c.isalnum() or c in " -_" else "" for c in (pc.name or "character")) or "character"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}.ndc"'},
    )


# ── AJAX: HP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/hp-async")
async def character_hp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
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
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
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
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
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
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
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
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
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
