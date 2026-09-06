import json
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from .. import retrieval as _retrieval
from ..database import get_db
from ..deps import check_llm_cooldown, get_world_ctx
from ..models import Fact
from ..templating import templates
from .ai import _with_heartbeat

router = APIRouter()

# Avoids re-invoking Ollama for the exact same question asked again within
# the window (a re-click, a page reload, two tabs) — keyed per (world,
# user, question, model, think) rather than per-world, since visible_facts/
# find_relevant_entities can legitimately differ between two non-GM users (a
# specific entity individually shared with one player but not another), so
# only the SAME user re-asking the SAME question is safe to serve from
# cache. model/think are part of the key too — a GM switching models or
# toggling Thinking must not be served a stale answer generated under a
# different one. TTL-only, no explicit invalidation on fact/entity writes —
# same tradeoff app.main's own short-TTL caches (_status_cache/
# _spotlight_cache) already make for read-mostly AI-adjacent data.
_ASK_CACHE_TTL = 30.0
_ask_cache: dict[tuple, tuple[float, dict]] = {}

_CHRONICLER_SYSTEM = (
    "You are the Chronicler, keeper of this campaign's history. Answer the question "
    "using ONLY the facts and entities listed below — never invent events, names, or "
    "details that aren't given to you. If the answer isn't in the provided context, "
    "say you don't know rather than guessing or making something up. Speak in a warm, "
    "slightly formal narrator's voice. Keep answers concise."
)

# Every visible Fact up to this cap goes into the prompt UNFILTERED (no
# keyword match against the question, unlike the entity retrieval just
# below) — on a mature campaign with thousands of logged facts that's a
# multi-thousand-token system prompt on every single question, most of it
# irrelevant to what was actually asked. Lowered from an unnamed inline 200
# specifically to bound that cost; trades "the Chronicler has seen every
# fact ever recorded" for materially fewer tokens per call. Facts are
# ordered newest-first (see visible_facts below), so this caps to the most
# RECENT facts, not a random/arbitrary subset — a deliberate choice given
# there's no per-fact relevance signal to filter by the way entities have
# (FTS5 body/summary matching via find_relevant_entities).
_CHRONICLER_FACT_LIMIT = 60


def visible_facts(db: Session, world_id: int, user) -> list:
    """The actual security boundary: a non-GM caller never gets a GM-only
    fact's content, even loaded into the prompt — not just told not to
    repeat it. Same visible_to_players convention as Entity/EntityNote.

    Capped at _CHRONICLER_FACT_LIMIT (newest first) — see that constant's
    own comment for why this isn't relevance-filtered the way entities are."""
    q = db.query(Fact).filter(Fact.world_id == world_id)
    if not (user and user.is_gm):
        q = q.filter(Fact.visible_to_players.isnot(False))
    return q.order_by(Fact.created_at.desc()).limit(_CHRONICLER_FACT_LIMIT).all()


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


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/api/chronicler/ask")
async def chronicler_ask(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Streams the answer over SSE (token-by-token, heartbeat-padded via
    _with_heartbeat — same mechanism app.routers.ai's /api/ai/stream uses)
    instead of returning one blocking JSON response. This used to be a
    single `await generate_chat(...)` — fine for a fast model, but a slow
    or thinking-enabled model easily runs past Cloudflare's ~100s
    no-bytes-in-flight timeout (HTTP 524), killing the whole request with
    nothing to show for it. Streaming keeps bytes flowing the entire time,
    the same fix already applied to every other long-running AI call in
    this app (see _with_heartbeat's own docstring)."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "No question provided")
    user = getattr(request.state, "user", None)
    user_id = user.id if user else 0
    is_gm = bool(user and user.is_gm)
    # Model/Thinking selection is GM-only — a player-facing chat surface
    # with its own cost-control cooldown below must never let the client
    # dictate a slower/pricier model or reasoning mode for itself, so a
    # non-GM's own claimed values are ignored server-side regardless of
    # what the request body contains (never trust the client for this).
    model = str(body.get("model") or "").strip() if is_gm else ""
    think = bool(body.get("think")) if is_gm else False
    # Cache check comes BEFORE the cooldown gate: serving a still-fresh
    # cached answer costs nothing (no Ollama call happens), so it
    # shouldn't consume/trigger the same rate limit that exists to stop a
    # player spamming real generations.
    cache_key = (world.id, user_id, question.lower(), model, think)
    cached = _ask_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _ASK_CACHE_TTL:
        cached_answer = cached[1]["answer"]

        async def _cached_gen():
            yield _sse({"token": cached_answer})
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _cached_gen(), media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )
    if not is_gm:
        check_llm_cooldown(user_id)
    system = build_chronicler_system_prompt(db, world.id, question, user)
    # Sizes num_ctx to actually fit the assembled system prompt (facts +
    # entity excerpts, now capped but still potentially large) + the
    # question — without this, a prompt longer than the GM's configured/
    # assumed context gets silently truncated by Ollama instead of raising,
    # which in practice (see condense_call_options' own docstring for the
    # exact failure mode observed) can corrupt the prompt badly enough that
    # the model responds with garbage instead of a real answer or a clean
    # error.
    options = _ai_module.context_sized_options(system + question)
    if think and "num_predict" not in options:
        options = {**options, **_ai_module._thinking_num_predict_override(True)}

    async def _gen():
        resolved_model, note = await _ai_module.resolve_model(model)
        if note:
            yield _sse({"note": note})
        chunks: list[str] = []

        async def _chat():
            async for token in _ai_module.stream_chat(
                [{"role": "user", "content": question}], system=system,
                model=resolved_model, options=options, think=think,
            ):
                chunks.append(token)
                yield token

        async for token in _with_heartbeat(_chat()):
            if token is None:
                yield ": keep-alive\n\n"
            else:
                yield _sse({"token": token})
        answer = "".join(chunks)
        _ask_cache[cache_key] = (now, {"answer": answer})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
