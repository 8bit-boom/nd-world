"""GM Cockpit (Phase 3) — a modular workspace of draggable, resizable
"windows" where the GM keeps everything they'd otherwise open in browser
tabs: the live battle map, dice tray, party vitals, active quests, any
entity's page, scratch notes, AI Chat.

Everything is a panel. Panels are added/removed/resized/rearranged freely
(the Add-panel modal lists the types), and the whole arrangement — plus
named presets like "Combat" / "Exploration" — is saved per world
(World.cockpit_ws_json) through the sanitizing workspace routes below, so
it follows the GM across browsers and devices. Live panels (party vitals,
quests) re-fetch over the Phase-1 live-sync bus; window content is either
an embedded live page (?embed=1 chrome-less mode) or a client-side render
of an existing JSON endpoint — no re-implementations of features.

GM-only by default (no _is_player_safe/_is_assistant_safe entry): it's a
composition of GM-facing tools, and the nav entry (nav_menus.py, id
"cockpit") is gm_only to match.
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import Party, Schematic
from ..templating import templates

router = APIRouter()

# The panel types the Add-panel modal offers. Everything else in a posted
# layout is dropped by the sanitizer.
PANEL_TYPES = {"map", "wmap", "dice", "party", "quests", "entity", "ecard",
               "notes", "ai", "combat", "tables", "calendar"}

MAX_PANELS = 24          # per layout
MAX_PRESETS = 12         # named layouts per world
MAX_PANELS_BYTES = 128 * 1024  # whole workspace JSON cap

_ALLOWED_PANEL_KEYS = {"id", "type", "ref", "title", "x", "y", "w", "h", "z",
                       "collapsed", "accent", "data"}

_ACCENT_RE = re.compile(r"^$|^#[0-9a-fA-F]{6}$")


def _clamp_int(v, lo, hi, default=0):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _sanitize_panels(raw_panels) -> list:
    """Round a posted panel list into the exact saved shape — unknown keys
    dropped, types allowlisted, geometry clamped, strings bounded. Raises
    ValueError on structural nonsense (not a list, too many panels)."""
    if not isinstance(raw_panels, list):
        raise ValueError("panels must be a list")
    if len(raw_panels) > MAX_PANELS:
        raise ValueError(f"too many panels (max {MAX_PANELS})")
    out = []
    for p in raw_panels[:MAX_PANELS]:
        if not isinstance(p, dict) or p.get("type") not in PANEL_TYPES:
            continue
        data = p.get("data")
        accent = str(p.get("accent") or "")
        out.append({
            "id": str(p.get("id") or "")[:40],
            "type": p["type"],
            "ref": str(p.get("ref") or "")[:120],
            "title": str(p.get("title") or "")[:120],
            "x": _clamp_int(p.get("x"), 0, 8000),
            "y": _clamp_int(p.get("y"), 0, 8000),
            "w": _clamp_int(p.get("w"), 160, 8000, default=420),
            "h": _clamp_int(p.get("h"), 120, 8000, default=300),
            "z": _clamp_int(p.get("z"), 0, 100000),
            "collapsed": bool(p.get("collapsed")),
            "accent": accent if _ACCENT_RE.match(accent) else "",
            "data": {"text": str((data or {}).get("text") or "")[:8000]}
            if isinstance(data, dict) else {},
        })
    return out


def _sanitize_workspace(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("workspace must be an object")
    presets_raw = raw.get("presets")
    if presets_raw is not None and not isinstance(presets_raw, dict):
        raise ValueError("presets must be an object")
    presets = {}
    for name, layout in list((presets_raw or {}).items())[:MAX_PRESETS]:
        name = str(name).strip()[:40]
        if not name:
            continue
        presets[name] = {"panels": _sanitize_panels((layout or {}).get("panels"))}
    return {
        "current": {"panels": _sanitize_panels((raw.get("current") or {}).get("panels"))},
        "presets": presets,
    }


def _load_workspace(db: Session, world) -> Optional[dict]:
    try:
        ws = json.loads(world.cockpit_ws_json) if world.cockpit_ws_json else None
    except ValueError:
        ws = None
    return ws if isinstance(ws, dict) else None


@router.get("/cockpit", response_class=HTMLResponse)
def cockpit(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)

    # Pickers for the Add-panel modal: floatable maps (non-HTML schematics
    # — HTML-type maps are external files with no embeddable view), the
    # world's file-based maps (the Leaflet viewer at /maps/{slug} — the
    # JSON-marker maps under <DB dir>/maps), and the world's parties.
    # Entities come from the existing /api/entities/picker (fetched on first
    # use of the modal, client-side filtered).
    maps = (
        db.query(Schematic)
        .filter(Schematic.world_id == world.id, Schematic.is_html.is_(False))
        .order_by(Schematic.name)
        .all()
    )
    parties = db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all()

    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "maps_json": [{"slug": s.slug, "name": s.name} for s in maps],
        "world_maps_json": _world_maps(world.id),
        "parties_json": [{"id": p.id, "name": p.name} for p in parties],
    })


def _maps_dir() -> Path:
    # Same derivation as main.py's _MAPS_DIR (routers can't import main —
    # circular): the maps dir sits beside the SQLite file.
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "maps"


def _world_maps(world_id: int) -> list:
    """The world's file-based (Leaflet) maps for the cockpit's world-map
    picker: [{slug, name, markers}]. Mirrors main.py's _iter_world_maps
    shape tolerantly — malformed/legacy files are skipped, never raised —
    kept local because importing main from a router would be circular."""
    out = []
    maps_dir = _maps_dir()
    if not maps_dir.exists():
        return out
    for jf in sorted(maps_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("world_id", 1) != world_id:
            continue
        markers = data.get("markers")
        out.append({
            "slug": jf.stem,
            "name": str(data.get("name") or jf.stem)[:120],
            "markers": len(markers) if isinstance(markers, list) else 0,
        })
    return out


@router.get("/api/cockpit/workspace")
async def cockpit_get_workspace(request: Request, db: Session = Depends(get_db),
                                active_world: str = Cookie(None)):
    """The saved workspace for the active world (or null if the GM never
    arranged this world's cockpit)."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_gm:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    return {"workspace": _load_workspace(db, world)}


@router.post("/api/cockpit/workspace")
async def cockpit_save_workspace(request: Request, db: Session = Depends(get_db),
                                 active_world: str = Cookie(None)):
    """Save (and switch/replace) the workspace: current panels plus named
    presets. Sanitized and size-capped — this is GM-authored UI state, not
    free-form storage."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_gm:
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    body = await request.json()
    try:
        ws = _sanitize_workspace(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    dumped = json.dumps(ws)
    if len(dumped) > MAX_PANELS_BYTES:
        raise HTTPException(400, "Workspace too large — trim notes or panels.")
    world.cockpit_ws_json = dumped
    db.commit()
    return {"ok": True}
