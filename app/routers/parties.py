import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CombatSession, Entity, Party, PlayerCharacter, Quest, World
from .combat import entity_to_combatant, pc_to_combatant, _COMBATANT_KINDS

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []


def _get_world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


@router.get("/parties", response_class=HTMLResponse)
def parties_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    parties = db.query(Party).filter(Party.world_id == (world.id if world else 1)).order_by(Party.name).all()
    member_counts = {
        p.id: len(json.loads(p.member_pc_ids_json or "[]")) + len(json.loads(p.member_entity_ids_json or "[]"))
        for p in parties
    }
    return templates.TemplateResponse("parties/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "parties": parties, "member_counts": member_counts,
    })


@router.post("/parties/new")
def party_create(name: str = Form("New Party"), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    p = Party(world_id=world.id if world else 1, name=name.strip() or "New Party")
    db.add(p)
    db.commit()
    db.refresh(p)
    return RedirectResponse(f"/parties/{p.id}", status_code=303)


@router.get("/parties/{party_id}", response_class=HTMLResponse)
def party_detail(party_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    entity_ids = json.loads(party.member_entity_ids_json or "[]")
    member_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
    member_entities = db.query(Entity).filter(Entity.id.in_(entity_ids)).all() if entity_ids else []
    all_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == party.world_id).order_by(PlayerCharacter.name).all()
    all_entities = db.query(Entity).filter(
        Entity.world_id == party.world_id, Entity.kind.in_(_COMBATANT_KINDS)
    ).order_by(Entity.name).all()
    assigned_quests = db.query(Quest).filter(Quest.assigned_party_id == party.id).all()
    loot = json.loads(party.loot_json or "[]")
    return templates.TemplateResponse("parties/detail.html", {
        "request": request, "world": world, "worlds": worlds, "party": party,
        "member_pcs": member_pcs, "member_entities": member_entities,
        "all_pcs": all_pcs, "all_entities": all_entities,
        "assigned_quests": assigned_quests, "loot": loot,
        "pc_ids": pc_ids, "entity_ids": entity_ids,
    })


@router.post("/parties/{party_id}/edit")
async def party_edit(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    form = await request.form()
    party.name = str(form.get("name", party.name)).strip() or party.name
    party.notes = str(form.get("notes", "")).strip()
    pc_ids = [int(v) for v in form.getlist("member_pc_ids")]
    entity_ids = [int(v) for v in form.getlist("member_entity_ids")]
    party.member_pc_ids_json = json.dumps(pc_ids)
    party.member_entity_ids_json = json.dumps(entity_ids)
    db.commit()
    return RedirectResponse(f"/parties/{party_id}", status_code=303)


@router.post("/parties/{party_id}/delete")
def party_delete(party_id: int, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    db.query(Quest).filter(Quest.assigned_party_id == party_id).update({"assigned_party_id": None})
    db.delete(party)
    db.commit()
    return RedirectResponse("/parties", status_code=303)


@router.post("/api/parties/{party_id}/loot")
async def party_loot(party_id: int, request: Request, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    body = await request.json()
    action = body.get("action")
    loot = json.loads(party.loot_json or "[]")
    if action == "add":
        loot.append({"name": body.get("name", "Item"), "qty": int(body.get("qty", 1) or 1), "notes": body.get("notes", "")})
    elif action == "remove":
        idx = int(body.get("index", -1))
        if 0 <= idx < len(loot):
            loot.pop(idx)
    party.loot_json = json.dumps(loot)
    db.commit()
    return {"loot": loot}


@router.post("/api/parties/{party_id}/launch-combat")
def party_launch_combat(party_id: int, db: Session = Depends(get_db)):
    party = db.query(Party).filter(Party.id == party_id).first()
    if not party:
        raise HTTPException(404)
    pc_ids = json.loads(party.member_pc_ids_json or "[]")
    entity_ids = json.loads(party.member_entity_ids_json or "[]")
    combatants = []
    for pc in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []:
        combatants.append(pc_to_combatant(pc))
    for ent in db.query(Entity).filter(Entity.id.in_(entity_ids)).all() if entity_ids else []:
        combatants.append(entity_to_combatant(ent))
    cs = CombatSession(world_id=party.world_id, name=f"{party.name} Encounter", combatants_json=json.dumps(combatants))
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return {"redirect": f"/combat/{cs.id}"}
