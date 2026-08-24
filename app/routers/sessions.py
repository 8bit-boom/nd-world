import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from .. import ai as _ai_module
from ..database import get_db
from ..deps import get_world_ctx, paginate
from ..models import CombatSession, Entity, Fact, GameSession, Party, PlayerCharacter, World
from ..templating import templates
from ..uploads import read_upload_bounded, reassemble_upload_chunks, save_upload_chunk

router = APIRouter()

# Same allowed set as the AI-attachment and Audio Library upload pipelines
# (app/routers/ai.py's _ATTACH_AUDIO_EXTS, app/routers/audio.py's
# _ALLOWED_EXTS) — .webm/.ogg covers what MediaRecorder produces in-browser.
_SESSION_AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"}
# A session recording can run long — same default ceiling as the Audio
# Library's own MAX_AUDIO_UPLOAD_BYTES, reusing that env var rather than
# introducing a second one for what's really the same kind of upload.
MAX_SESSION_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
# One live-recording chunk (see the "Live session recording" section below)
# is only ever a minute or so of audio — this just needs to be generous
# enough that a slightly-longer-than-expected chunk (a slow browser tab, a
# missed stop/restart) doesn't 413 and silently drop that segment.
MAX_LIVE_CHUNK_BYTES = int(os.environ.get("MAX_LIVE_CHUNK_BYTES", str(25 * 1024 * 1024)))


def _session_audio_chunks_root() -> Path:
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_chunks"


async def _transcribe_chunk(file: UploadFile, max_bytes: int = MAX_LIVE_CHUNK_BYTES) -> str:
    """Save an uploaded audio file to a temp path just long enough to run it
    through Whisper, then delete it — shared by the one-shot
    summarize-from-audio route and the live-transcript chunk-append route
    below, neither of which needs the audio itself kept afterward."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _SESSION_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_SESSION_AUDIO_EXTS))}")
    raw = read_upload_bounded(file, max_bytes=max_bytes)
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        return await _ai_module.transcribe_audio(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/sessions", response_class=HTMLResponse)
def sessions_list(request: Request, page: int = 1, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    base_q = db.query(GameSession).filter(
        GameSession.world_id == (world.id if world else 1)
    ).order_by(GameSession.session_num.desc())
    sessions, page, total_pages = paginate(base_q, page)
    return templates.TemplateResponse("sessions/list.html", {
        "request": request, "world": world, "worlds": worlds, "sessions": sessions,
        "page": page, "total_pages": total_pages,
    })


@router.get("/sessions/new", response_class=HTMLResponse)
def session_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    parties = db.query(Party).filter(Party.world_id == (world.id if world else 1)).order_by(Party.name).all()
    last = db.query(GameSession).filter(
        GameSession.world_id == (world.id if world else 1)
    ).order_by(GameSession.session_num.desc()).first()
    next_num = (last.session_num + 1) if last else 1
    return templates.TemplateResponse("sessions/detail.html", {
        "request": request, "world": world, "worlds": worlds, "gsession": None,
        "parties": parties, "next_num": next_num, "linked_combats": [],
    })


@router.post("/sessions/new")
async def session_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    form = await request.form()
    gs = GameSession(
        world_id=world.id if world else 1,
        title=str(form.get("title", "")).strip() or "Untitled Session",
        session_num=int(form.get("session_num") or 1),
        session_date=str(form.get("session_date", "")).strip() or None,
        summary=str(form.get("summary", "")),
        party_id=int(form["party_id"]) if form.get("party_id") else None,
        xp_awarded=int(form.get("xp_awarded") or 0),
    )
    db.add(gs)
    db.commit()
    db.refresh(gs)
    return RedirectResponse(f"/sessions/{gs.id}", status_code=303)


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    parties = db.query(Party).filter(Party.world_id == gs.world_id).order_by(Party.name).all()
    linked_combats = db.query(CombatSession).filter(CombatSession.game_session_id == gs.id).all()
    npcs = json.loads(gs.npcs_json or "[]")
    entity_map = {e.id: e for e in db.query(Entity).filter(Entity.id.in_([n["entity_id"] for n in npcs])).all()} if npcs else {}
    npc_names = [entity_map[n["entity_id"]].name for n in npcs if entity_map.get(n["entity_id"])]
    all_entities = db.query(Entity).filter(Entity.world_id == gs.world_id).order_by(Entity.name).all()
    party_pc_ids = json.loads(gs.party.member_pc_ids_json or "[]") if gs.party else []
    party_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(party_pc_ids)).all() if party_pc_ids else []
    return templates.TemplateResponse("sessions/detail.html", {
        "request": request, "world": world, "worlds": worlds, "gsession": gs,
        "parties": parties, "next_num": gs.session_num, "linked_combats": linked_combats,
        "npc_names": npc_names, "all_entities": all_entities, "party_pcs": party_pcs,
        "prep": json.loads(gs.prep_json or "[]"), "loot": json.loads(gs.loot_json or "[]"),
        "npcs": npcs,
    })


@router.post("/sessions/{session_id}/edit")
async def session_edit(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    form = await request.form()
    gs.title = str(form.get("title", gs.title)).strip() or gs.title
    gs.session_num = int(form.get("session_num") or gs.session_num)
    gs.session_date = str(form.get("session_date", "")).strip() or None
    gs.summary = str(form.get("summary", ""))
    party_id = form.get("party_id")
    gs.party_id = int(party_id) if party_id else None
    npc_ids = [int(v) for v in form.getlist("npc_entity_ids")]
    entity_names = {e.id: e.name for e in db.query(Entity).filter(Entity.id.in_(npc_ids)).all()} if npc_ids else {}
    gs.npcs_json = json.dumps([{"entity_id": i, "name": entity_names.get(i, "")} for i in npc_ids])
    db.commit()
    return RedirectResponse(f"/sessions/{session_id}?saved=1", status_code=303)


@router.post("/sessions/{session_id}/delete")
def session_delete(session_id: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    db.query(CombatSession).filter(CombatSession.game_session_id == session_id).update({"game_session_id": None})
    db.delete(gs)
    db.commit()
    return RedirectResponse("/sessions", status_code=303)


@router.post("/api/sessions/{session_id}/prep/toggle")
async def prep_toggle(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    body = await request.json()
    idx = int(body.get("index", -1))
    prep = json.loads(gs.prep_json or "[]")
    if 0 <= idx < len(prep):
        prep[idx]["done"] = not prep[idx].get("done", False)
    gs.prep_json = json.dumps(prep)
    db.commit()
    return {"prep": prep}


@router.post("/api/sessions/{session_id}/prep/add")
async def prep_add(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    body = await request.json()
    task = str(body.get("task", "")).strip()
    prep = json.loads(gs.prep_json or "[]")
    if task:
        prep.append({"task": task, "done": False})
    gs.prep_json = json.dumps(prep)
    db.commit()
    return {"prep": prep}


@router.post("/api/sessions/{session_id}/prep/{idx}/delete")
def prep_delete(session_id: int, idx: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    prep = json.loads(gs.prep_json or "[]")
    if 0 <= idx < len(prep):
        prep.pop(idx)
    gs.prep_json = json.dumps(prep)
    db.commit()
    return {"prep": prep}


@router.post("/api/sessions/{session_id}/xp")
async def session_xp(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    body = await request.json()
    delta = int(body.get("delta", 0))
    pc_ids = body.get("pc_ids")
    if not pc_ids and gs.party:
        pc_ids = json.loads(gs.party.member_pc_ids_json or "[]")
    pc_ids = pc_ids or []
    updated = []
    for pc in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all():
        pc.xp = max(0, pc.xp + delta)
        updated.append(pc.name)
    gs.xp_awarded = (gs.xp_awarded or 0) + delta
    db.commit()
    return {"updated": updated, "xp_awarded": gs.xp_awarded}


@router.post("/api/sessions/{session_id}/loot/transfer")
async def session_loot_transfer(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    body = await request.json()
    action = body.get("action")
    loot = json.loads(gs.loot_json or "[]")
    if action == "add":
        loot.append({"name": body.get("name", "Item"), "qty": int(body.get("qty", 1) or 1), "notes": body.get("notes", "")})
        gs.loot_json = json.dumps(loot)
        db.commit()
        return {"loot": loot}
    idx = int(body.get("index", -1))
    if not (0 <= idx < len(loot)):
        raise HTTPException(400, "Invalid loot index")
    item = loot.pop(idx)
    gs.loot_json = json.dumps(loot)
    if action == "transfer" and gs.party:
        party_loot = json.loads(gs.party.loot_json or "[]")
        party_loot.append(item)
        gs.party.loot_json = json.dumps(party_loot)
    db.commit()
    return {"loot": loot}


# ── AI recap assist (GM-only, via the Sessions edit page) ────────────────────

@router.post("/api/sessions/ai/expand-notes")
async def api_expand_recap_notes(request: Request):
    """Expand terse GM notes (whatever's currently in the Summary textarea)
    into a polished narrative recap. Session-independent — works on the New
    Session form too, before anything has been saved."""
    body = await request.json()
    notes = str(body.get("notes", "")).strip()
    if not notes:
        raise HTTPException(400, "No notes provided")
    return {"recap": await _ai_module.expand_recap_notes(notes)}


@router.post("/api/sessions/ai/condense-recap")
async def api_condense_recap(request: Request):
    body = await request.json()
    recap = str(body.get("recap", "")).strip()
    if not recap:
        raise HTTPException(400, "No recap provided")
    return {"recap": await _ai_module.condense_recap(recap)}


@router.post("/api/sessions/ai/summarize-from-audio")
async def api_summarize_from_audio(file: UploadFile = File(...)):
    """Transcribe an uploaded (file-picked, dropped, or mic-recorded) session
    recording via Whisper, then summarize the transcript into a narrative
    recap — same one-shot "AI draft, GM reviews/applies" flow as the notes/
    facts recap buttons on this page. Session-independent, like expand-notes/
    condense-recap above (works on the New Session form too, before anything
    has been saved) — unlike summarize-from-facts, nothing here depends on a
    session already existing in the database.

    The audio itself only ever sits in a temp file for the duration of the
    transcription call and is never saved permanently; a GM who wants to
    keep the recording should upload it to the Audio Library separately."""
    transcript = await _transcribe_chunk(file, max_bytes=MAX_SESSION_AUDIO_BYTES)
    if not transcript:
        raise HTTPException(
            400,
            "Could not transcribe this audio — check that Whisper is configured and reachable "
            "(see the AI page's 🎙 Whisper tab) and that the clip actually has speech in it.",
        )
    recap = await _ai_module.summarize_transcript(transcript)
    return {"transcript": transcript, "recap": recap}


@router.post("/api/sessions/ai/summarize-from-audio/chunk")
async def api_summarize_from_audio_chunk(
    file: UploadFile = File(...), upload_id: str = Form(...), chunk_index: int = Form(...),
):
    """Receive one part of a large session recording — a whole-session
    upload can easily clear Cloudflare's fixed 100MB request-body cap (see
    docs/DEPLOYMENT.md), so ndChunkedUpload (static/js/chunked-upload.js)
    splits it client-side and sends each part here; .../complete reassembles
    and transcribes once every part has arrived. Mirrors app/routers/
    audio.py's chunked-upload pair; GM-only like the rest of this route's
    session-independent AI helpers (no _is_player_safe entry)."""
    save_upload_chunk(_session_audio_chunks_root(), upload_id, chunk_index, file, max_bytes=MAX_SESSION_AUDIO_BYTES)
    return {"ok": True}


@router.post("/api/sessions/ai/summarize-from-audio/complete")
async def api_summarize_from_audio_complete(
    upload_id: str = Form(...), filename: str = Form(...), total_chunks: int = Form(...),
):
    """Reassemble the parts uploaded via .../chunk and finish exactly like
    the one-shot /api/sessions/ai/summarize-from-audio — same response
    shape, just fed from disk instead of the request body directly. Like
    that route, the reassembled audio only ever sits in a temp file for the
    duration of transcription and is never saved permanently."""
    ext = Path(filename or "").suffix.lower()
    if ext not in _SESSION_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_SESSION_AUDIO_EXTS))}")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        reassemble_upload_chunks(_session_audio_chunks_root(), upload_id, total_chunks, tmp_path, max_bytes=MAX_SESSION_AUDIO_BYTES)
        transcript = await _ai_module.transcribe_audio(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not transcript:
        raise HTTPException(
            400,
            "Could not transcribe this audio — check that Whisper is configured and reachable "
            "(see the AI page's 🎙 Whisper tab) and that the clip actually has speech in it.",
        )
    recap = await _ai_module.summarize_transcript(transcript)
    return {"transcript": transcript, "recap": recap}


# ── Live session recording: short chunks, transcribed and saved as they
# arrive, so a multi-hour recording survives a crashed tab or dropped
# connection with at most one chunk lost instead of the whole session. The
# browser side (sessions/detail.html) stops and restarts a fresh short
# MediaRecorder segment every ~minute and uploads each one here in order as
# it finishes — this endpoint has no idea how long the overall recording
# has been running, it only ever sees one chunk at a time (see
# _transcribe_chunk above, shared with summarize-from-audio).

@router.post("/api/sessions/{session_id}/live-transcript/append")
async def api_live_transcript_append(session_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    chunk_text = (await _transcribe_chunk(file)).strip()
    if chunk_text:
        gs.live_transcript = (gs.live_transcript or "") + (" " if gs.live_transcript else "") + chunk_text
        db.commit()
    # chunk_text can legitimately be "" (a silent segment) — that's not an
    # error, just nothing to append; the client still needs the running
    # total either way to keep its live display in sync.
    return {"chunk_text": chunk_text, "transcript": gs.live_transcript}


@router.post("/api/sessions/{session_id}/live-transcript/clear")
def api_live_transcript_clear(session_id: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    gs.live_transcript = ""
    db.commit()
    return {"transcript": ""}


@router.post("/api/sessions/{session_id}/ai/summarize-live-transcript")
async def api_summarize_live_transcript(session_id: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    if not (gs.live_transcript or "").strip():
        raise HTTPException(400, "No live transcript recorded for this session yet.")
    recap = await _ai_module.summarize_transcript(gs.live_transcript)
    return {"transcript": gs.live_transcript, "recap": recap}


@router.post("/api/sessions/{session_id}/ai/summarize-from-facts")
async def api_summarize_from_facts(session_id: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    facts = db.query(Fact).filter(Fact.game_session_id == session_id).order_by(Fact.created_at).all()
    if not facts:
        raise HTTPException(400, "No facts logged for this session yet — log some on the Facts page first.")
    recap = await _ai_module.summarize_session_from_facts([f.content for f in facts])
    return {"recap": recap}


# ── Player-facing session log (read-only, AI-synthesized) ───────────────────
#
# Deliberately separate from /sessions (GM-only): that page's Summary field
# is the GM's raw recap, free-text with no visibility flag of its own, so it
# can (and often does) contain secrets — exactly why the Facts feature
# exists as a *discrete*, individually-flagged log. What a player sees here
# is never that raw text; it's synthesized fresh, each time, purely from
# this session's Facts already marked visible_to_players (or all facts, for
# a GM browsing the same page) — the same security boundary the Chronicler
# uses, just narrated for one session instead of the whole world.

@router.get("/session-log", response_class=HTMLResponse)
def session_log_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    sessions = db.query(GameSession).filter(
        GameSession.world_id == (world.id if world else 1)
    ).order_by(GameSession.session_num.desc()).all()
    return templates.TemplateResponse("sessions/player_list.html", {
        "request": request, "world": world, "worlds": worlds, "sessions": sessions,
    })


@router.get("/session-log/{session_id}", response_class=HTMLResponse)
def session_log_detail(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    user = getattr(request.state, "user", None)
    if not gs or not auth.user_can_access_world(db, user, db.get(World, gs.world_id)):
        raise HTTPException(404)
    return templates.TemplateResponse("sessions/player_detail.html", {
        "request": request, "world": world, "worlds": worlds, "gsession": gs,
    })


@router.post("/api/session-log/{session_id}/recap")
async def api_session_log_recap(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    user = getattr(request.state, "user", None)
    if not gs or not auth.user_can_access_world(db, user, db.get(World, gs.world_id)):
        raise HTTPException(404)
    q = db.query(Fact).filter(Fact.game_session_id == session_id)
    if not (user and user.is_gm):
        q = q.filter(Fact.visible_to_players.isnot(False))
    facts = q.order_by(Fact.created_at).all()
    if not facts:
        return {"recap": "", "empty": True}
    recap = await _ai_module.summarize_session_from_facts([f.content for f in facts])
    return {"recap": recap}
