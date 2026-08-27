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
    ("POST", "/worlds/1/theme/import", False),
    ("POST", "/worlds/1/theme/clear", False),
    ("GET", "/worlds", False),
    ("GET", "/settings", False),
    ("POST", "/settings/system", False),
    ("POST", "/maps/some-map/rename", False),
    ("POST", "/maps/some-map/delete", False),
    ("GET", "/maps/schematic/some-slug", False),
    ("POST", "/maps/schematic/some-slug/rename", False),
    ("POST", "/maps/schematic/some-slug/delete", False),
    ("POST", "/entity/new", False),
    ("GET", "/characters/templates", False),  # explicitly excluded despite the /characters prefix
    ("GET", "/worlds/1/home/edit", False),
    ("POST", "/worlds/1/home/edit", False),
    ("POST", "/api/worlds/1/home/quick-link", False),
    ("GET", "/facts", False),
    ("POST", "/facts/new", False),
    ("POST", "/facts/5/edit", False),
    ("POST", "/facts/5/delete", False),
    ("POST", "/api/facts/parse", False),
    ("POST", "/api/facts/bulk", False),
    ("POST", "/api/entities/bulk-visibility", False),
    # /export and everything under it (including the new Rules-and-Notes
    # bundle) is GM-only — it includes notes hidden from players unfiltered,
    # same trust level as /admin/backup.zip.
    ("GET", "/export", False),
    ("GET", "/export/rules-and-notes.md", False),
    ("GET", "/export/foundry.json", False),
    # The dedicated GM "/ai" World Chat page (quick prompts, image gen, etc.)
    # stays GM-only — only the shared streaming endpoint it and the entity
    # panel both call is opened up, gated per-world by players_can_ask_ai.
    ("GET", "/ai", False),
    ("POST", "/audio/upload", False),
    ("POST", "/audio/1/edit", False),
    ("POST", "/audio/1/delete", False),
    ("POST", "/audio/albums/new", False),
    ("POST", "/audio/albums/1/rename", False),
    ("POST", "/audio/albums/1/delete", False),
    ("POST", "/video/upload", False),
    ("POST", "/video/1/edit", False),
    ("POST", "/video/1/delete", False),
    ("POST", "/video/albums/new", False),
    ("POST", "/video/albums/1/rename", False),
    ("POST", "/video/albums/1/delete", False),
    # Player-safe — read-only world/lore browsing and their own character(s).
    ("GET", "/", True),
    ("GET", "/account", True),
    ("POST", "/account/name", True),
    ("POST", "/account/password", True),
    ("GET", "/rules", True),
    ("GET", "/rules/download.md", True),
    ("GET", "/audio", True),
    ("GET", "/audio/albums/1", True),
    ("GET", "/video", True),
    ("GET", "/video/albums/1", True),
    ("GET", "/search", True),
    ("GET", "/maps", True),
    # A plain map named e.g. "Schematic Vault" slugifies to "schematic-vault" —
    # confirms the /maps/schematic exclusion above is segment-anchored, not a
    # bare string-prefix check that would false-positive-403 this.
    ("GET", "/maps/schematic-vault", True),
    ("GET", "/characters", True),
    ("GET", "/characters/new", True),
    ("POST", "/api/characters/5/hp-async", True),
    ("GET", "/entity/5", True),
    ("GET", "/entity/5/download.md", True),
    ("GET", "/api/entity/5/preview", True),
    ("GET", "/api/hover-preview/config", True),
    ("GET", "/kind/character", True),
    ("GET", "/kind/character/download.zip", True),
    ("GET", "/kind/character/download-selected.zip", True),
    ("POST", "/api/ai/stream", True),
    ("POST", "/api/ai/attachments/upload", True),
    ("POST", "/api/ai/attachments/audio-jobs", True),
    ("POST", "/api/ai/attachments/audio-jobs/chunk", True),
    ("POST", "/api/ai/attachments/audio-jobs/complete", True),
    ("GET", "/api/ai/attachments/audio-jobs/5", True),
    ("GET", "/api/ai/attachments/audio-jobs", True),
    ("POST", "/api/sessions/ai/audio-jobs", False),
    ("GET", "/api/sessions/ai/audio-jobs/5", False),
    ("GET", "/uploads/portraits/x.png", True),
    ("GET", "/maps/some-map", True),
    ("GET", "/worlds/switch/some-world", True),
    ("GET", "/chronicler", True),
    ("POST", "/api/chronicler/ask", True),
    ("GET", "/session-log", True),
    ("GET", "/session-log/5", True),
    ("POST", "/api/session-log/5/recap", True),
]


@pytest.mark.parametrize("method,path,expected", CASES)
def test_is_player_safe(method, path, expected):
    assert _is_player_safe(method, path) is expected, f"{method} {path} expected player_safe={expected}"


def test_non_get_defaults_to_gm_only_unless_explicitly_allowlisted():
    """New routes are GM-only by default — a POST/PUT/DELETE must be matched by
    an explicit regex above the `method != "GET"` cutoff to be player-safe."""
    assert _is_player_safe("POST", "/some/brand/new/route") is False
    assert _is_player_safe("DELETE", "/entity/5") is False
