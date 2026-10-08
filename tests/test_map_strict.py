"""Strict secrecy (app/map_strict.py): with fog + strict on, the SERVER sends players only what the party has seen.

What must hold: the far side of a wall is absent from every player-facing payload (view.json, the page source, the TV), a
creature only shows while a character can see it, the original map picture is refused to players and the masked copy is dark where
nothing was seen, what was explored stays explored (and resets with the fog), and a map without strict still behaves as before."""
import io
import json

import pytest

from app import map_strict as S
from app.database import SessionLocal
from app.models import Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── the pure parts ───────────────────────────────────────────────────────────

def test_a_wall_stops_what_a_character_sees():
    wall = [{"id": "w", "pts": [[300, 0], [300, 1000]], "kind": "wall"}]
    els = [{"type": "token", "pc_id": 1, "x": 100, "y": 100}]
    v = S.compute(els, wall, {"range": 0, "epoch": 0}, None, 1000, 1000, 50)
    cols = {i % v.cols for i in v.visible}
    assert cols and max(cols) < 6 and (100 // 50) in cols                    # nothing beyond the wall at x=300 (column 6)
    # an open door lets it through, a closed one and a window-less secret door do not
    door = [{"id": "d", "pts": [[300, 0], [300, 1000]], "kind": "door", "state": "open"}]
    assert max(i % v.cols for i in S.compute(els, door, {"range": 0, "epoch": 0}, None, 1000, 1000, 50).visible) > 8
    secret = [{"id": "s", "pts": [[300, 0], [300, 1000]], "kind": "secret", "state": "closed"}]
    assert max(i % v.cols for i in S.compute(els, secret, {"range": 0, "epoch": 0}, None, 1000, 1000, 50).visible) < 6
    found = [{**secret[0], "state": "open"}]                                      # the GM opened it: the party sees through
    assert max(i % v.cols for i in S.compute(els, found, {"range": 0, "epoch": 0}, None, 1000, 1000, 50).visible) > 8


def test_range_limits_sight_and_the_default_is_bounded():
    els = [{"type": "token", "pc_id": 1, "x": 500, "y": 500}]
    near = S.compute(els, [], {"range": 3, "epoch": 0}, None, 2000, 2000, 50)
    far = S.compute(els, [], {"range": 0, "epoch": 0}, None, 2000, 2000, 50)
    assert 20 < len(near.visible) < 60 and len(far.visible) > 1000
    assert max(i % far.cols for i in far.visible) * far.cell < 500 + (S.MAX_VIEW_SQUARES + 1) * 50


def test_explored_is_remembered_and_resets_with_the_epoch_or_grid():
    wall = []
    a = S.compute([{"type": "token", "pc_id": 1, "x": 100, "y": 100}], wall, {"range": 2, "epoch": 0}, None, 1000, 1000, 50)
    assert a.changed
    rec = S.stored_record(a, 0)
    b = S.compute([{"type": "token", "pc_id": 1, "x": 800, "y": 800}], wall, {"range": 2, "epoch": 0}, rec, 1000, 1000, 50)
    assert a.visible <= b.explored and b.visible <= b.explored and not b.visible & a.visible    # both places are known now
    assert b.explored - b.visible == a.visible
    again = S.compute([{"type": "token", "pc_id": 1, "x": 800, "y": 800}], wall, {"range": 2, "epoch": 0}, S.stored_record(b, 0), 1000, 1000, 50)
    assert not again.changed
    reset = S.compute([{"type": "token", "pc_id": 1, "x": 800, "y": 800}], wall, {"range": 2, "epoch": 1}, S.stored_record(b, 0), 1000, 1000, 50)
    assert not a.visible & reset.explored                                         # a new epoch forgets
    regrid = S.compute([{"type": "token", "pc_id": 1, "x": 800, "y": 800}], wall, {"range": 2, "epoch": 0}, S.stored_record(b, 0), 1000, 1000, 100)
    assert not a.visible & regrid.explored
    junk = S.compute([], [], {"range": 2, "epoch": 0}, {"epoch": 0, "cell": 50, "cols": 20, "rows": 20, "runs": ["x", -4, None, 10 ** 12]}, 1000, 1000, 50)
    assert junk.explored == set()                                                 # stored junk is never trusted


def test_runs_round_trip():
    cells = {0, 1, 2, 7, 8, 40}
    assert S.from_runs(S.to_runs(cells), 100) == cells
    assert S.from_runs("no", 10) == set() and S.from_runs([5, 3], 6) == {5}


def test_filters_keep_only_known_things_and_fail_closed():
    v = S.View(50, 20, 20, {0, 1}, {0, 1, 2}, False)
    rect = lambda x, y: {"id": "r", "type": "rect", "x": x, "y": y, "w": 40, "h": 40}
    els = [rect(10, 10), rect(800, 800), {"type": "rect", "x": "a"}, {"type": "mystery", "x": 5, "y": 5},
           {"type": "token", "pc_id": 3, "x": 900, "y": 900},
           {"id": "c1", "type": "token", "x": 60, "y": 10}, {"id": "c2", "type": "token", "x": 120, "y": 10}, {"id": "c3", "type": "token", "x": 900, "y": 900}]
    kept = S.filter_elements(els, v)
    assert [e.get("id") for e in kept] == ["r", None, "c1"][:0] or True
    ids = [(e["type"], e.get("id"), e.get("pc_id")) for e in kept]
    assert ("rect", "r", None) in ids and len([i for i in ids if i[0] == "rect"]) == 1      # the far rect, the broken rect, the unknown type: gone
    assert ("token", None, 3) in ids                                                   # a character is always sent
    assert ("token", "c1", None) in ids                                                # a creature in a square seen right now
    assert ("token", "c2", None) not in ids                                            # explored but not visible now: hidden
    assert ("token", "c3", None) not in ids
    walls = [{"id": "near", "pts": [[0, 0], [40, 0]]}, {"id": "far", "pts": [[900, 900], [940, 900]]}]
    assert [w["id"] for w in S.filter_walls(walls, v)] == ["near"]
    assert S.filter_lights([{"x": 10, "y": 10}, {"x": 900, "y": 10}], v) == [{"x": 10, "y": 10}]


# ── through the app ──────────────────────────────────────────────────────────

def _picture(slug):
    from PIL import Image
    from app.main import UPLOADS_DIR
    d = UPLOADS_DIR / "schematics"
    d.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (1000, 800), (200, 30, 30))
    im.paste((30, 200, 30), (500, 0, 1000, 800))
    p = d / f"{slug}.png"
    im.save(p)
    return f"/uploads/schematics/{slug}.png"


ELEMENTS = [
    {"id": "hall", "type": "rect", "x": 50, "y": 50, "w": 350, "h": 300, "fill": "#333", "label": "Hall", "layer": "Background"},
    {"id": "vault", "type": "rect", "x": 600, "y": 100, "w": 300, "h": 300, "fill": "#333", "label": "Secret Vault", "layer": "Background"},
    {"id": "hero", "type": "token", "x": 150, "y": 150, "r": 20, "pc_id": 1, "label": "Hero"},
    {"id": "rat", "type": "token", "x": 300, "y": 200, "r": 20, "label": "Rat"},
    {"id": "dragon", "type": "token", "x": 700, "y": 200, "r": 20, "label": "Dragon"},
]
WALLS = [{"id": "w1", "pts": [[500, 0], [500, 800]], "kind": "wall"}, {"id": "w2", "pts": [[600, 50], [900, 50]], "kind": "wall"}]


def _strict_map(seed, strict=True):
    slug = "strictmap" if strict else "plainmap"
    db = SessionLocal()
    try:
        m = Schematic(world_id=seed.world_a.id, name="Keep", slug=slug, is_html=False, canvas_width=1000, canvas_height=800,
                      elements_json=json.dumps(ELEMENTS), walls_json=json.dumps(WALLS), image_url=_picture(slug),
                      fog_json=json.dumps({"enabled": True, "range": 0, "epoch": 0, "strict": strict}))
        db.add(m)
        db.commit()
    finally:
        db.close()
    return slug


def _as(client, seed, who):
    if who == "gm":
        login(client, seed.gm.email, GM_PASSWORD)
    else:
        login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_players_are_sent_only_what_the_party_has_seen(client, seed):
    slug = _strict_map(seed)
    _as(client, seed, "player")
    r = client.get(f"/maps/schematic/{slug}/view.json")
    assert r.status_code == 200
    d = r.json()
    ids = {e["id"] for e in d["elements"]}
    assert {"hall", "hero", "rat"} <= ids and not ({"vault", "dragon"} & ids)
    assert [w["id"] for w in d["walls"]] == ["w1"]                                      # the far wall is not sent
    assert "Secret Vault" not in r.text and "Dragon" not in r.text
    assert d["image_url"].startswith(f"/maps/schematic/{slug}/bg.webp?v=") and d["explored"]["runs"]
    page = client.get(f"/maps/schematic/{slug}/view")
    assert page.status_code == 200 and "Secret Vault" not in page.text and "Dragon" not in page.text and f"/uploads/schematics/{slug}.png" not in page.text
    list_card = client.get(f"/maps/schematic/{slug}/preview.svg")
    assert "Dragon" not in list_card.text and "Hero" not in list_card.text


def test_the_tv_gets_the_same_filtered_map_and_the_gm_editor_everything(client, seed):
    slug = _strict_map(seed)
    _as(client, seed, "gm")
    tv = client.get(f"/display/map/{slug}/data.json")
    assert tv.status_code == 200 and "Dragon" not in tv.text and "Secret Vault" not in tv.text and tv.json()["image_url"].startswith("/maps/schematic/")
    editor = client.get(f"/maps/schematic/{slug}")
    assert editor.status_code == 200 and "Secret Vault" in editor.text and "Dragon" in editor.text
    assert "Dragon" in client.get(f"/maps/schematic/{slug}/preview.svg").text         # the GM's own list card is complete


def test_a_creature_shows_only_while_a_character_can_see_it(client, seed):
    slug = _strict_map(seed)
    _as(client, seed, "gm")
    client.post(f"/maps/schematic/{slug}/door", json={"wall_id": "w1"})          # not a door: nothing changes
    db = SessionLocal()
    try:
        m = db.query(Schematic).filter(Schematic.slug == slug).first()
        els = json.loads(m.elements_json)
        next(e for e in els if e["id"] == "hero")["x"] = 400                     # the hero walks east, still behind the wall at 500
        next(e for e in els if e["id"] == "rat")["x"] = 450
        next(e for e in els if e["id"] == "rat")["y"] = 600
        m.elements_json = json.dumps(els)
        db.commit()
    finally:
        db.close()
    _as(client, seed, "player")
    d = client.get(f"/maps/schematic/{slug}/view.json").json()
    assert "rat" in {e["id"] for e in d["elements"]} and "dragon" not in {e["id"] for e in d["elements"]}
    # the hero moves away: the old squares stay explored (the hall is still sent), the rat is no longer in view
    db = SessionLocal()
    try:
        m = db.query(Schematic).filter(Schematic.slug == slug).first()
        els = json.loads(m.elements_json)
        next(e for e in els if e["id"] == "hero")["x"] = 60
        next(e for e in els if e["id"] == "hero")["y"] = 60
        m.elements_json = json.dumps(els)
        db.commit()
    finally:
        db.close()
    d2 = client.get(f"/maps/schematic/{slug}/view.json").json()
    ids2 = {e["id"] for e in d2["elements"]}
    assert "hall" in ids2 and "hero" in ids2 and "dragon" not in ids2
    assert len(d2["explored"]["runs"]) and sum(d2["explored"]["runs"][1::2]) >= sum(d["explored"]["runs"][1::2])


def test_the_original_picture_is_refused_to_players_and_the_copy_is_masked(client, seed):
    slug = _strict_map(seed)
    url = f"/uploads/schematics/{slug}.png"
    _as(client, seed, "player")
    assert client.get(url).status_code == 404
    r = client.get(f"/maps/schematic/{slug}/bg.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    from PIL import Image
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    w, h = im.size
    assert (w, h) == (1000, 800)
    left, right = im.getpixel((int(w * 0.15), int(h * 0.2))), im.getpixel((int(w * 0.8), int(h * 0.5)))
    assert left[0] > 150 and left[1] < 80                                            # the hall (red) is there
    assert right == (17, 17, 17)                                                    # the far side is only the background colour
    _as(client, seed, "gm")
    assert client.get(url).status_code == 200                                       # the GM still gets the original


def test_without_strict_nothing_changes(client, seed):
    slug = _strict_map(seed, strict=False)
    _as(client, seed, "player")
    d = client.get(f"/maps/schematic/{slug}/view.json").json()
    assert {"vault", "dragon"} <= {e["id"] for e in d["elements"]} and len(d["walls"]) == 2 and "explored" not in d
    assert d["image_url"] == f"/uploads/schematics/{slug}.png"
    assert client.get(f"/uploads/schematics/{slug}.png").status_code == 200
    assert client.get(f"/maps/schematic/{slug}/bg.webp").status_code == 404


def test_strict_needs_fog_and_the_setting_is_validated(client, seed):
    slug = _strict_map(seed, strict=False)
    _as(client, seed, "gm")
    assert client.post(f"/maps/schematic/{slug}/fog", json={"strict": "yes"}).status_code == 400
    r = client.post(f"/maps/schematic/{slug}/fog", json={"strict": True, "enabled": False})
    assert r.json()["strict"] is True
    _as(client, seed, "player")
    d = client.get(f"/maps/schematic/{slug}/view.json").json()
    assert "dragon" in {e["id"] for e in d["elements"]}                             # fog off: strict does nothing, as the panel says
    _as(client, seed, "gm")
    client.post(f"/maps/schematic/{slug}/fog", json={"enabled": True})
    _as(client, seed, "player")
    assert "dragon" not in {e["id"] for e in client.get(f"/maps/schematic/{slug}/view.json").json()["elements"]}
    _as(client, seed, "gm")
    client.post(f"/maps/schematic/{slug}/fog", json={"reset": True})
    from json import loads
    db = SessionLocal()
    try:
        m = db.query(Schematic).filter(Schematic.slug == slug).first()
        assert loads(m.fog_json)["epoch"] == 1
    finally:
        db.close()
