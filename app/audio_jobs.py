"""Durable background jobs for audio transcription (+ optional
summarization) — see AudioJob in app/models.py for the full rationale.
Kept in its own module (not routers/sessions.py or routers/ai.py) since
both routers start/poll the identical job engine.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from . import ai as _ai_module
from .database import SessionLocal
from .models import AudioJob

_log = logging.getLogger("nd.audio_jobs")

# Must hold a strong reference to every in-flight task — asyncio only keeps
# a weak reference of its own, so a task with nothing else referencing it
# can be garbage-collected mid-run. Keyed by AudioJob.id; each task removes
# its own entry once it finishes (success or failure) via a done-callback.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "transcribing", "summarizing")


def create_job(
    world_id: int, purpose: str, filename: str, audio_path: Path,
    delete_after: bool = True, game_session_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None, attachment_url: str = "",
) -> int:
    """Create the job row and start its background task immediately —
    returns the job id right away, well before transcription (let alone
    summarization) has even started, so the caller's HTTP response can
    return instantly regardless of how long the actual work takes. The
    background task keeps running in the server process independent of
    this (or any) HTTP connection, so closing the tab that started it
    doesn't stop it."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose=purpose, filename=filename,
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            attachment_url=attachment_url, status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id, audio_path, purpose, delete_after))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t: _running_tasks.pop(job_id, None))
    return job_id


async def _run_job(job_id: int, audio_path: Path, purpose: str, delete_after: bool) -> None:
    def _set(**fields):
        db = SessionLocal()
        try:
            job = db.get(AudioJob, job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
        finally:
            db.close()

    try:
        _set(status="transcribing")
        transcript = await _ai_module.transcribe_audio(audio_path)
        if not transcript:
            _set(status="error", error=(
                "Could not transcribe this audio — check that Whisper is configured and "
                "reachable (see the AI page's \U0001f399 Whisper tab) and that the clip "
                "actually has speech in it."
            ))
            return
        _set(transcript=transcript)

        if purpose == "session_recap":
            _set(status="summarizing")
            recap = await _ai_module.summarize_transcript(transcript)
            _set(status="done", recap=recap)
        else:
            _set(status="done")
    except asyncio.CancelledError:
        # cancel_job() below calls Task.cancel() — record it as a distinct
        # outcome (not "error") before letting the cancellation actually
        # propagate, so the row doesn't sit at whatever status it was in
        # forever (a GM cancelling from the Background Jobs tab is the only
        # way this fires; a process restart goes through
        # sweep_interrupted_jobs instead, since there's no task to cancel).
        _set(status="cancelled", error="Cancelled by GM.")
        raise
    except Exception as exc:
        _log.exception("audio job %s failed", job_id)
        _set(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if delete_after:
            audio_path.unlink(missing_ok=True)


def cancel_job(job_id: int) -> bool:
    """Cancel an in-flight job's background task. Returns False if the job
    isn't currently running (already finished, or never started in this
    process — e.g. the id is stale/unknown), in which case the caller
    should treat it as a no-op rather than an error."""
    task = _running_tasks.get(job_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def sweep_interrupted_jobs() -> None:
    """Called once at startup: any job still mid-flight when the process
    last stopped (a crash, a deploy, a Watchtower update) has no background
    task to resume it — asyncio.create_task's state doesn't survive a
    process restart — so mark it failed with a clear reason instead of
    leaving it stuck showing "transcribing" forever."""
    db = SessionLocal()
    try:
        stuck = db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "error"
            job.error = "Interrupted by a server restart — please re-upload."
        if stuck:
            db.commit()
    finally:
        db.close()
