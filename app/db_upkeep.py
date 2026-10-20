"""Database upkeep (efficiency plan, step 14): nothing is deleted, the file is only kept tidy.

- `PRAGMA optimize` once a day and on shutdown: SQLite refreshes the statistics its query planner uses, cheap and only where needed.
- Space from big deletes (a removed world, cleared transcripts) comes back: when more than a quarter of the file is free pages, the database
  is switched once to incremental auto-vacuum (one VACUUM) and afterwards the free pages are released a little at a time, daily.
Runs in one background thread with its own connection; a failure is logged and tried again the next day."""
import logging
import sqlite3
import threading
import time

_log = logging.getLogger("nd.db_upkeep")
FREE_SHARE_FOR_VACUUM = 0.25
DAY = 24 * 60 * 60
FIRST_DELAY = 60 * 60          # an hour after start, not while the server is busy booting


def run_once(db_path: str) -> dict:
    """One pass. Returns what it did: {"optimized", "free_share", "vacuumed", "incremental_pages"}."""
    out = {"optimized": False, "free_share": 0.0, "vacuumed": False, "incremental_pages": 0}
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA optimize")
        out["optimized"] = True
        pages = con.execute("PRAGMA page_count").fetchone()[0] or 1
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
        out["free_share"] = free / pages
        mode = con.execute("PRAGMA auto_vacuum").fetchone()[0]          # 0 none, 1 full, 2 incremental
        if mode == 2:
            if free:
                con.execute("PRAGMA incremental_vacuum(%d)" % min(free, 25000))
                out["incremental_pages"] = min(free, 25000)
        elif out["free_share"] > FREE_SHARE_FOR_VACUUM:
            con.execute("PRAGMA auto_vacuum=INCREMENTAL")
            con.execute("VACUUM")                                         # needed once for the mode change to take effect
            out["vacuumed"] = True
        con.commit()
    finally:
        con.close()
    return out


_started = False


def start(db_path: str) -> None:
    """The daily loop, once per process (the test suite re-enters the app's lifespan hundreds of times)."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(FIRST_DELAY)
        while True:
            try:
                _log.info("database upkeep: %s", run_once(db_path))
            except Exception:
                _log.warning("database upkeep failed; trying again tomorrow", exc_info=True)
            time.sleep(DAY)
    threading.Thread(target=loop, name="nd-db-upkeep", daemon=True).start()


def optimize_now(db_path: str) -> None:
    try:
        con = sqlite3.connect(db_path, timeout=5)
        con.execute("PRAGMA optimize")
        con.close()
    except Exception:
        _log.debug("PRAGMA optimize on shutdown failed", exc_info=True)
