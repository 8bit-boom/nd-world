"""Process-wide "we're stopping" coordination shared by the three
background-job engines (app/audio_jobs.py, app/image_jobs.py,
app/chat_jobs.py) and the chunk loops in app/ai.py they drive.

Why this exists: a routine `git pull && docker compose up -d --build` (or
`docker compose restart`) used to just kill whatever background job was
mid-flight — a Whisper transcription chunk can take minutes on CPU, so no
Docker stop grace period can wait for one to actually finish. The fix isn't
"wait longer", it's "checkpoint continuously, and on shutdown stop fast":

  Phase A — request_stop() flips a process-wide flag and app.main's shutdown
  handler waits up to STOP_GRACE_SECONDS. A job's chunk loop in app/ai.py
  checks stopping() at each chunk boundary and raises JobInterrupted if it's
  set, so a job that happens to land on a boundary inside the grace window
  exits cleanly with a complete checkpoint already saved. Most won't — that's
  fine, the checkpoint from the last completed chunk is still there.

  Phase B — drain() cancels whatever's still running and waits a bounded
  settle window for each task's own (synchronous) CancelledError handler to
  persist its final status.

  Phase C — belt-and-braces: each engine's own mark_stragglers_interrupted()
  sweeps anything still mid-flight in the DB after drain() returns, same
  shape as the boot-time sweep_interrupted_jobs() for an unclean death
  (SIGKILL, OOM, power loss) where no handler ran at all.

On asyncio.shield: deliberately not used anywhere in this design. Every
checkpoint is written on the success path after a chunk completes, never
inside a cancellation handler — so there's no "write while being cancelled"
moment to protect. The CancelledError handlers themselves only call a fully
synchronous status-set (SessionLocal -> setattr -> commit -> close, no
`await`), and cancellation only ever delivers at an `await` point, so a
handler with no `await` in it cannot itself be interrupted by a second
cancellation. Keep it that way — adding an `await` to a cancellation-handling
path would silently reintroduce the need for a shield there.
"""
import asyncio
import logging
import os

_log = logging.getLogger("nd.job_shutdown")

# How long app.main's shutdown handler waits for in-flight jobs to reach a
# checkpoint boundary on their own before cancelling them outright. NOT "wait
# for the job to finish" — a single Whisper chunk can take minutes, so raising
# this well past a few seconds buys almost nothing (the checkpoint already
# covers the rest) while eating into the deployment's stop_grace_period
# budget. Tests set this to 0 (see tests/conftest.py) — the TestClient enters
# and exits the ASGI lifespan once per test, and several tests deliberately
# leave a hanging background task in flight.
STOP_GRACE_SECONDS = max(0.0, float(os.environ.get("ND_JOB_STOP_GRACE_SECONDS", "5")))

# Phase B's own bounded wait for a cancelled task's synchronous cleanup to
# actually run after cancel() — not configurable, this is bookkeeping
# overhead (one DB write), not job work, so it doesn't need to scale with
# STOP_GRACE_SECONDS.
_CANCEL_SETTLE_SECONDS = 3.0

# How many times an interrupted job auto-resumes itself on a subsequent boot
# before giving up and surfacing an error instead — bounds a job whose
# interruption is actually caused by something that crashes the process
# itself (e.g. a pathological input) to a few attempts, not an infinite
# crash-loop-and-retry on every restart.
MAX_AUTO_RESUMES = 3

_stopping = False


class JobInterrupted(Exception):
    """Raised out of a chunk loop in app/ai.py when stopping() went true at a
    chunk boundary. Deliberately NOT a subclass of asyncio.CancelledError —
    that's a BaseException with propagation semantics asyncio itself relies
    on, and re-purposing it here would risk a job engine's `except
    asyncio.CancelledError` catching this instead of the dedicated handling
    it needs (interrupted-with-a-clean-checkpoint is a different outcome from
    cancelled-mid-chunk-by-a-hard-cancel). This is an ordinary exception a
    job engine catches and turns into status="interrupted"."""


def stopping() -> bool:
    """True once the shutdown handler has called request_stop(). Checked by
    app/ai.py's chunk loops at each chunk boundary, and by each job engine's
    CancelledError handler to distinguish a shutdown-driven cancellation
    (-> "interrupted", resumable) from a GM clicking Cancel (-> "cancelled",
    not auto-resumed)."""
    return _stopping


def request_stop() -> None:
    """Called once, at the start of app.main's shutdown handler."""
    global _stopping
    _stopping = True


def clear_stop() -> None:
    """Reset the flag for the next boot. Real deployments only ever start
    once per process, so this only matters for tests: tests/conftest.py's
    `client` fixture re-enters the ASGI lifespan once per test, and without
    resetting here, stopping() would stay true for the lifetime of the test
    process after the very first test's shutdown — a chunk loop in any later
    test would see stopping() already true and raise JobInterrupted
    immediately instead of running normally."""
    global _stopping
    _stopping = False


async def drain(tasks: list[asyncio.Task], grace: float | None = None) -> int:
    """Phases A/B of shutdown for one job engine's still-running tasks.
    Waits up to `grace` (STOP_GRACE_SECONDS if not given) for `tasks` to
    finish on their own, then cancels whatever's left and waits a bounded
    _CANCEL_SETTLE_SECONDS for each cancelled task's own synchronous
    CancelledError handler to persist its final status. Returns how many
    tasks had to be cancelled (0 if all of them finished inside the grace
    window, or `tasks` was empty — the overwhelming common case, including
    roughly one call per test in the full suite). Never raises: a task whose
    handler swallows cancellation and never actually completes is logged,
    not awaited forever — app.main's shutdown must still return so the
    process can exit."""
    if not tasks:
        return 0

    # A task whose own event loop is already closed can never run or finish
    # again — in the test suite a previous test's TestClient portal loop can
    # leave a cancelled-but-never-settled job task in an engine's registry,
    # and handing it to asyncio.wait below would burn the whole settle window
    # on a task that cancel() cannot even reach (it raises through the task's
    # own closed loop). Exclude them up front; mark_stragglers_interrupted()
    # is the fallback for whatever DB state they were in when their loop died.
    live = [t for t in tasks if not t.get_loop().is_closed()]
    stranded = len(tasks) - len(live)
    if stranded:
        _log.warning(
            "%d job task(s) belong to an already-closed event loop and can never "
            "settle; treating them as shutdown stragglers",
            stranded,
        )
    if not live:
        return stranded

    _done, pending = await asyncio.wait(live, timeout=STOP_GRACE_SECONDS if grace is None else grace)
    if not pending:
        return stranded

    for task in pending:
        task.cancel()
    _settled, still_pending = await asyncio.wait(pending, timeout=_CANCEL_SETTLE_SECONDS)
    if still_pending:
        _log.warning(
            "%d job task(s) did not settle within %.1fs of being cancelled during shutdown "
            "— their own cleanup handler may not have run; mark_stragglers_interrupted() "
            "is the fallback for whatever state they left in the DB",
            len(still_pending), _CANCEL_SETTLE_SECONDS,
        )
    return len(pending) + stranded
