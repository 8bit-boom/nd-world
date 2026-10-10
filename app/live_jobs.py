"""A register of the in-memory AI jobs, so the Background Jobs page can list, cancel and restart them.

The durable job engines (audio / chat / image / video / media rename) keep their own database rows. The rest - a map
build, the character draft, an auto-tag run, ... - are an asyncio task plus a small status dict in a router module. They
all run through `ai_queue.serialized(label, kind, store=<that dict>)`, which calls into this leaf module (no router or
ai_queue imports, so anything may import it): each run is recorded with the arguments it was started with, which is
exactly what a restart needs.

Everything here is in memory, like the jobs themselves - a server restart forgets them.
"""
import asyncio
import secrets
import time
from typing import Optional

RETAIN_SECONDS = 6 * 3600
MAX_RETAINED = 30                     # finished records kept (each holds its start arguments, so keep it small)


class LiveJob:
    __slots__ = ("id", "label", "store", "job_id", "wrapper", "args", "kwargs", "initial", "status", "started",
                 "finished", "error", "task")

    def __init__(self, label, store, wrapper, args, kwargs):
        self.id = secrets.token_urlsafe(8)
        self.label = label
        self.store = store
        self.job_id = args[0]
        self.wrapper = wrapper
        self.args, self.kwargs = args, kwargs
        entry = store.get(self.job_id)
        # What the job's own status dict looked like when it was started - a restart puts it back so the pollers that
        # expect its keys (user_id, draft, ...) keep working.
        self.initial = dict(entry) if isinstance(entry, dict) else {"status": "running", "error": ""}
        self.status = "queued"        # queued | running | done | error | cancelled
        self.started = time.time()
        self.finished: Optional[float] = None
        self.error = ""
        self.task: Optional[asyncio.Task] = None

    def view(self) -> dict:
        end = self.finished or time.time()
        return {"id": self.id, "label": self.label, "status": self.status, "error": self.error,
                "started": self.started, "elapsed": int(end - self.started),
                "can_cancel": self.status in ("queued", "running"),
                "can_restart": self.status not in ("queued", "running")}


_JOBS: dict = {}
_STRONG: set = set()                  # restarted tasks must not be garbage collected mid-run


def _sweep() -> None:
    now = time.time()
    for rec in [r for r in _JOBS.values() if r.finished and now - r.finished > RETAIN_SECONDS]:
        _JOBS.pop(rec.id, None)
    finished = sorted((r for r in _JOBS.values() if r.finished), key=lambda r: r.finished)
    for rec in finished[: max(0, len(finished) - MAX_RETAINED)]:
        _JOBS.pop(rec.id, None)


def begin(label: str, store, wrapper, args: tuple, kwargs: dict) -> Optional[LiveJob]:
    """Record a run that is about to wait for its turn; None when it cannot be tracked (no status dict / no job id)."""
    if store is None or not args or not isinstance(args[0], int):
        return None
    _sweep()
    rec = LiveJob(label, store, wrapper, args, kwargs)
    rec.task = asyncio.current_task()
    _JOBS[rec.id] = rec
    return rec


def running(rec: Optional[LiveJob]) -> None:
    if rec is not None:
        rec.status = "running"


def _settle(rec: LiveJob, status: str, error: str = "") -> None:
    rec.status, rec.error, rec.finished = status, error, time.time()


def done(rec: Optional[LiveJob]) -> None:
    """The runner returned: its own status dict says whether it worked (the runners catch their own errors)."""
    if rec is None:
        return
    entry = rec.store.get(rec.job_id)
    if isinstance(entry, dict) and entry.get("status") == "error":
        _settle(rec, "error", str(entry.get("error") or "Failed."))
    else:
        _settle(rec, "done")


def failed(rec: Optional[LiveJob], exc: BaseException) -> None:
    if rec is None:
        return
    msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    entry = rec.store.get(rec.job_id)
    if isinstance(entry, dict) and entry.get("status") == "running":
        entry.update(status="error", error=msg)
    _settle(rec, "error", msg)


def cancelled(rec: Optional[LiveJob]) -> None:
    """The task was cancelled (by the GM, or by the server stopping): its pollers must stop waiting for it."""
    if rec is None:
        return
    entry = rec.store.get(rec.job_id)
    if isinstance(entry, dict) and entry.get("status") == "running":
        entry.update(status="error", error="Cancelled.")
    _settle(rec, "cancelled", "Cancelled.")


# ── what the Background Jobs page uses ───────────────────────────────────────────────────────────────────────────
def all_jobs() -> list:
    _sweep()
    return sorted(_JOBS.values(), key=lambda r: -r.started)


def get(job_key: str) -> Optional[LiveJob]:
    return _JOBS.get(job_key)


def cancel(rec: LiveJob) -> bool:
    if rec.status not in ("queued", "running") or rec.task is None or rec.task.done():
        return False
    rec.task.cancel()
    return True


def restart(rec: LiveJob) -> bool:
    """Run it again from the same arguments, under the same job id (so a page that still polls it sees the new run)."""
    if rec.status in ("queued", "running"):
        return False
    rec.store[rec.job_id] = dict(rec.initial, status="running", error="", started=time.time())
    _JOBS.pop(rec.id, None)
    task = asyncio.get_running_loop().create_task(rec.wrapper(*rec.args, **rec.kwargs))
    _STRONG.add(task)
    task.add_done_callback(_STRONG.discard)
    return True


def forget(rec: LiveJob) -> bool:
    if rec.status in ("queued", "running"):
        return False
    _JOBS.pop(rec.id, None)
    return True
