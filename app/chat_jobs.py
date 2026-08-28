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
from . import job_shutdown as _job_shutdown
from .database import SessionLocal
from .models import ChatJob

_log = logging.getLogger("nd.chat_jobs")

# Must hold a strong reference to every in-flight task — see audio_jobs.py's
# identical comment; the same GC risk applies here.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "generating")

_INTERRUPTED_NOTE = (
    "Paused by a server restart — it will restart automatically from the same request."
)


# generate_chat() never raises — a failure comes back as a sentinel string
# instead (see _ai_module.is_failure_sentinel's own docstring). A background
# job with no exception to catch would otherwise show "✓ Done" with an error
# message as its "result" — check for that sentinel so the job list can be
# honest about what actually happened. Delegates to the shared predicate
# rather than keeping a local copy of the prefix check — audio_jobs.py's own
# copy once drifted to check only one of the two sentinel families.
def _looks_like_failure(result: str) -> bool:
    return _ai_module.is_failure_sentinel(result)


def _forget_task(job_id: int, task: asyncio.Task) -> None:
    """Done-callback for a job's background task — ported from
    audio_jobs.py's own (see its docstring for the full race/reconciliation
    rationale, identical here). No file to reconcile on cancel (chat jobs
    don't own working storage), just the row's status."""
    if _running_tasks.get(job_id) is not task:
        return
    del _running_tasks[job_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        job = db.get(ChatJob, job_id)
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
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
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
        # See ai.ollama_job_semaphore's own docstring — shared with
        # audio_jobs.py so a queued chat job and a queued recap job don't
        # run their Ollama calls on top of each other either.
        async with _ai_module.ollama_job_semaphore:
            result = await _ai_module.generate_chat(messages, system, model, options)
        if _looks_like_failure(result):
            _set(status="error", error=result)
        else:
            _set(status="done", result=result)
    except asyncio.CancelledError:
        # A server shutdown calls Task.cancel() too (via job_shutdown.drain)
        # when this job doesn't finish inside the stop grace window —
        # stopping() tells that apart from a GM-initiated cancel. Unlike
        # AudioJob, there's no intermediate state to checkpoint here (one
        # opaque non-streaming generate_chat call) — an "interrupted" job
        # restarts from the same saved request on the next boot rather than
        # truly resuming, see resume_interrupted_jobs. Everything here is
        # synchronous (no `await`), so this handler cannot itself be
        # re-cancelled and does not need asyncio.shield.
        if _job_shutdown.stopping():
            _set(status="interrupted", error=_INTERRUPTED_NOTE)
        else:
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


def live_tasks() -> list[asyncio.Task]:
    """Every currently-running task this engine owns — app.main's shutdown
    handler passes this (alongside audio_jobs'/image_jobs' own) to
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
        stuck = db.query(ChatJob).filter(ChatJob.status.in_(IN_PROGRESS_STATUSES)).all()
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
        stuck = db.query(ChatJob).filter(ChatJob.status.in_(IN_PROGRESS_STATUSES)).all()
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
    messages_json/system/model/options_json. A job past the cap is marked
    "error" instead, so a completion that crashes the process itself
    doesn't retry forever on every subsequent boot. Returns how many jobs
    were restarted, for the caller to log."""
    db = SessionLocal()
    try:
        job_ids = [j.id for j in db.query(ChatJob).filter(ChatJob.status == "interrupted").all()]
    finally:
        db.close()

    resumed = 0
    for job_id in job_ids:
        db = SessionLocal()
        try:
            job = db.get(ChatJob, job_id)
            if not job or job.status != "interrupted":
                continue
            if job.resumed_count >= _job_shutdown.MAX_AUTO_RESUMES:
                job.status = "error"
                job.error = (
                    f"Interrupted by a server restart {job.resumed_count} times in a row — "
                    "try again once the server is stable."
                )
                db.commit()
                continue
            messages = json.loads(job.messages_json or "[]")
            system = job.system or ""
            model = job.model or ""
            options = json.loads(job.options_json or "{}")
            job.status = "pending"
            job.error = ""
            job.resumed_count += 1
            db.commit()
        finally:
            db.close()
        task = asyncio.create_task(_run_job(job_id, messages, system, model, options))
        _running_tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
        resumed += 1
    return resumed
