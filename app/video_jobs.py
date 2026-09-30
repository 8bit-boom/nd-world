"""Durable background jobs for AI video generation via Studio's /v1/videos
— see VideoJob in app/models.py for the full rationale. Mirrors app/
image_jobs.py's shape (create_job/_run_job/cancel_job/sweep_interrupted_
jobs) deliberately so the three media-job engines stay recognizable as
the same pattern.

The one real difference from ImageJob: Studio itself owns the generation
state. nd-world POSTs the request, gets back a generation id, and then
polls the /v1/videos list until that entry looks finished before fetching
the bytes — so a server restart can RE-ATTACH to a generation Studio is
still working on (via the persisted studio_video_id) instead of paying
for the whole generation twice. The exact /v1/videos entry shape is NOT
live-verified yet (Phase 0 findings only pinned the list envelope), so
every field read here is defensive: unknown shapes mean "keep polling",
never a crash.

Honesty note for a CPU-only box: video generation is far slower than
image generation. The job pipeline (durable rows, restart re-attach,
Background Jobs page) is what makes it usable at all today; a GPU makes
it pleasant.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from . import job_shutdown as _job_shutdown
from . import unsloth_extras as _studio
from .database import SessionLocal
from .models import VideoClip, VideoJob

_log = logging.getLogger("nd.video_jobs")

# Must hold a strong reference to every in-flight task — see audio_jobs.py's
# identical comment; the same GC risk applies here.
_running_tasks: dict[int, asyncio.Task] = {}

IN_PROGRESS_STATUSES = ("pending", "generating", "fetching")

_INTERRUPTED_NOTE = (
    "Paused by a server restart — it will re-attach to the Studio-side "
    "generation automatically on the next boot."
)

# How long to wait, end to end, before giving up on one generation.
# Default 4 h: generous for a CPU-only box where video is very slow;
# env-tunable like the other AI budgets.
_MAX_WAIT_SECONDS = float(os.environ.get("ND_VIDEO_JOB_MAX_SECONDS", str(4 * 3600)))
_POLL_INTERVAL_SECONDS = 10.0
# ~5 minutes of consecutive failed polls (10s apart) before giving up.
_MAX_CONSECUTIVE_POLL_ERRORS = 30


def _forget_task(job_id: int, task: asyncio.Task) -> None:
    """Done-callback for a job's background task — ported from
    audio_jobs.py's own (see its docstring for the race/reconciliation
    rationale, identical here)."""
    if _running_tasks.get(job_id) is not task:
        return
    del _running_tasks[job_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
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
    returns the job id right away, before Studio has even accepted the
    generation, so the caller's HTTP response returns instantly."""
    db = SessionLocal()
    try:
        job = VideoJob(
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


def _extract_video_id(resp) -> str:
    """Pull the generation id out of a POST /v1/videos response without
    assuming the exact shape (unverified on live Studio): a bare id, an
    OpenAI-style {data:[{id}]}, or {video:{id}} / {job:{id}} wrappers."""
    if not isinstance(resp, dict):
        return ""
    for key in ("id", "video_id", "job_id"):
        v = resp.get(key)
        if isinstance(v, (str, int)):
            return str(v)
    for wrapper in ("data", "video", "job"):
        inner = resp.get(wrapper)
        if isinstance(inner, dict):
            v = inner.get("id") or inner.get("video_id")
            if isinstance(v, (str, int)):
                return str(v)
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            v = inner[0].get("id")
            if isinstance(v, (str, int)):
                return str(v)
    return ""


_FAILED_MARKERS = ("fail", "error", "cancel")
_DONE_MARKERS = ("succeed", "done", "complete", "ready")


def _status_tokens_match(value: str, markers) -> bool:
    """Whole-word prefix match on the status string's tokens: 'completed'
    matches the 'complete' marker, 'incomplete' does NOT, 'not_completed'
    splits on underscores so its 'not' and 'completed' tokens are examined
    individually — 'completed' matching there is acceptable (a status
    literally containing the word 'completed' as its own token is a done
    signal in every phrasing this defends against)."""
    import re as _re
    tokens = [t for t in _re.split(r"[\s_\-]+", value) if t]
    return any(tok.startswith(m) for m in markers for tok in tokens)


def _entry_state(entry) -> str:
    """'running' | 'done' | 'failed' for one /v1/videos list entry,
    decided from whichever of status/state/phase + content-url keys this
    Studio build actually populates. Unknown/absent fields mean
    'running' — the poll loop's deadline is the backstop, never a guess
    that mistakes a still-running generation for a finished one.

    Status tokens match on whole words: "complete" inside "incomplete"
    must NOT read as done, and a populated url field only counts when no
    status field exists at all (REST list entries often carry a self-url
    while still queued — audit 2026-09-30, unsloth finding 8)."""
    if not isinstance(entry, dict):
        return "running"
    import re as _re
    for key in ("status", "state", "phase"):
        v = str(entry.get(key) or "").lower()
        if not v:
            continue
        if _status_tokens_match(v, _FAILED_MARKERS):
            return "failed"
        if _status_tokens_match(v, _DONE_MARKERS):
            return "done"
        # A status field EXISTS and said something unrecognized — do not
        # fall through to url-based guessing below on top of it.
        return "running"
    # No status field at all: a populated content/url is the done signal.
    for key in ("content_url", "video_url", "output_url"):
        if entry.get(key):
            return "done"
    return "running"


async def _poll_until_done(video_id: str) -> None:
    """Raise StudioError('failed…') when Studio reports failure; return
    when the entry looks done; keep polling (deadline-bounded) while it's
    running or hasn't appeared in the list yet. Transient poll errors (a
    Studio restart, a network blip) are tolerated up to
    _MAX_CONSECUTIVE_POLL_ERRORS in a row — one 503 three hours into a
    CPU-box generation must not abandon a job that may complete fine
    (audit 2026-09-30, unsloth finding 3)."""
    deadline = asyncio.get_event_loop().time() + _MAX_WAIT_SECONDS
    poll_errors = 0
    while True:
        try:
            listing = await _studio.video_list()
            poll_errors = 0
        except _studio.StudioError as exc:
            poll_errors += 1
            if poll_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                raise
            _log.warning("video poll %r failed (%s) — retrying, %d/%d",
                         video_id, exc, poll_errors, _MAX_CONSECUTIVE_POLL_ERRORS)
        entries = listing.get("data") if isinstance(listing, dict) else listing
        entry = None
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict) and str(e.get("id") or "") == video_id:
                    entry = e
                    break
        if entry is not None:
            state = _entry_state(entry)
            if state == "failed":
                msg = str(entry.get("error") or entry.get("status") or "generation failed")
                raise _studio.StudioError(f"Studio video generation failed: {msg}", 502)
            if state == "done":
                return
        if asyncio.get_event_loop().time() >= deadline:
            raise _studio.StudioError(
                f"Video generation did not finish within {_MAX_WAIT_SECONDS / 3600:.1f} h "
                "(ND_VIDEO_JOB_MAX_SECONDS) — it may still complete on the Studio side; "
                "try again with a shorter clip or raise the limit.", 504)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _video_ext(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "webm" in ct:
        return ".webm"
    if "quicktime" in ct:
        return ".mov"
    return ".mp4"


async def _run_job(job_id: int, params: dict) -> None:
    def _set(**fields):
        db = SessionLocal()
        try:
            job = db.get(VideoJob, job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
        finally:
            db.close()

    def _get_video_id() -> str:
        db = SessionLocal()
        try:
            job = db.get(VideoJob, job_id)
            return (job.studio_video_id or "") if job else ""
        finally:
            db.close()

    try:
        # Re-attach: a restarted job already carries its Studio-side
        # generation id — skipping the POST is the whole point (no double
        # billing of a minutes-long generation).
        video_id = _get_video_id()
        if not video_id:
            _set(status="generating")
            body: dict = {"prompt": params.get("prompt") or ""}
            if params.get("model"):
                body["model"] = params["model"]
            if params.get("seconds"):
                body["seconds"] = params["seconds"]
            if params.get("size"):
                body["size"] = params["size"]
            resp = await _studio.video_generate(body)
            video_id = _extract_video_id(resp)
            if not video_id:
                raise _studio.StudioError(
                    "Studio accepted the video request but returned no generation id — "
                    "its /v1/videos response shape isn't one nd-world recognizes "
                    "(see docs/UNSLOTH_PHASE0_FINDINGS.md); update nd-world once verified.",
                    502)
            _set(studio_video_id=video_id)
        else:
            _set(status="generating")

        await _poll_until_done(video_id)

        _set(status="fetching")
        content, content_type = await _studio.video_content(video_id)

        # Land the bytes as a normal VideoClip — GM-only until reviewed,
        # exactly like every other generated asset.
        import uuid as _uuid
        ext = _video_ext(content_type)
        target_dir = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "video"
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"ai-video-{_uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(content)

        db = SessionLocal()
        try:
            job = db.get(VideoJob, job_id)
            if not job:
                return
            name = (params.get("name") or "").strip()[:250] or \
                (job.prompt or "AI video")[:60]
            clip = VideoClip(
                world_id=job.world_id,
                name=name,
                description=("Generated by Unsloth Studio — " + (job.prompt or ""))[:500],
                file_url=f"/uploads/video/{dest.name}",
                visible_to_players=False,
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)
            job.status = "done"
            job.clip_id = clip.id
            db.commit()
        finally:
            db.close()
    except asyncio.CancelledError:
        # Same contract as image_jobs: a shutdown cancel marks the job
        # interrupted (re-attached next boot via studio_video_id); a GM
        # cancel marks it cancelled. The Studio-side generation keeps
        # running either way — nd-world never tells Studio to stop, it
        # just stops caring.
        if _job_shutdown.stopping():
            _set(status="interrupted", error=_INTERRUPTED_NOTE)
        else:
            _set(status="cancelled", error="Cancelled by GM.")
        raise
    except Exception as exc:
        _log.exception("video job %s failed", job_id)
        _set(status="error", error=str(exc) or exc.__class__.__name__)


def cancel_job(job_id: int) -> bool:
    """Cancel an in-flight job's background task (False = not running /
    unknown — a no-op, not an error, same contract as image_jobs)."""
    task = _running_tasks.get(job_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def delete_job(job_id: int) -> bool:
    """Remove a finished job's row. The VideoClip it produced is NOT
    deleted — unlike image_jobs (whose files exist only for the job),
    a clip is a first-class library asset the GM may already have placed
    in an album or an entity page; deleting it from under that would be
    surprising. Delete the clip itself from the Video page if unwanted."""
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        if not job or job.status in IN_PROGRESS_STATUSES:
            return False
        db.delete(job)
        db.commit()
        return True
    finally:
        db.close()


def live_tasks() -> list[asyncio.Task]:
    """Every currently-running task this engine owns — app.main's shutdown
    handler passes these to job_shutdown.drain()."""
    return list(_running_tasks.values())


def mark_stragglers_interrupted() -> None:
    """Shutdown-time belt-and-braces sweep — see image_jobs.py's identical
    function for the rationale."""
    db = SessionLocal()
    try:
        stuck = db.query(VideoJob).filter(VideoJob.status.in_(IN_PROGRESS_STATUSES)).all()
        for job in stuck:
            job.status = "interrupted"
            job.error = _INTERRUPTED_NOTE
        if stuck:
            db.commit()
    finally:
        db.close()


def sweep_interrupted_jobs() -> None:
    """Boot-time: any job still mid-flight after an UNCLEAN stop has no
    task in THIS process — mark it interrupted so resume (next) picks it
    up. Identical to image_jobs' sweep."""
    mark_stragglers_interrupted()


def resume_interrupted_jobs() -> int:
    """Boot-time: re-attach every interrupted job (up to
    job_shutdown.MAX_AUTO_RESUMES times each). Because the Studio-side
    generation id was persisted, resuming here means re-entering the poll
    loop for a generation that may already be done — cheap — rather than
    restarting the generation itself."""
    db = SessionLocal()
    try:
        job_ids = [j.id for j in db.query(VideoJob).filter(VideoJob.status == "interrupted").all()]
    finally:
        db.close()

    resumed = 0
    for job_id in job_ids:
        db = SessionLocal()
        try:
            job = db.get(VideoJob, job_id)
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
            try:
                params = json.loads(job.params_json or "{}")
            except ValueError:
                params = {}
            job.resumed_count = (job.resumed_count or 0) + 1
            job.status = "pending"
            db.commit()
        finally:
            db.close()

        task = asyncio.create_task(_run_job(job_id, params))
        _running_tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: _forget_task(jid, t))
        resumed += 1
    return resumed
