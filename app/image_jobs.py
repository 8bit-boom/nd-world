"""Durable background jobs for image generation — see ImageJob in
app/models.py for the full rationale. Mirrors app/audio_jobs.py's shape
(create_job/_run_job/cancel_job/sweep_interrupted_jobs) deliberately, so
the two stay recognizable as the same pattern even though they're
independent modules (an image generation's params don't fit AudioJob's
purpose/transcript/recap shape at all).
"""
import asyncio
import json
import logging
from pathlib import Path

from . import ai as _ai_module
from .database import SessionLocal
from .models import ImageJob

_log = logging.getLogger("nd.image_jobs")

# Must hold a strong reference to every in-flight task — see audio_jobs.py's
# identical comment; the same GC risk applies here.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "generating")


def create_job(world_id: int, prompt: str, params: dict, created_by_user_id=None) -> int:
    """Create the job row and start its background task immediately —
    returns the job id right away, before generation has even started, so
    the caller's HTTP response can return instantly regardless of how long
    the actual generation takes."""
    db = SessionLocal()
    try:
        job = ImageJob(
            world_id=world_id, prompt=prompt, params_json=json.dumps(params),
            created_by_user_id=created_by_user_id, status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id, params))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t: _running_tasks.pop(job_id, None))
    return job_id


async def _run_job(job_id: int, params: dict) -> None:
    def _set(**fields):
        db = SessionLocal()
        try:
            job = db.get(ImageJob, job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
        finally:
            db.close()

    try:
        _set(status="generating")
        call_params = {**params, "uploads_dir": Path(params["uploads_dir"])}
        urls = await _ai_module.imagegen_generate(**call_params)
        _set(status="done", result_urls_json=json.dumps(urls))
    except asyncio.CancelledError:
        # Same rationale as audio_jobs.py's identical handler: record the
        # cancellation as a distinct outcome before letting it propagate, so
        # the row doesn't sit at "generating" forever.
        _set(status="cancelled", error="Cancelled by GM.")
        raise
    except Exception as exc:
        _log.exception("image job %s failed", job_id)
        _set(status="error", error=str(exc))


def cancel_job(job_id: int) -> bool:
    """Cancel an in-flight job's background task. Returns False if the job
    isn't currently running (already finished, or unknown in this process),
    in which case the caller should treat it as a no-op rather than an
    error."""
    task = _running_tasks.get(job_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def sweep_interrupted_jobs() -> None:
    """Called once at startup: any job still mid-flight when the process
    last stopped has no background task to resume it — mark it failed with
    a clear reason instead of leaving it stuck showing "generating"
    forever. Same rationale as audio_jobs.py's identical sweep."""
    db = SessionLocal()
    try:
        stuck = db.query(ImageJob).filter(ImageJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "error"
            job.error = "Interrupted by a server restart — please try again."
        if stuck:
            db.commit()
    finally:
        db.close()
