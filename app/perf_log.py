"""Per-request timing, kept in memory (performance plan, step 10).

For every page / API request (not /static, not the live event streams) it records the route template, the HTTP status, how long the request
took, how many SQL statements it ran and how long the database took - and nothing else: no query strings, no bodies, no cookies, no user names,
so the log can never hold private content or credentials. The last RING_SIZE records live in a ring buffer (GET /api/perf/recent, GET
/api/perf/summary, the System Monitor page); a request slower than SLOW_MS (env ND_SLOW_MS, default 800) is also written to the log as one line.
"""
import contextvars
import logging
import os
import threading
import time
from collections import deque

from sqlalchemy import event

RING_SIZE = 1000
SLOW_MS = float(os.environ.get("ND_SLOW_MS", "800") or 800)

_log = logging.getLogger("nd.perf")
_ring: deque = deque(maxlen=RING_SIZE)
_lock = threading.Lock()
_current: contextvars.ContextVar = contextvars.ContextVar("nd_perf_request", default=None)


def begin() -> dict:
    """Start counting for the request being handled (called by PerfMiddleware); worker threads started for it share the same dict."""
    rec = {"queries": 0, "db_ms": 0.0}
    _current.set(rec)
    return rec


def finish(rec: dict, method: str, route: str, status: int, started: float) -> None:
    ms = (time.perf_counter() - started) * 1000
    row = {"t": round(time.time(), 1), "method": method, "route": route, "status": status, "ms": round(ms, 1),
           "queries": rec["queries"], "db_ms": round(rec["db_ms"], 1)}
    with _lock:
        _ring.append(row)
    if ms >= SLOW_MS:
        _log.warning("slow request %s %s -> %s in %.0f ms (%d queries, %.0f ms in the database)", method, route, status, ms, rec["queries"], rec["db_ms"])


def recent(limit: int = 200) -> list:
    with _lock:
        return list(_ring)[-limit:]


def summary() -> list:
    """Per route: calls, median / 95th percentile / max milliseconds, the most queries one call ran and the average database share."""
    with _lock:
        rows = list(_ring)
    by: dict = {}
    for r in rows:
        by.setdefault((r["method"], r["route"]), []).append(r)
    out = []
    for (method, route), rs in by.items():
        ms = sorted(r["ms"] for r in rs)
        out.append({
            "method": method, "route": route, "calls": len(rs), "p50_ms": ms[len(ms) // 2], "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))],
            "max_ms": ms[-1], "max_queries": max(r["queries"] for r in rs), "avg_db_ms": round(sum(r["db_ms"] for r in rs) / len(rs), 1),
            "errors": sum(1 for r in rs if r["status"] >= 500),
        })
    return sorted(out, key=lambda o: -o["p95_ms"])


def reset() -> None:
    with _lock:
        _ring.clear()


def install(engine) -> None:
    """Count statements and database time for whichever request is current (a statement outside any request - a background job - is ignored)."""
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        rec = _current.get()
        if rec is not None:
            context._nd_t0 = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        rec = _current.get()
        t0 = getattr(context, "_nd_t0", None)
        if rec is not None and t0 is not None:
            rec["queries"] += 1
            rec["db_ms"] += (time.perf_counter() - t0) * 1000
