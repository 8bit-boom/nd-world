"""Two HTTP-layer performance middlewares plus a maps-JSON cache.

1. PerfMiddleware (pure ASGI, outermost):
   - SSE carve-out: strips the request's Accept-Encoding on server-sent-event
     paths so an inner GZipMiddleware never compresses those responses —
     gzip can re-buffer streamed events behind proxies, which is exactly
     what the no-buffering headers on those routes are trying to prevent.
   - Static cache headers: every /static reference is content-versioned
     (style.css?v=<sha1> — see templating.asset_v), so responses get
     `Cache-Control: public, max-age=31536000, immutable`. One page load
     used to cost one revalidation round trip PER asset on EVERY visit;
     with the header, versioned assets come straight from disk cache and
     a deploy busts them via the URL change.

2. GZipMiddleware (added just inside PerfMiddleware): HTML/JSON/static
   responses compress ~4-5x (cockpit.js alone is ~75 KB raw). Intentionally
   NOT applied to SSE via the carve-out above.

3. map_json_cache: /maps and the map viewer re-read and re-parse every
   maps/*.json on every request; this memoizes parses keyed by
   (path, mtime_ns, size) so edits (rename/upload rewrite the file) bust
   it automatically and nothing can serve a stale map.
"""
import asyncio as _asyncio
import json as _json
import os
import re as _re
import time as _time
import weakref as _weakref
from pathlib import Path as _Path

from . import perf_log as _perf_log
from typing import Optional as _Optional

from starlette.datastructures import MutableHeaders as _MutableHeaders

# Paths whose responses are server-sent events — never gzip these.
_SSE_EXACT = {"/api/live", "/api/chronicler/ask"}


# Building a page is CPU work that holds Python's GIL, so more of them in flight than a couple only take turns slowly: measured on this app (home page,
# 4 cores) 16 browsers at once gave 6 pages/s with 8-40 worker threads against 28 pages/s with 2, and a median wait of 4.8 s against 0.6 s.
# So at most RENDER_SLOTS HTML page requests run at once (env ND_RENDERS, default 2); the rest wait their turn in the event loop, which costs nothing.
# API calls, uploads, streams and static files are not gated.
RENDER_SLOTS = max(1, int(os.environ.get("ND_RENDERS", "2") or 2))
_GATES: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()      # one gate per event loop (a semaphore must not be shared between loops)


def _gate():
    loop = _asyncio.get_running_loop()
    g = _GATES.get(loop)
    if g is None:
        g = _GATES[loop] = _asyncio.Semaphore(RENDER_SLOTS)
    return g


def _is_sse_path(path: str) -> bool:
    return path in _SSE_EXACT or path.endswith("/stream")


class PerfMiddleware:
    """Outermost ASGI middleware: SSE no-gzip carve-out + /static cache
    headers. Pure pass-through for everything else."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if _is_sse_path(path):
            # Drop Accept-Encoding on the REQUEST: the inner GZipMiddleware
            # negotiates from it, so no header = no compression for this
            # response, and the SSE stream keeps flushing event-by-event.
            headers = _MutableHeaders(scope=scope)
            if "accept-encoding" in headers:
                del headers["accept-encoding"]

        if path.startswith("/static/") and scope.get("method") in ("GET", "HEAD"):
            pre = _precompressed(path, _MutableHeaders(scope=scope).get("accept-encoding", ""))
            if pre is not None:
                await pre(scope, receive, send)
                return
        timed = not path.startswith("/static/") and not _is_sse_path(path)
        rec = _perf_log.begin() if timed else None
        started = _time.perf_counter()
        status = {"code": 500}
        # Page renders take turns (see RENDER_SLOTS): hold a slot from the request's arrival until its response STARTS (the headers go out when the
        # page is built), so a slow download or a stream that runs on afterwards never keeps one.
        gated = (timed and scope.get("method") == "GET" and not path.startswith(("/uploads/", "/api/"))
                 and "text/html" in _MutableHeaders(scope=scope).get("accept", ""))
        gate = _gate() if gated else None
        if gate is not None:
            await gate.acquire()
        held = {"gate": gated}

        def release():
            if held["gate"]:
                held["gate"] = False
                gate.release()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                release()
                status["code"] = message["status"]
                if path.startswith("/static/"):
                    headers = _MutableHeaders(scope=message)
                    headers["cache-control"] = "public, max-age=31536000, immutable"
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            release()
            if timed:
                route = getattr(scope.get("route"), "path", None) or _re.sub(r"\d+", "{n}", path)      # the route template, never the real URL
                _perf_log.finish(rec, scope.get("method", "GET"), route, status["code"], started)


# ── pre-compressed static files ──────────────────────────────────────────────
# scripts/precompress_static.py (run at image build) writes name.js.br / name.js.gz next to every text asset. A request that accepts br or gzip is
# answered straight from that file: no compression work per request (GZipMiddleware re-compressed cockpit.js and style.css on EVERY load), and
# brotli is smaller than gzip. Anything without a pre-compressed twin (a dev checkout, a new file) falls through to the normal path.
_STATIC_DIR = _Path(__file__).resolve().parent.parent / "static"
_PRE_ENCODINGS = (("br", ".br"), ("gzip", ".gz"))


def _precompressed(path: str, accept_encoding: str):
    import mimetypes
    from starlette.responses import FileResponse
    rel = path[len("/static/"):]
    try:
        src = (_STATIC_DIR / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not src.is_relative_to(_STATIC_DIR) or not src.is_file() or src.suffix in (".br", ".gz"):
        return None
    accepted = {p.split(";")[0].strip().lower() for p in accept_encoding.split(",") if p.strip()}
    for token, ext in _PRE_ENCODINGS:
        twin = src.with_name(src.name + ext)
        if token in accepted and twin.is_file() and twin.stat().st_mtime_ns >= src.stat().st_mtime_ns:
            media = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
            return FileResponse(twin, media_type=media, headers={
                "Content-Encoding": token, "Vary": "Accept-Encoding", "Cache-Control": "public, max-age=31536000, immutable",
            })
    return None


# ── maps JSON cache ──────────────────────────────────────────────────────────
_MAP_JSON_CACHE: dict = {}


def map_json_cached(jf: _Path) -> _Optional[dict]:
    """json-parses a map file, memoized by (path, mtime_ns, size). Any write
    to the file (rename, upload, GM edit) changes its stat tuple and busts
    the entry, so a stale map can never be served. Returns None for missing
    or malformed files, exactly like main.py's _map_data."""
    try:
        st = jf.stat()
    except OSError:
        _MAP_JSON_CACHE.pop(str(jf), None)
        return None
    key = (str(jf), st.st_mtime_ns, st.st_size)
    if key in _MAP_JSON_CACHE:
        return _MAP_JSON_CACHE[key]
    try:
        data = _json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if len(_MAP_JSON_CACHE) > 512:
        _MAP_JSON_CACHE.clear()
    _MAP_JSON_CACHE[key] = data
    return data
