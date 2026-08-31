"""Scheduled SQLite snapshots for disaster recovery.

VACUUM INTO produces a consistent, defragmented copy of the database in a
single statement — safe to run against the live WAL-mode database while the
app is serving traffic (the same primitive /export's snapshot button uses,
see app/main.py). This module wraps it with:

- unique, sortable filenames (world-YYYYmmdd-HHMMSS.db)
- retention pruning (keep the newest N, delete the rest)
- an optional background thread that runs a snapshot every
  ND_BACKUP_INTERVAL_SECONDS (default: daily)

Configuration (all env, all read at call time so tests can monkeypatch):
- ND_BACKUP_DIR — destination directory; unset disables the scheduler and
  the API routes report "not configured" (400) rather than guessing a path
- ND_BACKUP_INTERVAL_SECONDS — scheduler period, default 86400
- ND_BACKUP_KEEP — how many snapshots to retain, default 7

A JSON world export via /export remains the portability/upgrade path; these
snapshots are the byte-exact "disk died mid-session" safety net.
"""
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path


def backup_dir() -> Path | None:
    raw = os.environ.get("ND_BACKUP_DIR", "").strip()
    return Path(raw) if raw else None


def _default_db_path() -> Path:
    # Imported lazily: app.database reads DB_PATH from the environment at
    # import time and several other modules import it first, so by the time
    # a backup runs the value is settled either way.
    from .database import DB_PATH
    return Path(DB_PATH)


def create_snapshot(db_path: Path, dest_dir: Path) -> Path:
    """VACUUM INTO a new snapshot of db_path inside dest_dir. Returns the
    snapshot's path. Raises OSError/sqlite3.Error through if the source is
    missing or the destination isn't writable — callers (the scheduler
    thread, the API route) log/report but don't mask the cause."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = dest_dir / f"world-{stamp}.db"
    n = 1
    while candidate.exists():
        # Two snapshots in the same second (a manual API run racing the
        # scheduler): VACUUM INTO refuses to overwrite, so disambiguate.
        candidate = dest_dir / f"world-{stamp}-{n}.db"
        n += 1
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("VACUUM INTO ?", (str(candidate),))
    finally:
        conn.close()
    return candidate


def list_snapshots(dest_dir: Path) -> list:
    """[{name, size_bytes, created_at_iso}], newest first. Timestamped
    names sort lexicographically in chronological order, so plain sort on
    the reversed name is both the listing and the pruning order."""
    out = []
    for p in dest_dir.glob("world-*.db"):
        stat = p.stat()
        out.append({
            "name": p.name,
            "size_bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    out.sort(key=lambda item: item["name"], reverse=True)
    return out


def prune_snapshots(dest_dir: Path, keep: int) -> int:
    """Delete all but the newest `keep` snapshots; returns how many were
    deleted. keep <= 0 keeps nothing (every snapshot is deletable) — the
    scheduler never passes that, but a manual prune call might."""
    snapshots = sorted(dest_dir.glob("world-*.db"), reverse=True)
    deleted = 0
    for p in snapshots[max(keep, 0):]:
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def run_backup_once() -> Path:
    dest = backup_dir()
    if dest is None:
        raise RuntimeError("ND_BACKUP_DIR is not set")
    path = create_snapshot(_default_db_path(), dest)
    try:
        keep = int(os.environ.get("ND_BACKUP_KEEP", "7"))
    except ValueError:
        keep = 7
    prune_snapshots(dest, keep)
    return path


_stop_event = threading.Event()
_thread: threading.Thread | None = None


def start():
    """Launch the scheduler thread if ND_BACKUP_DIR is configured. No-op
    otherwise — an unconfigured deployment must not grow surprise writes,
    and the test suite (which never sets ND_BACKUP_DIR) must not have a
    background thread snapshotting the shared test database."""
    global _thread
    if backup_dir() is None or _thread is not None:
        return
    try:
        interval = int(os.environ.get("ND_BACKUP_INTERVAL_SECONDS", "86400"))
    except ValueError:
        interval = 86400
    interval = max(interval, 60)

    def loop():
        # Wait-first: the app just booted, the DB was presumably fine a
        # moment ago — the first snapshot only matters after one full
        # interval of use has passed.
        while not _stop_event.wait(interval):
            try:
                run_backup_once()
            except Exception:
                pass

    _stop_event.clear()
    _thread = threading.Thread(target=loop, name="nd-backups", daemon=True)
    _thread.start()


def stop():
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2)
        _thread = None
