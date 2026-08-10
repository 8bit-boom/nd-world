import re

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity, Fact, entity_player_access
from ..templating import templates

router = APIRouter()

_CHRONICLER_SYSTEM = (
    "You are the Chronicler, keeper of this campaign's history. Answer the question "
    "using ONLY the facts and entities listed below — never invent events, names, or "
    "details that aren't given to you. If the answer isn't in the provided context, "
    "say you don't know rather than guessing or making something up. Speak in a warm, "
    "slightly formal narrator's voice. Keep answers concise."
)


def _visible_facts(db: Session, world_id: int, user) -> list:
    """The actual security boundary: a non-GM caller never gets a GM-only
    fact's content, even loaded into the prompt — not just told not to
    repeat it. Same visible_to_players convention as Entity/EntityNote."""
    q = db.query(Fact).filter(Fact.world_id == world_id)
    if not (user and user.is_gm):
        q = q.filter(Fact.visible_to_players.isnot(False))
    return q.order_by(Fact.created_at.desc()).limit(200).all()


def _visible_entities(db: Session, world_id: int, query: str, user, limit: int = 15) -> list:
    """Filtered variant of main.py's _find_relevant_entities — duplicated
    rather than imported since routers can't import from main (main.py
    imports every router, so the reverse would be circular; see
    characters.py's _upload_portrait for the same pattern)."""
    words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
    q = db.query(Entity).filter(Entity.world_id == world_id)
    if words:
        filters = [
            or_(Entity.name.ilike(f'%{w}%'), Entity.summary.ilike(f'%{w}%'), Entity.tags.ilike(f'%{w}%'))
            for w in words
        ]
        q = q.filter(or_(*filters))
    if not (user and user.is_gm):
        if user:
            shared = db.query(entity_player_access.c.entity_id).filter(
                entity_player_access.c.user_id == user.id
            )
            q = q.filter(or_(Entity.visible_to_players.isnot(False), Entity.id.in_(shared)))
        else:
            q = q.filter(Entity.visible_to_players.isnot(False))
    return q.order_by(Entity.kind, Entity.name).limit(limit).all()


def build_chronicler_system_prompt(db: Session, world_id: int, question: str, user) -> str:
    facts = _visible_facts(db, world_id, user)
    entities = _visible_entities(db, world_id, question, user)
    lines = [_CHRONICLER_SYSTEM, "", "## Known facts"]
    if facts:
        lines.extend(f"- {f.content}" for f in facts)
    else:
        lines.append("(none recorded yet)")
    if entities:
        lines.append("")
        lines.append("## Relevant entities")
        for e in entities:
            line = f"- [{e.kind}] {e.name}"
            if e.subtype:
                line += f" ({e.subtype})"
            if e.summary:
                line += f": {e.summary}"
            lines.append(line)
    return "\n".join(lines)


@router.get("/chronicler", response_class=HTMLResponse)
def chronicler_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("chronicler.html", {
        "request": request, "world": world, "worlds": worlds,
    })


@router.post("/api/chronicler/ask")
async def chronicler_ask(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "No question provided")
    user = getattr(request.state, "user", None)
    system = build_chronicler_system_prompt(db, world.id, question, user)
    answer = await _ai_module.generate_chat([{"role": "user", "content": question}], system=system)
    return {"answer": answer}
