import json

from datetime import datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from .. import ai_assist as _ai_assist
from .. import audio_jobs as _audio_jobs
from ..database import get_db
from ..deps import get_world_ctx
from ..models import AudioJob, Fact, GameSession
from ..templating import templates
from .sessions import _rag_options_from_body

router = APIRouter()


def _create_facts_from_items(db: Session, world_id: int, session_id, author_id, items: list) -> tuple:
    """Shared by api_facts_bulk (the manual parse-review Confirm & Save) and
    api_facts_from_job (the auto-drafted-on-job-completion review) — same
    per-item validation/creation/dedup either way, so the two confirm flows
    can't quietly drift apart on what counts as a valid draft fact.

    Duplicates are SKIPPED, not re-inserted: an item whose normalized
    content (app.ai._normalized_fact_key — the same case/punctuation-blind
    key the parser itself dedupes chunks by) matches an existing Fact
    anywhere in this WORLD (not just this session — a fact may legitimately
    live on no session or a different one, and "the same sentence is
    already logged somewhere in this world" is the duplicate condition a GM
    actually cares about), or an earlier item in this same payload, is
    dropped and counted separately rather than created.

    Returns (created_facts, skipped_duplicates)."""
    seen_keys = {
        _ai_module._normalized_fact_key(content)
        for (content,) in db.query(Fact.content).filter(Fact.world_id == world_id).all()
    }
    created = []
    skipped_duplicates = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        key = _ai_module._normalized_fact_key(content)
        if key in seen_keys:
            skipped_duplicates += 1
            continue
        seen_keys.add(key)
        f = Fact(
            world_id=world_id, game_session_id=session_id, content=content,
            visible_to_players=bool(item.get("visible_to_players", True)),
            author_id=author_id,
            # Belt-and-braces alongside the column default — see
            # fact_create's own comment for the freshness-rule reasoning.
            updated_at=datetime.utcnow(),
        )
        db.add(f)
        created.append(f)
    return created, skipped_duplicates


def _bump_recap_content_touch(world) -> None:
    """Advance a world's durable recap-staleness watermark (World.
    recap_content_touch) — called for the fact mutations that leave no Fact
    row behind to timestamp (a DELETE), plus any other write path that wants
    belt-and-braces invalidation. The session-log recap route compares job
    created_at against max(newest Fact timestamp, this watermark); see its
    own comment block in app/routers/sessions.py for why this replaced an
    in-process module global (restart safety, per-world precision, and
    MCP/web parity — every write path can reach this through the DB even
    from another process)."""
    if world is None:
        return
    world.recap_content_touch = datetime.utcnow()


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
        # Belt-and-braces alongside the column default: the session-log
        # recap's freshness rule reads max(coalesce(updated_at, created_at))
        # over the session's rows, so a fact INSERT invalidates old recaps
        # purely by existing with a fresh timestamp — setting updated_at
        # explicitly keeps that true even if some future insert path stops
        # going through the ORM default.
        updated_at=datetime.utcnow(),
    )
    db.add(f)
    db.commit()
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/edit")
async def fact_edit(fact_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    # World-scoped like every other fact route (the list only shows the
    # active world's facts, and /facts/new writes into it): without this
    # check a GM posting a bare fact id from another world could edit (or,
    # on delete below, remove) a fact they were looking at in a different
    # tab's world — 404 keeps the boundary the list page already implies.
    world, _ = get_world_ctx(request, db, active_world)
    fact = db.get(Fact, fact_id)
    if not fact or not world or fact.world_id != world.id:
        raise HTTPException(404)
    form = await request.form()
    content = str(form.get("content", "")).strip()
    if content:
        fact.content = content
    fact.visible_to_players = form.get("visible_to_players") is not None
    # The edit form carries only content + visibility — a missing
    # game_session_id must PRESERVE the existing association, not clear it
    # (nulling it here silently un-linked the fact from its session, which
    # the per-session recap staleness rule then read as "no facts"). The
    # field is honored when a caller genuinely sends it (bulk-style tools).
    session_id = form.get("game_session_id")
    if session_id:
        fact.game_session_id = int(session_id)
    # Explicit (not just the column's onupdate): the session-log recap
    # freshness rule compares updated_at against cached job rows, so a
    # visibility flip on an otherwise-identical edit must still move it —
    # a players-audience recap genuinely differs before/after one.
    fact.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/delete")
def fact_delete(
    fact_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    fact = db.get(Fact, fact_id)
    if not fact or not world or fact.world_id != world.id:
        raise HTTPException(404)
    db.delete(fact)
    # A deletion leaves NO Fact row behind to timestamp, so the per-fact
    # half of the recap freshness rule can't see it — without this bump,
    # deleting a session's NEWEST fact would make every older done recap
    # look fresh again and keep serving text the GM just removed (including
    # a fact they had just un-hidden from players). World-level rather than
    # session-level by necessity (nothing session-addressable remains), the
    # same scope the recap-instructions watermark already uses.
    _bump_recap_content_touch(world)
    db.commit()
    return RedirectResponse("/facts", status_code=303)


@router.post("/api/facts/parse")
async def api_facts_parse(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Turns a rough recap into draft facts via the local model — returns the
    draft list without writing anything to the DB. The GM reviews/edits in
    the UI, then POST /api/facts/bulk does the actual write once confirmed.

    Kept as-is (the Facts page itself uses the background variant below)
    because it's a documented API and the right tool for a SHORT recap, where
    a job's create/poll round-trips aren't worth it. True option parity with
    parse-job now: the same model/think/RAG body options, validated by the
    same helper (blank vs 0 RAG limits mean the same thing on both routes),
    and RAG lore is built with the same _build_rag_context call _run_job
    makes for a facts_parse job — previously this route silently ignored
    every option the job route honored."""
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "No recap text provided")
    # Same option reading/ validation as api_facts_parse_job below — one
    # shared shape, so a caller can switch a request between the sync and
    # job variants without changing anything else.
    model = str(body.get("model", "")).strip()
    think = bool(body.get("think", False))
    use_rag, rag_entity_limit, rag_notes_limit = _rag_options_from_body(body)
    world_context = ""
    if use_rag:
        # RAG lore for name accuracy, same construction the job runner uses
        # (see _run_job's shared RAG block in app/audio_jobs.py): the text
        # being parsed is the relevance query, and blank limits fall back to
        # the module's own defaults. World-scoped like parse-job below (that
        # route needs the world for its job row; this one only needs it for
        # the lore query, and refuses the same way when there isn't one).
        world, _ = get_world_ctx(request, db, active_world)
        if not world:
            raise HTTPException(400, "No active world")
        world_context = _audio_jobs._build_rag_context(
            world.id,
            text,
            rag_entity_limit if rag_entity_limit is not None else _audio_jobs._DEFAULT_RAG_ENTITY_LIMIT,
            rag_notes_limit if rag_notes_limit is not None else _audio_jobs._DEFAULT_RAG_NOTES_LIMIT,
        )
    try:
        facts = await _ai_module.parse_facts_from_recap(
            text, model=model, think=think, world_context=world_context,
        )
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    return {"facts": facts}


@router.post("/api/facts/folk-tale")
async def api_facts_folk_tale(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Turns this world's logged Facts into an in-world folk tale/legend/song
    via app.ai_assist's folk_tale op (see app/routers/sessions.py's sibling
    button, which works from a session's Summary field instead — this one is
    for when the GM has logged Facts but never wrote a prose recap).
    game_session_id, if given, scopes to just that session's facts; omitted
    or blank uses every Fact in the world, oldest first, same ordering the
    session-log recap pipeline reads facts in. Synchronous like
    /api/facts/parse above — a joined Facts list is short GM-editorial
    content, not a long transcript, so a job's create/poll round-trip isn't
    worth it here either."""
    body = await request.json()
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    game_session_id = body.get("game_session_id")
    q = db.query(Fact).filter(Fact.world_id == world.id)
    if game_session_id:
        q = q.filter(Fact.game_session_id == int(game_session_id))
    facts = q.order_by(Fact.created_at).all()
    if not facts:
        raise HTTPException(400, "No facts logged yet" + (" for that session" if game_session_id else "") + ".")
    content = "\n".join(f"- {f.content}" for f in facts)
    instruction = str(body.get("instruction", "")).strip()
    model = str(body.get("model", "")).strip()
    think = bool(body.get("think", False))
    use_rag, rag_entity_limit, rag_notes_limit = _rag_options_from_body(body)
    world_context = ""
    if use_rag:
        world_context = _audio_jobs._build_rag_context(
            world.id, content,
            rag_entity_limit if rag_entity_limit is not None else _audio_jobs._DEFAULT_RAG_ENTITY_LIMIT,
            rag_notes_limit if rag_notes_limit is not None else _audio_jobs._DEFAULT_RAG_NOTES_LIMIT,
        )
    try:
        result = await _ai_assist.run_assist(
            "folk_tale", content=content, instruction=instruction,
            model=model, think=think, world_context=world_context,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if _ai_module.is_failure_sentinel(result.get("text", "")):
        raise HTTPException(502, result["text"])
    return result


@router.post("/api/facts/parse-job")
async def api_facts_parse_job(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Same parse as /api/facts/parse above but as a durable background job —
    see create_facts_parse_job's docstring for why: a long recap against a
    CPU-local model used to make the synchronous route a routine way to trip
    Cloudflare Tunnel's ~100s timeout (HTTP 524), losing the whole request.
    Returns the job id immediately; the client polls GET /api/audio-jobs/{id}
    (job.transcript holds the input text, job.result_json the finished
    {content, visible_to_players} draft array). The world scoping matters
    here, unlike the sync route: the job row is world-addressable so "Restore
    last parse" (GET /api/facts/last-parse) can find it again after a reload."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "No recap text provided")
    session_id = body.get("game_session_id")
    user = getattr(request.state, "user", None)
    # Model/Thinking/RAG: the Facts page's own pickers beside the parse
    # button — think defaults to False (a parse needs clean JSON back; the
    # checkbox starts unchecked), RAG options parsed/validated by the same
    # helper the Sessions routes use so 0-vs-unset limits mean the same
    # thing on both pages.
    use_rag, rag_entity_limit, rag_notes_limit = _rag_options_from_body(body)
    job_id = _audio_jobs.create_facts_parse_job(
        world_id=world.id, text=text,
        game_session_id=int(session_id) if session_id else None,
        model=str(body.get("model", "")).strip(),
        created_by_user_id=user.id if user else None,
        think=bool(body.get("think", False)),
        use_rag=use_rag, rag_entity_limit=rag_entity_limit, rag_notes_limit=rag_notes_limit,
    )
    return {"job_id": job_id}


@router.get("/api/facts/last-parse")
def api_facts_last_parse(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The latest finished, NOT-YET-CONSUMED facts-parse job's draft for the
    active world — the Facts page's "Restore last parse" button. The parsed
    draft used to live only in the browser from parse time until confirm, so
    a reload (or arriving from the Background Jobs page's "📋 Extract facts"
    hand-off that auto-ran a parse and then got closed) silently threw the
    work away; now it's persisted on the job row (result_json) and can
    always be fetched back. draft_consumed rows (the draft was already
    reviewed and saved via /api/facts/bulk, which flags the job it came
    from) are skipped — re-restoring a consumed draft and saving it again
    would silently duplicate every fact in it. 404 when there's no eligible
    done facts_parse job yet — the button is simply not useful before the
    first parse, and the client hides the resulting message rather than
    surfacing it as an error. game_session_id rides along so the page can
    preselect the session the draft was parsed for."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    job = (
        db.query(AudioJob)
        .filter(
            AudioJob.world_id == world.id, AudioJob.purpose == "facts_parse", AudioJob.status == "done",
            # isnot(True), not a bare == False: the column is NULL on every
            # row that predates it (healed installs), and NULL must read as
            # "not consumed" — exactly the behavior those rows always had.
            AudioJob.draft_consumed.isnot(True),
        )
        .order_by(AudioJob.created_at.desc())
        .first()
    )
    if not job:
        raise HTTPException(404)
    try:
        facts = json.loads(job.result_json or "[]")
    except ValueError:
        facts = []  # a corrupt blob degrades to an empty draft, never a 500
    return {
        "job_id": job.id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "game_session_id": job.game_session_id,
        "facts": facts,
    }


@router.post("/api/facts/bulk")
async def api_facts_bulk(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Bulk-insert facts — the recap-review UI's Confirm & Save action.

    Duplicate facts are SKIPPED, not re-inserted: an item whose normalized
    content (app.ai._normalized_fact_key — the same case/punctuation-blind
    key the parser itself dedupes chunks by) matches an existing Fact in
    this world, or an earlier item in the same payload, is dropped and
    counted in skipped_duplicates. "Restore last parse" plus a re-parse of
    the same recap used to be a silent double-write of every fact; the
    response now tells the GM exactly what happened ({created, skipped_
    duplicates}).

    `job_id` (optional) is the facts_parse job the draft came from — when
    present (and actually a facts_parse job of this world) the job row is
    flagged draft_consumed so GET /api/facts/last-parse stops offering the
    just-saved draft for a second round."""
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
    created, skipped_duplicates = _create_facts_from_items(db, world.id, session_id, user.id if user else None, items)
    db.commit()
    job_id = body.get("job_id")
    if job_id:
        # Best-effort consume flag: a stale/bogus job_id is ignored (the
        # facts themselves were saved above regardless), but a job_id from
        # ANOTHER world's parse is not — flagging it would hide that
        # world's restorable draft behind this world's save.
        job = db.get(AudioJob, int(job_id)) if str(job_id).isdigit() else None
        if job and job.purpose == "facts_parse" and job.world_id == world.id:
            job.draft_consumed = True
            db.commit()
    return {"created": len(created), "skipped_duplicates": skipped_duplicates}


@router.post("/api/facts/from-job/{job_id}")
async def api_facts_from_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    """Confirm (or dismiss) the Facts a session_recap job auto-drafted on
    completion (AudioJob.pending_facts_json — see app.audio_jobs.
    _auto_extract_pending_facts) — the review step on the Background Jobs
    page. `facts` may be an edited/trimmed subset of what was drafted (or
    an empty list to dismiss without saving any of them); either way the
    job's pending draft is cleared once handled, so it doesn't keep
    reappearing on every page load. Uses the job's own game_session_id
    automatically (the draft came from that session's transcript) rather
    than making the GM re-pick a session, unlike the free-standing /facts
    parse-review flow this shares its creation/dedup logic with."""
    job = db.get(AudioJob, job_id)
    if not job:
        raise HTTPException(404)
    body = await request.json()
    items = body.get("facts")
    if not isinstance(items, list):
        raise HTTPException(400, '"facts" must be a list (possibly empty, to dismiss)')
    user = getattr(request.state, "user", None)
    created, skipped_duplicates = _create_facts_from_items(
        db, job.world_id, job.game_session_id, user.id if user else None, items,
    )
    job.pending_facts_json = "[]"
    db.commit()
    return {"created": len(created), "skipped_duplicates": skipped_duplicates}
