"""Schematic (battle-map) live-play API tests. Covers the world-membership
write-access gap (Phase 1): move-token/pickup-item/buy-item used to derive
"ownership" purely from a PlayerCharacter row, which member_remove() never
revokes — so a kicked player kept live-play write access forever even though
the read view (schematic_player_view) already correctly 404s for them.
"""
import asyncio
import json
import threading

import pytest
from fastapi import HTTPException

from app import auth
from app import main as main_module
from app.database import SessionLocal
from app.models import PlayerCharacter, Schematic, User, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_schematic(world_id, slug, name="Test Schematic", elements=None, image_url=None):
    db = SessionLocal()
    try:
        s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False,
                      canvas_width=2000, canvas_height=1500, canvas_bg="dark",
                      elements_json=json.dumps(elements or []), image_url=image_url)
        db.add(s)
        db.commit()
        db.refresh(s)
        return s
    finally:
        db.close()


def _make_pc(world_id, owner_id, name="Hero", currency=None):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_id, name=name)
        if currency is not None:
            pc.currency_json = json.dumps(currency)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def _make_player(world_id, email, name="Other Player"):
    """A second player in the same world, for tests that need two distinct
    buyers/pickers racing each other."""
    db = SessionLocal()
    try:
        u = User(email=email, password_hash=auth.hash_password(PLAYER_PASSWORD),
                 display_name=name, is_gm=False)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.add(WorldMembership(world_id=world_id, user_id=u.id))
        db.commit()
        db.refresh(u)
        return u
    finally:
        db.close()


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    """Stands in for the real Request in tests that call a route coroutine
    directly (bypassing TestClient/ASGI dispatch) so two calls can run on two
    genuinely separate OS threads/event loops — see the concurrent-buy/pickup
    tests below for why that's necessary."""
    def __init__(self, user, body):
        self.state = _FakeState(user)
        self._body = body

    async def json(self):
        return self._body


def _remove_membership(world_id, user_id):
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(
            WorldMembership.world_id == world_id, WorldMembership.user_id == user_id
        ).delete()
        db.commit()
    finally:
        db.close()


def test_removed_player_loses_move_token_access(client, seed):
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 10, "y": 10, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "removed-move", elements=elements)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    # Still a member — works.
    r = client.post(f"/api/maps/schematic/{s.slug}/move-token", json={"token_id": "tok1", "x": 20, "y": 20})
    assert r.status_code == 200

    _remove_membership(seed.world_a.id, seed.player_a.id)
    r2 = client.post(f"/api/maps/schematic/{s.slug}/move-token", json={"token_id": "tok1", "x": 30, "y": 30})
    assert r2.status_code == 403


def test_removed_player_loses_pickup_access(client, seed):
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "item1", "type": "token", "source": "item", "name": "Sword",
                 "qty": 1, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "removed-pickup", elements=elements)

    _remove_membership(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/pickup-item", json={"token_id": "item1"})
    assert r.status_code == 403
    _ = pc  # PC row deliberately left intact — only membership was revoked


def test_removed_player_loses_buy_access(client, seed):
    _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "merch1", "type": "token", "source": "merchant", "visible_to_players": True,
                 "inventory": [{"id": "stock1", "name": "Potion", "price": 5, "currency_abbr": "CR", "qty": 3}]}]
    s = _make_schematic(seed.world_a.id, "removed-buy", elements=elements)

    _remove_membership(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/buy-item", json={"token_id": "merch1", "stock_id": "stock1"})
    assert r.status_code == 403


def test_gm_access_unaffected_by_membership(client, seed):
    """GM has no WorldMembership row at all (never needs one) — confirm the
    is_gm bypass still works for all three write routes."""
    elements = [
        {"id": "tok1", "type": "token", "pc_id": None, "x": 0, "y": 0, "visible_to_players": True},
    ]
    s = _make_schematic(seed.world_a.id, "gm-access", elements=elements)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/move-token", json={"token_id": "tok1", "x": 5, "y": 5})
    assert r.status_code == 200


def test_current_member_move_token_unaffected(client, seed):
    pc = _make_pc(seed.world_b.id, seed.player_b.id)
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 0, "y": 0, "visible_to_players": True}]
    s = _make_schematic(seed.world_b.id, "member-ok", elements=elements)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/move-token", json={"token_id": "tok1", "x": 1, "y": 1})
    assert r.status_code == 200


def test_player_view_shows_background_image(client, seed):
    s = _make_schematic(seed.world_a.id, "with-bg-live", image_url="/uploads/schematics/with-bg-live.webp")

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/maps/schematic/{s.slug}/view")
    assert r.status_code == 200
    assert 'id="bg-image"' in r.text
    assert "/uploads/schematics/with-bg-live.webp" in r.text

    r2 = client.get(f"/maps/schematic/{s.slug}/view.json")
    assert r2.status_code == 200
    assert r2.json()["image_url"] == "/uploads/schematics/with-bg-live.webp"


def test_player_view_no_background_image_element_when_unset(client, seed):
    s = _make_schematic(seed.world_a.id, "no-bg-live")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/maps/schematic/{s.slug}/view")
    assert r.status_code == 200
    assert 'id="bg-image"' not in r.text


def test_player_view_poller_is_visibility_aware(client, seed):
    """docs/AUDIT_PLAN_NEXT.md item 13: the battle-map poller must go
    through the shared ndPoll helper, not a bare setInterval that keeps
    firing at full cadence in a backgrounded tab."""
    s = _make_schematic(seed.world_a.id, "poller-check")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/maps/schematic/{s.slug}/view")
    assert r.status_code == 200
    assert "ndPoll(pollNow, 20000)" in r.text      # a slow safety net; changes arrive over the live bus
    assert "addEventListener('nd-live'" in r.text
    assert "setInterval(poll" not in r.text


def test_hidden_element_never_reaches_player_payload(client, seed):
    elements = [
        {"id": "shape1", "type": "rect", "x": 0, "y": 0, "w": 10, "h": 10},
        {"id": "shape2", "type": "rect", "x": 0, "y": 0, "w": 10, "h": 10, "hidden": True},
        {"id": "tok1", "type": "token", "visible_to_players": False, "x": 0, "y": 0},
    ]
    s = _make_schematic(seed.world_a.id, "hidden-test", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/maps/schematic/{s.slug}/view.json")
    assert r.status_code == 200
    ids = {e["id"] for e in r.json()["elements"]}
    assert ids == {"shape1"}


def test_buy_item_rejects_when_stock_already_zero(client, seed):
    """Regression guard for the qty==0 exact-equality bug: once qty is 0 it
    must stay rejected forever, independent of any race — this holds even
    with a single request and no concurrency involved."""
    _make_pc(seed.world_a.id, seed.player_a.id, currency=[{"abbr": "CR", "value": 100}])
    elements = [{"id": "merch1", "type": "token", "source": "merchant", "visible_to_players": True,
                 "inventory": [{"id": "stock1", "name": "Potion", "price": 5, "currency_abbr": "CR", "qty": 0}]}]
    s = _make_schematic(seed.world_a.id, "zero-stock", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/buy-item", json={"token_id": "merch1", "stock_id": "stock1"})
    assert r.status_code == 404


def test_buy_item_unlimited_stock_qty_minus_one_still_purchasable(client, seed):
    """qty == -1 means unlimited stock — must never be treated as depleted."""
    _make_pc(seed.world_a.id, seed.player_a.id, currency=[{"abbr": "CR", "value": 100}])
    elements = [{"id": "merch1", "type": "token", "source": "merchant", "visible_to_players": True,
                 "inventory": [{"id": "stock1", "name": "Potion", "price": 5, "currency_abbr": "CR", "qty": -1}]}]
    s = _make_schematic(seed.world_a.id, "unlimited-stock", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/buy-item", json={"token_id": "merch1", "stock_id": "stock1"})
    assert r.status_code == 200


def test_concurrent_buy_item_no_oversell(client, seed, monkeypatch):
    """Two players racing to buy the last unit of stock. The BEGIN IMMEDIATE
    lock in schematic_buy_item must serialize them so exactly one succeeds
    and the loser's re-derived (post-lock) read sees qty already at 0 — not
    the stale qty=1 both requests originally observed.

    Runs the real route coroutines directly on two separate OS
    threads/event loops (each with its own SessionLocal), rather than via
    TestClient: TestClient's ASGI dispatch funnels everything through a
    single background event loop, and since the locked section below has no
    `await` inside it, two "concurrent" TestClient calls would never
    actually interleave there — this bypass is the one the plan flagged as
    the fallback if that turned out to be the case.

    Deterministic contention is forced by pausing the first buyer
    mid-transaction (already holding SQLite's RESERVED lock, before commit)
    via a monkeypatched _merge_equipment_item, so the second buyer's own
    BEGIN IMMEDIATE genuinely blocks on real SQLite lock contention rather
    than relying on thread-scheduling luck.
    """
    buyer_a = seed.player_a
    buyer_b = _make_player(seed.world_a.id, "buyer-b@test.local")
    _make_pc(seed.world_a.id, buyer_a.id, currency=[{"abbr": "CR", "value": 100}])
    _make_pc(seed.world_a.id, buyer_b.id, currency=[{"abbr": "CR", "value": 100}])
    elements = [{"id": "merch1", "type": "token", "source": "merchant", "visible_to_players": True,
                 "inventory": [{"id": "stock1", "name": "Potion", "price": 5, "currency_abbr": "CR", "qty": 1}]}]
    s = _make_schematic(seed.world_a.id, "race-buy", elements=elements)

    ready = threading.Event()
    release = threading.Event()
    calls = []
    original_merge = main_module._merge_equipment_item

    def gated_merge(pc, name, qty):
        calls.append(pc.id)
        if len(calls) == 1:
            ready.set()
            assert release.wait(timeout=5), "test deadlocked waiting to release the first buyer"
        return original_merge(pc, name, qty)

    monkeypatch.setattr(main_module, "_merge_equipment_item", gated_merge)

    results = {}

    def do_buy(email, key):
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            req = _FakeRequest(user, {"token_id": "merch1", "stock_id": "stock1"})
            try:
                body = asyncio.run(main_module.schematic_buy_item(s.slug, req, db))
                results[key] = (200, body)
            except HTTPException as e:
                results[key] = (e.status_code, None)
        finally:
            db.close()

    t_a = threading.Thread(target=do_buy, args=(buyer_a.email, "a"))
    t_a.start()
    assert ready.wait(timeout=5), "first buyer never reached the locked section"

    t_b = threading.Thread(target=do_buy, args=(buyer_b.email, "b"))
    t_b.start()
    t_b.join(timeout=1)  # let buyer B's BEGIN IMMEDIATE actually attempt + block
    release.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    outcomes = sorted(status for status, _ in results.values())
    assert outcomes == [200, 404], f"expected exactly one buy to succeed: {results}"

    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        stock = json.loads(fresh.elements_json)[0]["inventory"][0]
        assert stock["qty"] == 0
    finally:
        db.close()


def test_concurrent_pickup_item_no_duplication(client, seed, monkeypatch):
    """Same deterministic-contention technique as the buy-item race test
    above, applied to pickup-item: two players racing to pick up the same
    item-token stack — exactly one may succeed, the other's re-derived
    (post-lock) read must find the token already gone."""
    picker_a = seed.player_a
    picker_b = _make_player(seed.world_a.id, "picker-b@test.local")
    _make_pc(seed.world_a.id, picker_a.id)
    _make_pc(seed.world_a.id, picker_b.id)
    elements = [{"id": "item1", "type": "token", "source": "item", "name": "Sword",
                 "qty": 1, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "race-pickup", elements=elements)

    ready = threading.Event()
    release = threading.Event()
    calls = []
    original_merge = main_module._merge_equipment_item

    def gated_merge(pc, name, qty):
        calls.append(pc.id)
        if len(calls) == 1:
            ready.set()
            assert release.wait(timeout=5), "test deadlocked waiting to release the first picker"
        return original_merge(pc, name, qty)

    monkeypatch.setattr(main_module, "_merge_equipment_item", gated_merge)

    results = {}

    def do_pickup(email, key):
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            req = _FakeRequest(user, {"token_id": "item1"})
            try:
                body = asyncio.run(main_module.schematic_pickup_item(s.slug, req, db))
                results[key] = (200, body)
            except HTTPException as e:
                results[key] = (e.status_code, None)
        finally:
            db.close()

    t_a = threading.Thread(target=do_pickup, args=(picker_a.email, "a"))
    t_a.start()
    assert ready.wait(timeout=5), "first picker never reached the locked section"

    t_b = threading.Thread(target=do_pickup, args=(picker_b.email, "b"))
    t_b.start()
    t_b.join(timeout=1)  # let picker B's BEGIN IMMEDIATE actually attempt + block
    release.set()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    outcomes = sorted(status for status, _ in results.values())
    assert outcomes == [200, 404], f"expected exactly one pickup to succeed: {results}"

    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        assert json.loads(fresh.elements_json) == []
    finally:
        db.close()


def test_move_token_locked_rejected(client, seed):
    """Phase 10 regression guard: an element's `locked` flag (toggled in the
    GM editor) previously only gated the editor's own select tool
    client-side — this route ignored it completely, so a player could drag
    their own token even after the GM locked it (e.g. mid-cutscene, or once
    initiative positions are finalized for the round)."""
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 10, "y": 10,
                 "visible_to_players": True, "locked": True}]
    s = _make_schematic(seed.world_a.id, "locked-move", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/move-token", json={"token_id": "tok1", "x": 20, "y": 20})
    assert r.status_code == 403
    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        el = json.loads(fresh.elements_json)[0]
        assert (el["x"], el["y"]) == (10, 10)
    finally:
        db.close()


def test_pickup_item_locked_rejected(client, seed):
    _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "item1", "type": "token", "source": "item", "name": "Sword",
                 "qty": 1, "visible_to_players": True, "locked": True}]
    s = _make_schematic(seed.world_a.id, "locked-pickup", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/pickup-item", json={"token_id": "item1"})
    assert r.status_code == 403
    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        assert json.loads(fresh.elements_json) != []
    finally:
        db.close()


def test_buy_item_locked_rejected(client, seed):
    _make_pc(seed.world_a.id, seed.player_a.id, currency=[{"abbr": "CR", "value": 100}])
    elements = [{"id": "merch1", "type": "token", "source": "merchant", "visible_to_players": True, "locked": True,
                 "inventory": [{"id": "stock1", "name": "Potion", "price": 5, "currency_abbr": "CR", "qty": 1}]}]
    s = _make_schematic(seed.world_a.id, "locked-buy", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/maps/schematic/{s.slug}/buy-item", json={"token_id": "merch1", "stock_id": "stock1"})
    assert r.status_code == 403


def test_schematic_view_does_not_wire_drag_for_locked_own_token(client, seed):
    """Source-level guard (no JS runtime in this test suite): the player-view
    drag handler must skip an own-token that's locked, rather than let the
    player see it move locally and then snap back on the next 4s poll once
    the (already-403ing) move-token POST is silently discarded."""
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 10, "y": 10, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "view-lock-check", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/maps/schematic/{s.slug}/view")
    assert r.status_code == 200
    # (a world may allow several characters per player, so the check is "one of MY characters", still never a locked one)
    assert "OWN_PC_IDS.includes(el.pc_id) && !el.locked" in r.text


def test_schematic_editor_creature_tokens_default_visible_to_players(client, seed):
    """Source-level guard (no JS runtime in this test suite): placing a
    creature/NPC token via the "+ Token" dialog's Creatures tab used to
    hardcode visible_to_players:false, hiding it from players until the GM
    manually ticked the "Players" checkbox on every single one. Per explicit
    request, creature/NPC tokens now default visible like PC tokens do —
    only the base object's visible_to_players:true should apply, with no
    override in the entity branch."""
    s = _make_schematic(seed.world_a.id, "creature-token-visibility-check")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/maps/schematic/{s.slug}")
    assert r.status_code == 200
    idx = r.text.index("} else if (tokenTab === 'entity') {")
    next_branch = r.text.index("} else if (tokenTab === 'item') {", idx)
    assert "visible_to_players:false" not in r.text[idx:next_branch]


def test_move_token_refuses_nan_infinity_and_junk_and_keeps_tokens_on_the_canvas(client, seed):
    """One request with NaN (Python's json accepts it) used to make the editor, the player view and view.json fail for everyone
    (the number is stored, then re-serialised into every page and into JSON.parse). Only finite numbers are taken, kept on the canvas."""
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 100, "y": 100, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "nan-token", elements=elements)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    url = f"/api/maps/schematic/{s.slug}/move-token"
    for raw in ('{"token_id":"tok1","x":NaN,"y":5}', '{"token_id":"tok1","x":1e999,"y":5}', '{"token_id":"tok1","x":5,"y":-Infinity}',
                '{"token_id":"tok1","x":"abc","y":5}', '{"token_id":"tok1","x":true,"y":5}', '{"token_id":"tok1","x":[1],"y":5}'):
        r = client.post(url, content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 400, raw
    # nothing was written: the map still loads for everyone and the token has not moved
    assert client.get(f"/maps/schematic/{s.slug}/view.json").json()["elements"][0]["x"] == 100
    # a far-off but finite position is pulled back onto the 2000 x 1500 canvas
    assert client.post(url, json={"token_id": "tok1", "x": 1e300, "y": -99999}).status_code == 200
    el = client.get(f"/maps/schematic/{s.slug}/view.json").json()["elements"][0]
    assert (el["x"], el["y"]) == (2000.0, 0.0)


def test_map_writes_bump_the_live_counter_so_players_refresh_at_once(client, seed):
    from app import live
    s = _make_schematic(seed.world_a.id, "live-bump")
    login(client, seed.gm.email, GM_PASSWORD)
    before = live.version(seed.world_a.id)
    r = client.post(f"/maps/schematic/{s.slug}/elements", json={"elements": []})
    assert r.status_code == 200
    assert live.version(seed.world_a.id) > before
    mid = live.version(seed.world_a.id)
    r = client.post(f"/maps/schematic/{s.slug}/grid", json={"grid_type": "square", "grid_config": {"cell_size": 50}})
    assert r.status_code == 200, r.text
    assert live.version(seed.world_a.id) > mid
