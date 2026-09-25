"""Automatic evidence capture for "the site stopped responding" incidents.

nd-world runs as a single uvicorn worker whose every route is `async def`
over a *sync* SQLAlchemy session — so when something makes a database call
wait (a starved connection pool, a SQLite write-lock pileup), the one event
loop blocks and the whole app freezes WITHOUT logging anything: unhandled
exceptions produce tracebacks, but a hang is silent. Docker logs also only
show what the process printed before it died, and `docker restart` — the
usual remedy — destroys the in-process state that would explain the hang.

This module adds three always-on, zero-config watchers:

1. Event-loop stall watchdog. An async heartbeat task on the app's own
   event loop stamps `time.monotonic()` once a second; a daemon thread
   checks the stamp every 2s. If it goes stale past
   ND_DIAG_STALL_SECONDS (default 15s), the thread writes a full dump —
   every thread's stack, a snapshot of the asyncio tasks taken from
   inside the loop, the SQLAlchemy pool status, and RSS — to a file under
   the diagnostics directory, and logs an ERROR naming that file. This is
   the py-spy-style "where is it stuck" answer, captured at the moment it
   is stuck instead of never.

2. Connection-pool pressure watcher. Same thread; warns (and dumps) when
   checked-out connections reach the pool's base size, i.e. before the
   (5 + 10 overflow) default pool is truly exhausted and requests start
   blocking 30s each on checkout — the slow-building variant of the same
   outage that a loop stall alone wouldn't catch until too late.

3. A persistent restart journal. Every startup/shutdown appends one line
   to `<diagnostics dir>/events.log` on the /data volume. After an
   unexplained outage this distinguishes the three classes immediately:
   a `startup` line with no `shutdown` before it = the process was killed
   (OOM/SIGKILL/watchtower recreation), while a clean shutdown with no
   subsequent startup = it never came back. Stall/dump/pool events go to
   the same journal, so one file reconstructs the whole incident.

Settings → Diagnostics (GM-only) renders the journal tail, the dump list,
and the current watcher status; `/admin/diagnostics/events` and
`/admin/diagnostics/dump/{name}` serve the full files as plain text.

All file writes are best-effort (unwritable dir → the watchers still log
to stdout/stderr, which docker captures); nothing here may ever raise into
the app. Configuration (all env, all read at call time so tests can
monkeypatch):

- ND_DIAG_DISABLED — "1"/"true" turns the whole module off
- ND_DIAG_DIR — defaults to `<dirname of DB_PATH>/diagnostics`, i.e.
  /data/diagnostics in the standard container layout, which is a bind
  mount on self-hosted deployments so dumps survive container restarts
- ND_DIAG_STALL_SECONDS — loop-stall dump threshold, default 15
- ND_DIAG_DUMP_COOLDOWN_SECONDS — min seconds between dump files
  (a wedged loop stays wedged; one dump per episode window is enough),
  default 300
- ND_DIAG_POOL_WARN_COOLDOWN_SECONDS — min seconds between pool-pressure
  warnings, default 300
"""
import asyncio
import contextlib
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("nd.diag")

_CHECK_INTERVAL = 2.0        # watchdog thread poll period
_HEARTBEAT_INTERVAL = 1.0    # loop task beat period
_TASK_SNAPSHOT_EVERY = 10    # beats between asyncio-task snapshot refreshes
_DUMP_STACK_LIMIT = 12       # frames per thread in a dump
_DUMP_NAME_RE = re.compile(r"^wedge-\d{8}-\d{6}(?:-\d+)?\.txt$")


# Module state. `_last_beat`/`_ready` are written by the event loop thread
# and read by the watchdog thread; both are single-word writes/reads
# (CPython assigns/reads atomically), which is the whole synchronization
# this needs — a torn value would at worst produce one spurious or missed
# 2s check, and both watchers rate-limit their side effects anyway.
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_heartbeat_task: asyncio.Task | None = None
_last_beat: float = 0.0
_ready = False               # True only once the first beat landed (a
                             # not-yet-beaten heart must not read as a stall)
_beats = 0
_task_snapshot: list[str] = []
_stall_since: float | None = None
# None = "hasn't happened yet". NOT 0.0: time.monotonic() starts near zero on
# a freshly booted machine/container, and `now - 0.0 < cooldown` would then
# read as "still cooling down" — silently suppressing the FIRST dump/warn of
# a process's life for up to the whole cooldown window (exactly when a
# watchtower/OOM recreation loop needs evidence most).
_last_dump: float | None = None
_last_pool_warn: float | None = None
_pool_saturated = False      # last observed at/past base size — set when the
                             # pool-recovered journal line has a left edge


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _disabled() -> bool:
    return os.environ.get("ND_DIAG_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def diag_dir() -> Path | None:
    """The diagnostics directory, created on demand. None (files disabled)
    if it can't be created — the stall/pool watchers then degrade to plain
    logging, which docker still captures."""
    raw = os.environ.get("ND_DIAG_DIR", "").strip()
    if not raw:
        # Lazy import: app.database reads DB_PATH from the environment at
        # import time, and reading it through the module (rather than
        # caching it here) keeps the default tied to whatever the app
        # actually settled on — same pattern as backups._default_db_path.
        from .database import DB_PATH
        raw = str(Path(DB_PATH).parent / "diagnostics")
    try:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return None


def _journal(event: str, **fields) -> None:
    d = diag_dir()
    if d is None:
        return
    parts = [datetime.now(timezone.utc).isoformat(timespec="seconds"), event]
    parts += [f"{k}={v}" for k, v in fields.items()]
    try:
        with (d / "events.log").open("a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except OSError:
        pass


def _rss() -> str:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return line.split(None, 1)[1].strip()
    except OSError:
        pass
    return "n/a"


def _pool_status_line() -> str:
    try:
        from .database import engine
        return f"sqlalchemy pool: {engine.pool.status()}"
    except Exception as exc:  # engine not importable in some test shape — say so
        return f"sqlalchemy pool: unavailable ({exc!r})"


def _pool_counts() -> tuple[int, int] | None:
    """(checked_out, base_size) — None if the pool can't be introspected."""
    try:
        from .database import engine
        # SQLAlchemy 2.x Pool.checkedout() returns the count as an int
        # (1.x returned a sequence of connections — len() of the int raised
        # TypeError, which turned the whole watcher silently "unavailable").
        checked = engine.pool.checkedout()
        if not isinstance(checked, int):
            checked = len(checked)
        return checked, engine.pool.size()
    except Exception:
        return None


def _fmt_task(t: asyncio.Task) -> str:
    coro = t.get_coro()
    name = getattr(coro, "__qualname__", None) or repr(coro)
    loc = ""
    try:
        stack = t.get_stack(limit=1)
        if stack:
            fr = stack[-1]
            loc = f" at {fr.f_code.co_filename.rsplit('/', 1)[-1]}:{fr.f_lineno} in {fr.f_code.co_name}"
    except Exception:
        pass
    return f"{t.get_name()}: {name}{loc}"


def _live_task_lines() -> list[str]:
    """Best-effort task listing straight from the loop reference. During a
    stall the loop is frozen so this iteration is stable; while the loop is
    merely pool-saturated it may raise (set mutated mid-iteration), which
    the snapshot taken inside the loop covers instead."""
    if _loop is None:
        return []
    try:
        return [_fmt_task(t) for t in asyncio.all_tasks(_loop)]
    except Exception:
        return []


def _write_dump(reason: str, lag: float | None = None) -> Path | None:
    d = diag_dir()
    if d is None:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = d / f"wedge-{stamp}.txt"
    n = 1
    while path.exists():
        # Two dumps inside the same second (a flapping stall with the
        # cooldown set to 0) must not overwrite each other — same pattern
        # as backups.create_snapshot's snapshot-name disambiguation.
        path = d / f"wedge-{stamp}-{n}.txt"
        n += 1
    lines = [
        f"nd-world diagnostic dump — {reason}",
        f"time: {datetime.now(timezone.utc).isoformat()}",
    ]
    if lag is not None:
        lines.append(f"event loop stalled for: {lag:.1f}s")
    lines.append(f"pid: {os.getpid()}  python: {sys.version.split()[0]}  rss: {_rss()}")
    lines.append(_pool_status_line())
    lines.append("")
    lines.append(f"== asyncio tasks (snapshot from inside the loop, ≤{_TASK_SNAPSHOT_EVERY}s stale) ==")
    lines += _task_snapshot or ["(empty snapshot)"]
    live = _live_task_lines()
    if live:
        lines.append("")
        lines.append("== asyncio tasks (live view from watchdog thread) ==")
        lines += live
    lines.append("")
    lines.append("== all thread stacks ==")
    names = {t.ident: t.name for t in threading.enumerate()}
    for tid, frame in sys._current_frames().items():
        lines.append(f"-- thread {names.get(tid, tid)} --")
        lines += traceback.format_stack(frame, limit=_DUMP_STACK_LIMIT)
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def _maybe_dump(reason: str, lag: float | None = None, **journal_fields) -> None:
    """Dump + journal + log, once per ND_DIAG_DUMP_COOLDOWN_SECONDS window."""
    global _last_dump
    now = time.monotonic()
    if _last_dump is not None and now - _last_dump < _env_float("ND_DIAG_DUMP_COOLDOWN_SECONDS", 300.0):
        return
    _last_dump = now
    path = _write_dump(reason, lag=lag)
    if path is None:
        _log.error("diagnostics: %s detected but the dump could not be written (dir unwritable?)", reason)
        _journal(f"{reason}-undumpable", **journal_fields)
        return
    _log.error("diagnostics: %s — full stack dump written to %s", reason, path)
    _journal("dump-written", reason=reason, file=path.name, **journal_fields)


def _evaluate(now: float) -> None:
    """One watchdog check of the heartbeat's staleness. Kept as a plain
    function of `now` so tests can drive the stall/recover/cooldown state
    machine without a real stalled loop."""
    global _stall_since
    if not _ready:
        return
    lag = now - _last_beat
    threshold = _env_float("ND_DIAG_STALL_SECONDS", 15.0)
    if lag >= threshold:
        if _stall_since is None:
            _stall_since = now - lag  # first sighting of an episode that began ~lag ago
            _log.warning("Event loop stalled %.1fs (threshold %.0fs)", lag, threshold)
            _journal("loop-stalled", seconds=f"{lag:.1f}")
        _maybe_dump("event-loop-stall", lag=lag, seconds=f"{lag:.1f}")
    elif _stall_since is not None:
        duration = now - _stall_since
        _stall_since = None
        _log.warning("Event loop recovered after a %.1fs stall", duration)
        _journal("loop-recovered", seconds=f"{duration:.1f}")


def _pool_check(now: float) -> None:
    """One watchdog check of connection-pool pressure. `checked_out >= size`
    means the pool is dipping into its overflow — every further checkout is
    one of the finite 10 overflow slots away from blocking requests 30s
    each (SQLAlchemy's default pool_timeout), which with this app's
    async-routes-over-sync-sessions architecture freezes the event loop
    30s at a time. Warn and dump while there's still headroom. Pressure
    clearing journals a matching pool-recovered line, so events.log shows
    both edges of the episode.

    Gated on _ready like _evaluate: the watchers run only while a lifespan
    is active, which in production is always (the first heartbeat lands
    milliseconds after startup) and in the test suite is never between
    TestClient boots — so a background thread iteration can never race a
    white-box test's own direct _pool_check call."""
    global _last_pool_warn, _pool_saturated
    if not _ready:
        return
    counts = _pool_counts()
    if counts is None:
        return
    checked_out, size = counts
    if not size or checked_out < size:
        if _pool_saturated:
            _pool_saturated = False
            _journal("pool-recovered", checked_out=checked_out, size=size)
        return
    _pool_saturated = True
    if _last_pool_warn is not None and now - _last_pool_warn < _env_float("ND_DIAG_POOL_WARN_COOLDOWN_SECONDS", 300.0):
        return
    _last_pool_warn = now
    _log.warning(
        "DB connection pool at or past base size: %d/%d checked out — "
        "slow requests will queue behind pool checkout",
        checked_out, size,
    )
    _journal("pool-saturated", checked_out=checked_out, size=size)
    _maybe_dump("pool-saturated", checked_out=checked_out, size=size)


def _watchdog_loop() -> None:
    # Runs for the process's whole lifetime (daemon thread, never joined):
    # the TestClient re-enters the app lifespan ~1000x in the test suite,
    # so — unlike backups' scheduler thread — this one must survive across
    # startup/shutdown cycles. _ready gates it to no-ops whenever no
    # lifespan is active.
    while True:
        time.sleep(_CHECK_INTERVAL)
        try:
            now = time.monotonic()
            _evaluate(now)
            _pool_check(now)
        except Exception:
            _log.exception("diagnostics watchdog iteration failed")


async def _heartbeat() -> None:
    global _last_beat, _ready, _beats, _task_snapshot
    while True:
        _last_beat = time.monotonic()
        _ready = True
        _beats += 1
        if _beats >= _TASK_SNAPSHOT_EVERY:
            _beats = 0
            try:
                _task_snapshot = [
                    _fmt_task(t) for t in asyncio.all_tasks()
                    if t is not asyncio.current_task()
                ]
            except RuntimeError:
                pass  # keep the previous snapshot rather than fail the beat
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


def start() -> None:
    """Called from main._startup_tasks (inside the running lifespan). Spawns
    the watchdog thread once per process and a heartbeat task once per
    lifespan, then journals the startup — the journal line is what lets a
    post-incident events.log tell a watchtower/OOM recreation (startup with
    no preceding shutdown) apart from a clean restart."""
    global _thread, _loop, _heartbeat_task, _ready, _stall_since
    if _disabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # started outside a loop: no stall detection, but the
                     # journal and pool watcher still work
    _loop = loop
    if loop is not None and (_heartbeat_task is None or _heartbeat_task.done()):
        _ready = False
        _stall_since = None
        _heartbeat_task = loop.create_task(_heartbeat())
    if _thread is None:
        _thread = threading.Thread(target=_watchdog_loop, name="nd-diagnostics", daemon=True)
        _thread.start()
    _journal(
        "startup",
        pid=os.getpid(),
        python=sys.version.split()[0],
        stall_seconds=_env_float("ND_DIAG_STALL_SECONDS", 15.0),
    )


async def stop() -> None:
    """Called from main._shutdown_tasks — async (unlike backups.stop) so the
    heartbeat task can be cancelled AND awaited to completion inside the
    still-running loop, rather than leaving a pending-destroyed task for
    the loop's final tick (the TestClient closes its loop right after
    lifespan shutdown, and a just-cancelled-but-unreaped task would warn
    ~1000x across the suite). Journals the shutdown for restart forensics."""
    global _heartbeat_task, _ready, _stall_since
    if _disabled():
        # Symmetric with start(): a disabled deployment must journal
        # neither half of the pair, or events.log would show a shutdown
        # with no startup — the exact signature of a killed process.
        return
    task = _heartbeat_task
    _heartbeat_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _ready = False
    _stall_since = None
    _journal("shutdown", pid=os.getpid())


# ── Read-only viewer surface: Settings → Diagnostics + /admin/diagnostics ────
# Everything below is passive introspection for the GM-only tab and routes;
# none of it touches watcher state.


def status() -> dict:
    """One snapshot for the Settings → Diagnostics tab: whether the watchdog
    is on, where its files land, the configured thresholds, and the pool's
    state right now. Runs on every /settings render, so it stays cheap."""
    d = diag_dir()
    counts = _pool_counts()
    return {
        "enabled": not _disabled(),
        "dir": str(d) if d is not None else None,
        "stall_seconds": _env_float("ND_DIAG_STALL_SECONDS", 15.0),
        "dump_cooldown_seconds": _env_float("ND_DIAG_DUMP_COOLDOWN_SECONDS", 300.0),
        "pool_warn_cooldown_seconds": _env_float("ND_DIAG_POOL_WARN_COOLDOWN_SECONDS", 300.0),
        "pool_checked_out": counts[0] if counts else None,
        "pool_size": counts[1] if counts else None,
        "watchdog_thread_alive": _thread is not None and _thread.is_alive(),
        "heartbeat_active": _heartbeat_task is not None and not _heartbeat_task.done(),
    }


def list_dumps() -> list[dict]:
    """Dump files newest-first: {name, size, modified}. Empty when files are
    disabled or the directory can't be listed."""
    d = diag_dir()
    if d is None:
        return []
    out = []
    try:
        for p in d.iterdir():
            if not _DUMP_NAME_RE.match(p.name) or not p.is_file():
                continue
            st = p.stat()
            out.append({
                "name": p.name,
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            })
    except OSError:
        return []
    out.sort(key=lambda item: item["modified"], reverse=True)
    return out


def read_events(limit: int | None = 200) -> list[str]:
    """Tail of events.log, oldest→newest; `limit=None` reads the whole file
    (what /admin/diagnostics/events serves). Empty list when files are
    disabled or nothing has been journaled yet."""
    d = diag_dir()
    if d is None:
        return []
    try:
        lines = (d / "events.log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines if limit is None else lines[-limit:]


def read_dump(name: str) -> str | None:
    """One dump file's text, or None. `name` must match the exact wedge-dump
    filename pattern AND resolve inside the diagnostics dir — the same
    containment idea as /uploads' serve_upload, so an encoded ../ traversal
    or a non-dump file (events.log, world.db, ...) can't be served through
    the dump route."""
    d = diag_dir()
    if d is None or not _DUMP_NAME_RE.match(name):
        return None
    try:
        root = d.resolve()
        path = (d / name).resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
