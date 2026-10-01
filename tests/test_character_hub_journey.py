"""The hub's Journey tab: a party roster and loot you can claim in place.

Claiming used to mean "go to the party page", which for a player is closed by
default (Parties is opt-in) — so by default a player could see unclaimed loot
on their Journey tab but never act on it. POST /api/characters/{id}/hub/loot
lets the OWNER of a party member claim / unclaim for that character only.
Also: the Journey tab's recent sessions respect the Sessions section level,
and its roster lists teammates (never GM-hidden companions).
"""
import json

from app.database import SessionLocal
from app.models import Entity, GameSession, Party, PlayerCharacter, World

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


def _scene(seed):
    mine = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Vex", level=3))
    mate = _add(PlayerCharacter(world_id=seed.world_a.id, name="Mira", level=4, race="Elf", char_class="Rogue",
                                max_hp=20, current_hp=11, conditions_json=json.dumps(["Burn"])))
    secret = _add(Entity(world_id=seed.world_a.id, kind="character", name="Secret Spy", visible_to_players=False))
    loot = [{"lid": "aaaaaaaa", "name": "Gem", "qty": 1, "claimed_by": []},
            {"lid": "bbbbbbbb", "name": "Rope", "qty": 2, "claimed_by": [mate]}]
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([mine, mate]),
                       member_entity_ids_json=json.dumps([secret]), loot_json=json.dumps(loot)))
    return mine, mate, party


def _owner(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _claims(party):
    db = SessionLocal()
    try:
        return {i["lid"]: i["claimed_by"] for i in json.loads(db.get(Party, party).loot_json)}
    finally:
        db.close()


def _post(client, pc, **body):
    return client.post(f"/api/characters/{pc}/hub/loot", json=body)


# ── claiming ─────────────────────────────────────────────────────────────────

def test_owner_can_claim_and_unclaim_for_their_character(client, seed):
    mine, mate, party = _scene(seed)
    _owner(client, seed)
    r = _post(client, mine, action="claim", lid="aaaaaaaa")
    assert r.status_code == 200 and _claims(party)["aaaaaaaa"] == [mine]
    assert _post(client, mine, action="claim", lid="aaaaaaaa").status_code == 200
    assert _claims(party)["aaaaaaaa"] == [mine], "claiming twice is idempotent"
    assert _post(client, mine, action="unclaim", lid="aaaaaaaa").status_code == 200
    assert _claims(party)["aaaaaaaa"] == []
    assert _claims(party)["bbbbbbbb"] == [mate], "other people's claims are untouched"


def test_claim_response_reports_the_new_state(client, seed):
    mine, _, party = _scene(seed)
    _owner(client, seed)
    d = _post(client, mine, action="claim", lid="aaaaaaaa").json()
    gem = next(i for i in d["loot"] if i["lid"] == "aaaaaaaa")
    assert gem["mine"] is True and gem["claimed"] is True
    rope = next(i for i in d["loot"] if i["lid"] == "bbbbbbbb")
    assert rope["mine"] is False and rope["claimed"] is True


def test_only_the_owner_may_claim(client, seed):
    mine, mate, party = _scene(seed)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert _post(client, mine, action="claim", lid="aaaaaaaa").status_code == 404
    client.post("/logout")
    login(client, seed.gm.email, GM_PASSWORD)
    assert _post(client, mine, action="claim", lid="aaaaaaaa").status_code == 404, "the hub API is owner-only, even for the GM"
    assert _claims(party)["aaaaaaaa"] == []


def test_a_body_pc_id_cannot_redirect_the_claim(client, seed):
    mine, mate, party = _scene(seed)
    _owner(client, seed)
    _post(client, mine, action="claim", lid="aaaaaaaa", pc_id=mate)
    assert _claims(party)["aaaaaaaa"] == [mine], "a claim is always for the owner's own character"


def test_unknown_lid_and_bad_input(client, seed):
    mine, _, party = _scene(seed)
    _owner(client, seed)
    assert _post(client, mine, action="claim", lid="deadbeef").status_code == 409
    assert _post(client, mine, action="claim").status_code == 400
    assert _post(client, mine, action="steal", lid="aaaaaaaa").status_code == 400
    assert client.post(f"/api/characters/{mine}/hub/loot", content=b"nope").status_code == 400


def test_no_party_no_claim(client, seed):
    solo = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Solo"))
    _owner(client, seed)
    assert _post(client, solo, action="claim", lid="aaaaaaaa").status_code == 404


def test_claim_notifies_live_sync(client, seed, monkeypatch):
    from app.routers import character_hub as hub
    touched = []
    monkeypatch.setattr(hub.live, "touch", lambda world_id: touched.append(world_id))
    mine, _, _ = _scene(seed)
    _owner(client, seed)
    _post(client, mine, action="claim", lid="aaaaaaaa")
    assert touched == [seed.world_a.id]


# ── what the tab renders ─────────────────────────────────────────────────────

def test_journey_lists_teammates_but_not_hidden_companions(client, seed):
    mine, _, _ = _scene(seed)
    _owner(client, seed)
    html = client.get(f"/characters/{mine}").text
    assert "Mira" in html and "Secret Spy" not in html
    assert "11/20" in html, "teammate HP is shown on the roster"
    assert "Burn" in html


def test_journey_loot_has_claim_controls(client, seed):
    mine, _, _ = _scene(seed)
    _owner(client, seed)
    html = client.get(f"/characters/{mine}").text
    assert 'data-lid="aaaaaaaa"' in html and 'data-claim="claim"' in html
    assert "send('loot'" in html, "the buttons must post to the hub loot route"


def test_journey_sessions_respect_the_sessions_section(client, seed):
    mine, _, party = _scene(seed)
    _add(GameSession(world_id=seed.world_a.id, title="The Hidden Vault Heist", session_num=3, party_id=party))
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).section_access_json = json.dumps({"sessions": {"player": "none"}})
        db.commit()
    finally:
        db.close()
    _owner(client, seed)
    assert "The Hidden Vault Heist" not in client.get(f"/characters/{mine}").text
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).section_access_json = json.dumps({"sessions": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    assert "The Hidden Vault Heist" in client.get(f"/characters/{mine}").text
