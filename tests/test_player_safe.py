"""_is_player_safe (app/main.py) is the entire authorization boundary for a
long list of routers — /combat, /tables, /quests, /sessions, /calendar,
/import, and most of main.py's own routes have no in-handler auth of their
own, so a regression in this allowlist directly exposes GM-only/destructive
routes to any logged-in player. Table-driven so a future accidental broadening
(e.g. a careless prefix match) shows up as a one-line failure here instead of
a live incident.
"""
import pytest

from app.main import _is_player_safe

# (method, path, expected_player_safe)
CASES = [
    # GM-only — no in-handler auth of their own, so this allowlist is the only
    # thing standing between a player and these routes.
    ("GET", "/combat", False),
    ("GET", "/tables", False),
    ("GET", "/quests", False),
    ("GET", "/sessions", False),
    ("GET", "/calendar", False),
    ("GET", "/import", False),
    ("POST", "/api/ai/chat", False),
    ("GET", "/admin/backup.zip", False),
    ("POST", "/worlds/1/delete", False),
    ("GET", "/worlds", False),
    ("GET", "/settings", False),
    ("POST", "/entity/new", False),
    ("GET", "/characters/templates", False),  # explicitly excluded despite the /characters prefix
    # Player-safe — read-only world/lore browsing and their own character(s).
    ("GET", "/", True),
    ("GET", "/rules", True),
    ("GET", "/search", True),
    ("GET", "/maps", True),
    ("GET", "/characters", True),
    ("GET", "/characters/new", True),
    ("POST", "/api/characters/5/hp-async", True),
    ("GET", "/entity/5", True),
    ("GET", "/kind/character", True),
    ("GET", "/uploads/portraits/x.png", True),
    ("GET", "/maps/some-map", True),
    ("GET", "/worlds/switch/some-world", True),
]


@pytest.mark.parametrize("method,path,expected", CASES)
def test_is_player_safe(method, path, expected):
    assert _is_player_safe(method, path) is expected, f"{method} {path} expected player_safe={expected}"


def test_non_get_defaults_to_gm_only_unless_explicitly_allowlisted():
    """New routes are GM-only by default — a POST/PUT/DELETE must be matched by
    an explicit regex above the `method != "GET"` cutoff to be player-safe."""
    assert _is_player_safe("POST", "/some/brand/new/route") is False
    assert _is_player_safe("DELETE", "/entity/5") is False
