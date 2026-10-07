"""Walls, doors and fog of war for a schematic - the GM/assistant side.

The geometry itself is validated by app/map_walls.py; what players are sent (and what they are NOT: secret doors) is decided
in app/schematic_payload.fog_payload. Every handler checks the Maps section's edit level and that the map belongs to the
active world, because the route allowlist alone admits every assistant."""
import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import live, map_walls
from ..database import get_db
from ..deps import get_world_ctx, world_can_edit_section
from ..models import Schematic

router = APIRouter()


def _map_for_edit(request: Request, db: Session, slug: str, active_world) -> Schematic:
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404, "No world selected")
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s or s.world_id != world.id or (s.is_html and s.html_file):
        raise HTTPException(404)
    if not world_can_edit_section(request, world, "maps"):
        raise HTTPException(403)
    return s


def _load(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


async def _json_body(request: Request):
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid JSON")


def _state(s: Schematic) -> dict:
    return {"walls": _load(s.walls_json, []), "fog": map_walls.clean_fog(_load(s.fog_json, {}))}


@router.get("/maps/schematic/{slug}/walls.json")
def walls_get(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Everything the editor needs: ALL walls (secret doors included, the GM knows) and the fog settings."""
    return _state(_map_for_edit(request, db, slug, active_world))


@router.post("/maps/schematic/{slug}/walls")
async def walls_save(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Replace the map's walls. Body: {"walls": [{id?, pts: [[x, y], ...], kind?: wall|door|window|secret, state?: open|closed}]}.
    Numbers must be finite and are kept on the canvas; at most 5,000 walls. Returns what was stored plus warnings."""
    s = _map_for_edit(request, db, slug, active_world)
    body = await _json_body(request)
    raw = body.get("walls") if isinstance(body, dict) else None
    try:
        walls, warnings = map_walls.clean_walls(raw, s.canvas_width, s.canvas_height)
    except ValueError as e:
        raise HTTPException(400, str(e))
    s.walls_json = json.dumps(walls)
    db.commit()
    live.touch(s.world_id)
    return {"walls": walls, "warnings": warnings}


@router.post("/maps/schematic/{slug}/fog")
async def fog_save(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Fog settings. Body: {"enabled"?: bool, "range"?: cells (0 = to the nearest wall), "reset"?: true}. `reset` raises the
    epoch, so every browser forgets what it had explored."""
    s = _map_for_edit(request, db, slug, active_world)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    cur = map_walls.clean_fog(_load(s.fog_json, {}))
    new = {**cur}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise HTTPException(400, "enabled must be true or false")
        new["enabled"] = body["enabled"]
    if "range" in body:
        new["range"] = map_walls.clean_fog({"range": body["range"]})["range"]
    if body.get("reset") is True:
        new["epoch"] = cur["epoch"] + 1
    new = map_walls.clean_fog(new)
    s.fog_json = json.dumps(new)
    db.commit()
    live.touch(s.world_id)
    return new


@router.post("/maps/schematic/{slug}/door")
async def door_toggle(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Open or close a door (a secret door too). Body: {"wall_id": "..."}; optional "state": "open"|"closed" sets it instead of
    flipping it. Returns {"wall_id", "state"}."""
    s = _map_for_edit(request, db, slug, active_world)
    body = await _json_body(request)
    wid = body.get("wall_id") if isinstance(body, dict) else None
    if not isinstance(wid, str):
        raise HTTPException(400, "wall_id required")
    walls = _load(s.walls_json, [])
    want = body.get("state") if body.get("state") in ("open", "closed") else None
    for w in walls:
        if w.get("id") == wid and w.get("kind") in ("door", "secret"):
            w["state"] = want or ("closed" if w.get("state") == "open" else "open")
            s.walls_json = json.dumps(walls)
            db.commit()
            live.touch(s.world_id)
            return {"wall_id": wid, "state": w["state"]}
    raise HTTPException(404, "No such door")
