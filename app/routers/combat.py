import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CombatSession, Entity, GameSession, PlayerCharacter, World

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []

_COMBATANT_KINDS = ("character", "creature")


def _get_world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


def pc_to_combatant(pc: PlayerCharacter) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": pc.name,
        "source": "pc",
        "pc_id": pc.id,
        "entity_id": None,
        "initiative": 0,
        "max_hp": pc.max_hp or 0,
        "hp": pc.current_hp or 0,
        "max_shock": pc.shock_max or 0,
        "shock": pc.shock_current or 0,
        "armor": 0,
        "conditions": json.loads(pc.conditions_json or "[]"),
        "notes": "",
    }


def entity_to_combatant(entity: Entity) -> dict:
    fields = json.loads(entity.custom_fields_json or "{}")
    def _int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    health = _int(fields.get("health"), 5)
    armor = _int(fields.get("armor"), 0)
    return {
        "id": str(uuid.uuid4()),
        "name": entity.name,
        "source": "entity",
        "pc_id": None,
        "entity_id": entity.id,
        "initiative": 0,
        "max_hp": health,
        "hp": health,
        "max_shock": 0,
        "shock": 0,
        "armor": armor,
        "conditions": [],
        "notes": "",
    }


def manual_combatant(name: str) -> dict:
    return {
        "id": str(uuid.uuid4()), "name": name or "Combatant", "source": "manual",
        "pc_id": None, "entity_id": None, "initiative": 0,
        "max_hp": 10, "hp": 10, "max_shock": 0, "shock": 0, "armor": 0,
        "conditions": [], "notes": "",
    }


def _candidates(db: Session, world_id: int):
    pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).order_by(PlayerCharacter.name).all()
    entities = db.query(Entity).filter(
        Entity.world_id == world_id, Entity.kind.in_(_COMBATANT_KINDS)
    ).order_by(Entity.name).all()
    pc_payload = [{
        "id": pc.id, "name": pc.name,
        "max_hp": pc.max_hp or 0, "hp": pc.current_hp or 0,
        "max_shock": pc.shock_max or 0, "shock": pc.shock_current or 0,
        "conditions": json.loads(pc.conditions_json or "[]"),
    } for pc in pcs]
    entity_payload = []
    for e in entities:
        prefill = entity_to_combatant(e)
        entity_payload.append({
            "id": e.id, "name": e.name, "kind": e.kind,
            "max_hp": prefill["max_hp"], "armor": prefill["armor"],
        })
    return pcs, entities, pc_payload, entity_payload


@router.get("/combat", response_class=HTMLResponse)
def combat_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    sessions = db.query(CombatSession).filter(
        CombatSession.world_id == (world.id if world else 1)
    ).order_by(CombatSession.updated_at.desc()).all()
    combatant_counts = {s.id: len(json.loads(s.combatants_json or "[]")) for s in sessions}
    return templates.TemplateResponse("combat/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "sessions": sessions, "combatant_counts": combatant_counts,
    })


@router.post("/combat/new")
def combat_create(name: str = Form("New Encounter"), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    cs = CombatSession(world_id=world.id if world else 1, name=name.strip() or "New Encounter")
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return RedirectResponse(f"/combat/{cs.id}", status_code=303)


@router.get("/combat/{combat_id}", response_class=HTMLResponse)
def combat_detail(combat_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    _, _, pc_payload, entity_payload = _candidates(db, cs.world_id)
    game_sessions = db.query(GameSession).filter(GameSession.world_id == cs.world_id).order_by(GameSession.session_num.desc()).all()
    linked_session = db.query(GameSession).filter(GameSession.id == cs.game_session_id).first() if cs.game_session_id else None
    return templates.TemplateResponse("combat/detail.html", {
        "request": request, "world": world, "worlds": worlds, "combat": cs,
        "combatants_json": cs.combatants_json or "[]",
        "pc_payload_json": json.dumps(pc_payload),
        "entity_payload_json": json.dumps(entity_payload),
        "game_sessions": game_sessions, "linked_session": linked_session,
    })


@router.get("/api/combat/{combat_id}/state")
def combat_get_state(combat_id: int, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    return {
        "combatants": json.loads(cs.combatants_json or "[]"),
        "round_num": cs.round_num,
        "active_idx": cs.active_idx,
    }


@router.post("/combat/{combat_id}/state")
async def combat_save_state(combat_id: int, request: Request, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    body = await request.json()
    cs.combatants_json = json.dumps(body.get("combatants", []))
    cs.round_num = int(body.get("round_num", cs.round_num) or 1)
    cs.active_idx = int(body.get("active_idx", cs.active_idx) or 0)
    if "name" in body and body["name"]:
        cs.name = str(body["name"])
    db.commit()
    return {"ok": True}


@router.post("/combat/{combat_id}/delete")
def combat_delete(combat_id: int, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    db.delete(cs)
    db.commit()
    return RedirectResponse("/combat", status_code=303)


@router.post("/combat/{combat_id}/link-session")
async def combat_link_session(combat_id: int, request: Request, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    form = await request.form()
    session_id = form.get("game_session_id")
    cs.game_session_id = int(session_id) if session_id else None
    db.commit()
    return RedirectResponse(f"/combat/{combat_id}", status_code=303)


@router.post("/combat/{combat_id}/unlink-session")
def combat_unlink_session(combat_id: int, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    cs.game_session_id = None
    db.commit()
    return RedirectResponse(f"/combat/{combat_id}", status_code=303)


@router.post("/api/combat/{combat_id}/sync-characters")
def combat_sync_characters(combat_id: int, db: Session = Depends(get_db)):
    cs = db.query(CombatSession).filter(CombatSession.id == combat_id).first()
    if not cs:
        raise HTTPException(404)
    combatants = json.loads(cs.combatants_json or "[]")
    synced, skipped = [], []
    for c in combatants:
        if c.get("source") != "pc" or not c.get("pc_id"):
            skipped.append(c.get("name"))
            continue
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == c["pc_id"]).first()
        if not pc:
            skipped.append(c.get("name"))
            continue
        max_hp = pc.max_hp if pc.max_hp > 0 else c.get("max_hp", 0)
        pc.current_hp = max(0, min(max_hp, int(c.get("hp", pc.current_hp))))
        shock_max = pc.shock_max or 0
        pc.shock_current = max(0, min(shock_max, int(c.get("shock", pc.shock_current))))
        pc.conditions_json = json.dumps(c.get("conditions", []))
        synced.append(pc.name)
    db.commit()
    return {"synced": synced, "skipped": skipped}
