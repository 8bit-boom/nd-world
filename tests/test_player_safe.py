"""_is_player_safe (app/main.py) is the authorization boundary that decides
whether a route is even REACHABLE to a logged-in player at all — a route
absent from it is 403'd by the auth_gate middleware before any handler
code runs. Most write/destructive routes have no in-handler auth of their
own, so a regression here directly exposes them. Most read routes covered
by World.section_access_json (combat/sessions/import/facts/export/ai/
boards/imagestudio/editor/code-assist/dice/races/professions/... — see
deps.SECTION_PERMISSION_IDS) ARE reachable, but gated in-handler by
deps.world_can_view_section/world_row_visible, off by default for every
world that hasn't opted in — this file only tests reachability, not that
off-by-default gate (see test_player_section_access.py for that). Table-
driven so a future accidental broadening (e.g. a careless prefix match)
shows up as a one-line failure here instead of a live incident.
"""
import pytest

from app.main import _is_player_safe

# (method, path, expected_player_safe)
CASES = [
    # GM-only — no in-handler auth of their own, so this allowlist is the only
    # thing standing between a player and these routes.
    ("POST", "/api/ai/chat", False),
    ("GET", "/admin/backup.zip", False),
    ("POST", "/worlds/1/delete", False),
    ("POST", "/worlds/1/theme/import", False),
    ("POST", "/worlds/1/theme/clear", False),
    ("POST", "/worlds/1/members/1/reset-password", False),
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
    ("POST", "/facts/new", False),
    ("POST", "/facts/5/edit", False),
    ("POST", "/facts/5/delete", False),
    ("POST", "/api/facts/parse", False),
    ("POST", "/api/facts/bulk", False),
    ("POST", "/api/entities/bulk-visibility", False),
    # Everything under /export EXCEPT the hub page itself is GM-only — it
    # includes notes hidden from players unfiltered, same trust level as
    # /admin/backup.zip. The hub page (GET /export alone) is reachable
    # (export_hub gates on world_can_view_section(..., "export"), off by
    # default) but exposes no actual download/restore action on its own.
    ("GET", "/export/rules-and-notes.md", False),
    ("GET", "/export/foundry.json", False),
    ("POST", "/admin/backup/restore", False),
    ("POST", "/admin/backup/restore/cancel", False),
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
    # World-toggleable read-only sections (World.section_access_json) —
    # reachable at all; deps.world_can_view_section is the real
    # off-by-default gate. Structural/full-only write routes stay
    # GM+Assistant-only regardless of the toggle.
    ("GET", "/calendar/config", False),
    ("POST", "/api/calendar/advance", False),
    # Own-item create/edit/delete for a player with "edit" level on
    # quests/calendar/tables — reachable at all; deps.world_can_edit_
    # section/world_can_edit_row are the real (off-by-default, own-rows-
    # only) gate. See _is_player_safe's own comment.
    ("POST", "/api/calendar/events", True),
    ("GET", "/quests/new", True),
    ("POST", "/quests/new", True),
    ("POST", "/quests/5/edit", True),
    ("POST", "/quests/5/delete", True),
    ("POST", "/api/quests/5/status", True),
    # Parties has no "own row" concept — only member-level notes/loot
    # editing opens up (parties.py's _party_edit_level); create/delete
    # stay GM+Assistant ("full" level) only.
    ("POST", "/parties/new", False),
    ("POST", "/parties/5/edit", True),
    ("POST", "/parties/5/delete", False),
    ("POST", "/api/parties/5/loot", True),
    ("GET", "/tables/new", True),
    ("POST", "/tables/new", True),
    ("GET", "/tables/5/edit", True),
    ("POST", "/tables/5/edit", True),
    ("POST", "/tables/5/delete", True),
    ("GET", "/tables/export", False),
    ("POST", "/tables/import", False),
    # Board creation/save/delete/export stay GM+Assistant ("full" level)
    # only — Boards' None/Read/Edit matrix entry only ever grants a player
    # up to Read (deps._NO_PLAYER_EDIT_SECTIONS), so nothing here opens up.
    # /boards/new is also excluded from the read-reachability regex below
    # (it's the create-form page, board_new_form, which has no in-handler
    # gate of its own) — see _is_player_safe's own comment.
    ("GET", "/boards/new", False),
    ("POST", "/boards/new", False),
    ("POST", "/boards/some-slug/save", False),
    ("POST", "/boards/some-slug/delete", False),
    ("GET", "/boards/some-slug/export", False),
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
    ("GET", "/pages/1", True),
    ("GET", "/pages/1/download", True),
    ("GET", "/calendar", True),
    ("GET", "/calendar/agenda", True),
    ("GET", "/quests", True),
    ("GET", "/quests/5", True),
    ("GET", "/parties", True),
    ("GET", "/parties/5", True),
    ("GET", "/tables", True),
    ("POST", "/api/tables/5/roll", True),
    # Reachable — each page's own handler-level world_can_view_section/
    # world_row_visible check (off by default, so no existing world's
    # behavior changes) is the real gate, same "reachable vs. actually
    # allowed" split as every route in this function.
    ("GET", "/combat", True),
    ("GET", "/sessions", True),
    ("GET", "/sessions/5", True),
    ("GET", "/import", True),
    ("GET", "/facts", True),
    ("GET", "/export", True),
    ("GET", "/ai", True),
    ("GET", "/boards", True),
    ("GET", "/boards/some-slug", True),
    ("GET", "/imagestudio", True),
    ("GET", "/editor", True),
    ("GET", "/tools/code-assist", True),
    ("GET", "/dice", True),
    ("POST", "/dice", True),
    ("POST", "/api/dice/roll", True),
    ("GET", "/api/dice/history", True),
    ("GET", "/races", True),
    ("GET", "/professions", True),
    ("GET", "/androidapp", True),
    # Character sheets — the router's own owner-or-GM check is the real
    # gate (404 for anyone else), same pattern as pages_viewer/pages_download
    # above; a player must be able to reach these to create/save/edit/delete
    # their OWN sheets.
    ("GET", "/pages/sheets", True),
    ("POST", "/pages/sheets/new", True),
    ("GET", "/pages/sheets/1", True),
    ("GET", "/pages/sheets/1/render", True),
    ("POST", "/pages/sheets/1/save", True),
    ("POST", "/pages/sheets/1/edit", True),
    ("POST", "/pages/sheets/1/delete", True),
    ("GET", "/pages/sheets/1/download", True),
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
    ("POST", "/api/ai/world-context-player", True),  # player-safe, visibility-filtered RAG lookup for /ai-chat
    ("POST", "/api/ai/entity-context", True),  # per-entity RAG lookup for the entity detail page's "Ask AI" panel
    ("POST", "/api/ai/chat/compact", True),
    ("GET", "/api/ai/models", True),  # read-only model catalog for the player-facing recap pickers
    ("POST", "/api/ai/models", False),  # GET-only — the rest of /api/ai stays GM-only
    ("GET", "/api/ai/world-summary", True),  # handler still gates on players_can_view_world_summary
    ("POST", "/api/ai/world-summary", False),  # generate/clear stay GM+Assistant only regardless
    ("DELETE", "/api/ai/world-summary", False),
    ("GET", "/ai-chat", True),  # handler still gates on players_can_use_ai_chat
    ("GET", "/image-gen", True),  # handler still gates on players_can_use_image_gen
    ("POST", "/api/ai/imagegen/player/generate", True),
    ("GET", "/api/ai/imagegen/player/jobs", True),
    ("GET", "/api/ai/imagegen/player/jobs/5", True),
    ("POST", "/api/ai/imagegen/player/jobs/5/cancel", True),
    ("DELETE", "/api/ai/imagegen/player/jobs/5", True),
    ("POST", "/api/ai/imagegen/generate", False),  # the GM's own full-surface route
    ("POST", "/api/ai/attachments/upload", True),
    ("POST", "/api/ai/attachments/audio-jobs", True),
    ("POST", "/api/ai/attachments/audio-jobs/chunk", True),
    ("POST", "/api/ai/attachments/audio-jobs/complete", True),
    ("GET", "/api/ai/attachments/audio-jobs/5", True),
    ("GET", "/api/ai/attachments/audio-jobs", True),
    ("POST", "/api/sessions/ai/audio-jobs", False),
    ("GET", "/api/sessions/ai/audio-jobs/5", False),
    ("POST", "/api/sessions/ai/audio-jobs/from-clip", False),
    ("GET", "/api/audio/clips", False),
    ("GET", "/uploads/portraits/x.png", True),
    ("GET", "/maps/some-map", True),
    ("GET", "/worlds/switch/some-world", True),
    ("GET", "/chronicler", True),
    ("POST", "/api/chronicler/ask", True),
    ("GET", "/session-log", True),
    ("GET", "/session-log/5", True),
    ("POST", "/api/session-log/5/recap", True),
    ("GET", "/api/entities/picker", True),
]


@pytest.mark.parametrize("method,path,expected", CASES)
def test_is_player_safe(method, path, expected):
    assert _is_player_safe(method, path) is expected, f"{method} {path} expected player_safe={expected}"


def test_non_get_defaults_to_gm_only_unless_explicitly_allowlisted():
    """New routes are GM-only by default — a POST/PUT/DELETE must be matched by
    an explicit regex above the `method != "GET"` cutoff to be player-safe."""
    assert _is_player_safe("POST", "/some/brand/new/route") is False
    assert _is_player_safe("DELETE", "/entity/5") is False


def test_player_can_fetch_the_model_list(client, seed):
    """Runtime half of the /api/ai/models row: the Session Log page's model
    picker fetches this as a logged-in player, and the 403 it used to get
    left the picker silently empty (the fetch's !res.ok just returns).
    Unreachable Ollama is fine here — _list_loaded() degrades to [] on any
    failure, so the route answers 200 with whatever catalog it has."""
    from .conftest import PLAYER_PASSWORD, login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/ai/models")
    assert r.status_code == 200, r.text
    assert "models" in r.json()
