"""Read-only share link: only the owner / GM manage it, anyone with the token reads it, nothing private is on it."""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import GM_PASSWORD, login
from .test_character_hub import _as, _pc


def _fill(pc_id):
    db = SessionLocal()
    try:
        r = db.get(PlayerCharacter, pc_id)
        r.stats_json = json.dumps([{"id": "str", "value": 4}, {"id": "wil", "value": 3}])
        r.feats_json = json.dumps([{"name": "Sneaky", "rank": "Rank 1"}])
        r.equipment_json = json.dumps([{"name": "Pistol", "qty": 1, "equipped": True}])
        r.notes = "SECRET-NOTE-XYZ"
        r.player_name = "SECRET-PLAYER"
        db.commit()
    finally:
        db.close()


def test_owner_makes_link_anyone_reads_it_revoke_and_replace(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Shared Hero")
    _fill(pc)
    _as(client, seed.player_a)
    assert client.get(f"/api/characters/{pc}/share").json()["active"] is False
    first = client.post(f"/api/characters/{pc}/share").json()
    assert first["active"] and "/share/" in first["url"]
    token = first["url"].rsplit("/", 1)[1]
    client.cookies.clear()
    page = client.get(f"/share/{token}")
    assert page.status_code == 200 and "Shared Hero" in page.text and "Sneaky" in page.text and "Pistol" in page.text
    assert "SECRET-NOTE-XYZ" not in page.text and "SECRET-PLAYER" not in page.text
    assert page.headers["cache-control"] == "no-store" and "noindex" in page.headers["x-robots-tag"]
    _as(client, seed.player_a)
    new = client.post(f"/api/characters/{pc}/share").json()
    client.cookies.clear()
    assert client.get(f"/share/{token}").status_code == 404                                  # replaced -> old one dead
    new_token = new["url"].rsplit("/", 1)[1]
    assert client.get(f"/share/{new_token}").status_code == 200
    _as(client, seed.player_a)
    assert client.delete(f"/api/characters/{pc}/share").json()["active"] is False
    client.cookies.clear()
    assert client.get(f"/share/{new_token}").status_code == 404
    assert client.get("/share/nonsense").status_code == 404


def test_only_owner_or_gm_manage_the_link(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Mine")
    _as(client, seed.player_b)
    assert client.post(f"/api/characters/{pc}/share").status_code == 404
    assert client.get(f"/api/characters/{pc}/share").status_code == 404
    client.cookies.clear()
    assert client.post(f"/api/characters/{pc}/share").status_code in (401, 403, 302, 303)
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post(f"/api/characters/{pc}/share").status_code == 200
