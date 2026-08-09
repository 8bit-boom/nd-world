"""Shared FastAPI route helpers used across main.py and every router.

Kept separate from main.py so routers (imported BY main.py) can use these
without a circular import — see app/templating.py for the same rationale.
"""
import json
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from . import auth
from .constants import KINDS, KIND_ICONS, SUBTYPES
from .models import World


def resolve_world_slug(request: Request, cookie_value: Optional[str]) -> Optional[str]:
    """?w=<slug> takes precedence over the active_world cookie, so a link
    that names its world explicitly (e.g. one shared with a player) always
    shows that world regardless of what the recipient's browser has
    cached — the cookie remains the fallback for links/bookmarks that
    don't specify a world."""
    return request.query_params.get("w") or cookie_value


def with_world(path: str, world) -> str:
    """Append ?w=<slug> (or &w=<slug> if `path` already has a query
    string) so a link generated while viewing `world` stays pinned to it
    for whoever opens it next. No-ops if `world` is falsy."""
    if not world:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}w={world.slug}"


def get_world_ctx(request: Request, db: Session, active_world: Optional[str]):
    """The active world plus the world-switcher list, filtered to what this
    viewer may access — GMs see every world, players only the ones they're a
    member of. World existence/names must not leak to non-members by ID
    enumeration, so this (not a raw `db.query(World).all()`) is what every
    handler that needs "the current world" should call.
    """
    active_world = resolve_world_slug(request, active_world)
    user = getattr(request.state, "user", None)
    accessible = auth.accessible_world_ids(db, user)
    q = db.query(World)
    if accessible is not None:
        q = q.filter(World.id.in_(accessible)) if accessible else q.filter(World.id.in_([]))
    worlds = q.order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


# GM-defined custom entity kinds (see World.custom_kinds_json) — namespaced
# so a GM-picked id can never collide with a built-in kind a future app
# update might add.
CUSTOM_KIND_PREFIX = "custom_"
MAX_CUSTOM_KINDS = 25


def load_custom_kinds(world: Optional[World]) -> list:
    """Parse+defensively validate world.custom_kinds_json. Never raises —
    malformed/legacy-shaped entries are dropped rather than blowing up every
    page render. world=None (or no custom kinds set) -> []."""
    if not world:
        return []
    try:
        raw = json.loads(world.custom_kinds_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("id")
        label = entry.get("label")
        if not isinstance(kid, str) or not kid.startswith(CUSTOM_KIND_PREFIX):
            continue
        if not isinstance(label, str) or not label.strip():
            continue
        subtypes = entry.get("subtypes")
        out.append({
            "id": kid,
            "label": label,
            "icon": entry.get("icon") or "🏷",
            "subtypes": [s for s in subtypes if isinstance(s, str)] if isinstance(subtypes, list) else [],
            "created_at": entry.get("created_at") or "",
        })
    return out


def effective_kinds(world: Optional[World]):
    """Built-in KINDS/KIND_ICONS plus this world's custom kinds appended in
    stored order. world=None returns the built-ins unchanged (no active
    world yet, e.g. the /worlds picker). THE single source of truth for
    "what kind values are valid content categories in this world" — every
    validation/render call site should use this (or receive it already
    computed) instead of importing KINDS/KIND_ICONS directly, so a custom
    kind works everywhere a built-in one does.

    Returns (kinds: list[str], kind_icons: dict[str, str])."""
    custom = load_custom_kinds(world)
    kinds = list(KINDS) + [c["id"] for c in custom]
    icons = dict(KIND_ICONS)
    icons.update({c["id"]: c["icon"] for c in custom})
    return kinds, icons


def effective_subtypes(world: Optional[World]):
    """Built-in SUBTYPES plus each custom kind's own suggestion list —
    same "suggestions only, not enforced" contract as the built-in dict."""
    subtypes = {k: list(v) for k, v in SUBTYPES.items()}
    for c in load_custom_kinds(world):
        if c["subtypes"]:
            subtypes[c["id"]] = list(c["subtypes"])
    return subtypes


PAGE_SIZE = 50


def paginate(query, page: int, page_size: int = PAGE_SIZE):
    """Slice an ordered SQLAlchemy query to one page, clamping `page` into
    range instead of returning an empty page for an out-of-bounds request.

    Only fits flat, already-ordered list queries — views that group results
    by folder/status/category (the entity browser, quests, random tables)
    need every row in the group to render correctly, so paginating the raw
    query would silently split a group across pages. Those are left as full
    loads for now rather than force-fit a slice that would corrupt the
    grouping; this is for straightforward "one row per card" lists.
    """
    page = max(1, page)
    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return items, page, total_pages
