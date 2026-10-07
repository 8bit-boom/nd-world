"""What the table is allowed to see of a schematic.

ONE function decides which elements leave the server for a player's phone or the TV on the table: anything the GM
marked hidden (the 👁 toggle) stays home, and a token is shown only when it is visible to players. The live player view,
view.json and the second screen all go through it, so a new surface cannot forget the rule. A leaf module (no router
imports) so any router can use it.
"""


def player_visible(elements) -> list:
    """The elements a player may be sent. Non-dict junk is dropped rather than trusted."""
    return [
        el for el in (elements or [])
        if isinstance(el, dict)
        and not el.get("hidden")
        and (el.get("type") != "token" or el.get("visible_to_players", True))
    ]
