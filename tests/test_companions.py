"""Companions (familiar / hireling / mount / vehicle) on the character hub: owner writes, GM reads, stranger 404s."""
from app.database import SessionLocal
from app.models import CharacterCompanion

from .conftest import GM_PASSWORD, login
from .test_character_hub import _as, _pc


def _url(pc, tail=""):
    return f"/api/characters/{pc}/hub/companions{tail}"


def test_owner_adds_edits_hurts_and_removes_a_companion(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    r = client.post(_url(pc), json={"name": "  Fang  ", "kind": "mount", "hp": 9, "hp_max": 8, "notes": "bites"})
    assert r.status_code == 200
    c = r.json()["companions"][0]
    assert c["name"] == "Fang" and c["kind"] == "mount" and c["hp"] == 8 and c["hp_max"] == 8      # HP never above max
    cid = c["id"]
    assert client.patch(_url(pc, f"/{cid}"), json={"hp_delta": -3}).json()["companions"][0]["hp"] == 5
    assert client.patch(_url(pc, f"/{cid}"), json={"hp_delta": -99}).json()["companions"][0]["hp"] == 0
    assert client.patch(_url(pc, f"/{cid}"), json={"kind": "dragon"}).json()["companions"][0]["kind"] == "other"
    assert client.post(_url(pc), json={"name": "  "}).status_code == 400
    assert client.post(_url(pc), json={"hp": 3}).status_code == 400
    assert client.post(_url(pc), json={"name": "X", "hp": "abc"}).status_code == 400
    assert client.get(f"/api/characters/{pc}/hub/now").json()["companions"][0]["name"] == "Fang"
    assert client.delete(_url(pc, f"/{cid}")).json()["companions"] == []


def test_companion_cap_and_access(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    for i in range(12):
        assert client.post(_url(pc), json={"name": f"A{i}"}).status_code == 200
    assert client.post(_url(pc), json={"name": "one more"}).status_code == 400
    cid = client.get(_url(pc)).json()["companions"][0]["id"]
    _as(client, seed.player_b)
    assert client.get(_url(pc)).status_code == 404
    assert client.post(_url(pc), json={"name": "Z"}).status_code == 404
    assert client.patch(_url(pc, f"/{cid}"), json={"hp": 1}).status_code == 404
    assert client.delete(_url(pc, f"/{cid}")).status_code == 404
    login(client, seed.gm.email, GM_PASSWORD)
    assert len(client.get(_url(pc)).json()["companions"]) == 12                                     # GM reads ...
    assert client.post(_url(pc), json={"name": "GM"}).status_code == 404                              # ... but does not write


def test_companions_go_with_the_character(client, seed):
    from app import char_extras
    pc = _pc(seed.player_a, seed.world_a)
    _as(client, seed.player_a)
    client.post(_url(pc), json={"name": "Moth"})
    db = SessionLocal()
    try:
        char_extras.delete_for_character(db, pc)
        db.commit()
        assert db.query(CharacterCompanion).filter(CharacterCompanion.character_id == pc).count() == 0
    finally:
        db.close()
