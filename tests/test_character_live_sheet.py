"""A sheet stays current while it is open, and read-only viewers get read-only controls.

GET /api/characters/{id}/vitals is the sheet's live-sync source: HP / Shock /
PP / MP / XP / conditions with the same effective maxima the sheet renders.
It is viewable by exactly the people who may view the sheet (never a stranger
in another world). The +/- buttons only render for someone who can manage the
character — a read-only viewer used to get working-looking buttons that 403'd
and then painted "undefined" into the HP box.
"""
import json
import re

from app.database import SessionLocal
from app.models import PlayerCharacter, User, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, _PLAYER_PASSWORD_HASH, login

STATS = [{"id": k, "value": v} for k, v in
         (("str", 3), ("dex", 3), ("bod", 3), ("per", 3), ("wil", 2), ("int", 2), ("cha", 2), ("itu", 2))]


def _pc(seed, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Vex",
                             stats_json=json.dumps(STATS), max_hp=0, current_hp=15, shock_max=0, shock_current=3,
                             pp_current=4, mp_current=2, xp=120, conditions_json=json.dumps(["Burn"]), **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def test_vitals_report_effective_maxima(client, seed):
    pc = _pc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/api/characters/{pc}/vitals")
    assert r.status_code == 200
    d = r.json()
    assert (d["hp"], d["max_hp"]) == (15, 22)
    assert (d["shock"], d["shock_max"]) == (3, 8)
    assert (d["pp"], d["pp_max"], d["mp"], d["mp_max"]) == (4, 12, 2, 8)
    assert d["xp"] == 120 and d["level"] == 1 and "xp_pct" in d
    assert d["conditions"] == ["Burn"]


def test_vitals_follow_sheet_view_access(client, seed):
    pc = _pc(seed)
    # a stranger from another world: no access
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert client.get(f"/api/characters/{pc}/vitals").status_code in (403, 404)
    client.post("/logout")
    # an unknown id is a 404 for the GM
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get("/api/characters/999999/vitals").status_code == 404
    assert client.get(f"/api/characters/{pc}/vitals").status_code == 200


def test_fellow_player_can_read_but_not_edit_when_party_is_visible(client, seed):
    db = SessionLocal()
    try:
        mate = User(email="mate@e2e.local", password_hash=_PLAYER_PASSWORD_HASH, display_name="mate", is_gm=False)
        db.add(mate)
        db.commit()
        db.refresh(mate)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=mate.id, role="player"))
        db.get(World, seed.world_a.id).players_see_party = True
        db.commit()
        email = mate.email
    finally:
        db.close()
    pc = _pc(seed)
    login(client, email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/characters/{pc}/vitals").status_code == 200
    assert client.post(f"/api/characters/{pc}/hp-async", json={"action": "delta", "value": -5}).status_code == 403
    html = client.get(f"/characters/{pc}").text
    for handler in ('onclick="hpDelta(', 'onclick="shockDelta(', 'onclick="ppRest(', 'onclick="mpRest(',
                    'onclick="xpDialog(', 'onclick="hpSetDialog('):
        assert handler not in html, f"read-only viewers must not get controls that 403: {handler}"
    assert 'id="hp-current"' in html, "the numbers themselves are still shown"
    assert 'id="cond-new"' not in html, "no way to add a condition on someone else's sheet"
    assert 'onclick="toggleCondition(' not in html, "condition chips are read-only for a fellow player"


def test_owner_gets_labelled_controls(client, seed):
    pc = _pc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    html = client.get(f"/characters/{pc}").text
    assert 'onclick="hpDelta(-1)"' in html and 'onclick="hpDelta(1)"' in html
    for label in ("Decrease HP", "Increase HP", "Decrease Shock", "Increase Shock",
                  "Decrease PP", "Increase PP", "Decrease MP", "Increase MP"):
        assert f'aria-label="{label}"' in html, label


def test_open_sheet_listens_for_live_updates(client, seed):
    pc = _pc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    html = client.get(f"/characters/{pc}").text
    assert f"/api/characters/{pc}/vitals" in html
    assert "nd-live" in html and "pc-vitals" in html
