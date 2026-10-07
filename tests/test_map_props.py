"""The map-object library (app/routers/map_props.py): who may use it, what an upload becomes, and what is kept."""
import io
import json

import pytest
from PIL import Image

from app.database import SessionLocal
from app.models import MapProp, Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _png(size=(120, 80), colour=(180, 30, 30, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", size, colour).save(buf, "PNG")
    return buf.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><rect width="100" height="50" fill="#963"/></svg>'
EVIL = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script></svg>'


def _gm(client, seed, world=None):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)


def _upload(client, *files, **data):
    return client.post("/api/maps/props/upload", files=[("files", f) for f in files], data=data)


def test_players_cannot_use_the_library(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/maps/props").status_code == 403
    assert _upload(client, ("bed.png", _png(), "image/png")).status_code == 403
    assert client.post("/api/maps/props/1/delete").status_code == 403


def test_several_pictures_in_several_formats_become_library_objects(client, seed):
    _gm(client, seed)
    r = _upload(client, ("Big Bed.png", _png(), "image/png"), ("table.svg", SVG, "image/svg+xml"), ("note.txt", b"hi", "text/plain"))
    assert r.status_code == 200
    d = r.json()
    assert [p["name"] for p in d["added"]] == ["Big Bed", "table"]
    assert [p["kind"] for p in d["added"]] == ["raster", "svg"]
    assert d["added"][0]["url"].endswith(".webp") and d["added"][1]["url"].endswith(".svg")
    assert d["refused"][0]["file"] == "note.txt"
    listing = client.get("/api/maps/props").json()
    assert [p["name"] for p in listing["props"]] == ["Big Bed", "table"]
    # the stored files are served
    assert client.get(d["added"][0]["url"]).status_code == 200


def test_a_hostile_svg_is_refused_without_losing_the_good_file(client, seed):
    _gm(client, seed)
    d = _upload(client, ("evil.svg", EVIL, "image/svg+xml"), ("ok.png", _png(), "image/png")).json()
    assert [p["name"] for p in d["added"]] == ["ok"]
    assert d["refused"][0]["file"] == "evil.svg"


def test_batch_width_sets_the_footprint(client, seed):
    _gm(client, seed)
    p = _upload(client, ("wall.png", _png((400, 100)), "image/png"), cells_w="4").json()["added"][0]
    assert (p["cells_w"], p["cells_h"]) == (4.0, 1.0)
    # nonsense is ignored in favour of the guess from the picture
    q = _upload(client, ("a.png", _png((200, 100)), "image/png"), cells_w="NaN").json()["added"][0]
    assert (q["cells_w"], q["cells_h"]) == (2.0, 1.0)


def test_update_clamps_and_cleans_and_is_world_scoped(client, seed):
    _gm(client, seed)
    p = _upload(client, ("a.png", _png(), "image/png")).json()["added"][0]
    r = client.post(f"/api/maps/props/{p['id']}/update", json={"name": "  Four   poster  bed ", "cells_w": 1e9, "cells_h": "x", "tags": "bed  furniture"})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "Four poster bed" and d["cells_w"] == 40.0 and d["tags"] == "bed furniture"
    raw = client.post(f"/api/maps/props/{p['id']}/update", content='{"cells_w": NaN, "cells_h": Infinity}',
                      headers={"Content-Type": "application/json"})
    assert raw.status_code == 200 and raw.json()["cells_w"] == 40.0       # NaN/Infinity never get stored
    other = SessionLocal()
    try:
        o = MapProp(world_id=seed.world_b.id, name="theirs", file_url="/uploads/props/x.webp")
        other.add(o); other.commit(); oid = o.id
    finally:
        other.close()
    assert client.post(f"/api/maps/props/{oid}/update", json={"name": "mine"}).status_code == 404
    assert client.post(f"/api/maps/props/{oid}/delete").status_code == 404
    assert [x["name"] for x in client.get("/api/maps/props").json()["props"]] == ["Four poster bed"]


def test_deleting_keeps_the_file_while_a_map_uses_it(client, seed):
    _gm(client, seed)
    a = _upload(client, ("a.png", _png(), "image/png")).json()["added"][0]
    b = _upload(client, ("b.png", _png((90, 60)), "image/png")).json()["added"][0]
    db = SessionLocal()
    try:
        db.add(Schematic(world_id=seed.world_a.id, name="Inn", slug="inn-props", is_html=False,
                         elements_json=json.dumps([{"id": "1", "type": "image", "href": a["url"], "x": 0, "y": 0, "w": 50, "h": 50}])))
        db.commit()
    finally:
        db.close()
    ra = client.post(f"/api/maps/props/{a['id']}/delete").json()
    rb = client.post(f"/api/maps/props/{b['id']}/delete").json()
    assert ra["file_kept"] is True and rb["file_kept"] is False
    assert client.get(a["url"]).status_code == 200
    assert client.get(b["url"]).status_code == 404


def test_upload_limits(client, seed):
    _gm(client, seed)
    assert _upload(client, *[(f"{i}.png", _png(), "image/png") for i in range(26)]).status_code == 400
    assert client.post("/api/maps/props/upload", data={}).status_code == 422
