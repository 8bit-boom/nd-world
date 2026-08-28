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
from . import job_shutdown as _job_shutdown
from .database import SessionLocal
from .models import ImageJob

_log = logging.getLogger("nd.image_jobs")

# Must hold a strong reference to every in-flight task — see audio_jobs.py's
# identical comment; the same GC risk applies here.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "generating")

_INTERRUPTED_NOTE = (
    "Paused by a server restart — it will restart automatically from the same request."
)


def _forget_task(job_id: int, task: asyncio.Task) -> None:
    """Done-callback for a job's background task — ported from
    audio_jobs.py's own (see its docstring for the full race/reconciliation
    rationale, identical here). No file to reconcile on cancel (image jobs
    don't own working storage the way AudioJob's audio_path does), just the
    row's status."""
    if _running_tasks.get(job_id) is not task:
        return
    del _running_tasks[job_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        job = db.get(ImageJob, job_id)
        if not job or job.status not in IN_PROGRESS_STATUSES:
            return
        if _job_shutdown.stopping():
            job.status = "interrupted"
            job.error = _INTERRUPTED_NOTE
        else:
            job.status = "cancelled"
            job.error = "Cancelled by GM."
        db.commit()
    finally:
        db.close()


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
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
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
        # A server shutdown calls Task.cancel() too (via job_shutdown.drain)
        # when this job doesn't finish inside the stop grace window —
        # stopping() tells that apart from a GM-initiated cancel. Unlike
        # AudioJob, there's no intermediate state to checkpoint here (one
        # opaque SwarmUI/ComfyUI call) — an "interrupted" job restarts from
        # the same saved params on the next boot rather than truly resuming,
        # see resume_interrupted_jobs. Everything here is synchronous (no
        # `await`), so this handler cannot itself be re-cancelled and does
        # not need asyncio.shield.
        if _job_shutdown.stopping():
            _set(status="interrupted", error=_INTERRUPTED_NOTE)
        else:
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


def delete_job(job_id: int) -> bool:
    """Permanently remove a finished job's row. Returns False (a no-op, not
    an error) if the job is still in progress — cancel it first — or the id
    is unknown, so the caller can 400/404 accordingly."""
    db = SessionLocal()
    try:
        job = db.get(ImageJob, job_id)
        if not job or job.status in IN_PROGRESS_STATUSES:
            return False
        db.delete(job)
        db.commit()
        return True
    finally:
        db.close()


def live_tasks() -> list[asyncio.Task]:
    """Every currently-running task this engine owns — app.main's shutdown
    handler passes this (alongside audio_jobs'/chat_jobs' own) to
    job_shutdown.drain() so it knows what to wait for/cancel."""
    return list(_running_tasks.values())


def mark_stragglers_interrupted() -> None:
    """Called right after job_shutdown.drain() returns during shutdown:
    belt-and-braces sweep for any row still mid-flight whose task's own
    CancelledError handler (or _forget_task) didn't get to run in time —
    same shape as the boot-time sweep_interrupted_jobs below, just running
    at the other end of the process's life instead."""
    db = SessionLocal()
    try:
        stuck = db.query(ImageJob).filter(ImageJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _INTERRUPTED_NOTE
        if stuck:
            db.commit()
    finally:
        db.close()


def sweep_interrupted_jobs() -> None:
    """Called once at startup, before resume_interrupted_jobs: any job
    still mid-flight when the process last stopped UNCLEANLY (a crash,
    OOM, or a SIGKILL past the graceful-shutdown window) has no background
    task to resume it in THIS process — marked "interrupted", the same
    status a clean shutdown leaves a paused job in, so
    resume_interrupted_jobs (called right after this, same startup hook)
    treats both cases identically."""
    db = SessionLocal()
    try:
        stuck = db.query(ImageJob).filter(ImageJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _INTERRUPTED_NOTE
        if stuck:
            db.commit()
    finally:
        db.close()


def resume_interrupted_jobs() -> int:
    """Called once at startup, right after sweep_interrupted_jobs:
    RESTARTS (not resumes — see this module's own docstring for why no
    true resume is possible here) every job left at status="interrupted",
    up to job_shutdown.MAX_AUTO_RESUMES times each, from its already-saved
    params_json. A job past the cap is marked "error" instead, so a
    generation that crashes the process itself doesn't retry forever on
    every subsequent boot. Returns how many jobs were restarted, for the
    caller to log."""
    db = SessionLocal()
    try:
        job_ids = [j.id for j in db.query(ImageJob).filter(ImageJob.status == "interrupted").all()]
    finally:
        db.close()

    resumed = 0
    for job_id in job_ids:
        db = SessionLocal()
        try:
            job = db.get(ImageJob, job_id)
            if not job or job.status != "interrupted":
                continue
            if job.resumed_count >= _job_shutdown.MAX_AUTO_RESUMES:
                job.status = "error"
                job.error = (
                    f"Interrupted by a server restart {job.resumed_count} times in a row — "
                    "try generating again once the server is stable."
                )
                db.commit()
                continue
            params = json.loads(job.params_json or "{}")
            job.status = "pending"
            job.error = ""
            job.resumed_count += 1
            db.commit()
        finally:
            db.close()
        task = asyncio.create_task(_run_job(job_id, params))
        _running_tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
        resumed += 1
    return resumed
