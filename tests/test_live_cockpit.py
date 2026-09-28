"""Tests for the Phase 1-4 GM session-workflow features:

- Phase 1 live sync: the per-world change counter (app/live.py), the
  /api/live SSE stream, the touch() wiring in the mutating vitals/party/
  quest/session routes, and the party vitals JSON refetch endpoint.
- Phase 2 float panels: ?embed=1 chrome-less rendering mode in base.html
  and the nd-float.js / nd-live.js script includes.
- Phase 3 GM Cockpit: the /cockpit composition page (gating, panels, map
  dock, live vitals) and its nav entry.
"""
import asyncio
import json

import pytest

from app import live
from app.database import SessionLocal
from app.models import Party, PlayerCharacter, Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _mk_pc(world_id, name, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, name=name, **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _mk_party(world_id, name, pc_ids=None):
    db = SessionLocal()
    try:
        p = Party(world_id=world_id, name=name,
                  member_pc_ids_json=json.dumps(pc_ids or []))
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def _mk_map(world_id, slug, name):
    db = SessionLocal()
    try:
        s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False)
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


# ── Phase 1: live version counter ────────────────────────────────────────────

def test_touch_bumps_only_that_world():
    live._VERSIONS.clear()
    before = live.version(101)
    live.touch(101)
    live.touch(101)
    live.touch(202)
    assert live.version(101) == before + 2
    assert live.version(202) == before + 1
    live._VERSIONS.clear()


def test_touch_ignores_none():
    live._VERSIONS.clear()
    live.touch(None)
    assert live._VERSIONS == {}
    live._VERSIONS.clear()


def test_hp_change_touches_world_version(client, seed):
    """The whole point of Phase 1: a vitals mutation anywhere (this window,
    a floated panel, a player's phone) is observable by every open page."""
    live._VERSIONS.clear()
    pc_id = _mk_pc(seed.world_a.id, "Vex", current_hp=20, max_hp=20)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/api/characters/{pc_id}/hp-async", json={"action": "delta", "value": -6})
    assert r.status_code == 200
    assert live.version(seed.world_a.id) >= 1
    live._VERSIONS.clear()


def test_party_loot_touches_world_version(client, seed):
    live._VERSIONS.clear()
    party_id = _mk_party(seed.world_a.id, "Looters")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/api/parties/{party_id}/loot", json={"action": "add", "name": "Gem"})
    assert r.status_code == 200
    assert live.version(seed.world_a.id) >= 1
    live._VERSIONS.clear()


# ── Phase 1: the SSE stream ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_generator_format():
    """The SSE body directly (not through TestClient — consuming an infinite
    stream over the test portal deadlocks the suite). First frame is the
    reconnect hint; every counter change emits an event+data frame."""
    live._VERSIONS.clear()
    gen = live.stream_world_events(55)
    try:
        first = await asyncio.wait_for(gen.__anext__(), 2)
        assert first == "retry: 3000\n\n"
        evt = await asyncio.wait_for(gen.__anext__(), 2)
        assert evt == "event: version\ndata: 0\n\n"
        live.touch(55)
        evt = await asyncio.wait_for(gen.__anext__(), 3)
        assert evt == "event: version\ndata: 1\n\n"
    finally:
        await gen.aclose()
    live._VERSIONS.clear()


@pytest.mark.asyncio
async def test_stream_generator_updates_propagate_mid_wait():
    """A touch that lands WHILE the generator is sleeping surfaces on the
    next frame within a poll interval — the property the frontend relies on."""
    live._VERSIONS.clear()
    gen = live.stream_world_events(77)
    try:
        await asyncio.wait_for(gen.__anext__(), 2)  # retry
        await asyncio.wait_for(gen.__anext__(), 2)  # version 0
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        live.touch(77)
        evt = await asyncio.wait_for(task, 3)
        assert evt == "event: version\ndata: 1\n\n"
    finally:
        await gen.aclose()
    live._VERSIONS.clear()


def test_live_route_registered_and_player_safe():
    from app.main import _fastapi_app, _is_player_safe

    assert any(getattr(r, "path", None) == "/api/live" for r in _fastapi_app.routes)
    # Bare-integer payload for an accessible world → reachable for players,
    # so their panels live-sync too (content enforcement is in get_world_ctx).
    assert _is_player_safe("GET", "/api/live")


# ── Phase 1: party vitals JSON ───────────────────────────────────────────────

def test_party_vitals_json(client, seed):
    pc_id = _mk_pc(seed.world_a.id, "Vex", current_hp=3, max_hp=20, armor_class=15)
    party_id = _mk_party(seed.world_a.id, "Vanguard", pc_ids=[pc_id])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/parties/{party_id}/vitals")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Vanguard"
    assert len(data["members"]) == 1
    m = data["members"][0]
    assert m["name"] == "Vex"
    assert m["hp"] == 3 and m["max_hp"] == 20 and m["ac"] == 15
    assert m["down"] is (m["hp"] <= 0)


def test_party_vitals_hidden_without_section_access(client, seed):
    """Parties isn't in the player default-read set, so a plain player gets
    the same 404 the party detail page gives them — until a GM opens the
    section, after which the JSON is readable like the page."""
    pc_id = _mk_pc(seed.world_a.id, "Vex")
    party_id = _mk_party(seed.world_a.id, "Vanguard", pc_ids=[pc_id])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/parties/{party_id}/vitals").status_code == 404

    db = SessionLocal()
    try:
        world = seed.world_a
        world.section_access_json = json.dumps({"parties": {"player": "read", "assistant": "read"}})
        db.merge(world)
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/parties/{party_id}/vitals").status_code == 200


def test_party_vitals_missing_party_404(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/parties/999999/vitals").status_code == 404


def test_party_detail_page_renders_refetch_hook(client, seed):
    """The party page carries the live-sync refetch wiring: a stable
    #party-vitals wrapper plus the ndLiveRefetch call."""
    pc_id = _mk_pc(seed.world_a.id, "Vex")
    party_id = _mk_party(seed.world_a.id, "Vanguard", pc_ids=[pc_id])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/parties/{party_id}").text
    assert 'id="party-vitals"' in html
    assert "ndLiveRefetch('#party-vitals')" in html


# ── Phase 2: embed mode + float/live script includes ─────────────────────────

def test_embed_mode_hides_chrome(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    plain = client.get("/dice").text
    embedded = client.get("/dice?embed=1").text
    assert 'class="nd-embed"' not in plain
    assert 'class="nd-embed"' in embedded
    assert "body.nd-embed .topbar" in embedded


def test_base_includes_float_and_live_scripts(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/dice").text
    assert "/static/js/nd-float.js" in html
    assert "/static/js/nd-live.js" in html
    assert 'data-world-slug="world-a"' in html


def test_dice_page_has_float_buttons(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/dice").text
    assert "nd-float-btn" in html
    assert "ndFloat(" in html and "ndFloatPip(" in html


def test_schematic_view_has_float_buttons(client, seed):
    _mk_map(seed.world_a.id, "tavern-map", "Tavern")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/maps/schematic/tavern-map/view").text
    assert "nd-float-btn" in html
    assert "/maps/schematic/tavern-map/view" in html


def test_floated_dice_page_uses_fetch_rolling(client, seed):
    """The dice tray no longer form-POSTs (a reload would reset a floated
    tray) — it rolls over /api/dice/roll and re-renders the log in place."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/dice?embed=1").text
    assert "/api/dice/roll" in html
    assert 'id="dice-form"' in html
    # The tray re-renders from /api/dice/history via the shared ndPoll
    # helper — no full-page reload in the dice script itself.
    assert "ndPoll(refresh, 5000)" in html
    assert "renderLog" in html


# ── Phase 3: GM Cockpit ──────────────────────────────────────────────────────

def test_cockpit_gm_renders_workspace_shell(client, seed):
    """The cockpit is a modular window workspace now: the page renders the
    shell (toolbar, add-panel modal, window manager JS) plus the per-world
    picker data; the windows themselves are built client-side."""
    _mk_party(seed.world_a.id, "Vanguard")
    _mk_map(seed.world_a.id, "tavern-map", "Tavern")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/cockpit")
    assert r.status_code == 200
    html = r.text
    assert "GM Cockpit" in html
    assert 'id="ck-viewport"' in html
    assert 'id="ck-add-overlay"' in html and "Add panel" in html
    assert 'id="ck-save-preset"' in html          # named layouts
    assert 'id="ck-pin-btn"' in html              # auto-arrange
    assert 'id="ck-add-kind"' in html             # entity picker kind filter
    assert "CK_WORLD_MAPS" in html                # file-based map picker data
    assert "'wmap'" in html                       # world-map panel type
    assert "/static/js/cockpit.js" in html
    assert '"slug": "tavern-map"' in html          # picker data for the modal
    assert '"name": "Vanguard"' in html


def test_cockpit_player_forbidden(client, seed):
    """GM-only by default — not in _is_player_safe, so a plain player's
    auth_gate 403s before the handler even runs."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/cockpit").status_code == 403


def test_cockpit_nav_entry_present_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/").text
    assert "/cockpit" in html
    assert "GM Cockpit" in html


def test_cockpit_nav_entry_hidden_for_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/dice").text
    assert "GM Cockpit" not in html


def test_cockpit_empty_world_renders_shell(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/cockpit")
    assert r.status_code == 200
    assert "Empty cockpit" in r.text
    # with no maps/parties the picker data is empty lists, not errors
    assert '"maps_json": []' in r.text or 'CK_MAPS = []' in r.text


# ── Phase 3: cockpit workspace persistence ───────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_cockpit_lists_file_based_maps(client, seed):
    """The world-map panel picker is fed from the DB-side maps dir (the same
    JSON-marker files /maps/{slug} serves), not from the schematics table."""
    import os

    from app.main import UPLOADS_DIR

    maps_dir = UPLOADS_DIR.parent / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    (maps_dir / "city-map.json").write_text(
        json.dumps({"world_id": seed.world_a.id, "name": "City of Yorm",
                    "width": 2000, "height": 1200,
                    "markers": [{"name": "Gate"}, {"name": "Harbor"}]}),
        encoding="utf-8")
    (maps_dir / "other-world.json").write_text(
        json.dumps({"world_id": seed.world_b.id, "name": "Not Mine"}),
        encoding="utf-8")
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    assert '"slug": "city-map"' in html
    assert '"City of Yorm"' in html
    assert '"markers": 2' in html
    assert "other-world" not in html  # another world's map must not leak


def test_workspace_roundtrip(client, seed):
    _login_gm(client, seed)
    assert client.get("/api/cockpit/workspace").json() == {"workspace": None}

    ws = {"current": {"panels": [
        {"id": "p1", "type": "map", "ref": "tavern", "title": "🗺 Tavern",
         "x": 10, "y": 20, "w": 700, "h": 500, "z": 11, "collapsed": False},
        {"id": "p2", "type": "notes", "ref": "", "title": "📝 Notes",
         "x": 0, "y": 0, "w": 300, "h": 200, "z": 12, "collapsed": False,
         "data": {"text": "Vex took 6 HP from the trap"}},
        {"id": "p3", "type": "hax", "ref": "", "title": "?", "x": 0, "y": 0,
         "w": 1, "h": 1, "z": 1},  # unknown type — dropped
    ]}, "presets": {"Combat": {"panels": [
        {"id": "p1", "type": "dice", "ref": "", "title": "🎲", "x": 5, "y": 5,
         "w": 420, "h": 480, "z": 1},
    ]}}}
    r = client.post("/api/cockpit/workspace", json=ws)
    assert r.status_code == 200
    back = client.get("/api/cockpit/workspace").json()["workspace"]
    assert len(back["current"]["panels"]) == 2  # the "hax" panel was dropped
    assert back["current"]["panels"][0]["ref"] == "tavern"
    assert back["current"]["panels"][1]["data"]["text"].startswith("Vex took 6 HP")
    assert back["presets"]["Combat"]["panels"][0]["type"] == "dice"


def test_workspace_sanitizes_geometry_and_strings(client, seed):
    _login_gm(client, seed)
    ws = {"current": {"panels": [
        {"id": "x" * 99, "type": "map", "ref": "m", "title": "t" * 300,
         "x": -500, "y": 99999, "w": 5, "h": 10 ** 9, "z": 10 ** 9,
         "data": {"text": "n" * 40000}},
    ]}, "presets": {}}
    r = client.post("/api/cockpit/workspace", json=ws)
    assert r.status_code == 200
    panel = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"][0]
    assert len(panel["id"]) <= 40 and len(panel["title"]) <= 120
    assert panel["x"] == 0 and panel["y"] == 8000          # clamped
    assert panel["w"] >= 160 and panel["h"] <= 8000
    assert len(panel["data"]["text"]) <= 8000


def test_workspace_rejects_structural_nonsense(client, seed):
    _login_gm(client, seed)
    assert client.post("/api/cockpit/workspace", json=["not", "a", "dict"]).status_code == 400
    assert client.post("/api/cockpit/workspace",
                       json={"current": {"panels": "nope"}, "presets": {}}).status_code == 400
    big = {"current": {"panels": [
        {"id": f"p{i}", "type": "dice", "x": 0, "y": 0, "w": 300, "h": 200, "z": i}
        for i in range(30)
    ]}, "presets": {}}
    assert client.post("/api/cockpit/workspace", json=big).status_code == 400


def test_workspace_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/cockpit/workspace").status_code == 403
    assert client.post("/api/cockpit/workspace", json={}).status_code == 403


def test_quests_board_json(client, seed):
    from app.models import Quest

    db = SessionLocal()
    try:
        db.add_all([
            Quest(world_id=seed.world_a.id, title="Witching Hour", status="active",
                  category="main", summary="The prologue thread"),
            Quest(world_id=seed.world_a.id, title="Old Business", status="complete"),
        ])
        db.commit()
    finally:
        db.close()
    party_id = _mk_party(seed.world_a.id, "Vanguard")
    db = SessionLocal()
    try:
        q = db.query(Quest).filter(Quest.title == "Witching Hour").one()
        q.assigned_party_id = party_id
        db.commit()
    finally:
        db.close()

    _login_gm(client, seed)
    r = client.get("/api/quests/board")
    assert r.status_code == 200
    quests = r.json()["quests"]
    assert [q["title"] for q in quests] == ["Witching Hour"]
    assert quests[0]["party"] == "Vanguard"
    assert quests[0]["summary"] == "The prologue thread"


def test_combat_recent_feed(client, seed):
    from app.models import CombatSession

    db = SessionLocal()
    try:
        db.add(CombatSession(world_id=seed.world_a.id, name="Goblin Ambush",
                             combatants_json="[]"))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)
    r = client.get("/api/combat/recent")
    assert r.status_code == 200
    combats = r.json()["combats"]
    assert len(combats) == 1 and combats[0]["name"] == "Goblin Ambush"
    assert combats[0]["round_num"] == 1


def test_combat_recent_feed_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/combat/recent").status_code == 403


def test_tables_options_feed(client, seed):
    from app.models import RandomTable

    db = SessionLocal()
    try:
        db.add(RandomTable(world_id=seed.world_a.id, name="Wild Encounters",
                           slug="wild-encounters", entries_json='[{"label":"Wolves","weight":1},{"label":"Bandits","weight":1}]'))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)
    r = client.get("/api/tables/options")
    assert r.status_code == 200
    tables = r.json()["tables"]
    mine = [t for t in tables if t["name"] == "Wild Encounters"]
    assert len(mine) == 1 and mine[0]["entries"] == 2


def test_tables_options_feed_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/tables/options").status_code == 403


def test_workspace_accepts_new_types_and_accent(client, seed):
    _login_gm(client, seed)
    ws = {"current": {"panels": [
        {"id": "c1", "type": "ecard", "ref": "7", "title": "Vex", "x": 0, "y": 0,
         "w": 360, "h": 400, "z": 1, "accent": "#ff2d78"},
        {"id": "c2", "type": "tables", "ref": "", "title": "Roll", "x": 0, "y": 0,
         "w": 360, "h": 280, "z": 2},
        {"id": "c3", "type": "combat", "ref": "3", "title": "Ambush", "x": 0, "y": 0,
         "w": 560, "h": 560, "z": 3, "accent": "javascript:alert(1)"},
        {"id": "c4", "type": "calendar", "ref": "", "title": "Cal", "x": 0, "y": 0,
         "w": 520, "h": 480, "z": 4},
        {"id": "c5", "type": "wmap", "ref": "city-map", "title": "Yorm", "x": 0, "y": 0,
         "w": 780, "h": 560, "z": 5},
    ]}, "presets": {}}
    assert client.post("/api/cockpit/workspace", json=ws).status_code == 200
    panels = client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"]
    types = [p["type"] for p in panels]
    assert types == ["ecard", "tables", "combat", "calendar", "wmap"]
    assert panels[4]["ref"] == "city-map"
    assert panels[0]["accent"] == "#ff2d78"
    assert panels[2]["accent"] == ""  # non-color string dropped


def test_js_includes_are_cache_busted(client, seed):
    """All local JS ships with a content-hash query (same pattern as
    style.css) — a stale cached cockpit.js/nd-live.js against fresh HTML
    after an update breaks the app in confusing ways."""
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    assert "/static/js/cockpit.js?v=" in html
    assert 'src="/static/js/nd-poll.js"' not in html  # the unversioned form
    assert "/static/js/nd-live.js?v=" in html


def test_embed_pages_skip_spotlight_poller(client, seed):
    """Embed documents (cockpit windows, floated panels) must not run the
    spotlight/now-playing poller — otherwise every window polls it and each
    hidden <audio> plays a broadcast on top of the main window's."""
    _login_gm(client, seed)
    plain = client.get("/dice").text
    embedded = client.get("/dice?embed=1").text
    assert "ndPoll(pollSpotlight" in plain
    # the guard sits at the top of the poller IIFE in both, but only embed
    # bodies carry the nd-embed class that triggers it
    assert "contains('nd-embed')" in plain and "contains('nd-embed')" in embedded


def test_quests_board_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/quests/board").status_code == 403
