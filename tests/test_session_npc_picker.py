"""Tests for the Session detail page's "NPCs Featured" picker
(app/routers/sessions.py's session_detail/session_edit) — previously a
flat checkbox list over every entity in the world regardless of kind, now
scoped to character/creature entities (excluding anything tagged
subtype="PC") and enriched with folder data for the client-side folder-tree
+ search picker in app/templates/sessions/detail.html. The save path
(session_edit, keyed off name="npc_entity_ids" checkboxes) is unchanged —
these tests confirm the narrower candidate set still round-trips correctly.
"""
import json

from app.database import SessionLocal
from app.models import Entity, GameSession

from .conftest import GM_PASSWORD, login


def _make_entity(world_id, kind, name, subtype=None, folder=""):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, subtype=subtype, name=name, folder=folder)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _make_session(world_id, title="Session 1"):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title=title, session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _extract_npc_candidates(html):
    marker = "const NPC_CANDIDATES = "
    start = html.index(marker) + len(marker)
    end = html.index(";", start)
    return json.loads(html[start:end])


def test_npc_picker_includes_characters_and_creatures(client, seed):
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC", folder="NPCs/Bazaar")
    creature_id = _make_entity(seed.world_a.id, "creature", "Giant Rat", subtype="neutral", folder="Creatures")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    assert r.status_code == 200
    candidates = _extract_npc_candidates(r.text)
    ids = {c["id"] for c in candidates}
    assert npc_id in ids
    assert creature_id in ids


def test_npc_picker_excludes_pc_subtype_and_other_kinds(client, seed):
    pc_id = _make_entity(seed.world_a.id, "character", "Aria the Player", subtype="PC")
    location_id = _make_entity(seed.world_a.id, "location", "The Bazaar")
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    candidates = _extract_npc_candidates(r.text)
    ids = {c["id"] for c in candidates}
    assert pc_id not in ids
    assert location_id not in ids
    assert npc_id in ids


def test_npc_picker_includes_entities_with_no_subtype_or_folder(client, seed):
    """subtype is a suggestion, not enforced — an entity with subtype=None
    is still a plain character/creature and should still be pickable."""
    unlabeled_id = _make_entity(seed.world_a.id, "character", "Unlabeled Bob", subtype=None, folder="")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    candidates = _extract_npc_candidates(r.text)
    matching = [c for c in candidates if c["id"] == unlabeled_id]
    assert len(matching) == 1
    assert matching[0]["folder"] == ""


def test_npc_picker_carries_folder_for_client_side_tree(client, seed):
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC", folder="NPCs/Bazaar")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    candidates = _extract_npc_candidates(r.text)
    matching = [c for c in candidates if c["id"] == npc_id]
    assert matching[0]["folder"] == "NPCs/Bazaar"


def test_npc_selection_still_round_trips_through_session_edit(client, seed):
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC")
    creature_id = _make_entity(seed.world_a.id, "creature", "Giant Rat")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/sessions/{gs_id}/edit", data={
        "title": "Session 1", "session_num": "1", "session_date": "", "summary": "", "party_id": "",
        "npc_entity_ids": [str(npc_id), str(creature_id)],
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        gs = db.get(GameSession, gs_id)
        saved_ids = {n["entity_id"] for n in json.loads(gs.npcs_json)}
    finally:
        db.close()
    assert saved_ids == {npc_id, creature_id}

    # Reload: the saved selection still resolves to real names in npc_names.
    r = client.get(f"/sessions/{gs_id}")
    assert "Elena the Merchant" in r.text
    assert "Giant Rat" in r.text
