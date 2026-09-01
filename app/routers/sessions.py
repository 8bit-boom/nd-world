import io
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import auth
from .. import ai as _ai_module
from .. import audio_jobs as _audio_jobs
from ..database import get_db
from ..deps import check_llm_cooldown, get_world_ctx, paginate
from ..models import AudioClip, AudioJob, CombatSession, Entity, Fact, GameSession, Party, PlayerCharacter, Quest, World
from ..templating import templates
from ..uploads import CHUNK_ID_RE, copy_upload_bounded, reassemble_upload_chunks, save_upload_chunk

router = APIRouter()

# Same name/approach as video.py's and ai.py's module loggers — ffmpeg
# failures in the live-audio concat path warn here instead of 500-ing.
_log = logging.getLogger("nd.sessions.router")

# Same allowed set as the AI-attachment and Audio Library upload pipelines
# (app/routers/ai.py's _ATTACH_AUDIO_EXTS, app/routers/audio.py's
# _ALLOWED_EXTS) — .webm/.ogg covers what MediaRecorder produces in-browser.
_SESSION_AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"}
# A session recording can run long — same default ceiling as the Audio
# Library's own MAX_AUDIO_UPLOAD_BYTES, reusing that env var rather than
# introducing a second one for what's really the same kind of upload.
MAX_SESSION_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
# A live-recording chunk (see the "Live session recording" section below) is
# GM-configurable from ~1 to ~15 minutes — this just needs to be generous
# enough that a slightly-longer-than-expected chunk (a slow browser tab, a
# missed stop/restart) doesn't 413 and silently drop that segment.
MAX_LIVE_CHUNK_BYTES = int(os.environ.get("MAX_LIVE_CHUNK_BYTES", str(25 * 1024 * 1024)))
# Per-segment cap for the opt-in raw-audio archive (api_live_transcript_append
# below). The longest segment the client's chunk-length selector can produce
# is 15 minutes, and a 15-minute browser MediaRecorder segment at even a
# generous Opus bitrate is well under 20 MB — so ~200 MB is purely a
# runaway-client bound (a GM pointing the endpoint at a movie file, say),
# sized to never reject a real segment, same spirit as MAX_LIVE_CHUNK_BYTES.
MAX_LIVE_SAVED_SEGMENT_BYTES = int(os.environ.get("MAX_LIVE_SAVED_SEGMENT_BYTES", str(200 * 1024 * 1024)))


def _session_audio_chunks_root() -> Path:
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_chunks"


def _session_audio_jobs_dir() -> Path:
    """Where a background job's uploaded audio waits to be transcribed —
    separate from _session_audio_chunks_root (that's just staging for
    reassembly) since a job's file must outlive the request that uploaded
    it: the background task in app/audio_jobs.py reads it after the
    response has already been sent, and deletes it itself once done."""
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_jobs"


async def _transcribe_chunk(file: UploadFile, max_bytes: int = MAX_LIVE_CHUNK_BYTES, glossary: str = "", language: str = "", denoise: bool = False) -> str:
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
        return await _ai_module.transcribe_audio(tmp_path, glossary=glossary, language=language, denoise=denoise)
    finally:
        tmp_path.unlink(missing_ok=True)


def _glossary_for_world(world, game_session_id: int | None = None) -> str:
    """Delegates to audio_jobs' own _glossary_for_world (world_id, not the
    World object this takes) rather than keeping a second copy of the
    entity-name merge logic — see its own docstring for what "glossary"
    actually includes beyond whatever the GM typed into World.
    whisper_glossary, and for what game_session_id does (feeds that
    session's "Entities Featured" picks to the front of the entity-name
    list)."""
    return _audio_jobs._glossary_for_world(world.id, game_session_id) if world else ""


def _language_for_world(world) -> str:
    return (world.whisper_language or "").strip() if world else ""


def _denoise_for_world(world) -> bool:
    return bool(world and world.whisper_denoise)


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


def _reject_if_too_long_to_condense(recap: str, extra_instructions: str) -> None:
    """See docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 3.3:
    app.ai.context_sized_options (what condense_call_options builds on)
    clamps its auto-sized num_ctx to app.ai.MAX_AUTO_NUM_CTX — necessary so
    a pathological paste can't make this module try to allocate a
    six-figure num_ctx worth of KV cache, but clamping alone would
    reintroduce the exact silent-truncation "model responds with garbage"
    failure condense_call_options exists to prevent, for input that
    genuinely exceeds the ceiling. condense_recap is always a single
    unchunked call with nothing else protecting it (unlike summarize_
    transcript's own chunking, which stays unaffected by this — each of
    its calls is already bounded well under the ceiling). Refuse outright
    instead, pointing the GM at Summarize rather than truncating."""
    text = recap + extra_instructions
    chars_per_token = _ai_module._chars_per_token_estimate(text)
    tokens = -(-len(text) // chars_per_token)  # ceil division, same convention context_sized_options uses
    ceiling = _ai_module.MAX_AUTO_NUM_CTX - _ai_module._CONTEXT_FIT_RESERVED_TOKENS
    if tokens > ceiling:
        raise HTTPException(
            400,
            f"This text is too long to condense in one call (≈{tokens} tokens > the "
            f"{ceiling}-token ceiling) — use Summarize, which splits long input into parts",
        )


def _recap_model(model: str) -> str:
    """Falls back to the "recap" surface default (Models tab) instead of
    letting an unspecified model fall straight through to resolve_model's
    instance-wide default — same per-surface fallback app.audio_jobs.
    _run_job already applies for job-based condense/summarize work,
    extended here to the direct (non-job) routes below that call
    condense_recap/summarize_transcript/summarize_session_from_facts
    straight from a request instead of going through a background job."""
    return model or _ai_module.get_defaults().get("recap", "")


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
    # "kind" defaults to "entity" — a pre-existing row saved before
    # PlayerCharacter support was added never had this key at all (see
    # _featured_entity_candidates' own docstring).
    npc_entity_ids = [n["entity_id"] for n in npcs if n.get("kind", "entity") == "entity"]
    npc_pc_ids = [n["entity_id"] for n in npcs if n.get("kind") == "player_character"]
    entity_map = {e.id: e for e in db.query(Entity).filter(Entity.id.in_(npc_entity_ids)).all()} if npc_entity_ids else {}
    npc_pc_map = {p.id: p for p in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(npc_pc_ids)).all()} if npc_pc_ids else {}
    npc_names = []
    npc_selected = []
    for n in npcs:
        if n.get("kind") == "player_character":
            pc = npc_pc_map.get(n["entity_id"])
            if pc:
                npc_names.append(pc.name)
                npc_selected.append(f"{_PC_ID_PREFIX}{n['entity_id']}")
        else:
            e = entity_map.get(n["entity_id"])
            if e:
                npc_names.append(e.name)
                npc_selected.append(n["entity_id"])
    party_pc_ids = json.loads(gs.party.member_pc_ids_json or "[]") if gs.party else []
    party_pcs = db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(party_pc_ids)).all() if party_pc_ids else []
    return templates.TemplateResponse("sessions/detail.html", {
        "request": request, "world": world, "worlds": worlds, "gsession": gs,
        "parties": parties, "next_num": gs.session_num, "linked_combats": linked_combats,
        "npc_names": npc_names, "party_pcs": party_pcs,
        "npc_candidates_json": _featured_entity_candidates(db, gs.world_id),
        "npc_selected_json": npc_selected,
        "prep": json.loads(gs.prep_json or "[]"), "loot": json.loads(gs.loot_json or "[]"),
        "npcs": npcs,
    })


# Groups for the "Entities Featured" picker (still keyed name "npc_entity_
# ids"/npcs_json on the wire and in the DB — see _featured_entity_candidates'
# own docstring for why this wasn't renamed). Each group gets its own
# top-level branch in the picker's folder tree by prefixing a synthetic
# "{label}/{entity's own folder}" — reuses ndEntityPicker's existing
# folder-grouping verbatim rather than teaching the shared JS component a
# second, kind-based grouping concept.
_FEATURED_ENTITY_GROUPS = [
    # (kinds, extra SQLAlchemy filter or None, group label)
    (("character",), lambda q: q.filter(or_(Entity.subtype != "PC", Entity.subtype.is_(None))), "👤 NPCs"),
    (("character",), lambda q: q.filter(Entity.subtype == "PC"), "🧑 Player Characters"),
    (("creature",), None, "☠ Creatures"),
    (("location",), None, "🗺 Locations"),
    (("organization",), None, "🏢 Organizations"),
    (("race",), None, "🧬 Races"),
    (("profession",), None, "🎭 Professions"),
    (("note",), None, "📄 Notes"),
]


_PC_ID_PREFIX = "pc:"


def _featured_entity_candidates(db: Session, world_id: int) -> list[dict]:
    """Candidate entities for a session's "Entities Featured" picker —
    originally "NPCs Featured" (character/creature entities only, mirroring
    parties.py's companion picker); broadened to also cover player-character
    lore write-ups (character entities tagged subtype="PC" — subtype is a
    suggestion, not enforced, see deps.effective_subtypes), locations,
    organizations, races, professions, and notes — so a GM can tag
    everything actually featured in a session, not just who showed up.

    Real PlayerCharacter sheets (the party's actual mechanical characters —
    most GMs never write a separate Entity lore write-up for these, so the
    "Player Characters" group was otherwise near-always empty) are ALSO
    included in this same group. PlayerCharacter has its own independent id
    sequence, unrelated to Entity's — its candidates get a "pc:" prefix on
    `id` (e.g. "pc:5") to keep the two id spaces from colliding when both
    end up in the same npc_entity_ids form field. Everything else about a
    PlayerCharacter candidate (folder grouping, its row in the picker) is
    otherwise identical to any Entity candidate; entity-picker.js needs no
    changes for this — `id` was always just an opaque value it echoes back
    into a checkbox's `value`, never assumed to be numeric.

    Still saved into the SAME gs.npcs_json field / npc_entity_ids form
    field as before, now with a "kind" tag per pick ("entity" or
    "player_character" — see session_edit/session_detail) so a saved
    PlayerCharacter pick can be told apart from an Entity one on load; a
    pre-existing row with no "kind" key defaults to "entity" — no schema
    change, no migration, and every existing session's saved NPC-only
    picks keep working unchanged.

    What a GM checks here also becomes the pinned/guaranteed set for RAG
    (see app.audio_jobs._session_featured_picks/_build_rag_context's
    `pinned_entity_ids`/`pinned_pc_ids`) — deterministic, unlike keyword
    search, which matters most for exactly the case that prompted this: a
    session transcribed in a different language than the World's entity
    names has no literal text for keyword search to match at all."""
    candidates = []
    for kinds, extra_filter, label in _FEATURED_ENTITY_GROUPS:
        q = db.query(Entity).filter(Entity.world_id == world_id, Entity.kind.in_(kinds))
        if extra_filter:
            q = extra_filter(q)
        for e in q.order_by(Entity.folder, Entity.name).all():
            folder = f"{label}/{e.folder}" if e.folder else label
            candidates.append({"id": e.id, "name": e.name, "folder": folder})
    pc_label = "🧑 Player Characters"
    for pc in db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).order_by(PlayerCharacter.name).all():
        candidates.append({"id": f"{_PC_ID_PREFIX}{pc.id}", "name": pc.name, "folder": pc_label})
    return candidates


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
    # "pc:N" values (see _featured_entity_candidates/_PC_ID_PREFIX) are a
    # PlayerCharacter id, not an Entity id — the two id sequences are
    # independent, so they're kept apart here rather than risking an
    # unrelated Entity/PlayerCharacter pair that happen to share a numeric
    # id getting conflated.
    raw_ids = form.getlist("npc_entity_ids")
    entity_ids = [int(v) for v in raw_ids if not v.startswith(_PC_ID_PREFIX)]
    pc_ids = [int(v[len(_PC_ID_PREFIX):]) for v in raw_ids if v.startswith(_PC_ID_PREFIX)]
    entity_names = {e.id: e.name for e in db.query(Entity).filter(Entity.id.in_(entity_ids)).all()} if entity_ids else {}
    pc_names = {p.id: p.name for p in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all()} if pc_ids else {}
    gs.npcs_json = json.dumps(
        [{"entity_id": i, "name": entity_names.get(i, ""), "kind": "entity"} for i in entity_ids]
        + [{"entity_id": i, "name": pc_names.get(i, ""), "kind": "player_character"} for i in pc_ids]
    )
    db.commit()
    return RedirectResponse(f"/sessions/{session_id}?saved=1", status_code=303)


@router.post("/sessions/{session_id}/delete")
def session_delete(session_id: int, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    db.query(CombatSession).filter(CombatSession.game_session_id == session_id).update({"game_session_id": None})
    # The opt-in raw-recording archive (uploads/live/<session_id>/, written
    # by api_live_transcript_append) is keyed by this session id and
    # referenced by nothing else once the row is gone — delete it with the
    # row, the same way the row's other on-disk dependents are cleaned up.
    shutil.rmtree(_live_audio_root(session_id), ignore_errors=True)
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


def _condense_token_bounds(body: dict) -> tuple[Optional[int], Optional[int]]:
    """Parse+validate Condense's optional min_tokens/max_tokens settings —
    a blank/missing value means "no target", not 0 (which would ask
    condense_recap for an empty response). Raises HTTPException(400) with
    a caller-displayable message on anything that isn't a positive int, or
    a min bigger than the max, so a bad request fails fast rather than
    quietly producing a confusing prompt."""
    bounds = {}
    for key, label in (("min_tokens", "Min tokens"), ("max_tokens", "Max tokens")):
        raw = body.get(key)
        if raw in (None, ""):
            bounds[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{label} must be a whole number")
        if value < 1:
            raise HTTPException(400, f"{label} must be at least 1")
        bounds[key] = value
    if bounds["min_tokens"] and bounds["max_tokens"] and bounds["min_tokens"] > bounds["max_tokens"]:
        raise HTTPException(400, "Min tokens can't be greater than max tokens")
    return bounds["min_tokens"], bounds["max_tokens"]


def _condense_strictness(body: dict) -> str:
    """Parse+validate Condense's optional strictness setting — same
    "blank means default" convention _condense_token_bounds uses. Missing/
    blank reads as "guideline" (today's best-effort wording); anything
    else that isn't one of the three known values raises HTTPException(400)
    so a typo'd request fails fast instead of silently downgrading to the
    soft default the GM didn't ask for."""
    raw = body.get("strictness")
    if raw in (None, ""):
        return "guideline"
    strictness = str(raw).strip()
    if strictness not in ("guideline", "firm", "strict"):
        raise HTTPException(400, "strictness must be guideline, firm, or strict")
    return strictness


def _rag_options_from_body(body: dict) -> tuple[bool, Optional[int], Optional[int]]:
    """Parse+validate Condense/Summarize's optional RAG opt-in from a JSON
    request body — same "blank means unset" convention _condense_token_bounds
    uses, except here 0 is itself a valid, meaningful value (see
    AudioJob.use_rag's own docstring: 0 means "retrieve none of that
    category", distinct from unset/"use the module's own default")."""
    use_rag = bool(body.get("use_rag"))
    limits = {}
    for key, label in (("rag_entity_limit", "Entity limit"), ("rag_notes_limit", "Notes limit")):
        raw = body.get(key)
        if raw in (None, ""):
            limits[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{label} must be a whole number")
        if value < 0:
            raise HTTPException(400, f"{label} must be 0 or greater")
        limits[key] = value
    return use_rag, limits["rag_entity_limit"], limits["rag_notes_limit"]


@router.post("/api/sessions/ai/expand-notes")
async def api_expand_recap_notes(
    request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Expand terse GM notes (whatever's currently in the Summary textarea)
    into a polished narrative recap. Session-independent — works on the New
    Session form too, before anything has been saved.

    Resolves the "recap" model surface and the world's standing
    recap_instructions the same way every sibling route below does (e.g.
    api_condense_recap) — previously this was the one recap-family member
    that used neither: it always ran the single-instance-wide default
    model and ignored a GM's "always write in Spanish"-style standing
    instruction."""
    world, _ = get_world_ctx(request, db, active_world)
    body = await request.json()
    notes = str(body.get("notes", "")).strip()
    if not notes:
        raise HTTPException(400, "No notes provided")
    recap = await _ai_module.expand_recap_notes(
        notes, model=_recap_model(""), think=_think_from_body(body),
        extra_instructions=_recap_instructions_for_world(world),
    )
    return {"recap": recap}


@router.post("/api/sessions/ai/condense-recap")
async def api_condense_recap(request: Request):
    """Blocking condense — kept for API compatibility only. Every UI caller
    switched to the job-based api_condense_job_create below once Condense
    became a background job; this route has no RAG option and shouldn't
    gain one — extend the job route instead if condense ever needs it."""
    body = await request.json()
    recap = str(body.get("recap", "")).strip()
    if not recap:
        raise HTTPException(400, "No recap provided")
    min_tokens, max_tokens = _condense_token_bounds(body)
    strictness = _condense_strictness(body)
    model = _recap_model(str(body.get("model", "")).strip())
    extra_instructions = str(body.get("extra_instructions", "")).strip()
    _reject_if_too_long_to_condense(recap, extra_instructions)
    # See app.ai.condense_call_options' own docstring: sizes num_ctx to fit
    # recap + extra_instructions + the requested output length whenever
    # fit_context was explicitly asked for, OR whenever the plain (non-fit)
    # call would otherwise risk silently overflowing the GM's configured/
    # assumed context — the failure mode for the latter isn't a clean
    # error, it's the model responding with garbage.
    think = _think_from_body(body)
    options = _ai_module.condense_call_options(
        recap, extra_instructions=extra_instructions, max_tokens=max_tokens,
        think=think, force_fit=bool(body.get("fit_context")),
    )
    recap_result = await _ai_module.condense_recap(
        recap, model=model, options=options, think=think,
        extra_instructions=extra_instructions, min_tokens=min_tokens, max_tokens=max_tokens,
        strictness=strictness,
    )
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
    min_tokens, max_tokens = _condense_token_bounds(body)
    strictness = _condense_strictness(body)
    extra_instructions = str(body.get("extra_instructions", "")).strip()
    _reject_if_too_long_to_condense(recap, extra_instructions)
    use_rag, rag_entity_limit, rag_notes_limit = _rag_options_from_body(body)
    game_session_id = body.get("game_session_id")
    gs_id = int(game_session_id) if game_session_id else None
    job_id = _audio_jobs.create_condense_job(
        world_id=world.id, text=recap, model=str(body.get("model", "")).strip(),
        think=_think_from_body(body), fit_context=bool(body.get("fit_context")),
        extra_instructions=extra_instructions,
        min_tokens=min_tokens, max_tokens=max_tokens,
        strictness=strictness,
        use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
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
        transcript = await _transcribe_chunk(file, max_bytes=MAX_SESSION_AUDIO_BYTES, glossary=_glossary_for_world(world), language=_language_for_world(world), denoise=_denoise_for_world(world))
    except _ai_module.WhisperError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not transcript:
        raise HTTPException(
            400,
            "Whisper transcribed this clip successfully but found no speech in it — "
            "check the recording actually captured audio.",
        )
    instructions = _combine_recap_instructions(_recap_instructions_for_world(world), extra_instructions)
    recap = await _ai_module.summarize_transcript(transcript, model=_recap_model(""), extra_instructions=instructions, think=think)
    # Server-side detection (not client string-matching, see is_failure_
    # sentinel) so the client knows to offer "Retry summary from this
    # transcript" (.../summarize-transcript, keeping the transcript already
    # in hand) instead of treating the sentinel text as an applyable draft.
    return {"transcript": transcript, "recap": recap, "recap_failed": _ai_module.is_failure_sentinel(recap)}


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
            transcript = await _ai_module.transcribe_audio(tmp_path, glossary=_glossary_for_world(world), language=_language_for_world(world), denoise=_denoise_for_world(world))
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
    recap = await _ai_module.summarize_transcript(transcript, model=_recap_model(""), extra_instructions=instructions, think=think)
    return {"transcript": transcript, "recap": recap, "recap_failed": _ai_module.is_failure_sentinel(recap)}


@router.post("/api/sessions/ai/summarize-transcript")
async def api_summarize_transcript_only(
    request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Re-run just the summarize step over an ALREADY-transcribed transcript
    — the retry path for when .../summarize-from-audio(/complete) transcribed
    fine but the summarize call itself failed (see is_failure_sentinel).
    Without this, the only way to retry was re-uploading and re-transcribing
    the whole recording from scratch, redoing potentially hours of Whisper
    compute to redo a step that already succeeded. Session-independent, same
    as summarize-from-audio itself (world comes from the active_world
    cookie, not a session id) — nothing here needs a GameSession row to
    exist. GM-only by architecture (no _is_player_safe entry), matching
    every other AI-assist route on this page."""
    world, _ = get_world_ctx(request, db, active_world)
    body = await request.json()
    transcript = str(body.get("transcript", "")).strip()
    if not transcript:
        raise HTTPException(400, "No transcript provided")
    extra_instructions = str(body.get("extra_instructions", "")).strip()
    instructions = _combine_recap_instructions(_recap_instructions_for_world(world), extra_instructions)
    recap = await _ai_module.summarize_transcript(
        transcript, model=_recap_model(""), extra_instructions=instructions, think=_think_from_body(body),
    )
    return {"transcript": transcript, "recap": recap, "recap_failed": _ai_module.is_failure_sentinel(recap)}


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
    use_rag: bool = Form(False), rag_entity_limit: str = Form(""), rag_notes_limit: str = Form(""),
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
        use_rag=use_rag,
        rag_entity_limit=int(rag_entity_limit) if rag_entity_limit.strip().isdigit() else None,
        rag_notes_limit=int(rag_notes_limit) if rag_notes_limit.strip().isdigit() else None,
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
    use_rag: bool = Form(False), rag_entity_limit: str = Form(""), rag_notes_limit: str = Form(""),
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
        use_rag=use_rag,
        rag_entity_limit=int(rag_entity_limit) if rag_entity_limit.strip().isdigit() else None,
        rag_notes_limit=int(rag_notes_limit) if rag_notes_limit.strip().isdigit() else None,
    )
    return {"job_id": job_id}


def _audio_clip_disk_path(clip: AudioClip) -> Optional[Path]:
    """Resolve an AudioClip's stored /uploads/... URL back to a file on
    disk — same URL-to-path convention app.routers.audio's own
    _delete_clip_file and app.routers.ai's _attachment_disk_path use
    (refusing anything that isn't under the uploads root, or a crafted
    "../" escape). Returns None for a missing/invalid/nonexistent file."""
    if not clip.file_url or not clip.file_url.startswith("/uploads/"):
        return None
    root = (Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads").resolve()
    try:
        path = (root / clip.file_url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


@router.post("/api/sessions/ai/audio-jobs/from-clip")
async def api_audio_job_create_from_clip(
    request: Request, clip_id: int = Form(...), game_session_id: str = Form(""),
    model: str = Form(""), extra_instructions: str = Form(""), think: bool = Form(True),
    use_rag: bool = Form(False), rag_entity_limit: str = Form(""), rag_notes_limit: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Same background transcribe+summarize job as api_audio_job_create, but
    sourced from a recording already saved in this world's Audio Library
    instead of a fresh upload — e.g. a session recorded on a phone and
    uploaded to the Library first, or a recording worth keeping AND
    transcribing. Reads the clip's file in place (delete_after=False)
    rather than copying it into the jobs dir the way an upload does — that
    copy exists only to give a job a file that outlives the request that
    created it; a Library clip's file already lives independently of any
    one job (same reasoning app.routers.ai's own attachment jobs use for
    their delete_after=False — "the file IS the attachment" there, "the
    file IS the Library's own copy" here)."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = db.query(AudioClip).filter(AudioClip.id == clip_id, AudioClip.world_id == world.id).first()
    if not clip:
        raise HTTPException(404)
    path = _audio_clip_disk_path(clip)
    if not path:
        raise HTTPException(404, "This clip's audio file is missing on disk")
    gs_id = int(game_session_id) if game_session_id.strip().isdigit() else None
    job_id = _audio_jobs.create_job(
        world_id=world.id, purpose="session_recap", filename=clip.name or path.name,
        audio_path=path, delete_after=False, game_session_id=gs_id,
        created_by_user_id=_current_user_id(request), model=model.strip(),
        extra_instructions=extra_instructions.strip(), think=think,
        use_rag=use_rag,
        rag_entity_limit=int(rag_entity_limit) if rag_entity_limit.strip().isdigit() else None,
        rag_notes_limit=int(rag_notes_limit) if rag_notes_limit.strip().isdigit() else None,
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
# MediaRecorder segment every chunk-length (GM-configurable, ~1–15 minutes)
# and uploads each one here in order as it finishes — this endpoint has no
# idea how long the overall recording has been running, it only ever sees
# one chunk at a time (see _transcribe_chunk above, shared with
# summarize-from-audio).

def _live_audio_root(session_id: int) -> Path:
    """Where a session's opt-in raw-recording archive lives: one directory
    per session, one subdirectory per recording (each recording start mints
    a fresh 32-hex recording_id, so two recordings never overwrite each
    other's segments), one zero-padded file per segment. Lives under the
    same <DB_PATH dir>/uploads root as every other stored upload."""
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "live" / str(session_id)


def _live_audio_files(gs: GameSession) -> list:
    """The saved raw segment paths (relative to the uploads dir, in recording
    order) for a session, from GameSession.live_audio_files_json — a JSON
    array maintained by api_live_transcript_append. Tolerant of NULL, blank,
    or corrupt JSON (old rows, a partially-written value): an unreadable
    list degrades to "nothing saved", never to a 500."""
    try:
        files = json.loads(gs.live_audio_files_json or "[]")
    except ValueError:
        return []
    return [str(p) for p in files] if isinstance(files, list) else []


def _ffmpeg_concat_quote(p: Path) -> str:
    """One `file '<path>'` line for ffmpeg's concat demuxer list file. The
    demuxer parses single-quoted strings shell-style, so an embedded quote
    must close the string, backslash-escape the quote, and reopen — the
    same '\'' idiom. Paths here are all server-generated (32-hex recording
    dirs, 6-digit index names), so this never fires in practice, but concat
    hard-fails on an unescaped one rather than skipping it."""
    return "file '" + str(p).replace("'", "'\\''") + "'"


async def _concat_live_segments(segs: list, out_path: Path) -> None:
    """Stitch a session's saved raw segments, in order, into one file at
    out_path — ffmpeg's concat demuxer with -c copy (no re-encode, so a
    multi-hour recording assembles in seconds). Writes the demuxer's list
    file and the output next to the segments, to a .part name replaced into
    place only on success so a failed run can never leave a half-written
    file cached as complete. Raises RuntimeError with the reason on any
    failure (ffmpeg missing, unparseable segment, crash) — the caller turns
    that into a 400, mirroring app/routers/video.py's best-effort ffmpeg
    error style, and its async-subprocess shape follows video.py's
    _convert_video (create_subprocess_exec, check returncode, non-empty
    output)."""
    import asyncio
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", dir=out_path.parent, delete=False, encoding="utf-8",
    ) as lst:
        lst.write("\n".join(_ffmpeg_concat_quote(p) for p in segs) + "\n")
        list_path = Path(lst.name)
    # ".part" goes in the MIDDLE ("recording.part.webm", not the shell-ish
    # "recording.webm.part") — ffmpeg picks its muxer from the output file's
    # extension, and an unknown ".part" tail makes muxer init fail outright.
    tmp_out = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(tmp_out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            # Tail, not head: ffmpeg's banner is the first 20 lines and the
            # actual error is always the last one.
            _log.warning("live-audio concat failed (rc=%s): %s", proc.returncode, stderr.decode(errors="replace")[-500:])
            raise RuntimeError(f"ffmpeg concat failed (rc={proc.returncode}): {stderr.decode(errors='replace')[-300:]}")
        tmp_out.replace(out_path)
    except FileNotFoundError as exc:
        _log.warning("live-audio concat errored: ffmpeg not available")
        raise RuntimeError("ffmpeg is not available on the server — cannot assemble the recording.") from exc
    finally:
        list_path.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


@router.post("/api/sessions/{session_id}/live-transcript/append")
async def api_live_transcript_append(
    session_id: int,
    file: UploadFile = File(...),
    # Opt-in raw-audio archive (the panel's "Save raw audio" checkbox): the
    # browser additionally tags each segment with a per-recording 32-hex id
    # and its zero-based position, so the server can keep the audio on disk
    # in recording order and later reassemble it. All three fields are
    # optional — an old client (or one with the checkbox off) sends none of
    # them and gets exactly the transcribe-and-discard behavior as before.
    save_audio: str = Form(""),
    recording_id: str = Form(""),
    segment_index: int = Form(-1),
    db: Session = Depends(get_db),
):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    world = db.get(World, gs.world_id)
    try:
        chunk_text = (await _transcribe_chunk(file, glossary=_glossary_for_world(world, gs.id), language=_language_for_world(world), denoise=_denoise_for_world(world))).strip()
    except _ai_module.WhisperError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Raw-audio save runs AFTER transcription, so a Whisper failure (the 400
    # above) leaves nothing half-saved, and the DB row below is committed
    # together with the transcript append as the plan requires. The upload
    # stream was consumed by _transcribe_chunk's bounded copy, hence the
    # seek(0) — an UploadFile is a spooled temp file, rewinding it is free.
    saved_rel = ""
    if save_audio:
        # Malformed archive fields are a hard 400 rather than a silent
        # fallback to not-saving: a client bug that quietly drops audio the
        # GM explicitly asked to keep is worse than a failed upload, which
        # the client's failed-chunk/retry UI surfaces. Same validation shape
        # as uploads.save_upload_chunk's "Invalid upload id"/"Invalid chunk
        # index" — recording_id must match uploads.CHUNK_ID_RE (32 hex, the
        # same generator ndChunkedUpload uses client-side), which also rules
        # out any path traversal since it admits nothing but hex chars.
        if not CHUNK_ID_RE.match(recording_id or ""):
            raise HTTPException(400, "Invalid recording id")
        if segment_index < 0:
            raise HTTPException(400, "Invalid segment index")
        live_root = _live_audio_root(session_id)
        seg_dir = live_root / recording_id
        seg_dir.mkdir(parents=True, exist_ok=True)
        # ext comes from the upload filename (MediaRecorder produces .webm in
        # every browser that ships the API today, hence the default) — already
        # validated against _SESSION_AUDIO_EXTS by _transcribe_chunk above.
        ext = Path(file.filename or "").suffix.lower() or ".webm"
        dest = seg_dir / f"{segment_index:06d}{ext}"
        file.file.seek(0)
        copy_upload_bounded(file, dest, max_bytes=MAX_LIVE_SAVED_SEGMENT_BYTES)
        saved_rel = dest.relative_to(live_root.parents[1]).as_posix()
        # A client retry of a segment whose response was lost overwrites the
        # same file — the JSON list must not grow a duplicate entry for it.
        files = _live_audio_files(gs)
        if saved_rel not in files:
            files.append(saved_rel)
            gs.live_audio_files_json = json.dumps(files)
    if chunk_text or saved_rel:
        if chunk_text:
            gs.live_transcript = (gs.live_transcript or "") + (" " if gs.live_transcript else "") + chunk_text
        db.commit()
    # chunk_text can legitimately be "" (a silent segment) — that's not an
    # error, just nothing to append; the client still needs the running
    # total either way to keep its live display in sync.
    return {"chunk_text": chunk_text, "transcript": gs.live_transcript}


@router.get("/api/sessions/{session_id}/live-audio")
def api_live_audio_list(session_id: int, db: Session = Depends(get_db)):
    """What raw audio the session's live recording has saved — feeds the
    recording panel's "Raw audio: N segment(s) (~X MB) — Download" line
    (sessions/detail.html), which fetches this on page load and when a
    recording stops. Paths are as stored (uploads-dir-relative, the same
    strings live_audio_files_json holds)."""
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    files = _live_audio_files(gs)
    uploads_dir = _live_audio_root(session_id).parents[1]
    total_bytes = 0
    for rel in files:
        try:
            total_bytes += (uploads_dir / rel).stat().st_size
        except OSError:
            pass  # row remembers a file the disk lost — it just counts as 0 here
    return {"files": files, "count": len(files), "total_bytes": total_bytes}


@router.get("/api/sessions/{session_id}/live-audio/download")
async def api_live_audio_download(session_id: int, db: Session = Depends(get_db)):
    """The whole raw recording as one downloadable file: every saved segment
    concatenated in recording order. Concatenation is -c copy via ffmpeg's
    concat demuxer (instant, no re-encode) and is cached next to the
    segments — rebuilt only when a segment is newer than the cached file
    (i.e. the segment set changed), since a concat of unchanged inputs is
    deterministic. One saved segment is served directly, no concat. ffmpeg
    missing or failing is a 400 with the reason, not a 500 — the raw
    segments stay on disk either way and the GM can retry."""
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    live_root = _live_audio_root(session_id)
    uploads_dir = live_root.parents[1]
    segs = [uploads_dir / rel for rel in _live_audio_files(gs)]
    segs = [p for p in segs if p.is_file()]
    if not segs:
        raise HTTPException(400, "No raw audio saved for this session.")
    ext = segs[0].suffix or ".webm"
    filename = f"session-{session_id}-recording{ext}"
    if len(segs) == 1:
        # Nothing to concatenate — serve the segment itself, byte-for-byte.
        return FileResponse(segs[0], headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    out_path = live_root / f"recording{ext}"
    newest = max(p.stat().st_mtime for p in segs)
    if not out_path.is_file() or out_path.stat().st_size == 0 or out_path.stat().st_mtime < newest:
        try:
            await _concat_live_segments(segs, out_path)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
    return FileResponse(out_path, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
        gs.live_transcript, model=_recap_model(""), extra_instructions=_recap_instructions_for_world(world),
        think=_think_from_body(body),
    )
    return {"transcript": gs.live_transcript, "recap": recap}


@router.post("/api/sessions/{session_id}/ai/summarize-live-transcript-job")
async def api_summarize_live_transcript_job(session_id: int, request: Request, db: Session = Depends(get_db)):
    """Same as the blocking .../summarize-live-transcript above but as a
    durable background job — see create_text_recap_job's own docstring for
    why: summarizing a multi-hour live transcript inline in one HTTP
    request is exactly the reverse-proxy-timeout trap background jobs
    exist to avoid, and the blocking route gets none of session_recap's
    checkpointing/RAG/Part-1-retry-ladder protection. Returns the job id
    immediately; the client picks the result up through the same
    sessionAudioJobs panel/poll loop every other session-scoped job already
    uses (purpose="session_recap" is already in _SESSION_JOB_PURPOSES). The
    blocking route stays as-is for a short live transcript, where a job's
    extra round-trip isn't worth it."""
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not gs:
        raise HTTPException(404)
    if not (gs.live_transcript or "").strip():
        raise HTTPException(400, "No live transcript recorded for this session yet.")
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    use_rag, rag_entity_limit, rag_notes_limit = _rag_options_from_body(body)
    job_id = _audio_jobs.create_text_recap_job(
        world_id=gs.world_id, text=gs.live_transcript, model=str(body.get("model", "")).strip(),
        think=_think_from_body(body), extra_instructions=str(body.get("extra_instructions", "")).strip(),
        game_session_id=session_id, created_by_user_id=_current_user_id(request),
        use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
    )
    return {"job_id": job_id}


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
        [f.content for f in facts], model=_recap_model(""), extra_instructions=_recap_instructions_for_world(world),
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


# 30 minutes, not a short poll-interval TTL like most caches in this
# codebase — this page's only real content input (Fact rows) already has
# EXACT invalidation via clear_session_log_recap_cache (routers.facts on
# every create/edit/delete, and routers.ai's recap-instructions save for
# the other input this recap bakes in), so a long TTL costs nothing in
# practice: a browsed session-log page was re-running a full
# summarize_session_from_facts call (think=True by default — paying
# thinking tokens too) on every visit within the old 20s window, for
# identical input almost all of the time. Residual staleness is bounded to
# switching the "recap" model surface default mid-window, which is rare
# and self-corrects on the next Fact edit or TTL expiry either way.
_SESSION_LOG_RECAP_CACHE_TTL = 1800.0
# Keyed by (session_id, is_gm) — Fact visibility for this route is purely
# GM/non-GM (no per-player entity_player_access-style individual sharing),
# so every non-GM caller for a given session legitimately gets the same
# answer and can safely share one cache entry. Cleared wholesale by
# routers.facts whenever a Fact is created/edited/deleted (see
# clear_session_log_recap_cache) — same "just clear it" pattern
# app.main._spotlight_cache already uses, rather than tracking exactly
# which session_id(s) a fact_edit reassignment touched.
_session_log_recap_cache: dict[tuple, tuple[float, dict]] = {}


def clear_session_log_recap_cache() -> None:
    _session_log_recap_cache.clear()


@router.post("/api/session-log/{session_id}/recap")
async def api_session_log_recap(session_id: int, request: Request, db: Session = Depends(get_db)):
    gs = db.query(GameSession).filter(GameSession.id == session_id).first()
    world = db.get(World, gs.world_id) if gs else None
    user = getattr(request.state, "user", None)
    if not gs or not auth.user_can_access_world(db, user, world):
        raise HTTPException(404)
    is_gm = bool(user and user.is_gm)
    # Cache check comes BEFORE the cooldown gate: serving a still-fresh
    # cached recap costs nothing (no Ollama call happens), so it shouldn't
    # consume/trigger the same rate limit that exists to stop a player
    # spamming real generations — e.g. reloading the session-log page
    # repeatedly should just keep hitting cache, not 429.
    cache_key = (session_id, is_gm)
    cached = _session_log_recap_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _SESSION_LOG_RECAP_CACHE_TTL:
        return cached[1]
    if not is_gm:
        check_llm_cooldown(user.id if user else 0)
    q = db.query(Fact).filter(Fact.game_session_id == session_id)
    if not is_gm:
        q = q.filter(Fact.visible_to_players.isnot(False))
    facts = q.order_by(Fact.created_at).all()
    if not facts:
        result = {"recap": "", "empty": True}
        _session_log_recap_cache[cache_key] = (now, result)
        return result
    recap = await _ai_module.summarize_session_from_facts([f.content for f in facts], model=_recap_model(""), extra_instructions=_recap_instructions_for_world(world))
    result = {"recap": recap}
    _session_log_recap_cache[cache_key] = (now, result)
    return result
