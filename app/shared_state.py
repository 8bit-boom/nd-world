"""Live state that more than one process must see (docs/RUST_REWRITE_PLAN.md, phase 0).

During a move to a Rust server (Axum) in front of this one, both processes serve routes that touch the same live state: the per-world change
counter and the named signals the live stream announces, and the second screen's stage. Held in Python's memory that state would be split in
two, so it lives in one SQLite table, `shared_state(key, value, n)`, that both read and write:

    live:<world>            n = the world's change counter
    sig:<world>:<name>      n = a named signal for one world ("spot", "dice", "combat")
    sigall:<name>           n = a signal for every world
    stage:<world>           value = the second screen's stage (JSON), n = how often it was written

Counters (`bump`) are written by ONE background thread, never by the caller: `live.touch` is called from request handlers that may still hold
SQLite's write lock (waiting for it from a second connection would block until busy_timeout, 30 s), and a counter only has to be seen soon, not at
once. A bump is visible in this process immediately (it is counted as pending until written) and in the other process after the write plus
its next refresh (REFRESH_EVERY). Readers (`value`, `snapshot`) read a small in-memory copy of the table, refreshed at most every REFRESH_EVERY
seconds, so a live stream polling once a second per browser costs one query per refresh for the whole process, not one per browser.

The stage is read-modify-write, done synchronously with an optimistic compare-and-swap on `n` (the routes that write it hold no database lock).

`reset_local()` forgets this process's copy (tests, which recreate the table every time).
"""
import json
import logging
import threading
import time
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .database import engine

_log = logging.getLogger("nd.shared_state")

REFRESH_EVERY = 0.25            # seconds a read of the table may be old
_lock = threading.Lock()        # guards the dicts below (short)
_io = threading.Lock()          # serialises table reads / writes so a refresh never sees a write that is not booked yet
_db: dict = {}                  # key -> last n seen in the table
_pending: dict = {}             # key -> bumps not written yet
_inflight: dict = {}            # key -> bumps being written now
_refreshed = 0.0
_wake = threading.Event()
_writer: Optional[threading.Thread] = None
_writer_lock = threading.Lock()
_idle = threading.Event()
_idle.set()


# ── counters ─────────────────────────────────────────────────────────────────

def bump(key: str) -> None:
    """Count one change under `key`; never blocks on the database."""
    with _lock:
        _pending[key] = _pending.get(key, 0) + 1
        _idle.clear()
    _ensure_writer()
    _wake.set()


def _effective(key: str) -> int:
    return _db.get(key, 0) + _pending.get(key, 0) + _inflight.get(key, 0)


def value(key: str) -> int:
    """The counter's value as this process knows it now: what the table held at the last refresh plus its own bumps not written yet."""
    refresh()
    with _lock:
        return _effective(key)


def snapshot(prefix: str) -> dict:
    """{key: n} for every counter whose key starts with `prefix`."""
    refresh()
    with _lock:
        keys = {k for k in list(_db) + list(_pending) + list(_inflight) if k.startswith(prefix)}
        return {k: _effective(k) for k in keys}


def refresh(force: bool = False) -> None:
    """Read the counters from the table (at most every REFRESH_EVERY seconds unless forced)."""
    global _refreshed
    now = time.monotonic()
    if not force and now - _refreshed < REFRESH_EVERY:
        return
    with _io:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text("SELECT key, n FROM shared_state WHERE key NOT LIKE 'stage:%'")).fetchall()
        except Exception:
            _log.debug("shared_state refresh failed", exc_info=True)
            _refreshed = now
            return
        with _lock:
            for k, n in rows:
                if n > _db.get(k, 0):
                    _db[k] = n
            _refreshed = time.monotonic()


def _flush_once() -> bool:
    """Write what is pending in one transaction; False when there was nothing to write or the write failed (it is queued again)."""
    with _lock:
        if not _pending:
            return False
        batch = dict(_pending)
        _pending.clear()
        for k, c in batch.items():
            _inflight[k] = _inflight.get(k, 0) + c
    got = None
    with _io:
        for attempt in range(6):
            try:
                with engine.begin() as conn:
                    got = {k: conn.execute(text(
                        "INSERT INTO shared_state(key, n) VALUES (:k, :c) ON CONFLICT(key) DO UPDATE SET n = n + :c RETURNING n"),
                        {"k": k, "c": c}).scalar() for k, c in batch.items()}
                break
            except OperationalError:                         # a lock race lost beyond busy_timeout, or the table is not there yet
                got = None
                time.sleep(0.05 * (attempt + 1))
            except Exception:
                _log.warning("shared_state write failed", exc_info=True)
                got = None
                break
        with _lock:
            for k, c in batch.items():
                left = _inflight.get(k, 0) - c
                if left > 0:
                    _inflight[k] = left
                else:
                    _inflight.pop(k, None)
                if got is not None:
                    _db[k] = max(_db.get(k, 0), got[k])
                else:                                         # not written: counted as pending again, so the change is not lost
                    _pending[k] = _pending.get(k, 0) + c
    return got is not None


def _run():
    while True:
        _wake.wait(1.0)
        _wake.clear()
        try:
            while _flush_once():
                pass
        except Exception:
            _log.warning("shared_state writer failed", exc_info=True)
            time.sleep(0.2)
        with _lock:
            if not _pending and not _inflight:
                _idle.set()


def _ensure_writer():
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    with _writer_lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_run, name="nd-shared-state", daemon=True)
            _writer.start()


def flush(timeout: float = 5.0) -> bool:
    """Wait until every bump so far is in the table (tests, shutdown); True if it got there in time."""
    _wake.set()
    return _idle.wait(timeout)


# ── JSON values with compare-and-swap (the second screen's stage) ────────────

def get_json(key: str, default=None):
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT value FROM shared_state WHERE key = :k"), {"k": key}).fetchone()
    except Exception:
        return default
    if not row or row[0] is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def update_json(key: str, fn: Callable[[Optional[dict]], dict]) -> dict:
    """Atomically replace the JSON value under `key` with fn(old or None); retries when another process wrote in between."""
    for _ in range(50):
        with engine.begin() as conn:
            row = conn.execute(text("SELECT value, n FROM shared_state WHERE key = :k"), {"k": key}).fetchone()
            old = json.loads(row[0]) if row and row[0] else None
            new = fn(json.loads(json.dumps(old)) if old is not None else None)
            blob = json.dumps(new)
            if row is None:
                got = conn.execute(text("INSERT OR IGNORE INTO shared_state(key, value, n) VALUES (:k, :v, 1)"), {"k": key, "v": blob}).rowcount
            else:
                got = conn.execute(text("UPDATE shared_state SET value = :v, n = n + 1 WHERE key = :k AND n = :n"),
                                   {"k": key, "v": blob, "n": row[1]}).rowcount
        if got:
            return new
    raise RuntimeError("shared_state: could not update " + key)


def delete_prefix(prefix: str) -> None:
    with _io:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM shared_state WHERE key LIKE :p ESCAPE '\\'"), {"p": prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"})
        with _lock:
            for d in (_db, _pending, _inflight):
                for k in [k for k in d if k.startswith(prefix)]:
                    del d[k]


def reset_local() -> None:
    """Forget this process's copy of the counters and anything not written yet (tests recreate the table for every test)."""
    global _refreshed
    with _io:                                             # waits for a write in progress
        with _lock:
            _db.clear(); _pending.clear(); _inflight.clear()
            _refreshed = 0.0
            _idle.set()
