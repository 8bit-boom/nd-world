"""What the table is allowed to see of a schematic.

ONE function decides which elements leave the server for a player's phone or the TV on the table: anything the GM
marked hidden (the 👁 toggle) stays home, and a token is shown only when it is visible to players. The live player view,
view.json and the second screen all go through it, so a new surface cannot forget the rule. A leaf module (no router
imports) so any router can use it.
"""
import json

from . import map_walls


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
    """The fog part of what a player (or the table's TV) is sent: {"fog": settings, "walls": [...]}.

    The walls are sent ONLY while fog is switched on - they exist for the browser to compute what a token can see - and
    pass through map_walls.for_player, so a secret door reaches players as an ordinary wall."""
    def load(raw, default):
        try:
            v = json.loads(raw or "")
            return v if isinstance(v, type(default)) else default
        except ValueError:
            return default
    fog = map_walls.clean_fog(load(getattr(schematic, "fog_json", None), {}))
    walls = map_walls.for_player(load(getattr(schematic, "walls_json", None), [])) if fog["enabled"] else []
    return {"fog": fog, "walls": walls}
