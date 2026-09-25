"""Talking with the world's characters — the persistent, per-entity
roleplay surface ("NPC Talk", GET /npc-talk).

The entity detail page's Ask AI panel already has a transient Roleplay
mode (entities/detail.html's epSend): client-held history that dies with
the page. This router is the durable, dedicated version of that:

- Conversations are ChatSession rows with surface="npc" and entity_id set
  — the exact reuse ChatSession's own docstring reserved for "a future AI
  surface — e.g. per-entity 'talk to this NPC'". One conversation per
  (user, entity, world): each participant talks to an NPC in their own
  thread, so two players interviewing the same character never see (or
  leak) each other's conversations.
- Unlike the panel, the model prompt is composed SERVER-side: the NPC's
  identity context (summary, body prefix, custom fields, visible notes,
  visible relations) is built here under the caller's own visibility
  rules and [gmonly] stripping, so no secret ever depends on a client
  composing its prompt correctly. The client only ever sends the new
  message text; history comes from the DB, not the request.
- Access mirrors every other AI surface: a GM always may; anyone else
  needs the world's players_can_ask_ai or players_can_use_ai_chat opt-in
  (the same _require_ask_ai_access rule POST /api/ai/stream uses) and can
  only reach entities their own entity list would show them.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ai as _ai
from .. import ai_instructions as _ai_instructions
from .. import retrieval as _retrieval
from ..database import SessionLocal, get_db
from ..deps import filter_visible_entities, get_world_ctx, is_gm
from ..models import ChatSession, Entity, EntityNote, World
from ..templating import templates
from .ai import _require_ask_ai_access, _with_heartbeat

router = APIRouter()

_log = logging.getLogger("nd.npc_talk")

# How much of the NPC's own body fronts the identity block. A character's
# voice/personality is conventionally established up top; the Ask AI panel
# solves "the answer sits 5000 chars deep" with a per-question excerpt
# search, but for roleplay a fixed generous prefix plus the extra context
# below (fields/notes/relations) is the whole identity — the conversation
# itself carries the rest.
_BODY_PREFIX_CHARS = 2500
# Turns of history sent to the model — the DB row keeps everything, this
# only bounds the prompt (same spirit as AI Chat's compaction, coarser).
_MAX_MODEL_TURNS = 40
# Notes included in the identity block, newest first.
_MAX_NOTE_ROWS = 10

_ROLEPLAY_SYSTEM = (
    "You are roleplaying AS the character described below, speaking entirely in first "
    "person and in character — never as a narrator or assistant, never breaking "
    "character to explain yourself. Infer this character's voice, personality, and "
    "manner of speech from the context given. If asked about something the character "
    "wouldn't plausibly know, respond the way the character actually would (deflect, "
    "guess, get confused, or admit ignorance in-character) rather than stepping "
    "outside the role to say you don't have that information. Keep replies "
    "conversational — a spoken answer, not a document."
)


class NpcTalkBody(BaseModel):
    message: str
    think: bool = False
    model: str = ""
    # GM-only, same rule as every other RAG opt-in this app has: retrieval
    # with no per-viewer filter must never be client-selectable by a
    # non-GM. Ignored (forced False) for non-GM callers below.
    use_rag: bool = False


def _npc_or_404(db: Session, request: Request, world: World, entity_id: int) -> Entity:
    """The entity, provided THIS viewer's own entity list would show it —
    the same filter_visible_entities pass /kind/{kind} applies, so a hidden
    (or not-shared-with-this-player) entity 404s rather than becoming
    talkable by id-guessing."""
    q = filter_visible_entities(
        db.query(Entity).filter(Entity.id == entity_id, Entity.world_id == world.id),
        request,
    )
    entity = q.first()
    if not entity:
        raise HTTPException(404)
    return entity


def _conversation(db: Session, world_id: int, user_id: int, entity_id: int) -> Optional[ChatSession]:
    return (
        db.query(ChatSession)
        .filter(
            ChatSession.world_id == world_id,
            ChatSession.user_id == user_id,
            ChatSession.surface == "npc",
            ChatSession.entity_id == entity_id,
        )
        .first()
    )


def _identity_context(db: Session, request: Request, entity: Entity, gm: bool) -> str:
    """Everything the model learns about the NPC, composed under the
    caller's own visibility rules — the server-side counterpart of the Ask
    AI panel's ENTITY_HEADER + entity_ai_extra, with [gmonly] stripped for
    non-GMs so a secret in the NPC's own body/fields/notes can never ride
    into a player-visible prompt (the panel achieves the same by only ever
    embedding already-stripped page variables; here it's enforced at the
    source)."""
    from ..rendering import strip_gm_only

    def _clean(text: str) -> str:
        return text if gm else strip_gm_only(text or "")

    header = f"{(entity.kind or 'entity').capitalize()}: {entity.name}"
    if entity.subtype:
        header += f"\nSubtype: {entity.subtype}"
    cleaned_summary = _clean(entity.summary).strip()
    if cleaned_summary:
        header += f"\nSummary: {cleaned_summary}"
    parts = [header]
    body = _clean(entity.body).strip()
    if body:
        parts.append(body[:_BODY_PREFIX_CHARS])
    # Custom fields (simple dict view — the detail page's template-driven
    # section layout is a rendering concern; the values are the same data).
    # A value that strips to nothing (all-[gmonly]) is skipped for players.
    try:
        fields = json.loads(entity.custom_fields_json or "{}")
    except ValueError:
        fields = {}
    field_lines = []
    for key, value in fields.items():
        if value in (None, "", []):
            continue
        shown = str(value) if gm else strip_gm_only(str(value)).strip()
        if shown:
            field_lines.append(f"- {key}: {shown}")
    if field_lines:
        parts.append("Custom fields:\n" + "\n".join(field_lines))
    notes_q = db.query(EntityNote).filter(EntityNote.entity_id == entity.id)
    if not gm:
        notes_q = notes_q.filter(EntityNote.visible_to_players.is_(True))
    notes = notes_q.order_by(EntityNote.created_at.desc()).limit(_MAX_NOTE_ROWS).all()
    related_ids = [r.id for r in entity.related]
    visible_related = (
        filter_visible_entities(
            db.query(Entity).filter(Entity.id.in_(related_ids)), request,
        ).order_by(Entity.kind, Entity.name).all()
        if related_ids else []
    )
    extra = _retrieval.entity_extra_context(
        [], fields, notes, visible_related, [], strip_gm_only=not gm,
    )
    if extra:
        parts.append(extra)
    return "\n\n".join(p for p in parts if p and p.strip())


@router.get("/npc-talk", response_class=HTMLResponse)
def npc_talk_page(request: Request, entity: int = 0, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    # Same rule as the API routes below: a non-GM without the world's AI
    # opt-in gets a 403 here too, not a picker whose every send would fail.
    _require_ask_ai_access(request, db, active_world)
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds")
    # Same reachability decision as the page's own API routes: the picker
    # lists every entity this viewer can already see, ordered so actual
    # characters lead (talking to an organization or a location is a
    # legitimate GM tool — they're just not who you usually come here for).
    _KIND_ORDER = {"character": 0, "creature": 1, "organization": 2}
    rows = (
        filter_visible_entities(db.query(Entity).filter(Entity.world_id == world.id), request)
        .all()
    )
    rows.sort(key=lambda e: (_KIND_ORDER.get(e.kind, 3), (e.name or "").lower()))
    selected = None
    if entity:
        selected = next((e for e in rows if e.id == entity), None)
    return templates.TemplateResponse("npc_talk.html", {
        "request": request, "world": world, "worlds": worlds,
        "npcs": rows, "selected": selected,
        "is_gm": is_gm(request),
    })


@router.get("/api/npc-talk/{entity_id}/history")
def npc_talk_history(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    _npc_or_404(db, request, world, entity_id)
    user = getattr(request.state, "user", None)
    session = _conversation(db, world.id, user.id, entity_id) if user else None
    return {"entity_id": entity_id, "messages": json.loads(session.messages_json or "[]") if session else []}


@router.delete("/api/npc-talk/{entity_id}/history")
def npc_talk_restart(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    _npc_or_404(db, request, world, entity_id)
    user = getattr(request.state, "user", None)
    session = _conversation(db, world.id, user.id, entity_id) if user else None
    if session:
        db.delete(session)
        db.commit()
    return {"ok": True}


@router.get("/api/npc-talk/{entity_id}/export.md")
def npc_talk_export(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    npc = _npc_or_404(db, request, world, entity_id)
    user = getattr(request.state, "user", None)
    session = _conversation(db, world.id, user.id, entity_id) if user else None
    messages = json.loads(session.messages_json or "[]") if session else []
    lines = [f"# Conversation with {npc.name}", ""]
    for m in messages:
        who = "You" if m.get("role") == "user" else npc.name
        lines += [f"**{who}:** {m.get('content', '')}", ""]
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="npc-{npc.id}-conversation.md"'},
    )


@router.post("/api/npc-talk/{entity_id}/stream")
async def npc_talk_stream(entity_id: int, body: NpcTalkBody, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    npc = _npc_or_404(db, request, world, entity_id)
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "Say something first")
    user = getattr(request.state, "user", None)
    gm = bool(user and user.is_gm)
    use_rag = body.use_rag and gm  # RAG retrieval is GM-only, same as every opt-in

    session = _conversation(db, world.id, user.id, entity_id) if user else None
    history = json.loads(session.messages_json or "[]") if session else []
    history.append({"role": "user", "content": text})

    # Server-composed system prompt: roleplay directive + identity (built
    # under this viewer's visibility) + standing AI instructions +, for a
    # GM who ticked lore, world context retrieved for THIS message.
    system = _ROLEPLAY_SYSTEM + "\n\n=== The character ===\n\n" + _identity_context(db, request, npc, gm)
    instructions = _ai_instructions.enabled_instructions_text(db, world.id, for_players=not gm)
    if instructions:
        system += (
            "\n\n=== Standing setting notes from the GM — inform how you portray "
            "the character, never break character to mention them ===\n\n" + instructions
        )
    if use_rag:
        rag_context, _n, _notes = _retrieval.smart_world_context(
            db, world.id, text, entity_limit=15, notes_limit=3, user=None,
        )
        if rag_context:
            system += (
                "\n\n=== Relevant world lore the character might know — use only what "
                "this character plausibly would ===\n\n" + rag_context
            )

    msgs = [
        {"role": m["role"], "content": m.get("content", "")}
        for m in history[-_MAX_MODEL_TURNS:]
    ]
    # Same model/option handling POST /api/ai/stream applies (that route's
    # own comments carry the reasoning): non-GM model choices are ignored,
    # thinking widens num_predict unless set, and num_ctx is sized to fit
    # unless the caller set it.
    requested = (body.model if gm else "") or _ai.get_defaults().get("ask_ai", "")
    options: dict = {}
    if body.think and "num_predict" not in options:
        options = {**options, **_ai._thinking_num_predict_override(True)}
    if "num_ctx" not in options:
        total_text = system + "".join((m.get("content") or "") for m in msgs)
        reserve = _ai._CONTEXT_FIT_RESERVED_TOKENS + (
            _ai._THINKING_HEADROOM_TOKENS if "num_predict" in options else 0
        )
        options = {**options, **_ai._ctx_override_if_needed(total_text, reserve)}

    async def _chat(model: str):
        async for piece in _ai.stream_chat(msgs, system, model, options, think=body.think, emit_thinking=True):
            yield piece

    async def _gen():
        model, note = await _ai.resolve_model(requested)
        if note:
            yield f"data: {json.dumps({'note': note})}\n\n"
        full, failed = "", False
        async for piece in _with_heartbeat(_chat(model)):
            if piece is None:
                yield ": keep-alive\n\n"
            elif piece.get("type") == "thinking":
                yield f"data: {json.dumps({'thinking': piece['text']})}\n\n"
            elif piece.get("type") == "error":
                failed = True  # a failure sentinel is never a real answer — don't save it
                yield f"data: {json.dumps({'error': piece['text']})}\n\n"
            else:
                full += piece["text"]
                yield f"data: {json.dumps({'token': piece['text']})}\n\n"
        # Persist both turns only on a clean finish, in a short-lived
        # session of its own — the request's own db dependency is held for
        # the whole stream already (FastAPI closes deps after the response
        # completes), and this write must not extend that hold.
        if not failed and full.strip():
            save = SessionLocal()
            try:
                row = _conversation(save, world.id, user.id, entity_id)
                if row is None:
                    row = ChatSession(
                        world_id=world.id, user_id=user.id, surface="npc",
                        entity_id=entity_id, title=npc.name,
                    )
                    save.add(row)
                row.messages_json = json.dumps(history + [{"role": "assistant", "content": full}])
                save.commit()
            except Exception:
                _log.exception("npc-talk: failed to persist conversation turn")
            finally:
                save.close()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
