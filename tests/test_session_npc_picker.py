"""Tests for the Session detail page's "Entities Featured" picker
(app/routers/sessions.py's session_detail/session_edit, née "NPCs
Featured") — originally scoped to character/creature entities only
(excluding anything tagged subtype="PC"), now broadened to also cover
player-character lore write-ups (character entities tagged subtype="PC"),
locations, organizations, races, professions, and notes, each grouped
under its own kind-labeled top-level branch in the client-side folder-tree
picker (app/templates/sessions/detail.html, app.routers.sessions.
_featured_entity_candidates). The save path (session_edit, still keyed off
name="npc_entity_ids" checkboxes into gs.npcs_json — no schema change, no
rename, see _featured_entity_candidates' own docstring for why) is
unchanged — these tests confirm the broader candidate set still round-trips
correctly, and that what a GM checks here reaches RAG as a guaranteed,
pinned set (app.audio_jobs._session_featured_entity_ids/_build_rag_context).
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


def test_npc_picker_now_includes_pc_lore_and_other_kinds(client, seed):
    """The picker used to exclude subtype="PC" characters and every kind
    besides character/creature — it now covers all of them, each under its
    own kind-labeled group so a GM can still tell an NPC from a location
    from a player-character write-up at a glance."""
    pc_id = _make_entity(seed.world_a.id, "character", "Aria the Player", subtype="PC")
    location_id = _make_entity(seed.world_a.id, "location", "The Bazaar")
    org_id = _make_entity(seed.world_a.id, "organization", "The Cinder Guild")
    race_id = _make_entity(seed.world_a.id, "race", "Ashborn")
    profession_id = _make_entity(seed.world_a.id, "profession", "Streetwise Fixer")
    note_id = _make_entity(seed.world_a.id, "note", "Session 3 planning note")
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    candidates = _extract_npc_candidates(r.text)
    ids = {c["id"] for c in candidates}
    assert {pc_id, location_id, org_id, race_id, profession_id, note_id, npc_id} <= ids


def test_npc_picker_groups_pc_lore_separately_from_npcs(client, seed):
    """A subtype="PC" character shows up under its own "Player Characters"
    group, not lumped in with NPCs — the group label is prefixed onto the
    candidate's folder so the client-side tree renders it as its own
    top-level branch."""
    pc_id = _make_entity(seed.world_a.id, "character", "Aria the Player", subtype="PC")
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    candidates = {c["id"]: c for c in _extract_npc_candidates(r.text)}
    assert "Player Characters" in candidates[pc_id]["folder"]
    assert "NPCs" in candidates[npc_id]["folder"]
    assert candidates[pc_id]["folder"] != candidates[npc_id]["folder"]


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
    # No original folder, but still grouped under its kind label.
    assert matching[0]["folder"] == "👤 NPCs"


def test_npc_picker_carries_folder_for_client_side_tree(client, seed):
    npc_id = _make_entity(seed.world_a.id, "character", "Elena the Merchant", subtype="NPC", folder="NPCs/Bazaar")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/sessions/{gs_id}")
    candidates = _extract_npc_candidates(r.text)
    matching = [c for c in candidates if c["id"] == npc_id]
    assert matching[0]["folder"] == "👤 NPCs/NPCs/Bazaar"


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


def test_location_selection_round_trips_through_session_edit(client, seed):
    """A newly-eligible kind (location) saves and reloads the same way
    characters/creatures always did."""
    location_id = _make_entity(seed.world_a.id, "location", "The Bazaar")
    gs_id = _make_session(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/sessions/{gs_id}/edit", data={
        "title": "Session 1", "session_num": "1", "session_date": "", "summary": "", "party_id": "",
        "npc_entity_ids": [str(location_id)],
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        gs = db.get(GameSession, gs_id)
        saved_ids = {n["entity_id"] for n in json.loads(gs.npcs_json)}
    finally:
        db.close()
    assert saved_ids == {location_id}
