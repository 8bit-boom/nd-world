"""What the table is allowed to see of a schematic.

ONE function decides which elements leave the server for a player's phone or the TV on the table: anything the GM
marked hidden (the 👁 toggle) stays home, and a token is shown only when it is visible to players. The live player view,
view.json and the second screen all go through it, so a new surface cannot forget the rule. A leaf module (no router
imports) so any router can use it.
"""
import json

from . import map_strict, map_walls


def player_visible(elements) -> list:
    """The elements a player may be sent. Non-dict junk is dropped rather than trusted."""
    return [
        el for el in (elements or [])
        if isinstance(el, dict)
        and not el.get("hidden")
        and (el.get("type") != "token" or el.get("visible_to_players", True))
    ]


# canvas background presets (the editor's "dark / blueprint / ..." choices)
BG_COLORS = {"dark": "#111111", "blueprint": "#0d1b2a", "grid-light": "#1a1a2e", "light": "#f0f0f0"}


def fog_payload(schematic) -> dict:
    """The fog and lighting part of what a player (or the table's TV) is sent: {"fog": settings, "walls": [...], "lights": [...]}.

    The walls are sent ONLY while fog or darkness is switched on - they exist for the browser to compute what a token can see
    and where shadows fall - and pass through map_walls.for_player, so a secret door reaches players as an ordinary wall.
    Lights are sent only while darkness is above zero, and only the ones that are on."""
    def load(raw, default):
        try:
            v = json.loads(raw or "")
            return v if isinstance(v, type(default)) else default
        except ValueError:
            return default
    fog = map_walls.clean_fog(load(getattr(schematic, "fog_json", None), {}))
    active = fog["enabled"] or fog["darkness"] > 0
    walls = map_walls.for_player(load(getattr(schematic, "walls_json", None), [])) if active else []
    lights = []
    if fog["darkness"] > 0:
        stored, _w = map_walls.clean_lights(load(getattr(schematic, "lights_json", None), []), schematic.canvas_width, schematic.canvas_height)
        lights = [{k: l[k] for k in ("id", "x", "y", "range", "color", "intensity")} for l in stored if l["on"]]
    return {"fog": fog, "walls": walls, "lights": lights}


def _load(raw, default):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


def grid_cell(schematic) -> float:
    cfg = _load(getattr(schematic, "grid_config_json", None), {})
    c = cfg.get("cell_size") if isinstance(cfg, dict) else None
    return float(c) if (getattr(schematic, "grid_type", "none") or "none") != "none" and isinstance(c, (int, float)) and c > 0 else 50.0


def strict_view(db, schematic):
    """(View, fog) when strict secrecy is on for this map, else (None, fog). Records newly explored squares."""
    fog = map_walls.clean_fog(_load(getattr(schematic, "fog_json", None), {}))
    if not map_strict.is_active(fog):
        return None, fog
    elements = _load(schematic.elements_json, [])
    walls = _load(schematic.walls_json, [])
    stored = _load(getattr(schematic, "explored_json", None), {})
    view = map_strict.compute(elements, walls, fog, stored, schematic.canvas_width, schematic.canvas_height, grid_cell(schematic))
    if view.changed and db is not None:
        try:
            from .models import Schematic
            record = json.dumps(map_strict.stored_record(view, fog["epoch"]))
            db.query(Schematic).filter(Schematic.id == schematic.id).update({"explored_json": record}, synchronize_session=False)
            db.commit()
        except Exception:                                           # a locked database must not break the page; it is recorded next time
            db.rollback()
    return view, fog


def player_scene(db, schematic, elements=None):
    """EVERYTHING a player (or the table's TV) is sent about a map, in one place: (elements, image_url, fog_payload).

    Hidden elements stay home always; under strict secrecy only what the party has seen is sent (app/map_strict.py), the picture
    is the masked copy, and the payload carries the explored area. Every player-facing surface goes through this."""
    if elements is None:
        elements = _load(schematic.elements_json, [])
    visible = player_visible(elements)
    payload = fog_payload(schematic)
    image = getattr(schematic, "image_url", None)
    view, fog = strict_view(db, schematic)
    if view is not None:
        visible = map_strict.filter_elements(visible, view)
        payload["walls"] = map_strict.filter_walls(payload["walls"], view)
        payload["lights"] = map_strict.filter_lights(payload["lights"], view)
        payload["explored"] = view.runs()
        if image:
            import hashlib
            tag = hashlib.sha1(json.dumps(payload["explored"]["runs"]).encode()).hexdigest()[:10]
            image = f"/maps/schematic/{schematic.slug}/bg.webp?v={tag}"
    return visible, image, payload


def map_is_strict(schematic) -> bool:
    return map_strict.is_active(map_walls.clean_fog(_load(getattr(schematic, "fog_json", None), {})))
