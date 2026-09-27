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

def test_cockpit_gm_renders_panels(client, seed):
    pc_id = _mk_pc(seed.world_a.id, "Vex", current_hp=10, max_hp=20)
    party_id = _mk_party(seed.world_a.id, "Vanguard", pc_ids=[pc_id])
    _mk_map(seed.world_a.id, "tavern-map", "Tavern")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/cockpit")
    assert r.status_code == 200
    html = r.text
    assert "GM Cockpit" in html
    assert 'id="ck-map"' in html                       # map dock
    assert 'id="ck-map-select"' in html
    assert "/maps/schematic/tavern-map/view" in html   # docked player view
    assert 'id="ck-dice"' in html                      # dice tray
    assert "/dice" in html
    assert 'id="cockpit-vitals"' in html               # live partials
    assert 'id="cockpit-quests"' in html
    assert "Vanguard" in html and "Vex" in html        # stacked party vitals
    assert "ndLiveRefetch('#cockpit-vitals')" in html
    assert "ndLiveRefetch('#cockpit-quests')" in html


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


def test_cockpit_empty_world_renders_hints(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/cockpit")
    assert r.status_code == 200
    assert "No floatable maps yet" in r.text
    assert "No parties yet" in r.text
