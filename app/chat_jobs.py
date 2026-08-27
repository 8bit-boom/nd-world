"""Durable background jobs for a single non-streaming chat completion — see
ChatJob in app/models.py for the full rationale. Mirrors app/image_jobs.py's
shape (create_job/_run_job/cancel_job/delete_job/sweep_interrupted_jobs)
deliberately, so all three background-job engines (audio/image/chat) stay
recognizable as the same pattern.
"""
import asyncio
import json
import logging

from . import ai as _ai_module
from .database import SessionLocal
from .models import ChatJob

_log = logging.getLogger("nd.chat_jobs")

# Must hold a strong reference to every in-flight task — see audio_jobs.py's
# identical comment; the same GC risk applies here.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "generating")

# generate_chat() never raises — a failure comes back as a sentinel string
# instead (see _ai_module.is_failure_sentinel's own docstring). A background
# job with no exception to catch would otherwise show "✓ Done" with an error
# message as its "result" — check for that sentinel so the job list can be
# honest about what actually happened. Delegates to the shared predicate
# rather than keeping a local copy of the prefix check — audio_jobs.py's own
# copy once drifted to check only one of the two sentinel families.
def _looks_like_failure(result: str) -> bool:
    return _ai_module.is_failure_sentinel(result)


def create_job(
    world_id: int, messages: list[dict], system: str, model: str, options: dict,
    created_by_user_id=None,
) -> int:
    """Create the job row and start its background task immediately —
    returns the job id right away, before generation has even started, so
    the caller's HTTP response can return instantly regardless of how long
    the actual generation takes. `messages` is expected to already include
    any injected lore/RAG context, same as what the direct /api/ai/stream
    route receives — this module has no context-building logic of its own."""
    prompt = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            prompt = m["content"]
            break
    db = SessionLocal()
    try:
        job = ChatJob(
            world_id=world_id, prompt=prompt[:500], messages_json=json.dumps(messages),
            system=system or "", model=model or None, options_json=json.dumps(options or {}),
            created_by_user_id=created_by_user_id, status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id, messages, system, model, options))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t: _running_tasks.pop(job_id, None))
    return job_id


async def _run_job(job_id: int, messages: list[dict], system: str, model: str, options: dict) -> None:
    def _set(**fields):
        db = SessionLocal()
        try:
            job = db.get(ChatJob, job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
        finally:
            db.close()

    try:
        _set(status="generating")
        result = await _ai_module.generate_chat(messages, system, model, options)
        if _looks_like_failure(result):
            _set(status="error", error=result)
        else:
            _set(status="done", result=result)
    except asyncio.CancelledError:
        # Same rationale as audio_jobs.py's identical handler: record the
        # cancellation as a distinct outcome before letting it propagate, so
        # the row doesn't sit at "generating" forever.
        _set(status="cancelled", error="Cancelled by GM.")
        raise
    except Exception as exc:
        _log.exception("chat job %s failed", job_id)
        _set(status="error", error=f"{type(exc).__name__}: {exc}")


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
        job = db.get(ChatJob, job_id)
        if not job or job.status in IN_PROGRESS_STATUSES:
            return False
        db.delete(job)
        db.commit()
        return True
    finally:
        db.close()


def sweep_interrupted_jobs() -> None:
    """Called once at startup: any job still mid-flight when the process
    last stopped has no background task to resume it — mark it failed with
    a clear reason instead of leaving it stuck showing "generating" forever.
    Same rationale as audio_jobs.py/image_jobs.py's identical sweep."""
    db = SessionLocal()
    try:
        stuck = db.query(ChatJob).filter(ChatJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "error"
            job.error = "Interrupted by a server restart — please try again."
        if stuck:
            db.commit()
    finally:
        db.close()
