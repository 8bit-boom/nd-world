"""Second screen: show an image or a text card on a display window kept on another monitor.

A GM running the game from a notebook with an extended desktop opens /display on the second
monitor once, then "sends" things to it from anywhere in the app — an entity's portrait, a gallery
image, a map, a note's text, a paragraph they selected — instead of turning the notebook around.

State is held per world in memory (current item + a short history of what was shown, newest
first), like the live-sync counter in app/live.py: single process by design, nothing to migrate,
and a restart just blanks the screen. The display page follows it over Server-Sent Events (with a
BroadcastChannel nudge from the sending window for zero latency).

This screen is what the TABLE sees, so:
* everything here is GM-only (none of these routes are in main.py's player/assistant allowlists);
* text taken from an entity is stripped of its [gmonly] / :::gm parts before it is rendered;
* only image URLs that cannot execute anything are accepted (a local /path or http(s)).
"""
import asyncio
import hashlib
import json
import re
from datetime import datetime
from typing import AsyncIterator

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import live
from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity, Schematic
from ..rendering import render_md, strip_gm_only
from ..rules_render import strip_gm_directives
from ..schematic_payload import BG_COLORS, player_scene
from ..templating import templates

router = APIRouter()

RECENT_LIMIT = 12
_TITLE_CAP, _CAPTION_CAP, _TEXT_CAP = 200, 600, 20000

# world_id -> {"seq": int, "current": item | None, "recent": [item, ...]}
_STAGES: dict = {}


# ── state ────────────────────────────────────────────────────────────────────

def reset_stage(world_id=None) -> None:
    """Forget one world's stage, or all of them (tests; also handy after a world is deleted)."""
    if world_id is None:
        _STAGES.clear()
    else:
        _STAGES.pop(world_id, None)


def _stage(world_id) -> dict:
    return _STAGES.setdefault(world_id, {"seq": 0, "current": None, "recent": []})


def get_state(world_id) -> dict:
    s = _stage(world_id)
    return {"seq": s["seq"], "current": s["current"], "recent": list(s["recent"])}


def _identity(item: dict) -> str:
    """What makes two items 'the same thing' for the recent list."""
    basis = item.get("url") or item.get("html") or ""
    return f"{item.get('kind')}:{hashlib.sha1(basis.encode('utf-8', 'ignore')).hexdigest()[:16]}"


def set_stage(world_id, item: dict) -> dict:
    """Put `item` on the screen: it becomes current, moves to the front of the history (no
    duplicates, capped), and the live counter is bumped so open pages notice."""
    s = _stage(world_id)
    s["seq"] += 1
    item = {**item, "id": s["seq"], "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    item["key"] = _identity(item)
    s["current"] = item
    s["recent"] = [item] + [r for r in s["recent"] if r.get("key") != item["key"]][: RECENT_LIMIT - 1]
    live.touch(world_id)
    return get_state(world_id)


def clear_stage(world_id) -> dict:
    s = _stage(world_id)
    s["seq"] += 1
    s["current"] = None
    live.touch(world_id)
    return get_state(world_id)


async def stream_stage(world_id, poll: float = 1.0, beat: float = 15.0) -> AsyncIterator[str]:
    """SSE body: a `state` frame (the full state) on connect and whenever it changes, keep-alive
    comments in between. A module-level function so tests can drive it directly."""
    yield "retry: 2000\n\n"
    last, since_beat = None, 0.0
    while True:
        state = get_state(world_id)
        if state["seq"] != last:
            last = state["seq"]
            yield f"event: state\ndata: {json.dumps(state)}\n\n"
            since_beat = 0.0
        else:
            since_beat += poll
            if since_beat >= beat:
                since_beat = 0.0
                yield ": keep-alive\n\n"
        await asyncio.sleep(poll)


# ── input cleaning ───────────────────────────────────────────────────────────

_SAFE_PATH = re.compile(r"^/(?!/)[^\s\x00-\x1f\\]*$")
_SAFE_ABS = re.compile(r"^https?://[^\s\x00-\x1f\\]+$", re.I)


def safe_image_url(url) -> str:
    """The URL if it can only ever be loaded as an image source — a site-local /path (not
    protocol-relative) or an absolute http(s) URL — else ''."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    return url if (_SAFE_PATH.match(url) or _SAFE_ABS.match(url)) else ""


def _text(value, cap) -> str:
    return value.strip()[:cap] if isinstance(value, str) else ""


def _text_html(markdown: str) -> str:
    return render_md(markdown)


def _entity_item(db: Session, world, entity_id, mode) -> dict:
    ent = db.get(Entity, entity_id) if isinstance(entity_id, int) else None
    if not ent or ent.world_id != world.id:
        raise HTTPException(404, "Entity not found")
    source = {"type": "entity", "id": ent.id}
    image = safe_image_url(ent.image_url or "")
    if mode != "text" and image:
        return {"kind": "image", "url": image, "title": _text(ent.name, _TITLE_CAP),
                "caption": _text(ent.summary or "", _CAPTION_CAP), "source": source}
    # the screen is what the table sees: never carry GM-only parts onto it
    body = strip_gm_directives(strip_gm_only(ent.body or ent.summary or ""))
    return {"kind": "text", "title": _text(ent.name, _TITLE_CAP), "html": _text_html(body[:_TEXT_CAP]),
            "image": image, "source": source}


def _map_item(db: Session, world, slug) -> dict:
    s = db.query(Schematic).filter(Schematic.slug == slug).first() if isinstance(slug, str) else None
    if not s or s.world_id != world.id or (s.is_html and s.html_file):
        raise HTTPException(404, "Map not found")
    return {"kind": "map", "url": f"/display/map/{s.slug}", "title": _text(s.name, _TITLE_CAP),
            "source": {"type": "map", "slug": s.slug}}


def _build_item(db: Session, world, body: dict) -> dict:
    kind = body.get("kind")
    if kind == "map":
        return _map_item(db, world, body.get("slug"))
    if kind == "entity":
        return _entity_item(db, world, body.get("entity_id"), body.get("mode"))
    if kind == "image":
        url = safe_image_url(body.get("url"))
        if not url:
            raise HTTPException(400, "That image address is not allowed")
        return {"kind": "image", "url": url, "title": _text(body.get("title"), _TITLE_CAP),
                "caption": _text(body.get("caption"), _CAPTION_CAP), "source": {"type": "image"}}
    if kind == "text":
        text = _text(body.get("text"), _TEXT_CAP)
        if not text:
            raise HTTPException(400, "Nothing to show")
        return {"kind": "text", "title": _text(body.get("title"), _TITLE_CAP), "html": _text_html(text),
                "source": {"type": "text"}}
    raise HTTPException(400, "kind must be image, text, entity or map")


# ── routes (GM only) ─────────────────────────────────────────────────────────

def _gm_world(request: Request, db: Session, active_world):
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404, "No world selected")
    return world, worlds


@router.get("/display", response_class=HTMLResponse)
def display_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The window to put on the second monitor: black, nothing but what was sent."""
    world, _ = _gm_world(request, db, active_world)
    return templates.TemplateResponse("display.html", {"request": request, "world": world})


def _tv_schematic(db: Session, world, slug: str) -> Schematic:
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s or s.world_id != world.id or (s.is_html and s.html_file):
        raise HTTPException(404)
    return s


@router.get("/display/map/{slug}", response_class=HTMLResponse)
def display_map_page(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """A live map for the table's TV: a bare page inside the second screen, one grid square = one inch once the
    screen size is known. It shows what players see (hidden elements never leave the server)."""
    world, _ = _gm_world(request, db, active_world)
    s = _tv_schematic(db, world, slug)
    return templates.TemplateResponse("schematic_tv.html", {"request": request, "world": world, "schematic": s})


@router.get("/display/map/{slug}/data.json")
def display_map_data(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    s = _tv_schematic(db, world, slug)
    try:
        elements = json.loads(s.elements_json or "[]")
    except ValueError:
        elements = []
    try:
        grid_config = json.loads(s.grid_config_json or "{}")
    except ValueError:
        grid_config = {}
    visible, image, fog_part = player_scene(db, s, elements)
    return {
        "elements": visible,
        "image_url": safe_image_url(image or "") or None,
        "canvas": {"w": s.canvas_width or 2000, "h": s.canvas_height or 1500, "bg": BG_COLORS.get(s.canvas_bg or "dark", "#111111")},
        "grid_type": s.grid_type or "none",
        "grid_config": grid_config if isinstance(grid_config, dict) else {},
        **fog_part,
    }


@router.get("/api/display/state")
def display_state(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    return get_state(world.id)


@router.get("/api/display/stream")
async def display_stream(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    return StreamingResponse(stream_stage(world.id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/display/show")
async def display_show(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Send an image / text / entity to the screen. Body: {"kind": "image", "url", "title"?, "caption"?} |
    {"kind": "text", "text", "title"?} | {"kind": "entity", "entity_id", "mode"?: "image"|"text"}."""
    world, _ = _gm_world(request, db, active_world)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    return set_stage(world.id, _build_item(db, world, body))


@router.post("/api/display/clear")
def display_clear(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    return clear_stage(world.id)
