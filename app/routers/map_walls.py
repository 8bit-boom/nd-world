"""Walls, doors and fog of war for a schematic - the GM/assistant side.

The geometry itself is validated by app/map_walls.py; what players are sent (and what they are NOT: secret doors) is decided
in app/schematic_payload.fog_payload. Every handler checks the Maps section's edit level and that the map belongs to the
active world, because the route allowlist alone admits every assistant."""
import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import live, map_rooms, map_walls
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
    lights, _w = map_walls.clean_lights(_load(s.lights_json, []), s.canvas_width, s.canvas_height)
    return {"walls": _load(s.walls_json, []), "fog": map_walls.clean_fog(_load(s.fog_json, {})), "lights": lights}


@router.get("/maps/schematic/{slug}/walls.json")
def walls_get(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Everything the editor needs: ALL walls (secret doors included, the GM knows), the fog and lighting settings, and ALL
    lights (switched-off ones too)."""
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
    """Fog and lighting settings. Body: {"enabled"?: bool, "range"?: squares (0 = to the nearest wall), "darkness"?: 0..0.95 (how
    dark it is where no light reaches), "personal"?: squares (the light every character carries), "strict"?: bool (with fog on, the server sends players only what
    the party has seen - app/map_strict.py), "reset"?: true}. `reset`
    raises the epoch, so every browser forgets what it had explored."""
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
    if "strict" in body:
        if not isinstance(body["strict"], bool):
            raise HTTPException(400, "strict must be true or false")
        new["strict"] = body["strict"]
    if "range" in body:
        new["range"] = map_walls.clean_fog({"range": body["range"]})["range"]
    for key in ("darkness", "personal"):
        if key in body:
            if isinstance(body[key], bool) or not isinstance(body[key], (int, float)):
                raise HTTPException(400, f"{key} must be a number")
            new[key] = body[key]
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


@router.get("/maps/schematic/{slug}/rooms.json")
def rooms_get(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The room-drawing tool's state (see app/map_rooms.py); {} when nothing has been drawn."""
    s = _map_for_edit(request, db, slug, active_world)
    state, _w = map_rooms.clean_rooms(_load(s.rooms_json, {}) or None)
    return state


@router.post("/maps/schematic/{slug}/rooms")
async def rooms_save(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Store the room-drawing state. Body is the state object itself ({cell, ox, oy, cols, rows, cells, spaces, marks}); an
    empty object clears it. The floors and walls are generated by the browser and saved through the elements and walls
    routes - this is only what makes the drawing editable again."""
    s = _map_for_edit(request, db, slug, active_world)
    body = await _json_body(request)
    try:
        state, warnings = map_rooms.clean_rooms(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    s.rooms_json = json.dumps(state)
    db.commit()
    return {"rooms": state, "warnings": warnings}


@router.post("/maps/schematic/{slug}/lights")
async def lights_save(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Replace the map's lights. Body: {"lights": [{id?, x, y, range (squares), color?: "#rrggbb", intensity?: 0.1..1, on?: bool,
    label?}]}. Finite numbers only, kept on the canvas, at most 60. Returns what was stored plus warnings."""
    s = _map_for_edit(request, db, slug, active_world)
    body = await _json_body(request)
    raw = body.get("lights") if isinstance(body, dict) else None
    try:
        lights, warnings = map_walls.clean_lights(raw, s.canvas_width, s.canvas_height)
    except ValueError as e:
        raise HTTPException(400, str(e))
    s.lights_json = json.dumps(lights)
    db.commit()
    live.touch(s.world_id)
    return {"lights": lights, "warnings": warnings}


@router.post("/maps/schematic/{slug}/light")
async def light_toggle(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Switch one light on or off, live for players and the table screen. Body: {"id", "on"?: bool} (flips it when `on` is left out)."""
    s = _map_for_edit(request, db, slug, active_world)
    body = await _json_body(request)
    lid = body.get("id") if isinstance(body, dict) else None
    if not isinstance(lid, str):
        raise HTTPException(400, "id required")
    lights, _w = map_walls.clean_lights(_load(s.lights_json, []), s.canvas_width, s.canvas_height)
    for l in lights:
        if l["id"] == lid:
            l["on"] = body["on"] if isinstance(body.get("on"), bool) else not l["on"]
            s.lights_json = json.dumps(lights)
            db.commit()
            live.touch(s.world_id)
            return {"id": lid, "on": l["on"]}
    raise HTTPException(404, "No such light")
