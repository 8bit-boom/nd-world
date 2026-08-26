"""Unified view over every durable background audio job (AudioJob, see
app/audio_jobs.py) regardless of which surface started it — the Session
recap flow and the AI attachment/Whisper Test flow each have their own
purpose-scoped create/chunk/complete/list routes (app/routers/sessions.py,
app/routers/ai.py) since the upload mechanics and per-purpose result shape
differ, but status/cancel is identical for every job once it exists, so
that part lives here once instead of being duplicated a third time. Powers
the standalone "Background Jobs" page (GM-only) where a GM can see
everything in flight across the whole world and cancel one, separate from
the smaller inline panels embedded on each originating page.
"""
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import audio_jobs as _audio_jobs
from ..database import get_db
from ..deps import get_world_ctx, paginate
from ..models import AudioJob
from ..templating import templates

router = APIRouter()

PURPOSE_LABELS = {"session_recap": "Session Recap", "attachment": "Voice Attachment"}


def _require_gm(request: Request) -> None:
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)


def _job_to_dict(job: AudioJob) -> dict:
    return {
        "id": job.id, "purpose": job.purpose,
        "purpose_label": PURPOSE_LABELS.get(job.purpose, job.purpose),
        "filename": job.filename, "status": job.status, "error": job.error,
        "transcript": job.transcript, "recap": job.recap, "model": job.model or "",
        "extra_instructions": job.extra_instructions or "",
        "attachment_url": job.attachment_url, "game_session_id": job.game_session_id,
        "chunk_current": job.chunk_current, "chunk_total": job.chunk_total,
        "run_started_at": job.run_started_at.isoformat() if job.run_started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.get("/background-jobs", response_class=HTMLResponse)
def background_jobs_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    return templates.TemplateResponse("background_jobs.html", {
        "request": request, "world": world, "worlds": worlds,
    })


@router.get("/api/audio-jobs")
def api_audio_job_list(
    request: Request, page: int = 1, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Every job for the active world, any purpose, most recent first."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    base_q = db.query(AudioJob).filter(AudioJob.world_id == world.id).order_by(AudioJob.created_at.desc())
    jobs, page, total_pages = paginate(base_q, page)
    return {"jobs": [_job_to_dict(j) for j in jobs], "page": page, "total_pages": total_pages}


@router.get("/api/audio-jobs/{job_id}")
def api_audio_job_status(job_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    job = db.query(AudioJob).filter(AudioJob.id == job_id, AudioJob.world_id == world.id).first()
    if not job:
        raise HTTPException(404)
    return _job_to_dict(job)


@router.post("/api/audio-jobs/{job_id}/cancel")
def api_audio_job_cancel(job_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    job = db.query(AudioJob).filter(AudioJob.id == job_id, AudioJob.world_id == world.id).first()
    if not job:
        raise HTTPException(404)
    if job.status not in _audio_jobs.IN_PROGRESS_STATUSES:
        raise HTTPException(400, "Job is not in progress")
    if not _audio_jobs.cancel_job(job_id):
        raise HTTPException(400, "Job isn't currently running (it may have just finished)")
    return {"ok": True}


@router.delete("/api/audio-jobs/{job_id}")
def api_audio_job_delete(job_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    job = db.query(AudioJob).filter(AudioJob.id == job_id, AudioJob.world_id == world.id).first()
    if not job:
        raise HTTPException(404)
    if not _audio_jobs.delete_job(job_id):
        raise HTTPException(400, "Job is still in progress — cancel it first")
    return {"ok": True}


@router.post("/api/audio-jobs/{job_id}/resummarize")
async def api_audio_job_resummarize(
    job_id: int, request: Request, model: str = Form(""), extra_instructions: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Kick off re-running the summarization step against a job's already-
    saved transcript — no re-upload or re-transcription needed. Lets a GM
    fix a job that summarized with the wrong (or an unavailable) model, or
    just try a different one, or add/change one-off instructions for this
    pass (blank keeps whatever the job was last summarized with — see
    start_resummarize_job's docstring), straight from the Background Jobs
    page. Returns as soon as the job is marked "summarizing" — the actual
    work runs as a background task (see start_resummarize_job's docstring
    for why: running a long transcript's summarization inline here used to
    be a routine way to trip the reverse proxy's own timeout). The caller
    polls the regular job list/status routes for the result, same as any
    other in-flight job on this page."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    job = db.query(AudioJob).filter(AudioJob.id == job_id, AudioJob.world_id == world.id).first()
    if not job:
        raise HTTPException(404)
    try:
        job = _audio_jobs.start_resummarize_job(job_id, model=model.strip(), extra_instructions=extra_instructions)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _job_to_dict(job)
