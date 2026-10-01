"""AI party-insight hooks can become hidden personal quests in one click.

The party page's AI Insights panel proposes one personal hook per member. Each
hook gets a "+ Quest" button that POSTs the existing /api/quests/apply with
category "personal", visible_to_players false and the party attached. The apply
route (GM-only) now accepts an `assigned_party_id` on a new quest, honoured only
for a party in the SAME world — anything else is ignored, never trusted.
"""
import json

from app.database import SessionLocal
from app.models import Party, Quest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _quests():
    db = SessionLocal()
    try:
        return db.query(Quest).order_by(Quest.id).all()
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _hook(**extra):
    return {"title": "Mira: owes the Guild", "summary": "owes the Guild a favour", "body": "owes the Guild a favour",
            "category": "personal", "status": "active", "visible_to_players": False, **extra}


def test_assigned_party_is_stored_for_a_same_world_party(client, seed):
    party = _add(Party(world_id=seed.world_a.id, name="Crew"))
    _gm(client, seed)
    r = client.post("/api/quests/apply", json={"new_quests": [_hook(assigned_party_id=party)]})
    assert r.status_code == 200 and r.json()["created"] == 1
    q = _quests()[0]
    assert (q.assigned_party_id, q.category, q.visible_to_players) == (party, "personal", False)


def test_other_worlds_party_and_junk_are_ignored(client, seed):
    foreign = _add(Party(world_id=seed.world_b.id, name="Elsewhere"))
    _gm(client, seed)
    for bad in (foreign, 99999, "7", None, True, [1], -3):
        r = client.post("/api/quests/apply", json={"new_quests": [_hook(title=f"hook {bad!r}", assigned_party_id=bad)]})
        assert r.status_code == 200 and r.json()["created"] == 1, bad
    assert all(q.assigned_party_id is None for q in _quests()), "an untrusted party id must never be stored"


def test_omitting_it_keeps_old_behaviour(client, seed):
    _gm(client, seed)
    client.post("/api/quests/apply", json={"new_quests": [{"title": "Plain quest"}]})
    q = _quests()[0]
    assert q.assigned_party_id is None and q.visible_to_players is True and q.category == "side"


def test_apply_is_still_gm_only(client, seed):
    party = _add(Party(world_id=seed.world_a.id, name="Crew"))
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/quests/apply", json={"new_quests": [_hook(assigned_party_id=party)]})
    assert r.status_code in (401, 403, 404)
    assert _quests() == []


def test_party_page_offers_the_quest_button_to_the_gm(client, seed):
    party = _add(Party(world_id=seed.world_a.id, name="Crew"))
    _gm(client, seed)
    html = client.get(f"/parties/{party}").text
    assert "+ Quest" in html and "/api/quests/apply" in html
    assert "assigned_party_id" in html and "visible_to_players: false" in html and "category: 'personal'" in html
