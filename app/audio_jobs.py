"""Durable background jobs for audio transcription (+ optional
summarization) — see AudioJob in app/models.py for the full rationale.
Kept in its own module (not routers/sessions.py or routers/ai.py) since
both routers start/poll the identical job engine.
"""
import asyncio
import json as _json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import ai as _ai_module
from . import job_shutdown as _job_shutdown
from .database import SessionLocal
from .models import AudioJob, World

_log = logging.getLogger("nd.audio_jobs")

# Must hold a strong reference to every in-flight task — asyncio only keeps
# a weak reference of its own, so a task with nothing else referencing it
# can be garbage-collected mid-run. Keyed by AudioJob.id; each task removes
# its own entry once it finishes (success or failure) via a done-callback.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "transcribing", "summarizing")


def _interrupted_note(chunk_current: Optional[int] = None, chunk_total: Optional[int] = None) -> str:
    """The status/error message for a job paused mid-run by a server
    shutdown — not really an error, just explains what happened and that
    the saved checkpoint is what makes a resume possible. One shared
    wording so every call site that can transition a job to "interrupted"
    (both CancelledError-adjacent handlers and JobInterrupted handling in
    both run functions below, plus the boot/shutdown sweeps) says the same
    thing."""
    if chunk_total and chunk_current:
        return (
            f"Paused by a server restart at part {chunk_current} of {chunk_total} — "
            "the work so far is saved; it will resume automatically, or use ▶ Resume."
        )
    return "Paused by a server restart — the work so far is saved; it will resume automatically, or use ▶ Resume."


def _forget_task(job_id: int, task: asyncio.Task) -> None:
    """Done-callback for a job's background task.

    Identity-checked — only removes the registry entry if it's still THIS
    task — because asyncio schedules done-callbacks via call_soon, so
    there's at least one event-loop turn between a task finishing (after
    it has already written a terminal status) and this callback actually
    running. A resume/resummarize started in that window sees a row
    that's already out of IN_PROGRESS_STATUSES, installs its own new task
    into _running_tasks, and then the OLD task's callback would delete
    that live task's registry entry out from under it: it becomes only
    weakly-referenced (asyncio doesn't hold a strong reference of its
    own — eligible for GC mid-run) and cancel_job() can no longer find it
    to cancel.

    Also reconciles a task that was cancelled before its coroutine body
    ever started running at all: asyncio.Task.cancel() on such a task
    skips the body entirely, so neither _run_job's own `except
    asyncio.CancelledError` nor its cleanup ever executes, leaving the row
    stuck in an in-progress status forever (cancel_job and delete_job both
    refuse a row in IN_PROGRESS_STATUSES). Safe to run unconditionally on
    every cancelled task: when the body DID run far enough to reach its
    own CancelledError handler, that handler has already moved the row out
    of IN_PROGRESS_STATUSES before this callback ever fires, so the checks
    below are then no-ops.

    audio_path/delete_after are read off the row itself, not passed in —
    create_job/start_resume_job persist both (see AudioJob's own
    docstring) specifically so a resume after a restart can find the audio
    again without this module needing to thread them through every call."""
    if _running_tasks.get(job_id) is not task:
        return
    del _running_tasks[job_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job or job.status not in IN_PROGRESS_STATUSES:
            return
        if _job_shutdown.stopping():
            # Cancelled before its body ever ran, during a shutdown drain —
            # keep the audio and any checkpoint already on the row so a
            # resume can pick this job back up, same as _run_job's own
            # CancelledError handling below.
            job.status = "interrupted"
            job.error = _interrupted_note(job.chunk_current, job.chunk_total)
        else:
            job.status = "cancelled"
            job.error = "Cancelled by GM."
            if job.delete_after and job.audio_path:
                Path(job.audio_path).unlink(missing_ok=True)
        job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


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
    it, see _combined_recap_instructions.

    audio_path and delete_after are persisted onto the row (not just
    passed to _run_job as arguments) so a resume after a server restart —
    which has none of this call's local variables, only the DB row — can
    find the audio again and know whether it owns the file. See
    AudioJob's own docstring."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=world_id, purpose=purpose, filename=filename,
            game_session_id=game_session_id, created_by_user_id=created_by_user_id,
            attachment_url=attachment_url, status="pending", model=model or None,
            extra_instructions=extra_instructions.strip() or None,
            audio_path=str(audio_path), delete_after=delete_after,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
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


async def _run_job(job_id: int) -> None:
    """Runs (or resumes) a job's transcribe [+ summarize] work. Everything
    this used to take as function arguments (audio_path, purpose,
    delete_after, model, world_id, extra_instructions) now lives on the
    row instead, read once at the top — create_job and start_resume_job
    both just start this same task, rather than one being a subtly
    different reimplementation of the other.

    If the row already has a transcript (job.transcript non-empty) AND
    there's no still-outstanding transcribe-phase checkpoint, transcription
    is skipped entirely and this picks up straight at summarizing, using
    that saved transcript — true when resuming a job interrupted during or
    after the summarize phase. The transcribe-phase-checkpoint carve-out
    matters because a checkpoint's own mirror (see _checkpoint below)
    ALSO populates job.transcript while transcription is only partway
    through the audio (see app.ai.transcribe_audio's on_checkpoint
    contract) — without it, resuming a job interrupted mid-transcription
    would skip straight to summarizing (or, for purpose="attachment",
    straight to "done") on only a partial transcript. A checkpoint left
    on the row (job.checkpoint_json) is passed through to whichever phase
    it belongs to; app.ai's transcribe_audio/summarize_transcript each
    validate it still matches before trusting it (see their own
    docstrings) and discard/start over silently if not."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            return
        audio_path = Path(job.audio_path) if job.audio_path else None
        purpose = job.purpose
        delete_after = job.delete_after
        model = job.model or ""
        world_id = job.world_id
        extra_instructions = job.extra_instructions or ""
        existing_transcript = job.transcript or ""
        checkpoint = _json.loads(job.checkpoint_json) if job.checkpoint_json else None
    finally:
        db.close()

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

    def _checkpoint(state: dict) -> None:
        fields = {"checkpoint_json": _json.dumps(state)}
        if state.get("phase") == "transcribe":
            # Mirrored into `transcript` (not just checkpoint_json) so the
            # partial is independently useful the same way WhisperError's
            # own partial_transcript already is — the existing "Retry
            # summary"/"Extract facts" actions key off job.transcript, and
            # a GM watching the Background Jobs page sees real progress
            # without waiting for the job to finish. Summarization
            # checkpoints are deliberately NOT mirrored into `recap` — a
            # half-written recap would look like a finished answer instead
            # of a work in progress.
            fields["transcript"] = state.get("text", "")
        _set(**fields)

    progress = {
        "current": (checkpoint or {}).get("chunks_done") or (checkpoint or {}).get("parts_done"),
        "total": (checkpoint or {}).get("chunk_total"),
    }

    def _on_progress(current, total):
        progress["current"] = current
        progress["total"] = total
        _set(chunk_current=current, chunk_total=total)

    keep_audio = False
    try:
        # A transcript on the row means transcription is DONE — except a
        # checkpoint's own mirror (see _checkpoint above) also populates
        # job.transcript while a transcribe-phase resume is still only
        # partially through the audio, so a checkpoint still on
        # "transcribe" always wins regardless of what transcript already
        # holds (same phase-precedence start_resume_job's own dispatch
        # uses, see its docstring — this must never disagree with it, or a
        # resumed job could skip straight to a purpose="attachment" "done"
        # after transcribing only part of the recording).
        skip_transcribe = bool(existing_transcript) and not (checkpoint and checkpoint.get("phase") == "transcribe")
        if not skip_transcribe:
            _set(status="transcribing", run_started_at=datetime.utcnow(), finished_at=None)
            glossary = _glossary_for_world(world_id) if world_id else ""
            language = _whisper_language_for_world(world_id) if world_id else ""
            transcribe_resume = checkpoint if checkpoint and checkpoint.get("phase") == "transcribe" else None
            try:
                transcript = await _ai_module.transcribe_audio(
                    audio_path, glossary=glossary, language=language,
                    on_progress=_on_progress, on_checkpoint=_checkpoint,
                    should_stop=_job_shutdown.stopping, resume=transcribe_resume,
                )
            except _ai_module.WhisperError as exc:
                fields = {"status": "error", "error": str(exc), "finished_at": datetime.utcnow(), "checkpoint_json": ""}
                if exc.partial_transcript:
                    # At least one chunk transcribed before the failure —
                    # save it so the GM can resummarize from the salvaged
                    # partial (start_resummarize_job only needs
                    # job.transcript) instead of re-uploading and
                    # re-transcribing the whole recording.
                    fields["transcript"] = exc.partial_transcript
                _set(**fields)
                return
            if not transcript:
                _set(status="error", finished_at=datetime.utcnow(), checkpoint_json="", error=(
                    "Whisper transcribed this clip successfully but found no speech in it "
                    "— check the recording actually captured audio."
                ))
                return
            _set(transcript=transcript, chunk_current=None, chunk_total=None, checkpoint_json="")
        else:
            transcript = existing_transcript

        if purpose == "session_recap":
            _set(status="summarizing")
            instructions = _combined_recap_instructions(
                _recap_instructions_for_world(world_id) if world_id else "", extra_instructions,
            )
            summarize_resume = checkpoint if checkpoint and checkpoint.get("phase") == "summarize" else None
            recap = await _ai_module.summarize_transcript(
                transcript, model=model, extra_instructions=instructions,
                on_progress=_on_progress, on_checkpoint=_checkpoint,
                should_stop=_job_shutdown.stopping, resume=summarize_resume,
            )
            if _looks_like_failure(recap):
                _set(status="error", error=recap, chunk_current=None, chunk_total=None,
                     finished_at=datetime.utcnow(), checkpoint_json="")
            else:
                _set(status="done", recap=recap, chunk_current=None, chunk_total=None,
                     finished_at=datetime.utcnow(), checkpoint_json="")
        else:
            _set(status="done", finished_at=datetime.utcnow())
    except _job_shutdown.JobInterrupted:
        keep_audio = True
        _set(status="interrupted", error=_interrupted_note(progress["current"], progress["total"]),
             finished_at=datetime.utcnow())
    except asyncio.CancelledError:
        # cancel_job() calls Task.cancel() for a GM-initiated cancel; a
        # server shutdown calls it too (via job_shutdown.drain) when a job
        # doesn't reach a chunk boundary inside the stop grace window —
        # stopping() is what tells the two apart. A process restart with
        # no task to cancel at all (a crash/SIGKILL) goes through
        # sweep_interrupted_jobs/_forget_task instead, not this handler.
        #
        # Everything below is synchronous (no `await`) — cancellation only
        # delivers at an await point, so this handler cannot itself be
        # re-cancelled and does not need asyncio.shield. Keep it that way;
        # adding an `await` here would silently reintroduce that need.
        if _job_shutdown.stopping():
            keep_audio = True
            _set(status="interrupted", error=_interrupted_note(progress["current"], progress["total"]),
                 finished_at=datetime.utcnow())
        else:
            _set(status="cancelled", error="Cancelled by GM.", finished_at=datetime.utcnow())
        raise
    except Exception as exc:
        _log.exception("audio job %s failed", job_id)
        _set(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=datetime.utcnow(), checkpoint_json="")
    finally:
        if audio_path and delete_after and not keep_audio:
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
    time too.

    Deliberately always a FRESH pass, not a resume of some prior
    interrupted attempt: checkpoint_json is cleared and resumed_count reset
    before starting, even if this job happened to be sitting on a
    checkpoint from an earlier run that got interrupted by a shutdown.
    Reusing that checkpoint here — possibly against a different model or
    different instructions — could silently splice output from two
    different summarization passes together. POST .../resume
    (start_resume_job) is the entry point that continues an interrupted
    run instead of restarting it; this one is the GM's explicit "redo it"
    action."""
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
        chosen_model = model or job.model or ""
        chosen_instructions = (extra_instructions or "").strip() or (job.extra_instructions or "")
        job.status = "summarizing"
        job.error = ""
        job.chunk_current = None
        job.chunk_total = None
        job.model = chosen_model or None
        job.extra_instructions = chosen_instructions or None
        job.checkpoint_json = ""
        job.resumed_count = 0
        job.run_started_at = datetime.utcnow()
        job.finished_at = None
        db.commit()
        db.refresh(job)
        job_snapshot = job
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_snapshot


def start_resume_job(job_id: int, reset_attempts: bool = False) -> AudioJob:
    """Continue a job that was paused by a server restart (status=
    "interrupted") from its saved checkpoint, instead of starting over.
    Mirrors start_resummarize_job's shape and contract: validates
    synchronously up front (raises ValueError with a caller-displayable
    message on any invalid state) and sets the row into its resuming
    phase BEFORE create_task, so no HTTP poll immediately after this call
    ever sees the job flicker back to "interrupted".

    Which phase it resumes into is decided from the checkpoint (or, if
    there isn't one — e.g. the job was interrupted right as transcription
    finished, just before the first summarize checkpoint could be
    written — from whether a transcript already exists): _run_job itself
    is what actually skips straight to summarizing when job.transcript is
    already set, so this only needs to validate the audio is still there
    when it ISN'T.

    `reset_attempts`, if true, zeroes resumed_count — for a GM manually
    resuming a job that hit job_shutdown.MAX_AUTO_RESUMES and gave up
    automatically; a manual resume is a deliberate human decision, not
    another automatic retry, so it shouldn't inherit the cap that stopped
    further AUTOMATIC attempts. The HTTP route always passes True."""
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        if not job:
            raise ValueError("Job not found.")
        if job.status != "interrupted":
            raise ValueError("Only a job paused by a server restart can be resumed this way.")
        if not job.audio_path and not job.transcript:
            raise ValueError("This job's audio is gone and it has no transcript to resume from — please re-upload.")
        checkpoint = _json.loads(job.checkpoint_json) if job.checkpoint_json else None
        phase = (checkpoint or {}).get("phase")
        resuming_summarize = phase == "summarize" or (not phase and bool(job.transcript))
        if resuming_summarize:
            job.status = "summarizing"
        else:
            if not job.audio_path or not Path(job.audio_path).is_file():
                raise ValueError("This job's audio file is gone — please re-upload to redo the recording.")
            job.status = "transcribing"
        job.error = ""
        job.chunk_current = ((checkpoint or {}).get("chunks_done") or (checkpoint or {}).get("parts_done")) if checkpoint else None
        job.chunk_total = (checkpoint or {}).get("chunk_total") if checkpoint else None
        job.resumed_count = 0 if reset_attempts else job.resumed_count + 1
        job.run_started_at = datetime.utcnow()
        job.finished_at = None
        db.commit()
        db.refresh(job)
        job_snapshot = job
    finally:
        db.close()

    task = asyncio.create_task(_run_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
    return job_snapshot


def live_tasks() -> list[asyncio.Task]:
    """Every currently-running task this engine owns — app.main's shutdown
    handler passes this (alongside image_jobs'/chat_jobs' own) to
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
        stuck = db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _interrupted_note(job.chunk_current, job.chunk_total)
            job.finished_at = datetime.utcnow()
        if stuck:
            db.commit()
    finally:
        db.close()


def sweep_interrupted_jobs() -> None:
    """Called once at startup, before resume_interrupted_jobs: any job
    still mid-flight when the process last stopped UNCLEANLY (a crash,
    OOM, or a SIGKILL past the graceful-shutdown window — job_shutdown's
    own drain()/mark_stragglers_interrupted already handle a clean
    shutdown) has no background task to resume it in THIS process —
    asyncio.create_task's state doesn't survive a process restart — so
    it's marked "interrupted" here too, the same status a clean shutdown
    leaves a paused job in, so resume_interrupted_jobs (called right after
    this, same startup hook) treats both cases identically."""
    db = SessionLocal()
    try:
        stuck = db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _interrupted_note(job.chunk_current, job.chunk_total)
            job.finished_at = datetime.utcnow()
        if stuck:
            db.commit()
    finally:
        db.close()


def resume_interrupted_jobs() -> int:
    """Called once at startup, right after sweep_interrupted_jobs:
    auto-resumes every job left at status="interrupted" up to
    job_shutdown.MAX_AUTO_RESUMES times each — a job past the cap, or with
    no viable resume point (its audio is gone and it never got as far as
    saving a transcript), is marked "error" instead with a message
    explaining why, keeping whatever transcript/recap was already
    salvaged. Auto-resuming (rather than just leaving it for a GM to
    notice and click Resume) is the point of this whole feature: a routine
    `git pull && docker compose up -d --build` shouldn't require a human to
    intervene to get their transcription finished. Returns how many jobs
    were resumed, for the caller to log."""
    db = SessionLocal()
    try:
        job_ids = [j.id for j in db.query(AudioJob).filter(AudioJob.status == "interrupted").all()]
    finally:
        db.close()

    resumed = 0
    for job_id in job_ids:
        db = SessionLocal()
        try:
            job = db.get(AudioJob, job_id)
            if not job or job.status != "interrupted":
                continue
            if job.resumed_count >= _job_shutdown.MAX_AUTO_RESUMES:
                job.status = "error"
                job.error = (
                    f"Interrupted by a server restart {job.resumed_count} times in a row — resume it by "
                    "hand from Background Jobs once the server is stable."
                )
                job.finished_at = datetime.utcnow()
                db.commit()
                continue
            if not job.transcript and (not job.audio_path or not Path(job.audio_path).is_file()):
                job.status = "error"
                job.error = "Interrupted by a server restart — please re-upload."
                job.finished_at = datetime.utcnow()
                db.commit()
                continue
        finally:
            db.close()
        try:
            start_resume_job(job_id)
            resumed += 1
        except ValueError as exc:
            _log.warning("could not auto-resume audio job %s: %s", job_id, exc)
    return resumed


# Same path routers/sessions.py's own _session_audio_jobs_dir() computes —
# kept as its own small copy here per this codebase's per-module
# convention, rather than importing across the router boundary for one path.
_SESSION_AUDIO_JOBS_CUTOFF_SECONDS = 24 * 60 * 60


def sweep_orphaned_job_audio() -> None:
    """Called once at startup, after sweep_interrupted_jobs/
    resume_interrupted_jobs: a session-recap job's uploaded audio (up to
    MAX_AUDIO_UPLOAD_BYTES, 1 GB default) is only ever deleted by
    _run_job's own cleanup — a crash, deploy, or Watchtower update mid-job
    used to skip that entirely, leaving the file behind with no DB record
    pointing at it. Now that AudioJob.audio_path is persisted, this builds
    a keep-set from it first: any file still referenced by a job that's in
    progress or interrupted (awaiting a resume) is kept regardless of
    age — deleting it out from under a job still waiting to resume would
    silently turn "resume" into "start over" the next time a GM (or
    auto-resume, just above) tries. Everything else under
    uploads/session_audio/_jobs/ older than the cutoff is still swept, same
    as before. Deliberately does NOT touch uploads/ai_attachments/: those
    jobs run with delete_after=False because the file IS the attachment,
    not working storage to clean up."""
    jobs_dir = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_jobs"
    if not jobs_dir.is_dir():
        return

    db = SessionLocal()
    try:
        keep = {
            Path(j.audio_path).name
            for j in db.query(AudioJob).filter(AudioJob.status.in_(IN_PROGRESS_STATUSES + ("interrupted",))).all()
            if j.audio_path
        }
    finally:
        db.close()

    cutoff = time.time() - _SESSION_AUDIO_JOBS_CUTOFF_SECONDS
    for child in jobs_dir.iterdir():
        try:
            if child.name in keep:
                continue
            if child.is_file() and child.stat().st_mtime < cutoff:
                child.unlink()
        except OSError:
            pass
