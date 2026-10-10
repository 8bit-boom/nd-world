"""Handouts: a picture the GM sends to the players is kept for whoever missed the pop-up (WorldHandout, hub/handouts)."""
from app.database import SessionLocal
from app.models import ImageAlbum, WorldHandout

from .conftest import GM_PASSWORD, login
from .test_character_hub import _add, _as, _pc


def _shown(seed, client, url="/uploads/a.png"):
    """Have the GM spotlight an image of the world (the image has to belong to it)."""
    import json
    _add(ImageAlbum(world_id=seed.world_a.id, name="Maps", image_urls_json=json.dumps([url])))


def test_spotlight_keeps_the_picture_and_the_player_sees_it_as_new(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _shown(seed, client)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/images/spotlight", json={"url": "/uploads/a.png"}).status_code == 200
    assert client.post("/images/spotlight", json={"url": "/uploads/a.png"}).status_code == 200      # the same picture twice: one handout
    client.cookies.clear()
    _as(client, seed.player_a)
    d = client.get(f"/api/characters/{pc}/hub/handouts").json()
    assert len(d["handouts"]) == 1 and d["handouts"][0]["url"] == "/uploads/a.png" and d["handouts"][0]["new"] is True
    assert client.get(f"/api/characters/{pc}/hub/now").json()["handouts_new"] == 1
    assert client.post(f"/api/characters/{pc}/hub/handouts/seen").status_code == 200
    assert client.get(f"/api/characters/{pc}/hub/now").json()["handouts_new"] == 0
    assert client.get(f"/api/characters/{pc}/hub/handouts").json()["handouts"][0]["new"] is False


def test_handouts_are_the_worlds_own(client, seed):
    pc_b = _pc(seed.player_b, seed.world_b, "Other")
    _add(WorldHandout(world_id=seed.world_a.id, url="/uploads/secret-a.png", label="A only"))
    _as(client, seed.player_b)
    assert client.get(f"/api/characters/{pc_b}/hub/handouts").json()["handouts"] == []
    pc_a = _pc(seed.player_a, seed.world_a, "Hero")
    assert client.get(f"/api/characters/{pc_a}/hub/handouts").status_code == 404
