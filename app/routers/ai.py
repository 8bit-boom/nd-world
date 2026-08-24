import asyncio as _asyncio
import base64 as _base64
import json as _json
import csv as _csv
import logging
import os as _os
import ollama as _ollama
import urllib.request as _urllib
from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse as _SR
from pydantic import BaseModel
from typing import List, Optional
from pathlib import Path as _Path
from .. import ai as _ai
from .. import audio_jobs as _audio_jobs
from ..constants import KINDS
from ..database import get_db
from ..deps import get_world_ctx
from ..models import AudioJob, ChatSession, PromptPreset
from ..uploads import (
    copy_upload_bounded, unique_upload_filename, reassemble_upload_chunks, save_upload_chunk,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])
_log = logging.getLogger("nd.ai.router")

# Reused by the chat/ask-ai attachment picker (image/audio/document drag-and-
# drop onto a chat message) — kept separate from the audio library's own
# _ALLOWED_EXTS (app/routers/audio.py) since that set is scoped to what an
# <audio> tag can play back, not what's reasonable to attach for reference.
_ATTACH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ATTACH_DOC_EXTS = {".txt", ".md", ".markdown", ".pdf"}
_ATTACH_AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"}
_ATTACH_SUBDIR = "ai_attachments"
# Env-overridable like MAX_UPLOAD_BYTES/MAX_AUDIO_UPLOAD_BYTES — a dropped
# document or portrait-sized image should comfortably fit under 25 MB.
_MAX_ATTACHMENT_BYTES = int(_os.environ.get("MAX_AI_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
# A dropped/recorded voice memo can run much longer than a document or
# image attachment — same default ceiling as the Audio Library/session
# recap uploads, reusing that env var rather than introducing a second one
# for what's really the same kind of upload.
_MAX_ATTACHMENT_AUDIO_BYTES = int(_os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
# How much of a document's extracted text gets folded into the prompt — long
# enough for a handout or a few rulebook pages, bounded so one attachment
# can't blow the model's context window on its own.
_MAX_ATTACHMENT_TEXT_CHARS = 12000


def _attachment_kind(ext: str) -> Optional[str]:
    if ext in _ATTACH_IMAGE_EXTS:
        return "image"
    if ext in _ATTACH_DOC_EXTS:
        return "document"
    if ext in _ATTACH_AUDIO_EXTS:
        return "audio"
    return None


def _uploads_root() -> _Path:
    return _Path(_os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"


def _attach_chunks_root() -> _Path:
    return _uploads_root() / _ATTACH_SUBDIR / "_chunks"


def _attachment_disk_path(url: str) -> Optional[_Path]:
    """Resolve an /uploads/... URL back to a file under this world's uploads
    root, refusing anything that isn't (a nonexistent file, or a path that
    escapes the uploads dir via a crafted "../" — same guard shape as
    audio.py's _delete_clip_file)."""
    if not url.startswith("/uploads/"):
        return None
    root = _uploads_root().resolve()
    try:
        path = (root / url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def _extract_document_text(path: _Path, ext: str) -> str:
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            _log.warning("PDF text extraction failed for %s: %s", path, exc)
            return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _require_gm(request: Request) -> None:
    """The main /ai Chat page (unlike the embedded Ask AI panel gated by
    _require_ask_ai_access below) is GM-only — see main.py's ai_chat_page —
    so its saved-conversation history stays GM-only too."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)


def _require_ask_ai_access(request: Request, db, active_world) -> None:
    """Same permission shape as the rest of this router's GM/player split —
    a GM always may, a player only if their world has opted in via
    World.players_can_ask_ai (off by default, app/models.py)."""
    user = getattr(request.state, "user", None)
    if user and user.is_gm:
        return
    world, _ = get_world_ctx(request, db, active_world)
    if not (world and world.players_can_ask_ai):
        raise HTTPException(403)


async def _with_heartbeat(agen, interval: float = 12.0):
    """Interleave an async generator's items with periodic `None` pings so a
    reverse proxy sitting in front of this app (e.g. Cloudflare, whose 524
    "origin timeout" fires after ~100s with no bytes in flight) doesn't kill
    the connection while a slow or cold-starting Ollama model is still
    working on the first token."""
    q: _asyncio.Queue = _asyncio.Queue()
    _done = object()

    async def _pump():
        try:
            async for item in agen:
                await q.put(item)
        finally:
            await q.put(_done)

    task = _asyncio.ensure_future(_pump())
    try:
        while True:
            try:
                item = await _asyncio.wait_for(q.get(), timeout=interval)
            except _asyncio.TimeoutError:
                yield None
                continue
            if item is _done:
                break
            yield item
    finally:
        task.cancel()


class ChatAttachment(BaseModel):
    kind: str  # "image" | "document" | "audio"
    url: str
    name: str = ""
    # Extracted text (documents only) — filled in by /attachments/upload at
    # upload time and just passed back through here so this endpoint never
    # has to re-read/re-parse the file on every turn of a conversation.
    text: str = ""


class ChatMessage(BaseModel):
    role: str
    content: str
    attachments: List[ChatAttachment] = []


class ChatBody(BaseModel):
    messages: List[ChatMessage]
    system: str = ""
    model: str = ""
    # Which per-surface default (see app.ai.DEFAULT_SURFACES) to fall back to
    # when `model` is blank — lets "Chat" and "Ask AI" run different models
    # without the caller having to know the configured default itself.
    surface: str = "chat"
    # Per-conversation Ollama generation tuning (a chat preset — see
    # /presets below), layered over the instance-wide Settings > System
    # defaults. Always passed through _clamp_options() before reaching
    # app.ai — never trust a client-supplied options dict directly.
    options: dict = {}


# Same fields/ranges Settings > System validates (app/main.py's
# settings_system_save) — a chat preset can only ever narrow to values a GM
# could already configure instance-wide, never send an arbitrary Ollama
# option straight through from the client.
_OPTION_ALLOWLIST = {
    "temperature": (float, 0.0, 2.0), "top_p": (float, 0.0, 1.0), "top_k": (int, 0, None),
    "repeat_penalty": (float, 0.0, 5.0), "num_predict": (int, -2, None), "num_ctx": (int, 1, None),
    "seed": (int, None, None), "mirostat": (int, 0, 2), "mirostat_tau": (float, 0.0, 100.0),
    "mirostat_eta": (float, 0.0, 10.0), "num_gpu": (int, 0, None),
}


def _clamp_options(raw: dict) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        if key not in _OPTION_ALLOWLIST or val is None:
            continue
        kind, lo, hi = _OPTION_ALLOWLIST[key]
        try:
            v = kind(val)
        except (TypeError, ValueError):
            continue
        if lo is not None and v < lo:
            continue
        if hi is not None and v > hi:
            continue
        out[key] = v
    return out


def _build_ollama_messages(messages: List[ChatMessage]) -> list[dict]:
    """Turn the chat's {role, content, attachments} messages into the plain
    {role, content[, images][, audios]} dicts app.ai.generate_chat/stream_chat
    forward straight to Ollama. A document attachment's extracted text is
    folded into the message content (same idea as how entities/detail.html
    already text-interpolates entity context into a prompt); an image
    attachment's bytes are base64-encoded into Ollama's per-message `images`
    field.

    Audio has two independent paths, and either or both can fire for the
    same attachment:

    1. A transcript (any audio format — see app.ai.transcribe_audio, which
       runs at upload time via an optional self-hosted whisper.cpp server)
       gets folded into `content` as plain text, exactly like a document.
       This is the reliable path: it works regardless of which chat model
       is configured, since the model never needs to understand audio at
       all — just text, same as everything else in the prompt.
    2. If the attachment is specifically a .wav file, its bytes are ALSO
       base64-encoded into an `audios` field, the same shape as `images`,
       on a best-effort basis for a genuinely audio-native chat model (e.g.
       a Gemma 3n/Gemma 4-style model) to additionally hear directly. This
       isn't in any released Ollama version yet (the installed client's
       Message type only declares `images` — verified directly against its
       source), but it validates messages permissively enough that an extra
       key still reaches the server rather than being silently stripped
       (verified: ChatRequest.messages is typed Union[Mapping[str, Any],
       Message], and pydantic keeps a plain dict as a Mapping instead of
       coercing it through Message, which would drop anything Message
       doesn't declare). The field name/shape match ollama/ollama#15243
       (open PR at the time of writing, linked from ollama/ollama#11798 and
       #15427 — the latter is someone finding an undocumented `audio` tag
       on a Gemma4 Ollama model page, which is what this was originally
       built against before finding the PR): `Audios []ImageData`. That
       PR's server only accepts WAV ("models already detect WAV format by
       magic bytes"; its OpenAI-compat shim explicitly rejects anything
       else), so a non-.wav attachment never goes in `audios` — its bytes
       would just fail WAV detection — regardless of whether it has a
       transcript from path 1.

    If neither path produced anything (no Whisper configured/reachable, and
    not a .wav file), the attachment still gets a plain text note so every
    model has *some* context about it rather than the upload silently doing
    nothing."""
    out = []
    for m in messages:
        content = m.content
        images = []
        audios = []
        for att in m.attachments:
            if att.kind == "document" and att.text:
                snippet = att.text[:_MAX_ATTACHMENT_TEXT_CHARS]
                content += f"\n\n---\nAttached file: {att.name or 'document'}\n\n{snippet}\n---"
            elif att.kind == "audio":
                is_wav = _Path(att.url).suffix.lower() == ".wav"
                sent_something = False
                if att.text:
                    snippet = att.text[:_MAX_ATTACHMENT_TEXT_CHARS]
                    content += (
                        f"\n\n---\nTranscript of attached audio file: {att.name or 'audio clip'}"
                        f"\n\n{snippet}\n---"
                    )
                    sent_something = True
                if is_wav:
                    path = _attachment_disk_path(att.url)
                    if path:
                        audios.append(_base64.b64encode(path.read_bytes()).decode())
                        if not att.text:
                            content += (
                                f"\n\n[Attached audio file: {att.name or 'audio clip'} — sent "
                                "to the model directly for models with audio input support]"
                            )
                        sent_something = True
                if not sent_something:
                    content += (
                        f"\n\n[Attached audio file: {att.name or 'audio clip'} — not sent "
                        "to the model: no transcription available and it isn't a .wav file "
                        "for direct audio input; mentioned here for context only]"
                    )
            elif att.kind == "image":
                path = _attachment_disk_path(att.url)
                if path:
                    images.append(_base64.b64encode(path.read_bytes()).decode())
        d = {"role": m.role, "content": content}
        if images:
            d["images"] = images
        if audios:
            d["audios"] = audios
        out.append(d)
    return out


@router.post("/chat")
async def ai_chat(body: ChatBody):
    msgs = _build_ollama_messages(body.messages)
    return {"result": await _ai.generate_chat(msgs, body.system, body.model, _clamp_options(body.options))}


class EntityFromTextBody(BaseModel):
    text: str


@router.post("/entity-from-text")
async def api_entity_from_text(
    body: EntityFromTextBody, request: Request, db=Depends(get_db), active_world: str = Cookie(None),
):
    """Draft a world entity from a passage of text (an AI Chat reply) —
    returns the draft without writing anything; the client reviews/edits it,
    then POSTs the confirmed shape to /api/import/execute (kind=entity_single)
    to actually create it. GM-only, matching the /ai page itself."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "No text provided")
    try:
        draft = await _ai.parse_entity_from_text(text, KINDS)
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    return draft


# ── Saved chat conversations (ai_chat.html's History sidebar) ──────────────
#
# Upserted on every completed assistant turn — the client always sends the
# full messages array (not a delta) and either creates a new row (session_id
# omitted/null) or overwrites an existing one (session_id given). The title
# is fixed at creation from the first user message and never recomputed on
# later saves, so renaming mid-conversation isn't a concern here.

class ChatSessionSaveBody(BaseModel):
    session_id: Optional[int] = None
    messages: List[ChatMessage]


def _chat_session_title(messages: List[ChatMessage]) -> str:
    for m in messages:
        if m.role == "user" and m.content.strip():
            return m.content.strip().split("\n")[0][:80]
    return "New chat"


@router.get("/sessions")
def api_chat_sessions_list(request: Request, db=Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.world_id == world.id, ChatSession.surface == "chat")
        .order_by(ChatSession.updated_at.desc())
        .all()
    )
    return {"sessions": [{"id": s.id, "title": s.title} for s in sessions]}


@router.post("/sessions")
def api_chat_sessions_save(
    body: ChatSessionSaveBody, request: Request, db=Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    session = None
    if body.session_id:
        session = db.query(ChatSession).filter(
            ChatSession.id == body.session_id, ChatSession.world_id == world.id,
        ).first()
    if not session:
        user = getattr(request.state, "user", None)
        session = ChatSession(
            world_id=world.id, user_id=user.id if user else None,
            surface="chat", title=_chat_session_title(body.messages),
        )
        db.add(session)
    session.messages_json = _json.dumps([m.model_dump() for m in body.messages])
    db.commit()
    db.refresh(session)
    return {"id": session.id}


@router.get("/sessions/{session_id}")
def api_chat_session_get(session_id: int, request: Request, db=Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id, ChatSession.world_id == world.id,
    ).first()
    if not session:
        raise HTTPException(404)
    return {"id": session.id, "messages": _json.loads(session.messages_json or "[]")}


@router.delete("/sessions/{session_id}")
def api_chat_session_delete(session_id: int, request: Request, db=Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id, ChatSession.world_id == world.id,
    ).first()
    if not session:
        raise HTTPException(404)
    db.delete(session)
    db.commit()
    return {"ok": True}


# ── Prompt library (GM-editable, per-world, shared by Chat's Quick Prompts ──
# and Image Studio's Prompt Presets) ─────────────────────────────────────────
#
# Replaces two previous implementations: Chat's Quick Prompts sidebar was a
# hardcoded list of generic-fantasy prompts baked into ai_chat.html that
# fired immediately on click; Image Studio's Prompt Presets were saved to
# localStorage, so they vanished on a different browser while every other
# saved thing in this app (starred images, audio jobs, model config) lives
# server-side. One shared per-world table, scoped by "chat" or "image".
_DEFAULT_CHAT_PRESETS = [
    {"icon": "👤", "label": "Random NPC", "text": "Create a detailed NPC with a dark secret and a memorable quirk"},
    {"icon": "⚡", "label": "Quest hook", "text": "Write a compelling quest hook for a party of adventurers"},
    {"icon": "🗺", "label": "Location", "text": "Describe a location that feels dangerous but intriguing — something the players will remember"},
    {"icon": "💬", "label": "Rumors", "text": "Generate 5 rumors the players might overhear in a bar or marketplace"},
    {"icon": "🏢", "label": "Faction", "text": "Design a secretive organization or faction with goals, methods, and internal tensions"},
    {"icon": "⚙", "label": "Loot table", "text": "Generate a loot table with 6 interesting and flavourful items"},
    {"icon": "☠", "label": "Villain", "text": "Create a memorable villain with a believable motivation"},
    {"icon": "📄", "label": "Lore snippet", "text": "Write a short piece of in-world lore or legend"},
]


def _preset_to_dict(p: PromptPreset) -> dict:
    return {"id": p.id, "scope": p.scope, "label": p.label, "icon": p.icon or "", "text": p.text or "", "negative": p.negative or ""}


@router.get("/prompt-presets")
def api_prompt_presets_list(scope: str, request: Request, db=Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    if scope not in ("chat", "image"):
        raise HTTPException(400, "scope must be 'chat' or 'image'")
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    q = lambda: (
        db.query(PromptPreset)
        .filter(PromptPreset.world_id == world.id, PromptPreset.scope == scope)
        .order_by(PromptPreset.sort_order, PromptPreset.id)
    )
    presets = q().all()
    # A brand-new world's Quick Prompts start seeded with the same defaults
    # that used to be hardcoded — as real, editable/deletable rows, not a
    # client-side fallback, so a GM can trim or rewrite them like anything
    # else here. Image scope starts empty (its old localStorage version did too).
    if not presets and scope == "chat":
        for i, d in enumerate(_DEFAULT_CHAT_PRESETS):
            db.add(PromptPreset(world_id=world.id, scope="chat", label=d["label"], icon=d["icon"], text=d["text"], sort_order=i))
        db.commit()
        presets = q().all()
    return {"presets": [_preset_to_dict(p) for p in presets]}


class PromptPresetBody(BaseModel):
    scope: str
    label: str
    icon: str = ""
    text: str = ""
    negative: str = ""


@router.post("/prompt-presets")
def api_prompt_presets_create(body: PromptPresetBody, request: Request, db=Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    if body.scope not in ("chat", "image"):
        raise HTTPException(400, "scope must be 'chat' or 'image'")
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "label is required")
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    p = PromptPreset(
        world_id=world.id, scope=body.scope, label=label, icon=body.icon.strip(),
        text=body.text, negative=body.negative,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _preset_to_dict(p)


@router.delete("/prompt-presets/{preset_id}")
def api_prompt_presets_delete(preset_id: int, request: Request, db=Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    p = db.query(PromptPreset).filter(PromptPreset.id == preset_id, PromptPreset.world_id == world.id).first()
    if not p:
        raise HTTPException(404)
    db.delete(p)
    db.commit()
    return {"ok": True}


async def _finish_attachment_upload(dest: _Path, ext: str, kind: str, original_filename: str) -> dict:
    """Shared tail of both /attachments/upload and .../upload/complete: given
    a saved file, extract/transcribe as appropriate and build the response
    the client's attachment picker expects."""
    if kind == "document":
        text = _extract_document_text(dest, ext)
    elif kind == "audio":
        # Whisper transcodes via ffmpeg server-side, so this works for any
        # of _ATTACH_AUDIO_EXTS, not just .wav — see app.ai.transcribe_audio.
        # "" (Whisper not configured, or the request failed) just means this
        # attachment falls back to _build_ollama_messages' non-transcript
        # handling instead of blocking the upload.
        text = await _ai.transcribe_audio(dest)
    else:
        text = ""
    return {
        "kind": kind,
        "url": f"/uploads/{_ATTACH_SUBDIR}/{dest.name}",
        "name": original_filename,
        "text": text,
    }


@router.post("/attachments/upload")
async def ai_attachment_upload(
    request: Request,
    file: UploadFile = File(...),
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    """Upload a file to attach to the next chat message on /ai or an
    entity's Ask AI panel. Returns immediately with the extracted/transcribed
    text for a document or audio file so the client never has to re-upload/
    re-parse it; an image is just stored and referenced by URL, read back
    from disk (see _build_ollama_messages) only once the message is
    actually sent. A file over ndChunkedUpload's threshold (static/js/
    chunked-upload.js) arrives via .../upload/chunk + .../upload/complete
    below instead of this route."""
    _require_ask_ai_access(request, db, active_world)
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = _Path(file.filename).suffix.lower()
    kind = _attachment_kind(ext)
    if not kind:
        allowed = sorted(_ATTACH_IMAGE_EXTS | _ATTACH_DOC_EXTS | _ATTACH_AUDIO_EXTS)
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(allowed)}")

    target_dir = _uploads_root() / _ATTACH_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    max_bytes = _MAX_ATTACHMENT_AUDIO_BYTES if kind == "audio" else _MAX_ATTACHMENT_BYTES
    copy_upload_bounded(file, dest, max_bytes=max_bytes)
    return await _finish_attachment_upload(dest, ext, kind, file.filename)


@router.post("/attachments/upload/chunk")
async def ai_attachment_upload_chunk(
    request: Request,
    file: UploadFile = File(...),
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    """Receive one part of a large attachment (currently only a voice memo
    realistically needs this — Cloudflare's free tier caps a request body at
    100MB with no way to raise it, see docs/DEPLOYMENT.md). See .../complete
    for reassembly; mirrors app/routers/audio.py's chunked-upload pair."""
    _require_ask_ai_access(request, db, active_world)
    save_upload_chunk(_attach_chunks_root(), upload_id, chunk_index, file, max_bytes=_MAX_ATTACHMENT_AUDIO_BYTES)
    return {"ok": True}


@router.post("/attachments/upload/complete")
async def ai_attachment_upload_complete(
    request: Request,
    upload_id: str = Form(...),
    filename: str = Form(...),
    total_chunks: int = Form(...),
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    """Reassemble the parts uploaded via .../upload/chunk and finish exactly
    like the one-shot /attachments/upload — same response shape, just fed
    from disk instead of the request body directly."""
    _require_ask_ai_access(request, db, active_world)
    if not filename:
        raise HTTPException(400, "No filename given")
    ext = _Path(filename).suffix.lower()
    kind = _attachment_kind(ext)
    if not kind:
        allowed = sorted(_ATTACH_IMAGE_EXTS | _ATTACH_DOC_EXTS | _ATTACH_AUDIO_EXTS)
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(allowed)}")

    target_dir = _uploads_root() / _ATTACH_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(filename, ext)
    max_bytes = _MAX_ATTACHMENT_AUDIO_BYTES if kind == "audio" else _MAX_ATTACHMENT_BYTES
    reassemble_upload_chunks(_attach_chunks_root(), upload_id, total_chunks, dest, max_bytes=max_bytes)
    return await _finish_attachment_upload(dest, ext, kind, filename)


# ── Durable background transcription jobs — an opt-in alternative to the
# blocking routes above for a recording long enough that waiting on one
# request isn't practical (Whisper Test tab, or an AI Chat/Ask AI voice-memo
# attachment — mechanically identical here, only what the client does with a
# finished job differs). The actual work runs in the server process via
# app/audio_jobs.py, independent of any one connection, so closing the tab
# that started it doesn't stop it. Audio-only (unlike the upload routes
# above, which also handle images/documents) since transcription is the
# whole point of running this in the background.

def _current_user_id(request: Request) -> Optional[int]:
    user = getattr(request.state, "user", None)
    return user.id if user else None


def _job_to_dict(job: AudioJob) -> dict:
    return {
        "id": job.id, "purpose": job.purpose, "filename": job.filename,
        "status": job.status, "error": job.error,
        "transcript": job.transcript, "attachment_url": job.attachment_url,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.post("/attachments/audio-jobs")
async def ai_attachment_audio_job_create(
    request: Request,
    file: UploadFile = File(...),
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    """Start a durable background transcription job instead of waiting on
    one blocking request. Returns the job id immediately — poll GET
    .../audio-jobs/{id} or check the recent-jobs list to see it finish."""
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = _Path(file.filename).suffix.lower()
    if ext not in _ATTACH_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_ATTACH_AUDIO_EXTS))}")
    target_dir = _uploads_root() / _ATTACH_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_MAX_ATTACHMENT_AUDIO_BYTES)
    job_id = _audio_jobs.create_job(
        world_id=world.id, purpose="attachment", filename=file.filename,
        audio_path=dest, delete_after=False,
        attachment_url=f"/uploads/{_ATTACH_SUBDIR}/{dest.name}",
        created_by_user_id=_current_user_id(request),
    )
    return {"job_id": job_id}


@router.post("/attachments/audio-jobs/chunk")
async def ai_attachment_audio_job_chunk(
    request: Request,
    file: UploadFile = File(...),
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    """Same chunk-receiving route as .../upload/chunk, reused since a
    background job's upload can be just as large."""
    _require_ask_ai_access(request, db, active_world)
    save_upload_chunk(_attach_chunks_root(), upload_id, chunk_index, file, max_bytes=_MAX_ATTACHMENT_AUDIO_BYTES)
    return {"ok": True}


@router.post("/attachments/audio-jobs/complete")
async def ai_attachment_audio_job_complete(
    request: Request,
    upload_id: str = Form(...),
    filename: str = Form(...),
    total_chunks: int = Form(...),
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    """Reassemble the parts uploaded via .../audio-jobs/chunk and start a
    background job — unlike .../upload/complete, this returns the job id
    immediately rather than blocking on transcription."""
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not filename:
        raise HTTPException(400, "No filename given")
    ext = _Path(filename).suffix.lower()
    if ext not in _ATTACH_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_ATTACH_AUDIO_EXTS))}")
    target_dir = _uploads_root() / _ATTACH_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(filename, ext)
    reassemble_upload_chunks(_attach_chunks_root(), upload_id, total_chunks, dest, max_bytes=_MAX_ATTACHMENT_AUDIO_BYTES)
    job_id = _audio_jobs.create_job(
        world_id=world.id, purpose="attachment", filename=filename,
        audio_path=dest, delete_after=False,
        attachment_url=f"/uploads/{_ATTACH_SUBDIR}/{dest.name}",
        created_by_user_id=_current_user_id(request),
    )
    return {"job_id": job_id}


@router.get("/attachments/audio-jobs/{job_id}")
def ai_attachment_audio_job_status(
    job_id: int, request: Request, db=Depends(get_db), active_world: Optional[str] = Cookie(None),
):
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    job = db.query(AudioJob).filter(
        AudioJob.id == job_id, AudioJob.world_id == world.id, AudioJob.purpose == "attachment",
    ).first()
    if not job:
        raise HTTPException(404)
    return _job_to_dict(job)


@router.get("/attachments/audio-jobs")
def ai_attachment_audio_job_list(request: Request, db=Depends(get_db), active_world: Optional[str] = Cookie(None)):
    """Recent background attachment-transcription jobs for the active
    world — lets a GM (or an opted-in player) find a job again after
    closing the tab that started it."""
    _require_ask_ai_access(request, db, active_world)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    jobs = (
        db.query(AudioJob)
        .filter(AudioJob.world_id == world.id, AudioJob.purpose == "attachment")
        .order_by(AudioJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [_job_to_dict(j) for j in jobs]


@router.post("/stream")
async def ai_stream(
    body: ChatBody,
    request: Request,
    db=Depends(get_db),
    active_world: Optional[str] = Cookie(None),
):
    _require_ask_ai_access(request, db, active_world)

    msgs = _build_ollama_messages(body.messages)
    requested = body.model or _ai.get_defaults().get(body.surface, "")
    options = _clamp_options(body.options)
    _log.info("stream requested model=%r surface=%r msgs=%d", requested, body.surface, len(body.messages))

    async def _chat(model: str):
        async for token in _ai.stream_chat(msgs, body.system, model, options):
            yield token

    async def _gen():
        model, note = await _ai.resolve_model(requested)
        if note:
            yield f"data: {_json.dumps({'note': note})}\n\n"
        async for token in _with_heartbeat(_chat(model)):
            if token is None:
                yield ": keep-alive\n\n"
            else:
                yield f"data: {_json.dumps({'token': token})}\n\n"
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/models")
async def ai_models():
    loaded = await _ai._list_loaded()
    loaded_lower = [ll.lower() for ll in loaded]

    def _is_loaded(model_id: str) -> bool:
        ml = model_id.lower()
        return any(ml == ll or ml in ll or ll in ml for ll in loaded_lower)

    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    result = [
        {**m, "loaded": _is_loaded(m["id"]), "builtin": m["id"] in builtin_ids}
        for m in _ai.all_models()
    ]
    # Auto-surface any Ollama model not already in the list
    listed_lower = {m["id"].lower() for m in result}
    for lid in loaded:
        ll = lid.lower()
        if not any(ll == r or ll in r or r in ll for r in listed_lower):
            result.append({"id": lid, "label": lid, "loaded": True, "builtin": False})
    return {
        "models": result, "default": _ai.effective_ollama_model(), "available": loaded,
        "defaults": _ai.get_defaults(),
    }


@router.get("/resident")
async def ai_resident_models():
    """What's actually resident in VRAM right now (app.ai.resident_models,
    via Ollama's /api/ps) — distinct from /models' "loaded" field, which
    means "downloaded to disk," not "in memory." Backs the Models tab's
    "Resident in VRAM" section."""
    return {"models": await _ai.resident_models()}


class UnloadModelBody(BaseModel):
    model_id: str


@router.post("/unload")
async def ai_unload_model(body: UnloadModelBody):
    model_id = body.model_id.strip()
    if not model_id:
        raise HTTPException(400, "model_id is required")
    ok = await _ai.unload_model(model_id)
    if not ok:
        raise HTTPException(502, f"Could not unload {model_id} — is Ollama reachable?")
    return {"ok": True}


@router.get("/defaults")
async def ai_get_defaults():
    return _ai.get_defaults()


class SetDefaultBody(BaseModel):
    surface: str
    model_id: str = ""


@router.post("/defaults")
async def ai_set_default(body: SetDefaultBody):
    if body.surface not in _ai.DEFAULT_SURFACES:
        raise HTTPException(400, f"unknown surface {body.surface!r}")
    _ai.set_default(body.surface, body.model_id.strip())
    return {"ok": True, "defaults": _ai.get_defaults()}


class PresetBody(BaseModel):
    label: str
    model: str = ""
    system_extra: str = ""
    options: dict = {}


@router.get("/presets")
async def ai_list_presets():
    return {"presets": _ai.list_presets()}


@router.post("/presets")
async def ai_save_preset(body: PresetBody):
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "label is required")
    _ai.save_preset({
        "label": label, "model": body.model.strip(), "system_extra": body.system_extra.strip(),
        "options": _clamp_options(body.options),
    })
    return {"ok": True, "presets": _ai.list_presets()}


@router.delete("/presets/{label}")
async def ai_delete_preset(label: str):
    _ai.delete_preset(label)
    return {"ok": True, "presets": _ai.list_presets()}


@router.get("/debug")
async def ai_debug():
    return await _ai.debug_info()


class AddModelBody(BaseModel):
    id: str
    label: str = ""


@router.post("/models/add")
async def ai_models_add(body: AddModelBody):
    model_id = body.id.strip()
    if not model_id:
        from fastapi import HTTPException
        raise HTTPException(400, "model id required")
    label = body.label.strip() or model_id.split("/")[-1].split(":")[0]
    _log.info("model add: %s", model_id)
    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    if model_id in builtin_ids:
        _ai.unhide_builtin(model_id)
    else:
        custom = _ai.load_custom_models()
        if not any(m["id"] == model_id for m in custom):
            custom.append({"id": model_id, "label": label})
            _ai.save_custom_models(custom)
    return {"ok": True}


class RemoveModelBody(BaseModel):
    model_id: str
    delete_from_ollama: bool = False


@router.post("/models/remove")
async def ai_models_remove(body: RemoveModelBody):
    _log.info("model remove: %s delete_from_ollama=%s", body.model_id, body.delete_from_ollama)
    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    if body.model_id in builtin_ids:
        _ai.hide_builtin(body.model_id)
    else:
        custom = _ai.load_custom_models()
        custom = [m for m in custom if m["id"] != body.model_id]
        _ai.save_custom_models(custom)
    if body.delete_from_ollama:
        try:
            await _ai._client().delete(body.model_id)
        except Exception:
            pass
    return {"ok": True}


@router.post("/models/reset")
async def ai_models_reset():
    """Restore all built-in models (clears hidden list)."""
    _ai.reset_hidden()
    _log.info("model list reset to defaults")
    return {"ok": True}


class PullBody(BaseModel):
    model_id: str


@router.post("/pull")
async def ai_pull(body: PullBody):
    _log.info("pull model=%r", body.model_id)

    async def _gen():
        try:
            async for progress in await _ai._client().pull(body.model_id, stream=True):
                yield f"data: {_json.dumps(progress.model_dump())}\n\n"
        except _ollama.ResponseError as exc:
            yield f"data: {_json.dumps({'error': f'Ollama {exc.status_code}: {exc.error}'})}\n\n"
        except Exception as exc:
            yield f"data: {_json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/whisper/model-status")
async def api_whisper_model_status():
    return _ai.whisper_model_status()


class WhisperPullBody(BaseModel):
    url: str = ""
    # One of app.ai.WHISPER_KNOWN_MODELS' filenames — downloads that known
    # model to a file of its own instead of overwriting the active slot.
    # download_whisper_model validates this against the known list itself
    # (rejecting anything else) since it becomes a filesystem path.
    filename: str = ""


@router.post("/whisper/pull")
async def api_whisper_pull(body: WhisperPullBody):
    _log.info("whisper model pull url=%r filename=%r", body.url or "(default)", body.filename or "(active)")

    async def _gen():
        async for progress in _ai.download_whisper_model(body.url, body.filename):
            yield f"data: {_json.dumps(progress)}\n\n"
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


class EntityBody(BaseModel):
    name: str
    type: str
    description: str = ""


class NpcBody(BaseModel):
    name: str
    role: str
    notes: str = ""


class LocationBody(BaseModel):
    name: str
    region: str
    notes: str = ""


class QuestBody(BaseModel):
    title: str
    context: str = ""


@router.post("/generate/entity")
async def gen_entity(body: EntityBody):
    prompt = (
        f"Write an expanded description for this {body.type} named '{body.name}'. "
        f"Existing notes: {body.description}"
    )
    return {"result": await _ai.generate(prompt)}


@router.post("/generate/npc")
async def gen_npc(body: NpcBody):
    prompt = (
        f"Create a backstory and personality for an NPC named '{body.name}' "
        f"who is a {body.role}. Notes: {body.notes}"
    )
    return {"result": await _ai.generate(prompt)}


@router.post("/generate/location")
async def gen_location(body: LocationBody):
    prompt = (
        f"Describe the location '{body.name}' in the region '{body.region}'. "
        f"Notes: {body.notes}"
    )
    return {"result": await _ai.generate(prompt)}


@router.post("/generate/quest")
async def gen_quest(body: QuestBody):
    prompt = f"Create a quest hook for '{body.title}'. World context: {body.context}"
    return {"result": await _ai.generate(prompt)}


@router.post("/status")
async def ai_status():
    return await _ai.status()


# ── Image generation routes ───────────────────────────────────────────────────

@router.get("/imagegen/status")
async def api_imagegen_status():
    return await _ai.imagegen_status()


@router.get("/imagegen/models")
async def api_imagegen_models():
    return {"models": await _ai.imagegen_models()}


@router.get("/imagegen/loras")
async def api_imagegen_loras():
    return {"loras": await _ai.imagegen_loras()}


@router.get("/imagegen/samplers-schedulers")
async def api_imagegen_samplers_schedulers():
    return await _ai.imagegen_samplers_schedulers()


@router.get("/imagegen/upscalers")
async def api_imagegen_upscalers():
    return {"upscalers": await _ai.imagegen_upscalers()}


@router.get("/imagegen/refiners")
async def api_imagegen_refiners():
    return {"refiners": await _ai.imagegen_refiners()}


@router.get("/imagegen/ipadapter-models")
async def api_imagegen_ipadapter_models():
    return {"models": await _ai.imagegen_ipadapter_models()}


@router.get("/imagegen/progress")
async def api_imagegen_progress():
    return await _ai.imagegen_progress()


# ── Tag sources ───────────────────────────────────────────────────────────────

_BUILTIN_SOURCES = [
    {
        "id": "danbooru",
        "label": "Danbooru",
        "description": "General anime/art tags sorted by usage · SDXL optimised · ~4 MB",
        "url": "https://github.com/BetaDoggo/danbooru-tag-list/releases/download/Model-Tags/ChenkinNoob-XL-V0.3_underscore.csv",
        "swarmui_name": "danbooru",
        "cat_colors": {0: "#aaa", 1: "#c0a060", 3: "#a060c0", 4: "#60a0c0", 5: "#60c080"},
    },
    {
        "id": "e621",
        "label": "e621",
        "description": "Furry / anthro art tags from e621 · ~3 MB",
        "url": "https://github.com/DominikDoom/a1111-sd-webui-tagcomplete/raw/main/tags/e621.csv",
        "swarmui_name": "e621",
        "cat_colors": {0: "#aaa", 1: "#60c080", 3: "#a060c0", 4: "#60a0c0"},
    },
]

# per-source in-memory cache: source_id -> list of [tag, category, count]
_tags_cache: dict[str, list[list]] = {}


def _tag_dir() -> _Path:
    data_dir = _Path(_os.environ.get("DB_PATH", "/data/world.db")).parent
    return data_dir / "tags"


def _tag_file_for(source_id: str) -> _Path:
    return _tag_dir() / f"{source_id}_tags.csv"


def _active_source_file() -> _Path:
    return _tag_dir() / "active_source.txt"


def _custom_sources_file() -> _Path:
    return _tag_dir() / "custom_sources.json"


def _swarmui_ac_dir() -> _Path | None:
    d = _os.environ.get("SWARMUI_AC_DIR", "/data/swarmui-ac")
    return _Path(d) if d else None


def _get_active_source_id() -> str:
    f = _active_source_file()
    if f.exists():
        s = f.read_text().strip()
        if s:
            return s
    # Backward-compat: if old danbooru_tags.csv exists treat as active
    if _tag_file_for("danbooru").exists():
        return "danbooru"
    return ""


def _set_active_source_id(source_id: str):
    f = _active_source_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(source_id)


def _all_sources() -> list[dict]:
    sources = list(_BUILTIN_SOURCES)
    cf = _custom_sources_file()
    if cf.exists():
        try:
            sources.extend(_json.loads(cf.read_text()))
        except Exception:
            pass
    return sources


def _source_by_id(source_id: str) -> dict | None:
    for s in _all_sources():
        if s["id"] == source_id:
            return s
    return None


def _load_tags_for(source_id: str) -> list[list]:
    if source_id in _tags_cache:
        return _tags_cache[source_id]
    tf = _tag_file_for(source_id)
    if not tf.exists():
        return []
    rows: list[list] = []
    with open(tf, newline="", encoding="utf-8") as f:
        for row in _csv.reader(f):
            if len(row) >= 2:
                try:
                    cat = int(row[1]) if len(row) > 1 else 0
                    cnt = int(row[2]) if len(row) > 2 else 0
                    rows.append([row[0], cat, cnt])
                except ValueError:
                    pass
    _tags_cache[source_id] = rows
    return rows


@router.get("/imagegen/tags/sources")
async def api_tags_sources():
    active = _get_active_source_id()
    result = []
    for s in _all_sources():
        tf = _tag_file_for(s["id"])
        downloaded = tf.exists()
        count = len(_load_tags_for(s["id"])) if downloaded else 0
        result.append({
            "id": s["id"],
            "label": s["label"],
            "description": s.get("description", ""),
            "builtin": any(b["id"] == s["id"] for b in _BUILTIN_SOURCES),
            "downloaded": downloaded,
            "count": count,
            "active": s["id"] == active,
        })
    return {"sources": result, "active": active}


@router.get("/imagegen/tags/status")
async def api_tags_status():
    active = _get_active_source_id()
    if active:
        count = len(_load_tags_for(active))
        return {"loaded": bool(count), "count": count, "active": active}
    return {"loaded": False, "count": 0, "active": ""}


class FetchTagsBody(BaseModel):
    source_id: str = "danbooru"
    url: str = ""
    label: str = ""


@router.post("/imagegen/tags/fetch")
async def api_tags_fetch(body: FetchTagsBody):
    import asyncio
    source_id = body.source_id.strip() or "danbooru"
    source = _source_by_id(source_id)
    fetch_url = body.url.strip() or (source["url"] if source else "")
    if not fetch_url:
        return {"ok": False, "error": "No URL for this source"}

    # Register as custom source if unknown
    if not source:
        label = body.label.strip() or source_id
        cf = _custom_sources_file()
        cf.parent.mkdir(parents=True, exist_ok=True)
        custom = []
        if cf.exists():
            try:
                custom = _json.loads(cf.read_text())
            except Exception:
                pass
        if not any(s["id"] == source_id for s in custom):
            custom.append({"id": source_id, "label": label, "description": "Custom",
                           "url": fetch_url, "swarmui_name": source_id, "cat_colors": {}})
            cf.write_text(_json.dumps(custom))
        source = _source_by_id(source_id)

    tf = _tag_file_for(source_id)
    tf.parent.mkdir(parents=True, exist_ok=True)

    def _download():
        opener = _urllib.build_opener()
        opener.addheaders = [("User-Agent", "nd-world/1.0")]
        with opener.open(fetch_url, timeout=120) as resp:
            data = resp.read()
        tf.write_bytes(data)
        ac_dir = _swarmui_ac_dir()
        if ac_dir and ac_dir.exists():
            try:
                swarmui_name = (source or {}).get("swarmui_name", source_id)
                (ac_dir / f"{swarmui_name}.csv").write_bytes(data)
                _log.info("copied %s tags to SwarmUI AC dir", source_id)
            except Exception as exc:
                _log.warning("could not copy tags to SwarmUI AC dir: %s", exc)
        return len(data)

    try:
        size = await asyncio.get_event_loop().run_in_executor(None, _download)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    _tags_cache.pop(source_id, None)
    count = len(_load_tags_for(source_id))
    if not _get_active_source_id():
        _set_active_source_id(source_id)
    ac_dir = _swarmui_ac_dir()
    return {"ok": True, "bytes": size, "count": count,
            "swarmui_ac": bool(ac_dir and ac_dir.exists())}


class ActivateTagBody(BaseModel):
    source_id: str


@router.post("/imagegen/tags/activate")
async def api_tags_activate(body: ActivateTagBody):
    if not _tag_file_for(body.source_id).exists():
        from fastapi import HTTPException
        raise HTTPException(400, f"Source '{body.source_id}' not downloaded yet")
    _set_active_source_id(body.source_id)
    count = len(_load_tags_for(body.source_id))
    return {"ok": True, "active": body.source_id, "count": count}


class DeleteTagSourceBody(BaseModel):
    source_id: str


@router.post("/imagegen/tags/delete")
async def api_tags_delete(body: DeleteTagSourceBody):
    tf = _tag_file_for(body.source_id)
    if tf.exists():
        tf.unlink()
    _tags_cache.pop(body.source_id, None)
    if _get_active_source_id() == body.source_id:
        _set_active_source_id("")
    cf = _custom_sources_file()
    if cf.exists():
        try:
            custom = [s for s in _json.loads(cf.read_text()) if s["id"] != body.source_id]
            cf.write_text(_json.dumps(custom))
        except Exception:
            pass
    return {"ok": True}


@router.get("/imagegen/tags")
async def api_tags_search(q: str = "", limit: int = 25):
    if len(q) < 1:
        return {"tags": []}
    active = _get_active_source_id()
    if not active:
        return {"tags": []}
    tags = _load_tags_for(active)
    source = _source_by_id(active)
    cat_colors = {int(k): v for k, v in (source or {}).get("cat_colors", {0: "#aaa"}).items()}
    q_norm = q.lower().replace(" ", "_")
    exact: list[list] = []
    prefix: list[list] = []
    for t in tags:
        name = t[0]
        if name == q_norm:
            exact.append(t)
        elif name.startswith(q_norm):
            prefix.append(t)
        if len(exact) + len(prefix) >= limit * 3:
            break
    results = (exact + prefix)[:limit]
    return {
        "tags": [
            {"tag": t[0], "category": t[1], "count": t[2],
             "color": cat_colors.get(t[1], "#aaa")}
            for t in results
        ]
    }


# ── Starred images ────────────────────────────────────────────────────────────

class StarBody(BaseModel):
    url: str
    prompt: str = ""
    negative: str = ""
    model: str = ""
    seed: int = -1
    params: dict = {}


@router.post("/imagegen/star")
async def api_imagegen_star(body: StarBody, db=Depends(get_db)):
    from ..models import StarredImage
    existing = db.query(StarredImage).filter(StarredImage.url == body.url).first()
    if existing:
        return {"ok": True, "id": existing.id, "already": True}
    img = StarredImage(url=body.url, prompt=body.prompt, negative=body.negative,
                       model=body.model, seed=body.seed,
                       params_json=_json.dumps(body.params))
    db.add(img)
    db.commit()
    db.refresh(img)
    return {"ok": True, "id": img.id}


class UnstarBody(BaseModel):
    url: str


@router.post("/imagegen/unstar")
async def api_imagegen_unstar(body: UnstarBody, db=Depends(get_db)):
    from ..models import StarredImage
    db.query(StarredImage).filter(StarredImage.url == body.url).delete()
    db.commit()
    return {"ok": True}


@router.get("/imagegen/starred")
async def api_imagegen_starred(db=Depends(get_db)):
    from ..models import StarredImage
    images = db.query(StarredImage).order_by(StarredImage.created_at.desc()).all()
    return {"images": [
        {"id": i.id, "url": i.url, "prompt": i.prompt, "negative": i.negative,
         "model": i.model, "seed": i.seed,
         "params": _json.loads(i.params_json or "{}"),
         "created_at": i.created_at.isoformat()}
        for i in images
    ]}


class ImagegenBody(BaseModel):
    prompt: str = ""
    negative: str = ""
    model: str = ""
    width: int = 512
    height: int = 512
    steps: int = 20
    cfg: float = 7.0
    seed: int = -1
    sampler: str = "euler"
    scheduler: str = "normal"
    batch_size: int = 1
    loras: str = ""
    lora_weights: str = ""
    vae: str = ""
    clip_skip: int = -1
    init_image: str = ""
    init_strength: float = 0.6
    upscale_model: str = ""
    upscale_factor: float = 1.0
    controlnet_image: str = ""
    controlnet_strength: float = 0.8
    controlnet_preprocessor: str = ""
    controlnet_model: str = ""
    hiresfix: bool = False
    hireswidth: int = 0
    hiresheight: int = 0
    hiresdenoisestrength: float = 0.5
    hiressteps: int = 0
    refiner_model: str = ""
    refiner_control: float = 0.8
    seamless_x: bool = False
    seamless_y: bool = False
    variation_seed: int = -1
    variation_strength: float = 0.0
    freeu_enabled: bool = False
    freeu_b1: float = 1.3
    freeu_b2: float = 1.4
    freeu_s1: float = 0.9
    freeu_s2: float = 0.2
    dynthresh_enabled: bool = False
    dynthresh_mimic_scale: float = 7.0
    dynthresh_percentile: float = 0.999
    cfg_rescale: float = 0.0
    ipadapter_image: str = ""
    ipadapter_strength: float = 0.6
    ipadapter_model: str = ""


@router.post("/imagegen/generate")
async def api_imagegen_generate(body: ImagegenBody):
    _uploads = _Path(_os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
    try:
        urls = await _ai.imagegen_generate(
            prompt=body.prompt, negative=body.negative, model=body.model,
            width=body.width, height=body.height, steps=body.steps,
            cfg=body.cfg, seed=body.seed, uploads_dir=_uploads,
            sampler=body.sampler, scheduler=body.scheduler,
            batch_size=body.batch_size, loras=body.loras,
            lora_weights=body.lora_weights, vae=body.vae,
            clip_skip=body.clip_skip, init_image=body.init_image,
            init_strength=body.init_strength,
            upscale_model=body.upscale_model, upscale_factor=body.upscale_factor,
            controlnet_image=body.controlnet_image,
            controlnet_strength=body.controlnet_strength,
            controlnet_preprocessor=body.controlnet_preprocessor,
            controlnet_model=body.controlnet_model,
            hiresfix=body.hiresfix, hireswidth=body.hireswidth,
            hiresheight=body.hiresheight,
            hiresdenoisestrength=body.hiresdenoisestrength,
            hiressteps=body.hiressteps,
            refiner_model=body.refiner_model, refiner_control=body.refiner_control,
            seamless_x=body.seamless_x, seamless_y=body.seamless_y,
            variation_seed=body.variation_seed,
            variation_strength=body.variation_strength,
            freeu_enabled=body.freeu_enabled,
            freeu_b1=body.freeu_b1, freeu_b2=body.freeu_b2,
            freeu_s1=body.freeu_s1, freeu_s2=body.freeu_s2,
            dynthresh_enabled=body.dynthresh_enabled,
            dynthresh_mimic_scale=body.dynthresh_mimic_scale,
            dynthresh_percentile=body.dynthresh_percentile,
            cfg_rescale=body.cfg_rescale,
            ipadapter_image=body.ipadapter_image,
            ipadapter_strength=body.ipadapter_strength,
            ipadapter_model=body.ipadapter_model,
        )
        return {"url": urls[0] if urls else "", "urls": urls}
    except Exception as exc:
        _log.error("imagegen_generate failed: %s", exc)
        return {"url": "", "urls": [], "error": str(exc)}


@router.get("/test-chat")
async def ai_test_chat(model: str = ""):
    """Non-streaming single-turn test. Shows exact Ollama error for a given model ID."""
    resolved, note = await _ai.resolve_model(model)
    result = await _ai.generate_chat(
        [{"role": "user", "content": "Say only the word OK."}],
        system="",
        model=resolved,
    )
    return {"requested": model, "resolved": resolved, "note": note, "result": result}


@router.get("/ping")
async def ai_ping():
    """SSE smoke test — streams 5 dummy tokens without touching Ollama."""
    import asyncio

    async def _gen():
        for word in ["SSE", " ", "ping", " ", "OK"]:
            yield f"data: {_json.dumps({'token': word})}\n\n"
            await asyncio.sleep(0.05)
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
