"""Bulk AI renaming as a durable background job - see MediaRenameRun in app/models.py.

A run takes the items the GM selected (hundreds is normal), asks the model a few at a time through
app/media_rename.suggest, and saves the proposals after EVERY chunk: closing the page costs nothing, a cancel keeps what
was gathered, and after a server restart the run carries on from the chunk it had reached. Nothing is renamed by a run -
the proposals wait for the GM's review on the rename page (which applies them with the normal apply route).

Shaped like app/chat_jobs.py (create / run / cancel / delete / live_tasks / mark_stragglers_interrupted /
sweep_interrupted / resume_interrupted), with one deliberate difference: the AI queue slot is taken per CHUNK, not for the
whole run, so a run of several hundred files does not lock chat and every other AI feature out for an hour - it simply
takes its turn again for each chunk.
"""
import asyncio
import json
import logging

from . import ai_queue as _ai_queue
from . import job_shutdown as _job_shutdown
from . import media_rename as _mr
from .database import SessionLocal
from .models import MediaRenameRun, World

_log = logging.getLogger("nd.media_rename_jobs")

MAX_RUN_ITEMS = 2000
CHUNK = 6
CHUNK_WITH_PICTURES = 3             # a picture is one model call each, so keep the saves frequent
IN_PROGRESS_STATUSES = ("pending", "running")
_INTERRUPTED_NOTE = "Paused by a server restart - it picks up from the last saved chunk."

# Strong references to every in-flight task (a bare create_task result can be garbage collected mid-run).
_running_tasks: dict = {}


def clean_options(options) -> dict:
    """Only the options the renamer understands, bounded: preset, style (<= 400 chars), pictures, listen."""
    options = options if isinstance(options, dict) else {}
    return {
        "preset": str(options.get("preset") or "")[:40],
        "style": str(options.get("style") or "")[:400],
        "pictures": bool(options.get("pictures")),
        "listen": bool(options.get("listen")),
    }


def chunk_size(options: dict) -> int:
    return CHUNK_WITH_PICTURES if (options or {}).get("pictures") else CHUNK


def _forget_task(run_id: int, task: asyncio.Task) -> None:
    """Done-callback: drop the strong reference and, if the task was cancelled before its own handler could record it
    (it never started running), settle the row's status."""
    if _running_tasks.get(run_id) is not task:
        return
    del _running_tasks[run_id]
    if not task.cancelled():
        return
    db = SessionLocal()
    try:
        run = db.get(MediaRenameRun, run_id)
        if run and run.status in IN_PROGRESS_STATUSES:
            run.status, run.error = ("interrupted", _INTERRUPTED_NOTE) if _job_shutdown.stopping() else ("cancelled", "Cancelled.")
            db.commit()
    finally:
        db.close()


def _start(run_id: int) -> None:
    task = asyncio.create_task(_run(run_id))
    _running_tasks[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _forget_task(rid, t))


def create_run(world_id: int, user_id, items: list, options: dict) -> int:
    """Save the run and start working on it right away; returns its id immediately. Raises ValueError for an empty or
    oversized selection."""
    if not isinstance(items, list) or not items:
        raise ValueError("Choose at least one item.")
    if len(items) > MAX_RUN_ITEMS:
        raise ValueError(f"At most {MAX_RUN_ITEMS} items in one run - narrow the selection.")
    db = SessionLocal()
    try:
        run = MediaRenameRun(
            world_id=world_id, created_by_user_id=user_id, status="pending", options_json=json.dumps(clean_options(options)),
            items_json=json.dumps(items), results_json="[]", errors_json="[]", total=len(items), done=0,
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    _start(run_id)
    return run_id


@_ai_queue.serialized("media rename", "job")
async def _process_chunk(world_id: int, chunk: list, options: dict) -> dict:
    """One chunk of items -> proposals, under the AI queue's one-at-a-time rule."""
    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        if not world:
            return {"results": [], "errors": ["The world no longer exists."]}
        return await _mr.suggest(db, world, chunk, style=options.get("style", ""), preset=options.get("preset", ""),
                                 pictures=bool(options.get("pictures")), listen=bool(options.get("listen")))
    finally:
        db.close()


def _set(run_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        run = db.get(MediaRenameRun, run_id)
        if run:
            for k, v in fields.items():
                setattr(run, k, v)
            db.commit()
    finally:
        db.close()


async def _run(run_id: int) -> None:
    try:
        _set(run_id, status="running", error="")
        while True:
            if _job_shutdown.stopping():
                _set(run_id, status="interrupted", error=_INTERRUPTED_NOTE)
                return
            db = SessionLocal()
            try:
                run = db.get(MediaRenameRun, run_id)
                if not run:
                    return
                items = json.loads(run.items_json or "[]")
                options = json.loads(run.options_json or "{}")
                world_id, done = run.world_id, run.done
            finally:
                db.close()
            if done >= len(items):
                break
            chunk = items[done:done + chunk_size(options)]
            out = await _process_chunk(world_id, chunk, options)
            db = SessionLocal()
            try:
                run = db.get(MediaRenameRun, run_id)
                if not run:
                    return
                results = json.loads(run.results_json or "[]") + out.get("results", [])
                errors = (json.loads(run.errors_json or "[]") + out.get("errors", []))[:200]
                # items the model could not be asked about at all still count as handled, so the run always ends
                run.results_json, run.errors_json = json.dumps(results), json.dumps(errors)
                run.done = done + len(chunk)
                db.commit()
            finally:
                db.close()
        _set(run_id, status="done")
    except asyncio.CancelledError:
        # synchronous only from here: a second cancellation cannot land inside it
        if _job_shutdown.stopping():
            _set(run_id, status="interrupted", error=_INTERRUPTED_NOTE)
        else:
            _set(run_id, status="cancelled", error="Cancelled.")
        raise
    except Exception as exc:
        _log.exception("media rename run %s failed", run_id)
        _set(run_id, status="error", error=f"{type(exc).__name__}: {exc}")


def cancel_run(run_id: int) -> bool:
    """Stop a running run, keeping the proposals so far. False when it is not running in this process."""
    task = _running_tasks.get(run_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def restart_run(run_id: int) -> bool:
    """Carry on a cancelled / failed / interrupted run from its last saved chunk (what is done is kept). False when it
    is already running, finished, or unknown."""
    task = _running_tasks.get(run_id)
    if task and not task.done():
        return False
    db = SessionLocal()
    try:
        run = db.get(MediaRenameRun, run_id)
        if not run or run.status not in ("cancelled", "error", "interrupted"):
            return False
        run.status, run.error = "pending", ""
        db.commit()
    finally:
        db.close()
    _start(run_id)
    return True


def delete_run(run_id: int) -> bool:
    """Remove a finished run. False (a no-op) while it is in progress, or for an unknown id."""
    db = SessionLocal()
    try:
        run = db.get(MediaRenameRun, run_id)
        if not run or run.status in IN_PROGRESS_STATUSES:
            return False
        db.delete(run)
        db.commit()
        return True
    finally:
        db.close()


def live_tasks() -> list:
    return list(_running_tasks.values())


def _mark_in_progress_interrupted() -> None:
    db = SessionLocal()
    try:
        stuck = db.query(MediaRenameRun).filter(MediaRenameRun.status.in_(IN_PROGRESS_STATUSES)).all()
        for run in stuck:
            run.status, run.error = "interrupted", _INTERRUPTED_NOTE
        if stuck:
            db.commit()
    finally:
        db.close()


def mark_stragglers_interrupted() -> None:
    """After job_shutdown.drain(): anything still mid-flight is left resumable."""
    _mark_in_progress_interrupted()


def sweep_interrupted_runs() -> None:
    """At startup: a run still mid-flight when the process last died has no task now - mark it interrupted."""
    _mark_in_progress_interrupted()


def resume_interrupted_runs() -> int:
    """At startup, after the sweep: carry on every interrupted run from its saved progress, up to
    job_shutdown.MAX_AUTO_RESUMES times each (then it is marked an error so a run that crashes the process cannot loop
    forever). Returns how many were restarted."""
    db = SessionLocal()
    try:
        ids = [r.id for r in db.query(MediaRenameRun).filter(MediaRenameRun.status == "interrupted").all()]
    finally:
        db.close()
    resumed = 0
    for run_id in ids:
        db = SessionLocal()
        try:
            run = db.get(MediaRenameRun, run_id)
            if not run or run.status != "interrupted":
                continue
            if run.resumed_count >= _job_shutdown.MAX_AUTO_RESUMES:
                run.status = "error"
                run.error = f"Interrupted by a server restart {run.resumed_count} times in a row - start a new run once the server is stable."
                db.commit()
                continue
            try:
                json.loads(run.items_json or "[]")
                json.loads(run.options_json or "{}")
                json.loads(run.results_json or "[]")
            except ValueError:
                run.status, run.error = "error", "Stored run data was corrupt (unreadable JSON) and could not be resumed."
                db.commit()
                _log.exception("MediaRenameRun %s has a corrupt JSON blob; marked error instead of resuming", run_id)
                continue
            run.status, run.error, run.resumed_count = "pending", "", run.resumed_count + 1
            db.commit()
        finally:
            db.close()
        _start(run_id)
        resumed += 1
    return resumed


def _ref_key(entry) -> str:
    return f"{entry.get('kind')}:{entry.get('url') if entry.get('kind') == 'image' else entry.get('id')}"


def forget_results(db, world_id: int, run_id, refs: list) -> None:
    """The GM dealt with these proposals (applied or skipped): drop them from the run, and drop a finished run that has
    nothing left to review. A missing / foreign / malformed run id is ignored."""
    try:
        run = db.query(MediaRenameRun).filter(MediaRenameRun.id == int(run_id), MediaRenameRun.world_id == world_id).first()
    except (TypeError, ValueError):
        return
    if not run:
        return
    gone = {_ref_key(r) for r in refs if isinstance(r, dict)}
    try:
        results = json.loads(run.results_json or "[]")
    except ValueError:
        return
    kept = [r for r in results if _ref_key(r) not in gone]
    run.results_json = json.dumps(kept)
    if not kept and run.status not in IN_PROGRESS_STATUSES:
        db.delete(run)
    db.commit()
