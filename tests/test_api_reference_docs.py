"""Docs drift test: every HTTP route the app actually registers must have a
row in docs/API_REFERENCE.md (method + path), enforcing the repo's own
stated convention ("check it before assuming an endpoint doesn't exist —
and add a row there for any new route", AGENTS.md).

The reverse (docs rows without a route) is NOT asserted — docs also list
the MCP server's tools and intentionally keep rows for a release or two
when a route is removed, so a stale-docs row is a docs bug, not a silent
API surprise like a missing one.
"""
import re
from pathlib import Path

from starlette.routing import Route

# main.py exports `app` as a thin ASGI dispatcher (routing /mcp around the
# middleware stack) — the real FastAPI instance with the route table on it
# is _fastapi_app.
from app.main import _fastapi_app as fastapi_app

DOCS_PATH = Path(__file__).parent.parent / "docs" / "API_REFERENCE.md"

# Framework-provided routes that exist in the table but aren't nd-world
# surface area worth documenting.
SKIP_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Docs rows use the compact combined forms `GET/POST` for methods and
# sometimes list several paths in one cell — and abbreviate path parameters
# (`{id}` vs the route's `{session_id}`), so both sides are normalized to a
# bare `{}` placeholder before comparing.
_ROW_RE = re.compile(r"^\|\s*([A-Z/]+)\s*\|\s*([^|]+)\|", re.MULTILINE)
_PATH_RE = re.compile(r"`(/[^`]*)`")


def _normalize(path):
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


def _documented_routes():
    text = DOCS_PATH.read_text(encoding="utf-8")
    out = set()
    for row in _ROW_RE.finditer(text):
        methods = [m for m in re.split(r"/", row.group(1)) if m in _METHODS]
        if not methods:
            continue
        for raw_path in _PATH_RE.findall(row.group(2)):
            for method in methods:
                out.add((method, _normalize(raw_path)))
    return out


def test_docs_file_exists():
    assert DOCS_PATH.is_file()


def test_every_http_route_is_documented():
    documented = _documented_routes()
    missing = []
    for route in fastapi_app.routes:
        if not isinstance(route, Route):
            continue  # Mounts (static, uploads) and websocket routes aren't HTTP pages
        for method in sorted(route.methods or ()):
            if method not in _METHODS:
                continue  # HEAD/OPTIONS are auto-derived
            path = _normalize(route.path)
            if path in SKIP_PATHS:
                continue
            if (method, path) not in documented:
                missing.append(f"{method} {path}")
    assert not missing, (
        f"{len(missing)} registered route(s) missing from docs/API_REFERENCE.md "
        f"(add a row for each):\n  " + "\n  ".join(sorted(missing))
    )
