import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx, is_gm, world_can_edit_row, world_can_edit_section, world_can_view_section, world_row_visible
from ..models import Entity, Party, Quest, World
from ..templating import templates

router = APIRouter()

STATUSES = ["active", "complete", "failed", "secret"]
CATEGORIES = ["main", "side", "personal"]


def _current_user_id(request: Request):
    user = getattr(request.state, "user", None)
    return user.id if user else None


@router.get("/quests", response_class=HTMLResponse)
def quests_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "quests"):
        raise HTTPException(403)
    q = db.query(Quest).filter(Quest.world_id == world.id)
    if not is_gm(request):
        # visible_to_players=False is the GM's "hide this quest from the
        # table entirely" flag — the MCP list tool and the world-summary
        # pipeline already honor it; the web list must too, or a player
        # granted quests-read sees hidden titles/summaries/bodies.
        q = q.filter(Quest.visible_to_players.isnot(False))
    quests = q.order_by(Quest.title).all()
    grouped: dict = {s: [] for s in STATUSES}
    for q in quests:
        grouped.setdefault(q.status or "active", []).append(q)
    return templates.TemplateResponse("quests/list.html", {
        "request": request, "world": world, "worlds": worlds, "grouped": grouped, "statuses": STATUSES,
        "can_create": world_can_edit_section(request, world, "quests"),
    })


@router.get("/quests/new", response_class=HTMLResponse)
def quest_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world or not world_can_edit_section(request, world, "quests"):
        raise HTTPException(403)
    parties = db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all()
    quests = db.query(Quest).filter(Quest.world_id == world.id).order_by(Quest.title).all()
    entities = db.query(Entity).filter(Entity.world_id == world.id).order_by(Entity.name).all()
    return templates.TemplateResponse("quests/detail.html", {
        "request": request, "world": world, "worlds": worlds, "quest": None,
        "parties": parties, "quests": quests, "entities": entities,
        "linked_entities": [], "statuses": STATUSES, "categories": CATEGORIES,
        "can_edit_this": True,
    })


@router.post("/quests/new")
async def quest_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world or not world_can_edit_section(request, world, "quests"):
        raise HTTPException(403)
    form = await request.form()
    # A plain player creating under their own "quests: edit" grant owns the
    # row (see Quest.created_by_user_id) and may only ever edit/delete this
    # one going forward — a GM or assistant creating one leaves it
    # GM-authored (None), same as every quest before this column existed,
    # since both already have unrestricted edit rights on every quest here.
    user = getattr(request.state, "user", None)
    is_gm_or_assistant = bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))
    q = Quest(
        world_id=world.id,
        title=str(form.get("title", "")).strip() or "Untitled Quest",
        status=str(form.get("status", "active")).strip() or "active",
        category=str(form.get("category", "main")).strip() or "main",
        summary=str(form.get("summary", "")).strip(),
        body=str(form.get("body", "")),
        parent_id=int(form["parent_id"]) if form.get("parent_id") else None,
        assigned_party_id=int(form["assigned_party_id"]) if form.get("assigned_party_id") else None,
        created_by_user_id=None if is_gm_or_assistant else _current_user_id(request),
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
    world, worlds = get_world_ctx(request, db, active_world)
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest or not world_row_visible(request, db, quest.world_id, "quests"):
        raise HTTPException(404)
    # Same hidden-quest rule as the list route: a non-GM gets a 404, not the
    # body of a quest the GM explicitly hid (guessable sequential ids).
    if not quest.visible_to_players and not is_gm(request):
        raise HTTPException(404)
    quest_world = world if (world and world.id == quest.world_id) else db.get(World, quest.world_id)
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
        "can_edit_this": world_can_edit_row(request, quest_world, "quests", quest.created_by_user_id),
    })


@router.post("/quests/{quest_id}/edit")
async def quest_edit(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    world = db.get(World, quest.world_id)
    if not world_can_edit_row(request, world, "quests", quest.created_by_user_id):
        raise HTTPException(403)
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
    world = db.get(World, quest.world_id)
    if not world_can_edit_row(request, world, "quests", quest.created_by_user_id):
        raise HTTPException(403)
    body = await request.json()
    status = str(body.get("status", "")).strip()
    if status:
        quest.status = status
        db.commit()
    return {"status": quest.status}


@router.post("/quests/{quest_id}/delete")
def quest_delete(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    world = db.get(World, quest.world_id)
    if not world_can_edit_row(request, world, "quests", quest.created_by_user_id):
        raise HTTPException(403)
    db.query(Quest).filter(Quest.parent_id == quest_id).update({"parent_id": None})
    db.delete(quest)
    db.commit()
    return RedirectResponse("/quests", status_code=303)
