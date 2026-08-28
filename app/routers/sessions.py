import io
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import auth
from .. import ai as _ai_module
from .. import audio_jobs as _audio_jobs
from ..database import get_db
from ..deps import get_world_ctx, paginate
from ..models import AudioJob, CombatSession, Entity, Fact, GameSession, Party, PlayerCharacter, Quest, World
from ..templating import templates
from ..uploads import copy_upload_bounded, reassemble_upload_chunks, save_upload_chunk

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


def _session_audio_jobs_dir() -> Path:
    """Where a background job's uploaded audio waits to be transcribed —
    separate from _session_audio_chunks_root (that's just staging for
    reassembly) since a job's file must outlive the request that uploaded
    it: the background task in app/audio_jobs.py reads it after the
    response has already been sent, and deletes it itself once done."""
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_jobs"


async def _transcribe_chunk(file: UploadFile, max_bytes: int = MAX_LIVE_CHUNK_BYTES, glossary: str = "", language: str = "") -> str:
    """Save an uploaded audio file to a temp path just long enough to run it
    through Whisper, then delete it — shared by the one-shot
    summarize-from-audio route and the live-transcript chunk-append route
    below, neither of which needs the audio itself kept afterward."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _SESSION_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_SESSION_AUDIO_EXTS))}")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    # Streamed straight to disk rather than read_upload_bounded (which
    # buffers the whole file in memory) — this route can see a full session
    # recording (MAX_SESSION_AUDIO_BYTES), and buffering one of those in RAM
    # per concurrent request risked exhausting memory well before hitting
    # any per-file size limit.
    copy_upload_bounded(file, tmp_path, max_bytes=max_bytes)
    try:
        return await _ai_module.transcribe_audio(tmp_path, glossary=glossary, language=language)
    finally:
        tmp_path.unlink(missing_ok=True)


def _glossary_for_world(world) -> str:
    return (world.whisper_glossary or "").strip() if world else ""


def _language_for_world(world) -> str:
    return (world.whisper_language or "").strip() if world else ""


def _recap_instructions_for_world(world) -> str:
    return (world.recap_instructions or "").strip() if world else ""


def _combine_recap_instructions(world_instructions: str, job_instructions: str) -> str:
    """The world's own persistent recap_instructions always applies;
    job_instructions is a one-off note for this specific summarize call
    only — neither replaces the other. Same combining rule as
    app.audio_jobs._combined_recap_instructions, kept as its own small copy
    here rather than shared, matching this codebase's per-module
    convention for small helpers like this."""
    parts = [p for p in (world_instructions.strip(), job_instructions.strip()) if p]
    return "\n\n".join(parts)


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
    # NPC picker candidates: character/creature entities only (mirrors
    # parties.py's companion picker), excluding anything explicitly tagged
    # subtype="PC" — a player's own mechanical sheet lives in
    # PlayerCharacter, not Entity, so a character Entity is either an NPC or
    # (rarely) a lore write-up of a PC for flavor, which "PC" marks. subtype
    # is a suggestion, not enforced (see deps.effective_subtypes), so this
    # is a best-effort filter, not a hard guarantee.
    npc_candidates = (
        db.query(Entity)
        .filter(Entity.world_id == gs.world_id, Entity.kind.in_(("character", "creature")))
        .filter(or_(Entity.subtype != "PC", Entity.subtype.is_(None)))
        .order_by(Entity.folder, Entity.name)
        .all()
    )
    party_pc_ids = json.loads(gs.party.member_pc_ids_json or "[]") if gs.party else []
    party_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(party_pc_ids)).all() if party_pc_ids else []
    return templates.TemplateResponse("sessions/detail.html", {
        "request": request, "world": world, "worlds": worlds, "gsession": gs,
        "parties": parties, "next_num": gs.session_num, "linked_combats": linked_combats,
        "npc_names": npc_names, "party_pcs": party_pcs,
        "npc_candidates_json": [{"id": e.id, "name": e.name, "folder": e.folder or ""} for e in npc_candidates],
        "prep": json.loads(gs.prep_json or "[]"), "loot": json.loads(gs.loot_json or "[]"),
        "npcs": npcs,
    })


def _session_download_filename(gs: GameSession, suffix: str) -> str:
    """Same sanitize-then-append idiom used by /entity/{id}/download.md and
    the character .ndc/.foundry.json exports — the title is free text, not
    a slug, so strip anything that isn't safe in a filename before adding
    the fixed suffix."""
    base = "".join(c if c.isalnum() or c in " -_" else "" for c in (gs.title or "session")) or "session"
    return f"{base}-{suffix}.md"


@router.get("/sessions/{session_id}/summary.md")
def session_download_summary(session_id: int, db: Session = Depends(get_db)):
    """GM-only, like every other route in this file that isn't in
    app.main._is_player_safe — the middleware already blocks a non-GM
    caller before this handler ever runs (see test_player_cannot_call_gm_
    ai_endpoints for the pattern this mirrors), so there's nothing to check
    here beyond the session existing. No active_world scoping either,
    matching session_detail/session_edit/session_delete above — a
    GameSession is looked up by id alone throughout this file."""
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    if not gs.summary:
        raise HTTPException(404, "This session has no summary yet")
    return StreamingResponse(
        io.BytesIO(gs.summary.encode()), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{_session_download_filename(gs, "summary")}"'},
    )


@router.get("/sessions/{session_id}/transcript.md")
def session_download_transcript(session_id: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    if not gs.live_transcript:
        raise HTTPException(404, "This session has no live transcript yet")
    return StreamingResponse(
        io.BytesIO(gs.live_transcript.encode()), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{_session_download_filename(gs, "transcript")}"'},
    )


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


def _session_prep_context(db: Session, gs: GameSession) -> str:
    """What the AI prep generator has to work with: the previous session's
    recap + facts, this world's open quests, and the assigned party's
    makeup. Deliberately just the most recent prior session (by
    session_num), not every fact ever logged — a prep sheet is about what's
    fresh, not the whole campaign history."""
    parts = []
    prior = (
        db.query(GameSession)
        .filter(GameSession.world_id == gs.world_id, GameSession.session_num < gs.session_num)
        .order_by(GameSession.session_num.desc())
        .first()
    )
    if prior:
        if prior.summary:
            parts.append(f"Recap of the last session (#{prior.session_num} {prior.title}):\n{prior.summary}")
        facts = db.query(Fact).filter(Fact.game_session_id == prior.id).order_by(Fact.created_at).all()
        if facts:
            parts.append("Facts from the last session:\n" + "\n".join(f"- {f.content}" for f in facts))

    quests = db.query(Quest).filter(Quest.world_id == gs.world_id, Quest.status == "active").order_by(Quest.title).all()
    if quests:
        lines = [f"- {q.title}" + (f": {q.summary}" if q.summary else "") for q in quests]
        parts.append("Open quests:\n" + "\n".join(lines))

    if gs.party_id:
        party = db.get(Party, gs.party_id)
        pc_ids = json.loads(party.member_pc_ids_json or "[]") if party else []
        pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all() if pc_ids else []
        if pcs:
            parts.append("Party: " + ", ".join(pc.name for pc in pcs))

    return "\n\n".join(parts)


@router.post("/api/sessions/{session_id}/prep/generate")
async def prep_generate(session_id: int, db: Session = Depends(get_db)):
    """Drafts a prep checklist from world state (see _session_prep_context)
    via the local model — returns the draft without writing anything. The
    client reviews/unchecks items, then adds confirmed ones through the
    existing prep/add route above, one at a time, rather than a new bulk
    write path."""
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    context = _session_prep_context(db, gs)
    if not context.strip():
        raise HTTPException(400, "Nothing to generate from yet — add a recap/facts to a prior session, an open quest, or a party first.")
    try:
        tasks = await _ai_module.generate_session_prep(context)
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    return {"tasks": tasks}


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

def _think_from_body(body: dict) -> bool:
    """The "Thinking" checkbox on the Recap panel / Retry-summary row is
    checked by default — a request that omits `think` entirely (an older
    client, or a direct API call) gets the same default the checkbox
    itself starts in, not the plain generate_chat default of False."""
    return bool(body.get("think", True))


@router.post("/api/sessions/ai/expand-notes")
async def api_expand_recap_notes(request: Request):
    """Expand terse GM notes (whatever's currently in the Summary textarea)
    into a polished narrative recap. Session-independent — works on the New
    Session form too, before anything has been saved."""
    body = await request.json()
    notes = str(body.get("notes", "")).strip()
    if not notes:
        raise HTTPException(400, "No notes provided")
    return {"recap": await _ai_module.expand_recap_notes(notes, think=_think_from_body(body))}


@router.post("/api/sessions/ai/condense-recap")
async def api_condense_recap(request: Request):
    body = await request.json()
    recap = str(body.get("recap", "")).strip()
    if not recap:
        raise HTTPException(400, "No recap provided")
    # fit_context: size num_ctx to this one recap instead of the GM's
    # configured/default context — see context_sized_options's docstring.
    # A one-call override only; the instance-wide default is untouched.
    options = _ai_module.context_sized_options(recap) if body.get("fit_context") else None
    model = str(body.get("model", "")).strip()
    recap_result = await _ai_module.condense_recap(recap, model=model, options=options, think=_think_from_body(body))
    return {"recap": recap_result}


@router.post("/api/sessions/ai/condense-job")
async def api_condense_job_create(
    request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Condense as a durable background job instead of a blocking request —
    same "keeps running after you close the tab or navigate away" contract
    audio_jobs.create_condense_job's own docstring describes. Returns the
    job id immediately; the client picks the result up through the same
    sessionAudioJobs panel/poll loop "Process in Background" summarize jobs
    already use (see api_audio_job_list/api_audio_job_status's purpose
    filter below, widened to include "condense")."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    body = await request.json()
    recap = str(body.get("recap", "")).strip()
    if not recap:
        raise HTTPException(400, "No recap provided")
    game_session_id = body.get("game_session_id")
    gs_id = int(game_session_id) if game_session_id else None
    job_id = _audio_jobs.create_condense_job(
        world_id=world.id, text=recap, model=str(body.get("model", "")).strip(),
        think=_think_from_body(body), fit_context=bool(body.get("fit_context")),
        game_session_id=gs_id, created_by_user_id=_current_user_id(request),
    )
    return {"job_id": job_id}


@router.post("/api/sessions/ai/summarize-from-audio")
async def api_summarize_from_audio(
    request: Request, file: UploadFile = File(...), extra_instructions: str = Form(""),
    think: bool = Form(True),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
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
    world, _ = get_world_ctx(request, db, active_world)
    try:
        transcript = await _transcribe_chunk(file, max_bytes=MAX_SESSION_AUDIO_BYTES, glossary=_glossary_for_world(world), language=_language_for_world(world))
    except _ai_module.WhisperError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not transcript:
        raise HTTPException(
            400,
            "Whisper transcribed this clip successfully but found no speech in it — "
            "check the recording actually captured audio.",
        )
    instructions = _combine_recap_instructions(_recap_instructions_for_world(world), extra_instructions)
    recap = await _ai_module.summarize_transcript(transcript, extra_instructions=instructions, think=think)
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
    request: Request,
    upload_id: str = Form(...), filename: str = Form(...), total_chunks: int = Form(...),
    extra_instructions: str = Form(""), think: bool = Form(True),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Reassemble the parts uploaded via .../chunk and finish exactly like
    the one-shot /api/sessions/ai/summarize-from-audio — same response
    shape, just fed from disk instead of the request body directly. Like
    that route, the reassembled audio only ever sits in a temp file for the
    duration of transcription and is never saved permanently."""
    world, _ = get_world_ctx(request, db, active_world)
    ext = Path(filename or "").suffix.lower()
    if ext not in _SESSION_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_SESSION_AUDIO_EXTS))}")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        reassemble_upload_chunks(_session_audio_chunks_root(), upload_id, total_chunks, tmp_path, max_bytes=MAX_SESSION_AUDIO_BYTES)
        try:
            transcript = await _ai_module.transcribe_audio(tmp_path, glossary=_glossary_for_world(world), language=_language_for_world(world))
        except _ai_module.WhisperError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    if not transcript:
        raise HTTPException(
            400,
            "Whisper transcribed this clip successfully but found no speech in it — "
            "check the recording actually captured audio.",
        )
    instructions = _combine_recap_instructions(_recap_instructions_for_world(world), extra_instructions)
    recap = await _ai_module.summarize_transcript(transcript, extra_instructions=instructions, think=think)
    return {"transcript": transcript, "recap": recap}


# ── Durable background transcription jobs — an opt-in alternative to the
# blocking routes above for a recording long enough that waiting on one
# HTTP request (up to WHISPER_TIMEOUT_SECONDS) isn't practical: the actual
# work runs in the server process via app/audio_jobs.py, independent of any
# one connection, so closing the tab that started it doesn't stop it. Same
# upload/reassembly plumbing as the direct routes above, just handed off to
# a background task instead of transcribed inline.

def _current_user_id(request: Request) -> Optional[int]:
    user = getattr(request.state, "user", None)
    return user.id if user else None


def _job_to_dict(job: AudioJob) -> dict:
    return {
        "id": job.id, "purpose": job.purpose, "filename": job.filename,
        "status": job.status, "error": job.error,
        "transcript": job.transcript, "recap": job.recap, "model": job.model or "",
        "extra_instructions": job.extra_instructions or "",
        "game_session_id": job.game_session_id,
        "chunk_current": job.chunk_current, "chunk_total": job.chunk_total,
        "run_started_at": job.run_started_at.isoformat() if job.run_started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.post("/api/sessions/ai/audio-jobs")
async def api_audio_job_create(
    request: Request, file: UploadFile = File(...), game_session_id: str = Form(""),
    model: str = Form(""), extra_instructions: str = Form(""), think: bool = Form(True),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Start a durable background transcribe+summarize job for a session
    recording, instead of waiting on one blocking request. Returns the job
    id immediately — poll GET .../audio-jobs/{id} or check the recent-jobs
    list to see it progress and finish."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _SESSION_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_SESSION_AUDIO_EXTS))}")
    jobs_dir = _session_audio_jobs_dir()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    dest = jobs_dir / f"{uuid.uuid4().hex}{ext}"
    copy_upload_bounded(file, dest, max_bytes=MAX_SESSION_AUDIO_BYTES)
    gs_id = int(game_session_id) if game_session_id.strip().isdigit() else None
    job_id = _audio_jobs.create_job(
        world_id=world.id, purpose="session_recap", filename=file.filename or "",
        audio_path=dest, delete_after=True, game_session_id=gs_id,
        created_by_user_id=_current_user_id(request), model=model.strip(),
        extra_instructions=extra_instructions.strip(), think=think,
    )
    return {"job_id": job_id}


@router.post("/api/sessions/ai/audio-jobs/chunk")
async def api_audio_job_chunk(
    file: UploadFile = File(...), upload_id: str = Form(...), chunk_index: int = Form(...),
):
    """Same chunk-receiving route as .../summarize-from-audio/chunk (a
    background job's upload can be just as large) — the only difference is
    what .../audio-jobs/complete does with the reassembled file afterward."""
    save_upload_chunk(_session_audio_chunks_root(), upload_id, chunk_index, file, max_bytes=MAX_SESSION_AUDIO_BYTES)
    return {"ok": True}


@router.post("/api/sessions/ai/audio-jobs/complete")
async def api_audio_job_complete(
    request: Request, upload_id: str = Form(...), filename: str = Form(...),
    total_chunks: int = Form(...), game_session_id: str = Form(""),
    model: str = Form(""), extra_instructions: str = Form(""), think: bool = Form(True),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Reassemble the parts uploaded via .../audio-jobs/chunk and start a
    background job — unlike .../summarize-from-audio/complete, this returns
    the job id immediately rather than blocking on transcription."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    ext = Path(filename or "").suffix.lower()
    if ext not in _SESSION_AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type {ext!r} — allowed: {', '.join(sorted(_SESSION_AUDIO_EXTS))}")
    jobs_dir = _session_audio_jobs_dir()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    dest = jobs_dir / f"{uuid.uuid4().hex}{ext}"
    reassemble_upload_chunks(_session_audio_chunks_root(), upload_id, total_chunks, dest, max_bytes=MAX_SESSION_AUDIO_BYTES)
    gs_id = int(game_session_id) if game_session_id.strip().isdigit() else None
    job_id = _audio_jobs.create_job(
        world_id=world.id, purpose="session_recap", filename=filename,
        audio_path=dest, delete_after=True, game_session_id=gs_id,
        created_by_user_id=_current_user_id(request), model=model.strip(),
        extra_instructions=extra_instructions.strip(), think=think,
    )
    return {"job_id": job_id}


# purpose IN (session_recap, condense): both are session-scoped recap-style
# jobs the sessionAudioJobs panel on the Sessions page polls/lists together
# — "attachment" jobs (AI Chat voice memos) are a different panel entirely
# and must stay excluded here.
_SESSION_JOB_PURPOSES = ("session_recap", "condense")


@router.get("/api/sessions/ai/audio-jobs/{job_id}")
def api_audio_job_status(job_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    job = db.query(AudioJob).filter(
        AudioJob.id == job_id, AudioJob.world_id == world.id, AudioJob.purpose.in_(_SESSION_JOB_PURPOSES),
    ).first()
    if not job:
        raise HTTPException(404)
    return _job_to_dict(job)


@router.get("/api/sessions/ai/audio-jobs")
def api_audio_job_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Recent background transcription jobs for the active world — lets a
    GM find a job again after closing the tab that started it (even from a
    different browser), not just while it's still visible on the page that
    kicked it off."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    jobs = (
        db.query(AudioJob)
        .filter(AudioJob.world_id == world.id, AudioJob.purpose.in_(_SESSION_JOB_PURPOSES))
        .order_by(AudioJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [_job_to_dict(j) for j in jobs]


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
    world = db.get(World, gs.world_id)
    try:
        chunk_text = (await _transcribe_chunk(file, glossary=_glossary_for_world(world), language=_language_for_world(world))).strip()
    except _ai_module.WhisperError as exc:
        raise HTTPException(400, str(exc)) from exc
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
async def api_summarize_live_transcript(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    if not (gs.live_transcript or "").strip():
        raise HTTPException(400, "No live transcript recorded for this session yet.")
    # world_id comes from the session itself, not the active_world cookie —
    # this route has no cookie param, and the session's own world is always
    # the right one regardless of which world tab is currently active.
    world = db.get(World, gs.world_id)
    # Same "predates taking a body" situation summarize-from-facts is in —
    # read think optionally rather than requiring a caller to start sending
    # an otherwise-pointless empty JSON body.
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    recap = await _ai_module.summarize_transcript(
        gs.live_transcript, extra_instructions=_recap_instructions_for_world(world),
        think=_think_from_body(body),
    )
    return {"transcript": gs.live_transcript, "recap": recap}


@router.post("/api/sessions/{session_id}/ai/summarize-from-facts")
async def api_summarize_from_facts(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    facts = db.query(Fact).filter(Fact.game_session_id == session_id).order_by(Fact.created_at).all()
    if not facts:
        raise HTTPException(400, "No facts logged for this session yet — log some on the Facts page first.")
    world = db.get(World, gs.world_id)
    # This route predates taking a body at all (the client's existing call
    # sends none) — read think optionally rather than requiring every
    # caller to start sending an (otherwise pointless) empty JSON body.
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    recap = await _ai_module.summarize_session_from_facts(
        [f.content for f in facts], extra_instructions=_recap_instructions_for_world(world),
        think=_think_from_body(body),
    )
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
    world = db.get(World, gs.world_id) if gs else None
    user = getattr(request.state, "user", None)
    if not gs or not auth.user_can_access_world(db, user, world):
        raise HTTPException(404)
    q = db.query(Fact).filter(Fact.game_session_id == session_id)
    if not (user and user.is_gm):
        q = q.filter(Fact.visible_to_players.isnot(False))
    facts = q.order_by(Fact.created_at).all()
    if not facts:
        return {"recap": "", "empty": True}
    recap = await _ai_module.summarize_session_from_facts([f.content for f in facts], extra_instructions=_recap_instructions_for_world(world))
    return {"recap": recap}
