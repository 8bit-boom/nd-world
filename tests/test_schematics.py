"""Schematic (battle-map) live-play API tests. Covers the world-membership
write-access gap (Phase 1): move-token/pickup-item/buy-item used to derive
"ownership" purely from a PlayerCharacter row, which member_remove() never
revokes — so a kicked player kept live-play write access forever even though
the read view (schematic_player_view) already correctly 404s for them.
"""
import json

import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter, Schematic, WorldMembership

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


def _make_pc(world_id, owner_id, name="Hero"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_id, name=name)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


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
