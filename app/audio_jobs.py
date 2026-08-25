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
from .models import AudioJob, World

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
    model: str = "",
) -> int:
    """Create the job row and start its background task immediately —
    returns the job id right away, well before transcription (let alone
    summarization) has even started, so the caller's HTTP response can
    return instantly regardless of how long the actual work takes. The
    background task keeps running in the server process independent of
    this (or any) HTTP connection, so closing the tab that started it
    doesn't stop it.

    `model`, if given, is the Ollama model to use for the summarization
    step (purpose="session_recap" only — ignored for "attachment", which
    only transcribes). Blank means "whatever the instance default is."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose=purpose, filename=filename,
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            attachment_url=attachment_url, status="pending", model=model or None,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id, audio_path, purpose, delete_after, model, world_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t: _running_tasks.pop(job_id, None))
    return job_id


def _glossary_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.whisper_glossary or "").strip() if w else ""
    finally:
        db.close()


def _recap_instructions_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.recap_instructions or "").strip() if w else ""
    finally:
        db.close()


async def _run_job(job_id: int, audio_path: Path, purpose: str, delete_after: bool, model: str = "", world_id: Optional[int] = None) -> None:
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
        glossary = _glossary_for_world(world_id) if world_id else ""
        try:
            transcript = await _ai_module.transcribe_audio(audio_path, glossary=glossary)
        except _ai_module.WhisperError as exc:
            _set(status="error", error=str(exc))
            return
        if not transcript:
            _set(status="error", error=(
                "Whisper transcribed this clip successfully but found no speech in it "
                "— check the recording actually captured audio."
            ))
            return
        _set(transcript=transcript)

        if purpose == "session_recap":
            _set(status="summarizing")
            instructions = _recap_instructions_for_world(world_id) if world_id else ""
            recap = await _ai_module.summarize_transcript(transcript, model=model, extra_instructions=instructions)
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


def delete_job(job_id: int) -> bool:
    """Permanently remove a finished job's row. Returns False (a no-op, not
    an error) if the job is still in progress — cancel it first — or the id
    is unknown, so the caller can 400/404 accordingly."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job or job.status in IN_PROGRESS_STATUSES:
            return False
        db.delete(job)
        db.commit()
        return True
    finally:
        db.close()


async def resummarize_job(job_id: int, model: str = "") -> AudioJob:
    """Re-run just the summarization step against a job's already-saved
    transcript, optionally with a different model — for when the first
    summary failed (wrong/unpulled model, Ollama unreachable) or a GM just
    wants a second pass, without re-uploading or re-transcribing the audio.
    Runs inline (awaited directly by the caller, same as every other manual
    summarize route in app/routers/sessions.py) rather than as a tracked
    background task, since summarizing an already-transcribed text is fast
    compared to transcription itself. Raises ValueError with a caller-
    displayable message on any invalid state."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            raise ValueError("Job not found.")
        if job.purpose != "session_recap":
            raise ValueError("Only session-recap jobs can be re-summarized.")
        if not job.transcript:
            raise ValueError("This job has no transcript yet to summarize.")
        transcript = job.transcript
        chosen_model = model or job.model or ""
        instructions = _recap_instructions_for_world(job.world_id) if job.world_id else ""
    finally:
        db.close()

    recap = await _ai_module.summarize_transcript(transcript, model=chosen_model, extra_instructions=instructions)

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            raise ValueError("Job not found.")
        job.status = "done"
        job.recap = recap
        job.error = ""
        if chosen_model:
            job.model = chosen_model
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


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
