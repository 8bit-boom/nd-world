"""Durable background jobs for audio transcription (+ optional
summarization) — see AudioJob in app/models.py for the full rationale.
Kept in its own module (not routers/sessions.py or routers/ai.py) since
both routers start/poll the identical job engine.
"""
import asyncio
import logging
from datetime import datetime
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


def _forget_task(job_id: int, task: asyncio.Task, audio_path: Optional[Path] = None, delete_after: bool = False) -> None:
    """Done-callback for a job's background task.

    Identity-checked — only removes the registry entry if it's still THIS
    task — because asyncio schedules done-callbacks via call_soon, so
    there's at least one event-loop turn between a task finishing (after
    it has already written a terminal status) and this callback actually
    running. A resummarize started in that window sees a row that's
    already out of IN_PROGRESS_STATUSES, installs its own new task into
    _running_tasks, and then the OLD task's callback — using the old
    lambda's bare `_running_tasks.pop(job_id, None)` — would delete that
    live task's registry entry out from under it: it becomes only
    weakly-referenced (asyncio doesn't hold a strong reference of its
    own — eligible for GC mid-run) and cancel_job() can no longer find it
    to cancel.

    Also reconciles a task that was cancelled before its coroutine body
    ever started running at all: asyncio.Task.cancel() on such a task
    skips the body entirely, so neither _run_job's own `except
    asyncio.CancelledError` nor its `finally: audio_path.unlink(...)` ever
    executes, leaving the row stuck at "pending" forever (cancel_job and
    delete_job both refuse a row in IN_PROGRESS_STATUSES) and leaking the
    uploaded audio. Safe to run unconditionally on every cancelled task:
    when the body DID run far enough to reach its own CancelledError
    handler, that handler has already moved the row out of
    IN_PROGRESS_STATUSES (and already deleted the audio file) before this
    callback ever fires, so the checks below are then no-ops."""
    if _running_tasks.get(job_id) is not task:
        return
    del _running_tasks[job_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if job and job.status in IN_PROGRESS_STATUSES:
            job.status = "cancelled"
            job.error = "Cancelled by GM."
            job.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    if delete_after and audio_path is not None:
        audio_path.unlink(missing_ok=True)


def _looks_like_failure(result: str) -> bool:
    """summarize_transcript() never raises on an Ollama-side failure — it
    returns a failure-sentinel string instead (see _ai_module.
    is_failure_sentinel's own docstring). Delegates there rather than
    keeping a second copy of the prefix check, after the two previously
    drifted apart: this module's own copy only checked one of the two
    sentinel families, which let a failed part-summary get woven into a
    "done" recap on the chunked summarization path — without this check
    entirely, the same bug applies to any Ollama-side failure."""
    return _ai_module.is_failure_sentinel(result)


def create_job(
    world_id: int, purpose: str, filename: str, audio_path: Path,
    delete_after: bool = True, game_session_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None, attachment_url: str = "",
    model: str = "", extra_instructions: str = "",
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
    only transcribes). Blank means "whatever the instance default is."

    `extra_instructions`, if given, is a one-off note for THIS run's
    summarization only (purpose="session_recap" only) — combined with the
    world's own persistent World.recap_instructions rather than replacing
    it, see _combined_recap_instructions."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose=purpose, filename=filename,
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            attachment_url=attachment_url, status="pending", model=model or None,
            extra_instructions=extra_instructions.strip() or None,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id, audio_path, purpose, delete_after, model, world_id, extra_instructions))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id, p=audio_path, d=delete_after: _forget_task(jid, t, p, d))
    return job_id


def _glossary_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.whisper_glossary or "").strip() if w else ""
    finally:
        db.close()


def _whisper_language_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.whisper_language or "").strip() if w else ""
    finally:
        db.close()


def _recap_instructions_for_world(world_id: int) -> str:
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        return (w.recap_instructions or "").strip() if w else ""
    finally:
        db.close()


def _combined_recap_instructions(world_instructions: str, job_instructions: str) -> str:
    """The world's own persistent recap_instructions (a standing GM
    preference, e.g. "always call out combat tactics") always applies;
    job_instructions is a one-off note for this specific run only (e.g.
    "this session was mostly shopping/downtime, keep it short"). Neither
    replaces the other — both get passed to summarize_transcript together,
    world-level guidance first so a longer one-off note can't crowd it out
    of the prompt."""
    parts = [p for p in (world_instructions.strip(), job_instructions.strip()) if p]
    return "\n\n".join(parts)


async def _run_job(
    job_id: int, audio_path: Path, purpose: str, delete_after: bool, model: str = "",
    world_id: Optional[int] = None, extra_instructions: str = "",
) -> None:
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
        _set(status="transcribing", run_started_at=datetime.utcnow(), finished_at=None)
        glossary = _glossary_for_world(world_id) if world_id else ""
        language = _whisper_language_for_world(world_id) if world_id else ""
        try:
            transcript = await _ai_module.transcribe_audio(
                audio_path, glossary=glossary, language=language,
                on_progress=lambda current, total: _set(chunk_current=current, chunk_total=total),
            )
        except _ai_module.WhisperError as exc:
            _set(status="error", error=str(exc), finished_at=datetime.utcnow())
            return
        if not transcript:
            _set(status="error", finished_at=datetime.utcnow(), error=(
                "Whisper transcribed this clip successfully but found no speech in it "
                "— check the recording actually captured audio."
            ))
            return
        _set(transcript=transcript, chunk_current=None, chunk_total=None)

        if purpose == "session_recap":
            _set(status="summarizing")
            instructions = _combined_recap_instructions(
                _recap_instructions_for_world(world_id) if world_id else "", extra_instructions,
            )
            recap = await _ai_module.summarize_transcript(
                transcript, model=model, extra_instructions=instructions,
                on_progress=lambda current, total: _set(chunk_current=current, chunk_total=total),
            )
            if _looks_like_failure(recap):
                _set(status="error", error=recap, chunk_current=None, chunk_total=None, finished_at=datetime.utcnow())
            else:
                _set(status="done", recap=recap, chunk_current=None, chunk_total=None, finished_at=datetime.utcnow())
        else:
            _set(status="done", finished_at=datetime.utcnow())
    except asyncio.CancelledError:
        # cancel_job() below calls Task.cancel() — record it as a distinct
        # outcome (not "error") before letting the cancellation actually
        # propagate, so the row doesn't sit at whatever status it was in
        # forever (a GM cancelling from the Background Jobs tab is the only
        # way this fires; a process restart goes through
        # sweep_interrupted_jobs instead, since there's no task to cancel).
        _set(status="cancelled", error="Cancelled by GM.", finished_at=datetime.utcnow())
        raise
    except Exception as exc:
        _log.exception("audio job %s failed", job_id)
        _set(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=datetime.utcnow())
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


def start_resummarize_job(job_id: int, model: str = "", extra_instructions: Optional[str] = None) -> AudioJob:
    """Kick off re-running just the summarization step against a job's
    already-saved transcript, optionally with a different model — for when
    the first summary failed (wrong/unpulled model, Ollama unreachable) or
    a GM just wants a second pass, without re-uploading or re-transcribing
    the audio. Returns immediately once the job is marked "summarizing";
    the actual work runs as a tracked background task, same engine as
    create_job/_run_job — a long transcript's map-reduce chunking (see
    summarize_transcript) can take several minutes, and running that
    inline inside the request/response cycle used to make it a routine way
    to trip a reverse proxy's own timeout (a raw 524 from Cloudflare, not
    even an error nd-world itself produced) long before Ollama finished.
    Raises ValueError with a caller-displayable message on any invalid
    state — checked synchronously up front so a bad request still fails
    fast rather than only surfacing after the caller starts polling.

    `extra_instructions`, same convention as `model` just above: blank/None
    keeps whatever the job was created with (or last resummarized with),
    a non-blank value replaces it for this run and is persisted for next
    time too."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            raise ValueError("Job not found.")
        if job.purpose != "session_recap":
            raise ValueError("Only session-recap jobs can be re-summarized.")
        if not job.transcript:
            raise ValueError("This job has no transcript yet to summarize.")
        if job.status in IN_PROGRESS_STATUSES:
            raise ValueError("This job is already in progress.")
        world_id = job.world_id
        chosen_model = model or job.model or ""
        chosen_instructions = (extra_instructions or "").strip() or (job.extra_instructions or "")
        job.status = "summarizing"
        job.error = ""
        job.chunk_current = None
        job.chunk_total = None
        job.extra_instructions = chosen_instructions or None
        job.run_started_at = datetime.utcnow()
        job.finished_at = None
        db.commit()
        db.refresh(job)
        job_snapshot = job
    finally:
        db.close()

    task = asyncio.create_task(_run_resummarize_job(job_id, chosen_model, world_id, chosen_instructions))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_snapshot


async def _run_resummarize_job(job_id: int, model: str, world_id: Optional[int], extra_instructions: str = "") -> None:
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

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        transcript = job.transcript if job else ""
    finally:
        db.close()

    try:
        instructions = _combined_recap_instructions(
            _recap_instructions_for_world(world_id) if world_id else "", extra_instructions,
        )
        recap = await _ai_module.summarize_transcript(
            transcript, model=model, extra_instructions=instructions,
            on_progress=lambda current, total: _set(chunk_current=current, chunk_total=total),
        )
        fields = {"chunk_current": None, "chunk_total": None, "finished_at": datetime.utcnow()}
        if model:
            fields["model"] = model
        if _looks_like_failure(recap):
            _set(status="error", error=recap, **fields)
        else:
            _set(status="done", recap=recap, error="", **fields)
    except asyncio.CancelledError:
        _set(status="cancelled", error="Cancelled by GM.", finished_at=datetime.utcnow())
        raise
    except Exception as exc:
        _log.exception("audio job %s resummarize failed", job_id)
        _set(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=datetime.utcnow())


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
            job.finished_at = datetime.utcnow()
        if stuck:
            db.commit()
    finally:
        db.close()
