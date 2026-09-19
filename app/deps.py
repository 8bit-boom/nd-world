"""Shared FastAPI route helpers used across main.py and every router.

Kept separate from main.py so routers (imported BY main.py) can use these
without a circular import — see app/templating.py for the same rationale.
"""
import json
import time
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import auth
from .constants import KINDS, KIND_ICONS, SUBTYPES
from .models import Entity, World, entity_player_access


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


def can_edit_content(request: Request) -> bool:
    """True if this request may create/edit/delete world CONTENT (entities,
    notes, sessions, calendar, tables, boards, maps, schematics, pages,
    gallery/audio/video clips, imports): a GM always, plus a non-GM whose
    membership in the ACTIVE world carries role="assistant" (see
    WorldMembership.role in app/models.py and _is_assistant_safe in
    app/main.py — the middleware is the enforcement boundary; this helper is
    the same answer routers and templates need locally). Reads request.state.
    is_assistant, which auth_gate computes for every non-GM request — so a
    router should call this instead of re-querying WorldMembership. Used by
    the Jinja global `can_edit(request)` (app/templating.py) too."""
    user = getattr(request.state, "user", None)
    return bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))


def is_gm(request: Request) -> bool:
    """True if this request's logged-in user is a GM — used by the media
    library routers (audio/video/pages) to decide whether a visibility
    filter applies, distinct from can_edit_content (which also admits a
    GM-Assistant)."""
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


# id -> (label, icon) for World.section_access_json (see its docstring in
# app/models.py) and the per-section Players/Assistants None/Read/Edit
# controls on Settings -> Navigation. Order here is display order. Combat
# Tracker and Investigation Boards deliberately aren't here — Combat's
# live state is often spoiler-heavy, and Boards is a large drag-and-drop
# canvas editor with no read/edit separation anywhere in its JS, so a safe
# read-only (let alone editable) mode for it is its own separate project.
SECTION_PERMISSION_IDS = {
    "maps": ("Maps", "🗺"),
    "calendar": ("Calendar", "🗓"),
    "quests": ("Quests", "📜"),
    "parties": ("Parties", "🛡"),
    "tables": ("Random Tables", "🎲"),
}

# Sections with no meaningful player-edit action — nothing in the app lets
# a player create/modify map content (Maps has no owner concept and no
# player-facing create path), so a "player" level read back as "edit" here
# (a stale row, or a hand-edited one) is floored to "read" rather than
# granted, and the Settings UI only ever offers None/Read for this role+
# section combination in the first place.
_NO_PLAYER_EDIT_SECTIONS = {"maps"}

_SECTION_LEVELS = ("none", "read", "edit")


def _default_section_levels(section_id: str) -> dict:
    """Pre-upgrade-equivalent defaults for a section with no (or corrupt)
    configuration: assistant="edit" everywhere (assistants already had
    blanket access via can_edit_content before this matrix existed), and
    player="read" on maps only / "none" elsewhere (maps was already open
    to every player unconditionally; the other four were GM-only)."""
    return {"player": "read" if section_id == "maps" else "none", "assistant": "edit"}


def world_section_access(world) -> dict:
    """Parses World.section_access_json into {section_id: {"player": lvl,
    "assistant": lvl}} for every id in SECTION_PERMISSION_IDS — tolerant of
    NULL/malformed/partial JSON, a missing section, an unknown role key, or
    an invalid level string, all of which fall back independently to
    _default_section_levels(section_id) rather than discarding the whole
    row (a bad value for one section/role must not reset every other one a
    GM already configured)."""
    raw = getattr(world, "section_access_json", None) if world else None
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for sid in SECTION_PERMISSION_IDS:
        defaults = _default_section_levels(sid)
        entry = data.get(sid) if isinstance(data.get(sid), dict) else {}
        levels = {}
        for role in ("player", "assistant"):
            val = entry.get(role)
            if val not in _SECTION_LEVELS:
                val = defaults[role]
            if role == "player" and val == "edit" and sid in _NO_PLAYER_EDIT_SECTIONS:
                val = "read"
            levels[role] = val
        out[sid] = levels
    return out


def sanitize_section_access(raw_json) -> str:
    """Validates a posted section_access_json payload (Settings ->
    Navigation's per-section Players/Assistants selects) before saving:
    restricts to known section ids (SECTION_PERMISSION_IDS) and known
    levels (_SECTION_LEVELS), flooring anything else to "none" (deny) —
    unlike world_section_access's tolerant degrade-to-safe-defaults on
    READ, a save is an explicit, deliberate action, so a garbled/tampered
    field here is denied rather than silently keeping a permissive
    fallback. Also floors "player": "edit" on a _NO_PLAYER_EDIT_SECTIONS
    section down to "read", same as world_section_access."""
    try:
        data = json.loads(raw_json) if raw_json else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for sid in SECTION_PERMISSION_IDS:
        entry = data.get(sid) if isinstance(data.get(sid), dict) else {}
        levels = {}
        for role in ("player", "assistant"):
            val = entry.get(role)
            if val not in _SECTION_LEVELS:
                val = "none"
            if role == "player" and val == "edit" and sid in _NO_PLAYER_EDIT_SECTIONS:
                val = "read"
            levels[role] = val
        out[sid] = levels
    return json.dumps(out)


def _role_for_request(request: Request) -> Optional[str]:
    """"assistant" or "player" for a logged-in non-GM request, else None
    (anonymous, or a GM — callers handle the GM case separately since a
    GM's level is always "edit" regardless of this matrix)."""
    user = getattr(request.state, "user", None)
    if not user or user.is_gm:
        return None
    return "assistant" if getattr(request.state, "is_assistant", False) else "player"


def world_section_level(request: Request, world, section_id: str) -> str:
    """"none"/"read"/"edit" — this request's access to `section_id` (one of
    SECTION_PERMISSION_IDS) for `world`. A GM always gets "edit"."""
    user = getattr(request.state, "user", None)
    if user and user.is_gm:
        return "edit"
    role = _role_for_request(request)
    if role is None:
        return "none"
    return world_section_access(world).get(section_id, _default_section_levels(section_id))[role]


def world_can_view_section(request: Request, world, section_id: str) -> bool:
    """True if this request may at least READ `section_id` for `world` —
    "read" or "edit" both qualify. Used by each section's own GET handler
    (the real enforcement — nav_menus.py's own check only decides whether
    the nav link/menu entry is shown)."""
    return world_section_level(request, world, section_id) in ("read", "edit")


def world_can_edit_section(request: Request, world, section_id: str) -> bool:
    """True if this request may create/edit/delete in `section_id` for
    `world` — "edit" only. For a GM this is always True. For a GM-Assistant
    or player, this REPLACES can_edit_content's blanket answer for these
    five sections specifically (can_edit_content stays the gate for
    everything else in the world — entities, pages, sheets, ... — which
    this matrix deliberately doesn't cover). Passing this check only means
    the SECTION is open to this role at the edit tier; a player additionally
    may only touch rows they own (Quest/CalendarEvent/RandomTable.
    created_by_user_id == their own id, or — for Parties, which has no
    owner column — a party containing one of their own PlayerCharacters).
    Enforcing that per-row ownership check is each route handler's job,
    same as it always has been for e.g. PlayerCharacter edits."""
    return world_section_level(request, world, section_id) == "edit"


def world_can_edit_row(request: Request, world, section_id: str, created_by_user_id: Optional[int]) -> bool:
    """True if this request may edit/delete a SPECIFIC row in `section_id`
    already known to belong to `world` (pair with world_row_visible, the
    read-side equivalent, for a row looked up by its own id/slug) — a GM
    or an edit-level GM-Assistant may touch ANY row once the section
    itself is open to them at the edit tier (an assistant is a trusted
    scoped-down GM, not restricted further per-row); an edit-level PLAYER
    may only touch a row they themselves created (created_by_user_id ==
    their own id) — see world_can_edit_section's own docstring for why
    players are narrower here. Only meaningful for the three section
    models that carry created_by_user_id (Quest, CalendarEvent,
    RandomTable) — Party has no owner column and uses its own
    membership-based check instead (see parties.py)."""
    if not world_can_edit_section(request, world, section_id):
        return False
    user = getattr(request.state, "user", None)
    if user and user.is_gm:
        return True
    if getattr(request.state, "is_assistant", False):
        return True
    return bool(user and created_by_user_id == user.id)


def world_row_visible(request: Request, db: Session, world_id: Optional[int], section_id: str) -> bool:
    """Like world_can_view_section, but for a row fetched by its OWN
    primary key/slug (a Quest, Party, RandomTable roll, ...) rather than
    the request's active world — quests.py/parties.py/tables.py look these
    up directly by id with no query-level world scoping at all (previously
    harmless, since only a GM — who can access every world — could ever
    reach them), so a caller must separately confirm the ROW'S OWN world is
    one this viewer may access at all before also checking the section is
    opened to their role, or a player could view another world's quest/
    party by guessing its id even with the section closed everywhere they
    actually belong. world_id=None (a global/built-in row shared across
    every world, e.g. a built-in RandomTable) is always visible — nothing
    world-specific to leak."""
    if world_id is None:
        return True
    user = getattr(request.state, "user", None)
    world = db.query(World).filter(World.id == world_id).first()
    if not auth.user_can_access_world(db, user, world):
        return False
    return world_can_view_section(request, world, section_id)


def require_can_edit(request: Request) -> None:
    """The write-side gate for content the media library routers manage
    (clips/pages/albums): a GM, or a GM-Assistant (WorldMembership.role ==
    "assistant") — same tier auth_gate's _is_assistant_safe already
    enforced on the way in; this re-check keeps each handler safe on its
    own. Deliberately NOT used by visibility filters/counts elsewhere in
    those routers — an assistant SEES what a player sees, per the role's
    whole premise."""
    if not can_edit_content(request):
        raise HTTPException(403)


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


# ── LLM-invoking endpoint cooldown ──────────────────────────────────────────
# A handful of player-facing endpoints call Ollama directly with no other
# rate limit (Chronicler's /api/chronicler/ask, the session log's own
# /recap) — a player mashing the button (or a script) would otherwise fire
# one real generation per click with nothing to slow it down. Process-local
# for the same reason app/routers/auth.py's login-lockout dict is (single
# uvicorn worker, no --workers flag) — see that module's own note.
_llm_cooldowns: dict[int, float] = {}  # user_id -> last-call monotonic time
_LLM_COOLDOWN_SECONDS = 3.0


def check_llm_cooldown(user_id: int, seconds: float = _LLM_COOLDOWN_SECONDS) -> None:
    """Raise 429 if this user already hit an LLM-cooldown-guarded endpoint
    within the last `seconds` — call at the very top of the route, before
    any real work starts, and only for non-GM callers (a GM is exempt at
    every other player-facing AI gate in this app — see
    _require_ask_ai_access in routers/ai.py — so callers should skip this
    check entirely for a GM rather than pass their id through)."""
    now = time.monotonic()
    last = _llm_cooldowns.get(user_id)
    if last is not None and now - last < seconds:
        raise HTTPException(429, "Please wait a few seconds before asking again.")
    _llm_cooldowns[user_id] = now


def filter_visible_entities(q, request: Request):
    """Restrict an Entity query to visible_to_players rows for non-GM
    viewers, plus any hidden entities specifically shared with this player.
    Lives here (rather than only in main.py, which every router already
    imports it from for backward compatibility) so routers can apply it
    directly without importing main.py back — every player-facing entity
    list in the app must go through this or an equivalent per-entity
    filter; a query that skips it leaks GM-only content to players."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        if user:
            shared = q.session.query(entity_player_access.c.entity_id).filter(
                entity_player_access.c.user_id == user.id
            )
            q = q.filter(or_(Entity.visible_to_players.isnot(False), Entity.id.in_(shared)))
        else:
            q = q.filter(Entity.visible_to_players.isnot(False))
    return q
