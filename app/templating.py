"""The single shared Jinja2Templates instance, used by main.py and every router.

Previously each router built its own Jinja2Templates() to avoid importing from
main.py (which would be circular, since main.py imports the routers). Starlette
constructs a fresh jinja2.Environment per Jinja2Templates instance, so that left
10 separate environments — only main.py's and characters.py's ever registered
the `kinds`/`subtypes`/`kind_icons` globals that base.html's nav loops over, so
the other eight routers silently rendered zero lore-kind nav links. Registering
everything exactly once here fixes that app-wide and is the reason this module
(like app/rendering.py and app/deps.py) deliberately doesn't import main.
"""
import json
from pathlib import Path

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import deps
from . import nav_menus as _nav_menus
from .constants import KIND_ICONS, KINDS, SUBTYPES
from .database import SessionLocal, get_app_settings
from .rendering import body_summary, entry_text, parse_stats, render_md, strip_md

# Duplicated from main.py's DEFAULT_WORLD_COOKIE — same rationale as this
# file's own docstring: importing from main.py here would be circular.
_ACTIVE_WORLD_COOKIE = "active_world"


def _kinds_context_processor(request: Request) -> dict:
    """Makes every `{% for k in kinds %}` / `kind_icons[k]` template
    (base.html's nav, index.html's home stat grid, entity forms, etc.)
    world-scoped for GM-defined custom kinds (see deps.effective_kinds),
    with zero changes to those templates: Starlette's TemplateResponse
    applies context processors *after* a route's own context dict, so this
    silently overrides the "kinds"/"kind_icons"/"subtypes" a handler may
    or may not have already set (harmless either way, and cheaper than
    threading world-awareness through the ~20 call sites that pass these
    explicitly today).

    One extra small World lookup per request — same per-request cost
    profile as auth_gate's own independent SessionLocal() user lookup."""
    db = SessionLocal()
    try:
        world, _ = deps.get_world_ctx(request, db, request.cookies.get(_ACTIVE_WORLD_COOKIE))
        kinds, kind_icons = deps.effective_kinds(world)
        settings = get_app_settings(db)
        user = getattr(request.state, "user", None)
        nav_menus, nav_ungrouped_items = _nav_menus.resolve_nav_menus(
            world, bool(settings.dreamlands_enabled), bool(settings.king_in_yellow_enabled),
            bool(user and user.is_gm),
        )
        return {
            "kinds": kinds, "kind_icons": kind_icons, "subtypes": deps.effective_subtypes(world),
            "dreamlands_enabled": bool(settings.dreamlands_enabled),
            "king_in_yellow_enabled": bool(settings.king_in_yellow_enabled),
            "nav_menus": nav_menus, "nav_ungrouped_items": nav_ungrouped_items,
            "world_theme": _parse_world_theme(world),
        }
    finally:
        db.close()


def _parse_world_theme(world) -> dict:
    """world.theme_json, pre-parsed once per request so base.html can just
    read world_theme.bg/.font/etc. without a template-side JSON filter.
    Already-validated by _sanitize_theme() at import time (see app/main.py)
    — this just guards against a JSON parse failure on a row written by
    something other than that import path (a raw DB edit, a future
    migration) rather than re-validating every field again per render."""
    raw = getattr(world, "theme_json", None) if world else None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(
    directory=str(BASE_DIR / "app" / "templates"),
    context_processors=[_kinds_context_processor],
)
# Last-resort default for the (nonexistent today) case of a template
# rendered outside any request — real renders get the per-world merged
# values from the context processor above.
templates.env.globals.update(kinds=KINDS, subtypes=SUBTYPES, kind_icons=KIND_ICONS)
templates.env.filters["md"] = render_md
templates.env.filters["strip_md"] = strip_md
templates.env.filters["body_summary"] = body_summary
templates.env.filters["parse_stats"] = parse_stats
templates.env.filters["entry_text"] = entry_text
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []


@jinja2.pass_context
def _wq(ctx, path):
    """Append the current template's ?w=<world slug> to an internal link,
    so a link generated while viewing a world stays pinned to it for
    whoever opens it next — see deps.with_world. No-ops (returns path
    unchanged) on pages that don't have a `world` in their context."""
    return deps.with_world(path, ctx.get("world"))


templates.env.filters["wq"] = _wq
