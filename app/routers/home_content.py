"""GM-customizable home page content: a welcome/announcement blurb plus an
ordered list of GM-defined tabs/sections (e.g. "Session Prep", "Player
Reference"), each holding its own ordered list of curated Quick Links.

Storage lives on World itself (home_welcome_md, home_sections_json) rather
than a new table — see app/models.py's World class docstring-style comments
for the exact JSON shape. This router only handles the edit form; rendering
the saved content on the actual home page (/) happens in app/main.py's
home() + _resolve_home_sections(), since that's where the page is already
built.
"""
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import deps
from ..database import get_db, get_app_settings
from ..deps import get_world_ctx
from ..imaging import convert_image
from ..models import Entity, GameSession, InvestBoard, Quest, Schematic, World
from ..templating import templates
from ..uploads import copy_upload_bounded

router = APIRouter()

_MAX_HOME_SECTIONS = 20
_MAX_LINKS_PER_SECTION = 50
_MAX_PINNED_TILES = 24
_HOME_LINK_TYPES = {"entity", "session", "quest", "board", "schematic", "map", "kind", "url"}

# Duplicated locally (same rationale as _MAPS_DIR below, and characters.py's
# _upload_portrait) — routers can't import these from main.py without a
# circular import.
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _upload_home_background(file: Optional[UploadFile], db: Session) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        return None
    target_dir = _UPLOADS_DIR / "home"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{uuid.uuid4().hex}{ext}"
    copy_upload_bounded(file, dest)
    settings = get_app_settings(db)
    dest = convert_image(dest, static_format=settings.static_format, animated_format=settings.animated_format)
    return f"/uploads/home/{dest.name}"

# Duplicated locally rather than imported from main.py — main.py imports this
# router, so the reverse would be circular. Same rationale/pattern as
# app/routers/importer.py's own _MAPS_DIR/_world_maps copy.
_MAPS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "maps"


def _world_maps(world_id: int) -> list[tuple[str, str]]:
    out = []
    if not _MAPS_DIR.exists():
        return out
    for jf in sorted(_MAPS_DIR.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("world_id", 1) == world_id:
            out.append((jf.stem, data.get("name", jf.stem)))
    return out


def _sanitize_link(raw, kinds) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label", "")).strip()[:200]
    target_type = str(raw.get("target_type", "")).strip()
    target_ref = str(raw.get("target_ref", "")).strip()
    if not label or target_type not in _HOME_LINK_TYPES or not target_ref:
        return None
    if target_type == "url" and not (
        target_ref.startswith("http://") or target_ref.startswith("https://") or target_ref.startswith("/")
    ):
        # Blocks javascript: (and other unlisted schemes) from being stored —
        # this app has no CSRF/output-sanitization layer to lean on elsewhere,
        # and this href is rendered directly for players to click.
        return None
    if target_type == "kind" and target_ref not in kinds:
        return None
    return {
        "label": label,
        "icon": str(raw.get("icon", "")).strip()[:32],
        "target_type": target_type,
        "target_ref": target_ref,
        "visible_to_players": bool(raw.get("visible_to_players", True)),
    }


def _sanitize_sections(raw_json: str, kinds) -> list[dict]:
    try:
        data = json.loads(raw_json or "[]")
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    out = []
    for sec in data[:_MAX_HOME_SECTIONS]:
        if not isinstance(sec, dict):
            continue
        name = str(sec.get("name", "")).strip()[:100] or "Untitled"
        raw_links = sec.get("links") or []
        links = [l for l in (_sanitize_link(x, kinds) for x in raw_links[:_MAX_LINKS_PER_SECTION]) if l]
        out.append({
            "name": name,
            "visible_to_players": bool(sec.get("visible_to_players", True)),
            "links": links,
        })
    return out


def _sanitize_pinned_tiles(raw_json: str, kinds) -> list[dict]:
    try:
        data = json.loads(raw_json or "[]")
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    return [l for l in (_sanitize_link(x, kinds) for x in data[:_MAX_PINNED_TILES]) if l]


def _sanitize_hidden_kinds(raw_json, kinds) -> list[str]:
    """A list of kind ids the GM has hidden from the home page's default
    stat-tile dashboard. Accepts either a JSON string (as stored) or an
    already-parsed list (the two callers below). Anything not a currently
    valid kind id for this world is dropped, so this can't be used to smuggle
    in an arbitrary string or keep a stale id alive after a custom kind is
    deleted."""
    if isinstance(raw_json, str):
        try:
            data = json.loads(raw_json or "[]")
        except Exception:
            data = []
    else:
        data = raw_json
    if not isinstance(data, list):
        data = []
    kind_set = set(kinds)
    seen = []
    for k in data:
        if isinstance(k, str) and k in kind_set and k not in seen:
            seen.append(k)
    return seen


@router.get("/worlds/{world_id}/home/edit", response_class=HTMLResponse)
def world_home_edit_form(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    world, worlds = get_world_ctx(request, db, active_world)
    world_kinds = deps.effective_kinds(w)[0]
    sections = _sanitize_sections(w.home_sections_json, world_kinds)
    if not sections:
        sections = [{"name": "Quick Links", "visible_to_players": True, "links": []}]
    return templates.TemplateResponse("home_edit.html", {
        "request": request, "world": world, "worlds": worlds, "edit_world": w,
        "initial_sections": sections,
        "initial_pinned_tiles": _sanitize_pinned_tiles(w.home_pinned_tiles_json, world_kinds),
        "initial_hidden_kinds": _sanitize_hidden_kinds(w.home_hidden_kinds_json, world_kinds),
        "kinds": world_kinds,
        "kind_icons": deps.effective_kinds(w)[1],
        "entities": db.query(Entity).filter(Entity.world_id == w.id).order_by(Entity.name).all(),
        "sessions": db.query(GameSession).filter(GameSession.world_id == w.id).order_by(GameSession.session_num).all(),
        "quests": db.query(Quest).filter(Quest.world_id == w.id).order_by(Quest.title).all(),
        "boards": db.query(InvestBoard).filter(InvestBoard.world_id == w.id).order_by(InvestBoard.name).all(),
        "schematics": db.query(Schematic).filter(Schematic.world_id == w.id).order_by(Schematic.name).all(),
        "maps": _world_maps(w.id),
    })


@router.post("/worlds/{world_id}/home/edit")
async def world_home_edit_post(
    world_id: int, request: Request,
    home_background_file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    form = await request.form()
    world_kinds = deps.effective_kinds(w)[0]
    w.home_welcome_md = str(form.get("home_welcome_md", "")).strip() or None
    w.home_sections_json = json.dumps(_sanitize_sections(
        str(form.get("home_sections_json", "[]") or "[]"), world_kinds,
    ))
    w.home_pinned_tiles_json = json.dumps(_sanitize_pinned_tiles(
        str(form.get("home_pinned_tiles_json", "[]") or "[]"), world_kinds,
    ))
    w.home_hidden_kinds_json = json.dumps(_sanitize_hidden_kinds(
        str(form.get("home_hidden_kinds_json", "[]") or "[]"), world_kinds,
    ))
    w.home_title = str(form.get("home_title", "")).strip()[:200] or None
    w.home_subtitle = str(form.get("home_subtitle", "")).strip()[:300] or None

    uploaded = _upload_home_background(home_background_file, db)
    if uploaded:
        w.home_background_url = uploaded
    elif str(form.get("remove_home_background", "")).strip():
        w.home_background_url = None
    else:
        pasted = str(form.get("home_background_url", "")).strip()[:512]
        if pasted:
            if pasted.startswith("http://") or pasted.startswith("https://") or pasted.startswith("/"):
                w.home_background_url = pasted
            # Silently ignored otherwise (e.g. javascript:) — same
            # url-scheme guard as _sanitize_link, no error surfaced since
            # this is one field in a bigger settings form, not its own page.

    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/api/worlds/{world_id}/home/quick-link")
async def world_home_quick_link(world_id: int, request: Request, db: Session = Depends(get_db)):
    """Append one link to a home-page section without the full edit-form
    round trip — the drag-a-nav-tab-onto-the-home-page interaction
    (app/templates/base.html's dragstart + index.html's drop handlers)
    posts here. Reuses the same sanitizer as the edit form; the only
    values this particular caller ever sends are target_type "kind" or
    "url" (see base.html's dragstart handler), but nothing here assumes
    that — any type _sanitize_link accepts is handled identically."""
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    world_kinds = deps.effective_kinds(w)[0]
    link = _sanitize_link({
        "label": payload.get("label", ""),
        "icon": "",
        "target_type": payload.get("target_type", ""),
        "target_ref": payload.get("target_ref", ""),
        "visible_to_players": True,
    }, world_kinds)
    if not link:
        raise HTTPException(400, "Invalid link")
    sections = _sanitize_sections(w.home_sections_json, world_kinds)
    idx = payload.get("section_index")
    if not isinstance(idx, int) or not (0 <= idx < len(sections)):
        if not sections:
            sections = [{"name": "Quick Links", "visible_to_players": True, "links": []}]
        idx = 0
    if len(sections[idx]["links"]) >= _MAX_LINKS_PER_SECTION:
        raise HTTPException(400, "Section is full")
    sections[idx]["links"].append(link)
    w.home_sections_json = json.dumps(sections)
    db.commit()
    return {"ok": True, "section_index": idx}


@router.post("/api/worlds/{world_id}/home/pinned-tile")
async def world_home_pinned_tile(world_id: int, request: Request, db: Session = Depends(get_db)):
    """Append one tile to the home page's stat-tile dashboard — the
    drag-a-nav-tab-onto-the-dashboard interaction (index.html) posts here,
    same payload shape and dragstart source as world_home_quick_link above,
    just landing in home_pinned_tiles_json instead of a Quick Links
    section."""
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    world_kinds = deps.effective_kinds(w)[0]
    tile = _sanitize_link({
        "label": payload.get("label", ""),
        "icon": "",
        "target_type": payload.get("target_type", ""),
        "target_ref": payload.get("target_ref", ""),
        "visible_to_players": True,
    }, world_kinds)
    if not tile:
        raise HTTPException(400, "Invalid tile")
    tiles = _sanitize_pinned_tiles(w.home_pinned_tiles_json, world_kinds)
    if len(tiles) >= _MAX_PINNED_TILES:
        raise HTTPException(400, "Dashboard is full")
    tiles.append(tile)
    w.home_pinned_tiles_json = json.dumps(tiles)
    db.commit()
    return {"ok": True, "tile": tile}


@router.post("/api/worlds/{world_id}/home/pinned-tile/remove")
async def world_home_pinned_tile_remove(world_id: int, request: Request, db: Session = Depends(get_db)):
    """Removes one tile from the home page's pinned dashboard by its
    position in the saved list — the ✕ button rendered directly on each
    pinned tile in index.html (GM view only) posts here, so deleting a
    pinned tile no longer requires a trip through the full /home/edit form.
    Same target audience/shape as world_home_pinned_tile above, just the
    inverse operation."""
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    index = payload.get("index")
    if not isinstance(index, int):
        raise HTTPException(400, "Invalid index")
    world_kinds = deps.effective_kinds(w)[0]
    tiles = _sanitize_pinned_tiles(w.home_pinned_tiles_json, world_kinds)
    if not (0 <= index < len(tiles)):
        raise HTTPException(400, "No such tile")
    tiles.pop(index)
    w.home_pinned_tiles_json = json.dumps(tiles)
    db.commit()
    return {"ok": True}


@router.post("/api/worlds/{world_id}/home/hide-kind")
async def world_home_hide_kind(world_id: int, request: Request, db: Session = Depends(get_db)):
    """Hides one built-in/custom kind's tile from the home page's default
    stat-tile dashboard — the hover ✕ on each tile (index.html, GM view
    only) posts here. Idempotent: hiding an already-hidden kind is a no-op,
    not an error. The Default Tiles checklist on home_edit.html is the
    inverse (un-hide / bulk manage)."""
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    kind = payload.get("kind")
    world_kinds = deps.effective_kinds(w)[0]
    if not isinstance(kind, str) or kind not in world_kinds:
        raise HTTPException(400, "Not a valid kind for this world")
    hidden = _sanitize_hidden_kinds(w.home_hidden_kinds_json, world_kinds)
    if kind not in hidden:
        hidden.append(kind)
    w.home_hidden_kinds_json = json.dumps(hidden)
    db.commit()
    return {"ok": True}
