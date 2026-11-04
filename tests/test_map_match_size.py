"""Uploading a map picture can also set the map's size to the picture's (match_size checkbox)."""
import io
import json

import pytest
from PIL import Image

from app.main import _MAPS_DIR

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean_maps_dir():
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()
    yield
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()


def _png(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(b, "PNG")
    return b.getvalue()


def _size(slug):
    d = json.loads((_MAPS_DIR / f"{slug}.json").read_text())
    return d["width"], d["height"]


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _make(world_id, slug):
    (_MAPS_DIR / f"{slug}.json").write_text(json.dumps({"name": slug, "world_id": world_id, "width": 3072, "height": 3072, "markers": []}))


def test_upload_without_the_box_keeps_size(client, seed):
    _make(seed.world_a.id, "m")
    _gm(client, seed)
    r = client.post("/maps/m/upload", files={"file": ("a.png", _png(640, 480), "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and _size("m") == (3072, 3072)


def test_upload_with_the_box_matches_image(client, seed):
    _make(seed.world_a.id, "m")
    _gm(client, seed)
    r = client.post("/maps/m/upload", data={"match_size": "1"}, files={"file": ("a.png", _png(640, 480), "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and _size("m") == (640, 480)


def test_big_and_tiny_pictures_are_clamped(client, seed):
    _make(seed.world_a.id, "m")
    _gm(client, seed)
    client.post("/maps/m/upload", data={"match_size": "1"}, files={"file": ("a.png", _png(9000, 3000), "image/png")})
    assert _size("m") == (8000, 2666)
    client.post("/maps/m/upload", data={"match_size": "on"}, files={"file": ("a.png", _png(50, 40), "image/png")})
    assert _size("m") == (200, 200)


def test_new_map_with_image_can_match(client, seed):
    _gm(client, seed)
    r = client.post("/maps/new", data={"name": "Fit Me", "width": "3072", "height": "3072", "match_size": "1"},
                    files={"image_file": ("a.png", _png(1000, 700), "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and _size("fit-me") == (1000, 700)
    r = client.post("/maps/new", data={"name": "Keep Me", "width": "3072", "height": "3072"},
                    files={"image_file": ("a.png", _png(1000, 700), "image/png")}, follow_redirects=False)
    assert _size("keep-me") == (3072, 3072)
