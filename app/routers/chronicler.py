import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from .. import retrieval as _retrieval
from ..database import get_db
from ..deps import check_llm_cooldown, get_world_ctx
from ..models import Fact
from ..templating import templates

router = APIRouter()

# Avoids re-invoking Ollama for the exact same question asked again within
# the window (a re-click, a page reload, two tabs) — keyed per (world,
# user, question) rather than per-world, since visible_facts/find_relevant_
# entities can legitimately differ between two non-GM users (a specific
# entity individually shared with one player but not another), so only the
# SAME user re-asking the SAME question is safe to serve from cache. TTL-
# only, no explicit invalidation on fact/entity writes — same tradeoff
# app.main's own short-TTL caches (_status_cache/_spotlight_cache) already
# make for read-mostly AI-adjacent data.
_ASK_CACHE_TTL = 30.0
_ask_cache: dict[tuple, tuple[float, dict]] = {}

_CHRONICLER_SYSTEM = (
    "You are the Chronicler, keeper of this campaign's history. Answer the question "
    "using ONLY the facts and entities listed below — never invent events, names, or "
    "details that aren't given to you. If the answer isn't in the provided context, "
    "say you don't know rather than guessing or making something up. Speak in a warm, "
    "slightly formal narrator's voice. Keep answers concise."
)


def visible_facts(db: Session, world_id: int, user) -> list:
    """The actual security boundary: a non-GM caller never gets a GM-only
    fact's content, even loaded into the prompt — not just told not to
    repeat it. Same visible_to_players convention as Entity/EntityNote."""
    q = db.query(Fact).filter(Fact.world_id == world_id)
    if not (user and user.is_gm):
        q = q.filter(Fact.visible_to_players.isnot(False))
    return q.order_by(Fact.created_at.desc()).limit(200).all()


def build_chronicler_system_prompt(db: Session, world_id: int, question: str, user) -> str:
    facts = visible_facts(db, world_id, user)
    # app.retrieval.find_relevant_entities's `user` param applies the same
    # visible_to_players/entity_player_access filter this router's own
    # ILIKE-only visible_entities() used to duplicate — using it directly
    # also gets Chronicler proper FTS5 body-text matching (and now body
    # excerpts, see format_context_from_entities) for free, which the old
    # duplicate never had.
    entities = _retrieval.find_relevant_entities(db, world_id, question, limit=15, user=user)
    lines = [_CHRONICLER_SYSTEM, "", "## Known facts"]
    if facts:
        lines.extend(f"- {f.content}" for f in facts)
    else:
        lines.append("(none recorded yet)")
    if entities:
        lines.append("")
        lines.append("## Relevant entities")
        lines.append(_retrieval.format_context_from_entities(entities))
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
    user_id = user.id if user else 0
    # Cache check comes BEFORE the cooldown gate: serving a still-fresh
    # cached answer costs nothing (no Ollama call happens), so it
    # shouldn't consume/trigger the same rate limit that exists to stop a
    # player spamming real generations.
    cache_key = (world.id, user_id, question.lower())
    cached = _ask_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _ASK_CACHE_TTL:
        return cached[1]
    if not (user and user.is_gm):
        check_llm_cooldown(user_id)
    system = build_chronicler_system_prompt(db, world.id, question, user)
    answer = await _ai_module.generate_chat([{"role": "user", "content": question}], system=system)
    result = {"answer": answer}
    _ask_cache[cache_key] = (now, result)
    return result
