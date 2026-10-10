"""The Background Jobs page's list of everything that runs on its own but is not an audio / chat / image / video job:
the AI buttons (app/ai_background.py tasks), the in-memory AI jobs - a map build, a character draft, auto-tag ... -
(app/live_jobs.py) and the bulk media-rename runs. One list, with Cancel / Restart / Remove for each.

A key says which kind a row is: `t:<task id>`, `m:<job record id>`, `r:<media-rename run id>`."""
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import ai_background as _bg
from .. import live_jobs as _live
from .. import media_rename_jobs as _rename
from ..database import get_db
from ..deps import get_world_ctx, world_can_edit_section, world_can_view_section
from ..models import MediaRenameRun, User

router = APIRouter()

_SECTION = "background_jobs"


def _world(request: Request, db: Session, active_world, write: bool):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    allowed = world_can_edit_section(request, world, _SECTION) if write else world_can_view_section(request, world, _SECTION)
    if not allowed:
        raise HTTPException(403)
    return world


def _row(key, source, label, status, started, finished=None, error="", detail="", can_cancel=False, can_restart=False,
         position=0, progress=""):
    end = finished or time.time()
    return {"key": key, "source": source, "label": label, "detail": detail, "status": status, "error": error,
            "started": started, "elapsed": max(0, int(end - started)), "position": position, "progress": progress,
            "can_cancel": can_cancel, "can_restart": can_restart, "can_remove": not can_cancel}


def _rename_status(run) -> str:
    return {"pending": "running"}.get(run.status, run.status)


@router.get("/api/live-jobs")
def list_live_jobs(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Everything the page lists besides the audio / chat / image / video jobs - running first, then newest."""
    world = _world(request, db, active_world, write=False)
    rows = []
    owners = {t.user_id for t in _bg.all_tasks()}
    names = {u.id: (u.display_name or u.email) for u in db.query(User).filter(User.id.in_(owners)).all()} if owners else {}
    for t in _bg.all_tasks():
        v = t.view()
        rows.append(_row("t:" + t.id, "task", t.label, t.status, t.started, t.finished,
                         error="" if t.status != "done" or t.http_status < 400 else f"Failed (HTTP {t.http_status}).",
                         detail="by " + names.get(t.user_id, "someone"), position=v.get("position", 0),
                         can_cancel=t.status in ("queued", "running"),
                         can_restart=t.status not in ("queued", "running") and t.rerun is not None))
    for rec in _live.all_jobs():
        rows.append(_row("m:" + rec.id, "memory", rec.label, rec.status, rec.started, rec.finished, error=rec.error,
                         can_cancel=rec.status in ("queued", "running"),
                         can_restart=rec.status not in ("queued", "running")))
    runs = (db.query(MediaRenameRun).filter(MediaRenameRun.world_id == world.id)
            .order_by(MediaRenameRun.id.desc()).limit(20).all())
    for run in runs:
        started = run.created_at.timestamp() if run.created_at else time.time()
        finished = run.updated_at.timestamp() if run.updated_at and run.status not in _rename.IN_PROGRESS_STATUSES else None
        status = _rename_status(run)
        rows.append(_row("r:%d" % run.id, "rename", "Bulk rename", status, started, finished, error=run.error or "",
                         progress=f"{run.done or 0} / {run.total or 0}",
                         can_cancel=run.status in _rename.IN_PROGRESS_STATUSES,
                         can_restart=run.status in ("cancelled", "error", "interrupted")))
    order = {"running": 0, "queued": 1}
    rows.sort(key=lambda r: (order.get(r["status"], 2), -r["started"]))
    return {"jobs": rows}


def _split(key: str):
    kind, _, ident = key.partition(":")
    if kind not in ("t", "m", "r") or not ident:
        raise HTTPException(404, "Unknown job")
    return kind, ident


def _run_or_404(db: Session, world, ident: str):
    try:
        run_id = int(ident)
    except ValueError:
        raise HTTPException(404, "Unknown job")
    run = db.query(MediaRenameRun).filter(MediaRenameRun.id == run_id, MediaRenameRun.world_id == world.id).first()
    if not run:
        raise HTTPException(404, "Unknown job")
    return run


@router.post("/api/live-jobs/{key}/cancel")
def cancel_live_job(key: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = _world(request, db, active_world, write=True)
    kind, ident = _split(key)
    if kind == "t":
        task = _bg._TASKS.get(ident)
        ok = bool(task) and _bg.cancel(task)
    elif kind == "m":
        rec = _live.get(ident)
        ok = bool(rec) and _live.cancel(rec)
    else:
        ok = _rename.cancel_run(_run_or_404(db, world, ident).id)
    if not ok:
        raise HTTPException(400, "That job is not running any more.")
    return {"ok": True}


@router.post("/api/live-jobs/{key}/restart")
async def restart_live_job(key: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Run it again: a task from its saved request, an in-memory job from the arguments it started with, a bulk rename
    from its last saved chunk."""
    world = _world(request, db, active_world, write=True)
    kind, ident = _split(key)
    if kind == "t":
        task = _bg._TASKS.get(ident)
        ok = bool(task) and _bg.restart(task) is not None
    elif kind == "m":
        rec = _live.get(ident)
        ok = bool(rec) and _live.restart(rec)
    else:
        ok = _rename.restart_run(_run_or_404(db, world, ident).id)
    if not ok:
        raise HTTPException(400, "That job cannot be restarted (it is still running, or the server no longer has it).")
    return {"ok": True}


@router.delete("/api/live-jobs/{key}")
def remove_live_job(key: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Drop a finished / cancelled / failed job from the list. A running one has to be cancelled first."""
    world = _world(request, db, active_world, write=True)
    kind, ident = _split(key)
    if kind == "t":
        task = _bg._TASKS.get(ident)
        ok = bool(task) and task.status not in ("queued", "running") and _bg._TASKS.pop(ident, None) is not None
    elif kind == "m":
        rec = _live.get(ident)
        ok = bool(rec) and _live.forget(rec)
    else:
        ok = _rename.delete_run(_run_or_404(db, world, ident).id)
    if not ok:
        raise HTTPException(400, "Cancel it first, or it is already gone.")
    return {"ok": True}
