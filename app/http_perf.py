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
import json as _json
from pathlib import Path as _Path
from typing import Optional as _Optional

from starlette.datastructures import MutableHeaders as _MutableHeaders

# Paths whose responses are server-sent events — never gzip these.
_SSE_EXACT = {"/api/live", "/api/chronicler/ask"}


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

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and path.startswith("/static/"):
                headers = _MutableHeaders(scope=message)
                headers["cache-control"] = "public, max-age=31536000, immutable"
            await send(message)

        await self.app(scope, receive, send_wrapper)


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
