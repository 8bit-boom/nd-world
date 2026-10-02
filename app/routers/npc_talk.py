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
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ai as _ai
from .. import ai_instructions as _ai_instructions
from .. import retrieval as _retrieval
from .. import unsloth_extras as _unsloth_extras
from ..database import SessionLocal, get_db
from ..deps import filter_visible_entities, get_world_ctx, is_gm
from ..models import ChatSession, Entity, EntityNote, EntityVoiceHint, World
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
# Kinds that can hold an in-character conversation. A Sword +2 or the
# Floating Market district has no voice — the picker lists only these
# kinds and the API routes refuse others. Organizations count: they're
# collective entities a GM can voice through a spokesperson.
TALKABLE_KINDS = {"character", "creature", "organization"}
# Cap on the GM-authored roleplay personality (it's prompt text, not
# lore storage — keep it tight).
_MAX_PERSONALITY_CHARS = 4000

_ROLEPLAY_SYSTEM = (
    "You are roleplaying AS the character described below, speaking entirely in first "
    "person and in character — never as a narrator or assistant, never breaking "
    "character to explain yourself. Infer this character's voice, personality, and "
    "manner of speech from the context given. If asked about something the character "
    "wouldn't plausibly know, respond the way the character actually would (deflect, "
    "guess, get confused, or admit ignorance in-character) rather than stepping "
    "outside the role to say you don't have this information. Keep replies "
    "conversational — a spoken answer, not a document."
)

# ── Spoken replies (TTS with an LLM-derived voice) ───────────────────────────
# The speak route below turns a saved NPC reply into audio through Studio's
# TTS, using the `instructions` field (delivery style) this app already
# supports. Instead of making the GM author a voice direction per NPC, the
# chat model derives one ONCE from the character's own sheet and it's cached
# in EntityVoiceHint — the LLM "helps in the background" exactly once per
# character, then every spoken line reuses it.
_VOICE_HINT_SYSTEM = (
    "You write text-to-speech voice directions. Given a character sheet, output ONE "
    "concise instruction sentence (at most ~30 words) describing how this character "
    "should sound when speaking: apparent age, pitch, pace, texture/roughness, accent "
    "only if the sheet clearly implies one, and overall attitude. Base it strictly on "
    "the sheet; where the sheet is silent, choose something plain and neutral. Output "
    "ONLY the instruction itself — no quotes, no preamble, no explanation."
)
# One spoken reply = one TTS call; a roleplay reply longer than this is a
# document, not a spoken answer, and on a CPU-only TTS backend would outrun
# any sane request budget anyway.
_MAX_SPEAK_CHARS = 4000


def _voice_card(entity: Entity) -> str:
    """The derivation input: the character's public-facing card. [gmonly]
    blocks are stripped for EVERYONE, GM included — the hint is cached per
    entity and shared by all callers, and it flows into TTS requests and
    (for GMs) API responses, so secret GM direction must never shape or
    leak through it; the public personality is plenty for a voice."""
    from ..rendering import strip_gm_only

    def _clean(text) -> str:
        return strip_gm_only(text or "").strip()

    lines = [f"{(entity.kind or 'character').capitalize()}: {entity.name}"]
    if entity.subtype:
        lines.append(f"Subtype: {entity.subtype}")
    if entity.tags:
        lines.append(f"Tags: {entity.tags}")
    summary = _clean(entity.summary)
    if summary:
        lines.append(f"Summary: {summary}")
    personality = _clean(getattr(entity, "roleplay_personality", ""))
    if personality:
        lines.append("How they speak (GM direction):\n" + personality[:_MAX_PERSONALITY_CHARS])
    body = _clean(entity.body)
    if body:
        lines.append("Sheet excerpt:\n" + body[:1200])
    return "\n".join(lines)


async def _voice_hint(entity: Entity, force: bool = False) -> tuple[str, bool]:
    """(instructions, was_derived) — the cached EntityVoiceHint, deriving it
    via one small chat-model call when absent (or force). DB reads/writes
    happen in short-lived sessions so the LLM call never sits on a pooled
    connection. A derivation that comes back empty/garbage falls back to ""
    (the model's natural delivery) rather than failing the whole speak."""
    if not force:
        db = SessionLocal()
        try:
            row = db.get(EntityVoiceHint, entity.id)
            if row and row.instructions.strip():
                return row.instructions.strip(), False
        finally:
            db.close()
    model = _ai.get_defaults().get("ask_ai", "")
    hint = ""
    try:
        raw = await _ai.generate_chat(
            [{"role": "user", "content": _voice_card(entity)}],
            system=_VOICE_HINT_SYSTEM, model=model,
            options={"num_predict": 120}, think=False,
        )
        # Strip the ways small models decorate "output only the instruction"
        # (surrounding quotes, a "Voice:" label, markdown) — one line max.
        hint = (raw or "").strip().split("\n")[0].strip()
        hint = hint.strip('"“”\'').strip()
        if hint.lower().startswith("voice:"):
            hint = hint[6:].strip()
        if len(hint) > 400:
            hint = hint[:400].rsplit(" ", 1)[0]
    except Exception as exc:
        _log.warning("npc-talk voice-hint derivation failed for entity %s: %s", entity.id, exc)
        return "", False
    db = SessionLocal()
    try:
        row = db.get(EntityVoiceHint, entity.id)
        if row is None:
            row = EntityVoiceHint(entity_id=entity.id, world_id=entity.world_id)
            db.add(row)
        row.instructions = hint
        row.model = model
        db.commit()
    finally:
        db.close()
    return hint, True


class NpcSpeakBody(BaseModel):
    # Index into the CALLER's OWN conversation's messages_json — the server
    # reads the text from the stored message, never from the request, so a
    # forged body can't make an NPC speak arbitrary text.
    index: int
    # Re-derive the cached voice hint (e.g. after editing the character or
    # switching models) and re-synthesize even if audio already exists.
    regenerate: bool = False


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
    if entity.kind not in TALKABLE_KINDS:
        # An existing, visible entity of a non-conversational kind (a
        # location, an item) — the picker only offers talkable kinds, so
        # reaching this means an id guessed or hand-typed for another kind.
        raise HTTPException(
            400,
            f"A {entity.kind} can't hold an in-character conversation — "
            "Talk to NPCs covers characters, creatures, and organizations.",
        )
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
        # List/dict values (repeatable "list"-type custom fields) render as
        # readable prose, not Python repr.
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            value = "; ".join(f"{k}: {v}" for k, v in value.items())
        shown = str(value) if gm else strip_gm_only(str(value)).strip()
        if shown:
            field_lines.append(f"- {key}: {shown}")
    if field_lines:
        parts.append("Custom fields:\n" + "\n".join(field_lines))
    # The GM's roleplay direction — how this character speaks and behaves.
    # [gmonly] stripped for players exactly like body/fields, so secret
    # direction ("never mention the mask") stays GM-side.
    personality = _clean(getattr(entity, "roleplay_personality", "") or "").strip()
    if personality:
        parts.append("How you speak and behave (standing direction from the GM — "
                     "follow it over any general instinct):\n"
                     + personality[:_MAX_PERSONALITY_CHARS])
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
    # The picker lists only conversational kinds (see TALKABLE_KINDS) that
    # this viewer can already see — an item or a location has no voice.
    _KIND_ORDER = {"character": 0, "creature": 1, "organization": 2}
    rows = (
        filter_visible_entities(
            db.query(Entity).filter(
                Entity.world_id == world.id, Entity.kind.in_(TALKABLE_KINDS),
            ), request,
        ).all()
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


@router.post("/api/npc-talk/{entity_id}/speak")
async def npc_talk_speak(entity_id: int, body: NpcSpeakBody, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Speak one saved reply of THIS caller's own conversation with the NPC
    as audio, via Studio TTS. The delivery-style `instructions` come from
    the cached EntityVoiceHint — derived once by the chat model from the
    character's sheet (see _voice_hint) — plus the world TTS defaults
    (model/voice/language) from Settings. The text always comes from the
    stored message, never the request, and only assistant messages are
    speakable. Re-speaking an already-spoken message returns the existing
    audio without re-synthesizing; `regenerate` re-derives the voice hint
    and re-synthesizes (overwriting the same deterministic file path).

    Deliberately NOT `db: Session = Depends(get_db)` across the LLM/TTS
    awaits — same pool-exhaustion shape the live-transcript route fixed:
    bookend with short-lived sessions instead. One accepted race: speaking
    an older message while a newer reply is still streaming can be undone
    by that stream's whole-array save (its snapshot predates the audio
    link) — the button simply reappears; re-speaking regenerates."""
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    npc = _npc_or_404(db, request, world, entity_id)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)

    # Phase 1 — read everything needed, then close the session before any
    # network await.
    session = _conversation(db, world.id, user.id, entity_id)
    if not session:
        raise HTTPException(404, "No conversation with this character yet — talk to them first")
    messages = json.loads(session.messages_json or "[]")
    if not (0 <= body.index < len(messages)):
        raise HTTPException(400, "No such message in your conversation")
    m = messages[body.index]
    if m.get("role") != "assistant":
        raise HTTPException(400, "Only the character's replies can be spoken")
    text = (m.get("content") or "").strip()
    if not text:
        raise HTTPException(400, "That reply has no text to speak")
    if len(text) > _MAX_SPEAK_CHARS:
        raise HTTPException(400, f"That reply is too long to speak in one go ({len(text)} chars — the cap is {_MAX_SPEAK_CHARS})")
    audio_url = m.get("audio") or ""
    needs_synth = body.regenerate or not audio_url
    hint = ""
    db.close()

    # Phase 2 — outside any pooled DB session: (maybe) derive the voice
    # hint once, then synthesize.
    if needs_synth:
        hint, _derived = await _voice_hint(npc, force=body.regenerate)
        audio, content_type = await _unsloth_extras.tts(
            text,
            model=_ai.get_tts_model(),
            voice=_ai.get_tts_voice(),
            instructions=hint,
            language=_ai.get_tts_language(),
        )
        ext = _unsloth_extras.audio_extension(content_type)
        # Deterministic per (entity, caller, message) — regeneration
        # overwrites the same file instead of orphaning the old one.
        target_dir = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "npc-talk"
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"npc{entity_id}-u{user.id}-m{body.index}{ext}"
        dest.write_bytes(audio)
        audio_url = f"/uploads/npc-talk/{dest.name}"

    # Phase 3 — re-read fresh and patch the audio link into the message.
    db2 = SessionLocal()
    try:
        row = _conversation(db2, world.id, user.id, entity_id)
        if row is None:
            raise HTTPException(404, "Conversation disappeared while speaking")
        current = json.loads(row.messages_json or "[]")
        if not (0 <= body.index < len(current)) or current[body.index].get("role") != "assistant":
            raise HTTPException(409, "Conversation changed while speaking — try again")
        current[body.index]["audio"] = audio_url
        row.messages_json = json.dumps(current)
        db2.commit()
    finally:
        db2.close()
    # The voice hint itself is GM-visibility only — it's derived from the
    # sheet and shown back so a GM can judge/regenerate it; a player gets
    # just the audio.
    out = {"audio_url": audio_url}
    if user.is_gm:
        out["voice_hint"] = hint or "(cached earlier)"
    return out
