import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from .. import audio_jobs as _audio_jobs
from ..database import get_db
from ..deps import get_world_ctx
from ..models import AudioJob, Fact, GameSession
from ..templating import templates
from .sessions import _rag_options_from_body, clear_session_log_recap_cache

router = APIRouter()


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
    )
    db.add(f)
    db.commit()
    clear_session_log_recap_cache()
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/edit")
async def fact_edit(fact_id: int, request: Request, db: Session = Depends(get_db)):
    fact = db.get(Fact, fact_id)
    if not fact:
        raise HTTPException(404)
    form = await request.form()
    content = str(form.get("content", "")).strip()
    if content:
        fact.content = content
    fact.visible_to_players = form.get("visible_to_players") is not None
    session_id = form.get("game_session_id")
    fact.game_session_id = int(session_id) if session_id else None
    db.commit()
    clear_session_log_recap_cache()
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/delete")
def fact_delete(fact_id: int, db: Session = Depends(get_db)):
    fact = db.get(Fact, fact_id)
    if not fact:
        raise HTTPException(404)
    db.delete(fact)
    db.commit()
    clear_session_log_recap_cache()
    return RedirectResponse("/facts", status_code=303)


@router.post("/api/facts/parse")
async def api_facts_parse(request: Request):
    """Turns a rough recap into draft facts via the local model — returns the
    draft list without writing anything to the DB. The GM reviews/edits in
    the UI, then POST /api/facts/bulk does the actual write once confirmed.

    Kept as-is (the Facts page itself now uses the background variant below)
    because it's a documented API and the right tool for a SHORT recap, where
    a job's create/poll round-trips aren't worth it."""
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "No recap text provided")
    try:
        facts = await _ai_module.parse_facts_from_recap(text)
    except ValueError as exc:
        raise HTTPException(502, str(exc))
    return {"facts": facts}


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
    """The latest finished facts-parse job's draft for the active world —
    the Facts page's "Restore last parse" button. The parsed draft used to
    live only in the browser from parse time until confirm, so a reload (or
    arriving from the Background Jobs page's "📋 Extract facts" hand-off that
    auto-ran a parse and then got closed) silently threw the work away; now
    it's persisted on the job row (result_json) and can always be fetched
    back. 404 when there's no done facts_parse job yet — the button is simply
    not useful before the first parse, and the client hides the resulting
    message rather than surfacing it as an error."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    job = (
        db.query(AudioJob)
        .filter(AudioJob.world_id == world.id, AudioJob.purpose == "facts_parse", AudioJob.status == "done")
        .order_by(AudioJob.created_at.desc())
        .first()
    )
    if not job:
        raise HTTPException(404)
    try:
        facts = json.loads(job.result_json or "[]")
    except ValueError:
        facts = []  # a corrupt blob degrades to an empty draft, never a 500
    return {"job_id": job.id, "created_at": job.created_at.isoformat() if job.created_at else None, "facts": facts}


@router.post("/api/facts/bulk")
async def api_facts_bulk(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Bulk-insert facts — the recap-review UI's Confirm & Save action."""
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
    created = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        f = Fact(
            world_id=world.id,
            game_session_id=session_id,
            content=content,
            visible_to_players=bool(item.get("visible_to_players", True)),
            author_id=user.id if user else None,
        )
        db.add(f)
        created.append(f)
    db.commit()
    clear_session_log_recap_cache()
    return {"created": len(created)}
