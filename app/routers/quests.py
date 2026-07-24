import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Entity, Party, Quest, World

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
templates.env.filters["md"] = lambda t: __import__("markdown2").markdown(
    t, extras=["fenced-code-blocks", "tables", "strike"]
) if t else ""

STATUSES = ["active", "complete", "failed", "secret"]
CATEGORIES = ["main", "side", "personal"]


def _get_world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


@router.get("/quests", response_class=HTMLResponse)
def quests_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    quests = db.query(Quest).filter(Quest.world_id == (world.id if world else 1)).order_by(Quest.title).all()
    grouped: dict = {s: [] for s in STATUSES}
    for q in quests:
        grouped.setdefault(q.status or "active", []).append(q)
    return templates.TemplateResponse("quests/list.html", {
        "request": request, "world": world, "worlds": worlds, "grouped": grouped, "statuses": STATUSES,
    })


@router.get("/quests/new", response_class=HTMLResponse)
def quest_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    parties = db.query(Party).filter(Party.world_id == (world.id if world else 1)).order_by(Party.name).all()
    quests = db.query(Quest).filter(Quest.world_id == (world.id if world else 1)).order_by(Quest.title).all()
    entities = db.query(Entity).filter(Entity.world_id == (world.id if world else 1)).order_by(Entity.name).all()
    return templates.TemplateResponse("quests/detail.html", {
        "request": request, "world": world, "worlds": worlds, "quest": None,
        "parties": parties, "quests": quests, "entities": entities,
        "linked_entities": [], "statuses": STATUSES, "categories": CATEGORIES,
    })


@router.post("/quests/new")
async def quest_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    form = await request.form()
    q = Quest(
        world_id=world.id if world else 1,
        title=str(form.get("title", "")).strip() or "Untitled Quest",
        status=str(form.get("status", "active")).strip() or "active",
        category=str(form.get("category", "main")).strip() or "main",
        summary=str(form.get("summary", "")).strip(),
        body=str(form.get("body", "")),
        parent_id=int(form["parent_id"]) if form.get("parent_id") else None,
        assigned_party_id=int(form["assigned_party_id"]) if form.get("assigned_party_id") else None,
    )
    raw_links = str(form.get("linked_entities_json", "[]") or "[]")
    try:
        json.loads(raw_links)
    except Exception:
        raw_links = "[]"
    q.linked_entities_json = raw_links
    db.add(q)
    db.commit()
    db.refresh(q)
    return RedirectResponse(f"/quests/{q.id}", status_code=303)


@router.get("/quests/{quest_id}", response_class=HTMLResponse)
def quest_detail(quest_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    parties = db.query(Party).filter(Party.world_id == quest.world_id).order_by(Party.name).all()
    quests = db.query(Quest).filter(Quest.world_id == quest.world_id, Quest.id != quest.id).order_by(Quest.title).all()
    entities = db.query(Entity).filter(Entity.world_id == quest.world_id).order_by(Entity.name).all()
    linked = json.loads(quest.linked_entities_json or "[]")
    entity_map = {e.id: e for e in db.query(Entity).filter(Entity.id.in_([l["entity_id"] for l in linked])).all()} if linked else {}
    linked_entities = [
        {"entity": {"id": entity_map[l["entity_id"]].id, "name": entity_map[l["entity_id"]].name}, "role": l.get("role", "")}
        for l in linked if entity_map.get(l["entity_id"])
    ]
    return templates.TemplateResponse("quests/detail.html", {
        "request": request, "world": world, "worlds": worlds, "quest": quest,
        "parties": parties, "quests": quests, "entities": entities,
        "linked_entities": linked_entities, "statuses": STATUSES, "categories": CATEGORIES,
    })


@router.post("/quests/{quest_id}/edit")
async def quest_edit(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    form = await request.form()
    quest.title = str(form.get("title", quest.title)).strip() or quest.title
    quest.status = str(form.get("status", "active")).strip() or "active"
    quest.category = str(form.get("category", "main")).strip() or "main"
    quest.summary = str(form.get("summary", "")).strip()
    quest.body = str(form.get("body", ""))
    parent_id = form.get("parent_id")
    quest.parent_id = int(parent_id) if parent_id and int(parent_id) != quest.id else None
    assigned = form.get("assigned_party_id")
    quest.assigned_party_id = int(assigned) if assigned else None
    raw_links = str(form.get("linked_entities_json", "[]") or "[]")
    try:
        json.loads(raw_links)
    except Exception:
        raw_links = "[]"
    quest.linked_entities_json = raw_links
    db.commit()
    return RedirectResponse(f"/quests/{quest_id}?saved=1", status_code=303)


@router.post("/api/quests/{quest_id}/status")
async def quest_status(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    body = await request.json()
    status = str(body.get("status", "")).strip()
    if status:
        quest.status = status
        db.commit()
    return {"status": quest.status}


@router.post("/quests/{quest_id}/delete")
def quest_delete(quest_id: int, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    db.query(Quest).filter(Quest.parent_id == quest_id).update({"parent_id": None})
    db.delete(quest)
    db.commit()
    return RedirectResponse("/quests", status_code=303)
