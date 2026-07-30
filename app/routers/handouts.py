"""Printable handouts: render one or more entities' body text as plain
print-friendly pages, for a GM to hand out physically or share as a PDF.
Thin wrapper over Entity.body — no new storage.
"""
from typing import List

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity
from ..rendering import render_md
from ..templating import templates

router = APIRouter()


@router.get("/handout/{entity_id}", response_class=HTMLResponse)
def handout_single(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if not entity or not world or entity.world_id != world.id:
        raise HTTPException(404)
    body_html = render_md(entity.body or "") if entity.body else ""
    return templates.TemplateResponse("handout.html", {
        "request": request, "entities": [{"entity": entity, "body_html": body_html}],
    })


@router.get("/handouts", response_class=HTMLResponse)
def handouts_gallery(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    entities = (
        db.query(Entity).filter(Entity.world_id == world.id).order_by(Entity.kind, Entity.name).all()
        if world else []
    )
    return templates.TemplateResponse("handouts_gallery.html", {
        "request": request, "world": world, "worlds": worlds, "entities": entities,
    })


@router.post("/handouts/print")
async def handouts_print(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    ids: List[int] = [int(x) for x in body.get("ids", [])]
    items = []
    for eid in ids:
        e = db.query(Entity).filter(Entity.id == eid, Entity.world_id == world.id).first()
        if e:
            items.append({"entity": e, "body_html": render_md(e.body or "") if e.body else ""})
    return templates.TemplateResponse("handout.html", {"request": request, "entities": items})
