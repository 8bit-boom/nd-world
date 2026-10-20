"""Tiled map pictures (efficiency plan, step 3): the pyramid's levels and grid, a small build end to end, serving, and the viewer's fallback."""
import json

from PIL import Image

from app import map_tiles

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_levels_cover_the_whole_map_and_keep_extra_detail():
    assert map_tiles.plan_levels(3072, 3072, 3072, 3072) == (-4, 0)       # one tile at -4, the picture's own size at 0
    assert map_tiles.plan_levels(6000, 6000, 3000, 3000) == (-4, 1)       # a sharper picture than the map's size keeps it at zoom 1
    assert map_tiles.plan_levels(200, 100, 200, 100) == (0, 0)
    assert map_tiles.plan_levels(10**6, 10, 10, 10)[1] == map_tiles.MAX_NATIVE_ZOOM
    assert map_tiles.tile_range(512, 512, 0) == ((0, 1), (-2, -1))        # CRS.Simple: y runs negative
    assert map_tiles.tile_range(512, 300, -1) == ((0, 0), (-1, -1))


def test_a_small_map_is_cut_and_its_tiles_line_up(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "world.db"))
    pic = tmp_path / "m.png"
    im = Image.new("RGB", (600, 400), "white")
    im.paste((255, 0, 0), (0, 0, 300, 200))                              # top-left quarter red
    im.save(pic)
    meta = map_tiles.build("m", pic, 600, 400)
    assert meta["zmin"] == -2 and meta["native"] == 0 and meta["tiles"] > 0
    assert json.loads((tmp_path / "map_tiles" / meta["key"] / "meta.json").read_text()) == meta
    # zoom 0: Leaflet tile (0, -2) covers pixel y -512..-257, i.e. picture rows -112..143 (the first 112 rows are outside the map)
    t = Image.open(map_tiles.tile_path(meta["key"], 0, 0, -2)).convert("RGBA")
    assert t.getpixel((5, 5))[3] == 0                                     # above the map: transparent
    assert t.getpixel((5, 120))[:3] == (255, 0, 0)                        # row 8 of the picture, in the red quarter
    assert map_tiles.build("m", pic, 600, 400) == meta                    # already built: reused, not rebuilt
    assert map_tiles.tile_path("../x", 0, 0, 0) is None and map_tiles.tile_path(meta["key"] + ".part", 0, 0, -1) is None
    pic.write_bytes(open(pic, "rb").read() + b"x" * 10)                   # a replaced picture gets a new pyramid
    meta2 = map_tiles.build("m", pic, 600, 400)
    assert meta2["key"] != meta["key"] and not (tmp_path / "map_tiles" / meta["key"]).exists()


def test_tiles_are_served_to_signed_in_viewers_only(client, seed, tmp_path, monkeypatch):
    import os
    root = map_tiles.root_dir()
    (root / "k-1" / "0").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (256, 256), "blue").save(root / "k-1" / "0" / "0_-1.webp", format="WEBP")
    (root / "k-1" / "meta.json").write_text("{}")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/map-tiles/k-1/0/0_-1.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp" and "private" in r.headers["cache-control"]
    assert client.get("/map-tiles/k-1/0/9_9.webp").status_code == 404
    assert client.get("/map-tiles/k-1/0/..%2Fmeta.json").status_code == 404
    client.cookies.clear()
    assert client.get("/map-tiles/k-1/0/0_-1.webp", follow_redirects=False).status_code in (303, 401)


def test_the_viewer_uses_tiles_when_they_exist_and_the_picture_until_then():
    src = open("app/templates/map_viewer.html").read()
    assert "if (MAP_TILES)" in src and "L.tileLayer('/map-tiles/'" in src and "L.imageOverlay(IMG_URL, bounds)" in src
