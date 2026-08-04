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
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..constants import KINDS
from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity, GameSession, InvestBoard, Quest, Schematic, World
from ..templating import templates

router = APIRouter()

_MAX_HOME_SECTIONS = 20
_MAX_LINKS_PER_SECTION = 50
_HOME_LINK_TYPES = {"entity", "session", "quest", "board", "schematic", "map", "kind", "url"}

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


def _sanitize_link(raw) -> Optional[dict]:
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
    if target_type == "kind" and target_ref not in KINDS:
        return None
    return {
        "label": label,
        "icon": str(raw.get("icon", "")).strip()[:32],
        "target_type": target_type,
        "target_ref": target_ref,
        "visible_to_players": bool(raw.get("visible_to_players", True)),
    }


def _sanitize_sections(raw_json: str) -> list[dict]:
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
        links = [l for l in (_sanitize_link(x) for x in raw_links[:_MAX_LINKS_PER_SECTION]) if l]
        out.append({
            "name": name,
            "visible_to_players": bool(sec.get("visible_to_players", True)),
            "links": links,
        })
    return out


@router.get("/worlds/{world_id}/home/edit", response_class=HTMLResponse)
def world_home_edit_form(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    world, worlds = get_world_ctx(request, db, active_world)
    sections = _sanitize_sections(w.home_sections_json)
    if not sections:
        sections = [{"name": "Quick Links", "visible_to_players": True, "links": []}]
    return templates.TemplateResponse("home_edit.html", {
        "request": request, "world": world, "worlds": worlds, "edit_world": w,
        "initial_sections": sections,
        "kinds": KINDS,
        "entities": db.query(Entity).filter(Entity.world_id == w.id).order_by(Entity.name).all(),
        "sessions": db.query(GameSession).filter(GameSession.world_id == w.id).order_by(GameSession.session_num).all(),
        "quests": db.query(Quest).filter(Quest.world_id == w.id).order_by(Quest.title).all(),
        "boards": db.query(InvestBoard).filter(InvestBoard.world_id == w.id).order_by(InvestBoard.name).all(),
        "schematics": db.query(Schematic).filter(Schematic.world_id == w.id).order_by(Schematic.name).all(),
        "maps": _world_maps(w.id),
    })


@router.post("/worlds/{world_id}/home/edit")
async def world_home_edit_post(world_id: int, request: Request, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    form = await request.form()
    w.home_welcome_md = str(form.get("home_welcome_md", "")).strip() or None
    w.home_sections_json = json.dumps(_sanitize_sections(str(form.get("home_sections_json", "[]") or "[]")))
    db.commit()
    return RedirectResponse("/", status_code=303)
