from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from ..database import get_db
from ..deps import get_world_ctx
from ..models import Fact, GameSession
from ..templating import templates
from .sessions import clear_session_log_recap_cache

router = APIRouter()


@router.get("/facts", response_class=HTMLResponse)
def facts_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    facts = (
        db.query(Fact)
        .filter(Fact.world_id == world_id)
        .order_by(Fact.created_at.desc())
        .all()
    )
    sessions = {
        s.id: s for s in db.query(GameSession).filter(GameSession.world_id == world_id).all()
    }
    return templates.TemplateResponse("facts/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "facts": facts, "sessions": sessions,
    })


@router.post("/facts/new")
async def fact_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    form = await request.form()
    user = getattr(request.state, "user", None)
    content = str(form.get("content", "")).strip()
    if not content:
        return RedirectResponse("/facts", status_code=303)
    session_id = form.get("game_session_id")
    f = Fact(
        world_id=world.id if world else 1,
        game_session_id=int(session_id) if session_id else None,
        content=content,
        visible_to_players=form.get("visible_to_players") is not None,
        author_id=user.id if user else None,
    )
    db.add(f)
    db.commit()
    clear_session_log_recap_cache()
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/edit")
async def fact_edit(fact_id: int, request: Request, db: Session = Depends(get_db)):
    fact = db.get(Fact, fact_id)
    if not fact:
        raise HTTPException(404)
    form = await request.form()
    content = str(form.get("content", "")).strip()
    if content:
        fact.content = content
    fact.visible_to_players = form.get("visible_to_players") is not None
    session_id = form.get("game_session_id")
    fact.game_session_id = int(session_id) if session_id else None
    db.commit()
    clear_session_log_recap_cache()
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/delete")
def fact_delete(fact_id: int, db: Session = Depends(get_db)):
    fact = db.get(Fact, fact_id)
    if not fact:
        raise HTTPException(404)
    db.delete(fact)
    db.commit()
    clear_session_log_recap_cache()
    return RedirectResponse("/facts", status_code=303)


@router.post("/api/facts/parse")
async def api_facts_parse(request: Request):
    """Turns a rough recap into draft facts via the local model — returns the
    draft list without writing anything to the DB. The GM reviews/edits in
    the UI, then POST /api/facts/bulk does the actual write once confirmed."""
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "No recap text provided")
    try:
        facts = await _ai_module.parse_facts_from_recap(text)
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    return {"facts": facts}


@router.post("/api/facts/bulk")
async def api_facts_bulk(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Bulk-insert facts — the recap-review UI's Confirm & Save action."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    user = getattr(request.state, "user", None)
    items = body.get("facts")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, '"facts" must be a non-empty list')
    session_id = body.get("game_session_id")
    session_id = int(session_id) if session_id else None
    created = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        f = Fact(
            world_id=world.id,
            game_session_id=session_id,
            content=content,
            visible_to_players=bool(item.get("visible_to_players", True)),
            author_id=user.id if user else None,
        )
        db.add(f)
        created.append(f)
    db.commit()
    clear_session_log_recap_cache()
    return {"created": len(created)}
