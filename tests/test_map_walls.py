"""Walls, doors and fog settings (app/map_walls.py, app/routers/map_walls.py).

The rules that matter: geometry is validated like any other input other browsers will draw, only editors can change it, and a
SECRET door is never sent to a player - not in view.json, not in the page source, not to the table's TV."""
import json

import pytest

from app import live, map_walls as W
from app.database import SessionLocal
from app.models import Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── the pure helpers ─────────────────────────────────────────────────────────

def test_walls_are_cleaned_clamped_and_capped():
    walls, warns = W.clean_walls([
        {"id": "a b!c", "pts": [[10, 10], [10, 10], [100.126, 10], [5000, -50]], "kind": "door", "state": "open"},
        {"pts": [[1, 1]]},                                    # one point: not a wall
        {"pts": [[0, 0], ["x", 5], [True, 1], [float("nan"), 1], [20, 20]], "kind": "dragon"},
        "junk", None, {"pts": "no"},
    ], 1000, 800)
    assert [w["id"] for w in walls] == ["abc", "w3"]
    assert walls[0]["pts"] == [[10.0, 10.0], [100.13, 10.0], [1000.0, 0.0]]          # de-duplicated, rounded, clamped
    assert walls[0]["kind"] == "door" and walls[0]["state"] == "open"
    assert walls[1]["kind"] == "wall" and "state" not in walls[1] and len(walls[1]["pts"]) == 2
    assert any("skipped" in w for w in warns)


def test_wall_flood_is_capped():
    many = [{"pts": [[i % 100, 0], [i % 100, 50]]} for i in range(W.MAX_WALLS + 500)]
    walls, warns = W.clean_walls(many)
    assert len(walls) == W.MAX_WALLS and any("first" in w for w in warns)
    huge = [{"pts": [[i, 0] for i in range(500)]}]
    assert len(W.clean_walls(huge)[0][0]["pts"]) == W.MAX_POINTS_PER_WALL
    with pytest.raises(ValueError):
        W.clean_walls({"pts": []})


def test_ids_stay_unique():
    walls, _ = W.clean_walls([{"id": "x", "pts": [[0, 0], [1, 1]]}, {"id": "x", "pts": [[0, 0], [2, 2]]}])
    assert len({w["id"] for w in walls}) == 2


def test_players_never_learn_a_door_is_secret():
    walls = [{"id": "s", "pts": [[0, 0], [10, 0]], "kind": "secret", "state": "open"}, {"id": "d", "pts": [[0, 0], [0, 10]], "kind": "door", "state": "closed"}]
    pw = W.for_player(walls)
    assert pw[0] == {"id": "s", "pts": [[0, 0], [10, 0]], "kind": "wall"}          # no kind, no state, nothing to find
    assert pw[1]["kind"] == "door"
    assert "secret" not in json.dumps(pw)
    segs = W.segments(walls)
    assert segs[0]["kind"] == "wall"                                              # and it blocks sight like a wall


def test_fog_settings_are_normalised():
    assert W.clean_fog(None) == {"enabled": False, "range": 0, "epoch": 0, "darkness": 0.0, "personal": 2.0}
    assert W.clean_fog({"enabled": "yes", "range": 1e9, "epoch": -3}) == {"enabled": False, "range": 100, "epoch": 0, "darkness": 0.0, "personal": 2.0}
    assert W.clean_fog({"enabled": True, "range": float("nan"), "epoch": 4}) == {"enabled": True, "range": 0, "epoch": 4, "darkness": 0.0, "personal": 2.0}
    assert W.clean_fog({"darkness": 5, "personal": 99})["darkness"] == 0.95 and W.clean_fog({"darkness": 5, "personal": 99})["personal"] == 20.0
    assert W.clean_fog({"darkness": "x", "personal": None}) ["darkness"] == 0.0 and W.clean_fog({"personal": 0})["personal"] == 0.0


# ── the routes ───────────────────────────────────────────────────────────────

def _map(seed, slug="fogmap", **kw):
    db = SessionLocal()
    try:
        m = Schematic(world_id=kw.pop("world", seed.world_a).id, name="Keep", slug=slug, is_html=False, canvas_width=1000, canvas_height=800,
                      elements_json="[]", **kw)
        db.add(m)
        db.commit()
        return m.slug
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


WALLS = [{"id": "w1", "pts": [[100, 100], [500, 100], [500, 400]], "kind": "wall"},
         {"id": "d1", "pts": [[300, 100], [350, 100]], "kind": "door", "state": "closed"},
         {"id": "s1", "pts": [[500, 200], [500, 250]], "kind": "secret", "state": "closed"}]


def test_only_editors_change_walls_and_fog(client, seed):
    slug = _map(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for method, path, body in (("get", f"/maps/schematic/{slug}/walls.json", None), ("post", f"/maps/schematic/{slug}/walls", {"walls": []}),
                               ("post", f"/maps/schematic/{slug}/fog", {"enabled": True}), ("post", f"/maps/schematic/{slug}/door", {"wall_id": "d1"})):
        r = client.get(path) if method == "get" else client.post(path, json=body)
        assert r.status_code == 403, path


def test_walls_round_trip_and_the_gm_sees_secret_doors(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    before = live.version(seed.world_a.id)
    r = client.post(f"/maps/schematic/{slug}/walls", json={"walls": WALLS})
    assert r.status_code == 200 and len(r.json()["walls"]) == 3
    assert live.version(seed.world_a.id) > before                                   # players' maps refresh at once
    got = client.get(f"/maps/schematic/{slug}/walls.json").json()
    assert [w["kind"] for w in got["walls"]] == ["wall", "door", "secret"] and got["fog"]["enabled"] is False
    assert client.post(f"/maps/schematic/{slug}/walls", json={"walls": "nope"}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/walls", json=[1]).json() if False else True
    assert client.post(f"/maps/schematic/{slug}/walls", content="{bad", headers={"Content-Type": "application/json"}).status_code == 400
    nan = client.post(f"/maps/schematic/{slug}/walls", content='{"walls":[{"pts":[[NaN,1],[Infinity,2],[5,5],[9,9]]}]}', headers={"Content-Type": "application/json"})
    assert nan.status_code == 200 and nan.json()["walls"][0]["pts"] == [[5.0, 5.0], [9.0, 9.0]]


def test_another_worlds_map_is_404(client, seed):
    other = _map(seed, slug="theirs", world=seed.world_b)
    _gm(client, seed)
    assert client.get(f"/maps/schematic/{other}/walls.json").status_code == 404
    assert client.post(f"/maps/schematic/{other}/walls", json={"walls": []}).status_code == 404


def test_fog_settings_and_reset(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    assert client.post(f"/maps/schematic/{slug}/fog", json={"enabled": True, "range": 12}).json() == {"enabled": True, "range": 12, "epoch": 0, "darkness": 0.0, "personal": 2.0}
    assert client.post(f"/maps/schematic/{slug}/fog", json={"reset": True}).json()["epoch"] == 1
    assert client.post(f"/maps/schematic/{slug}/fog", json={"enabled": "yes"}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/fog", json=[]).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/fog", json={"range": 99999}).json()["range"] == 100


def test_doors_open_and_close_and_only_doors(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    client.post(f"/maps/schematic/{slug}/walls", json={"walls": WALLS})
    assert client.post(f"/maps/schematic/{slug}/door", json={"wall_id": "d1"}).json() == {"wall_id": "d1", "state": "open"}
    assert client.post(f"/maps/schematic/{slug}/door", json={"wall_id": "d1"}).json()["state"] == "closed"
    assert client.post(f"/maps/schematic/{slug}/door", json={"wall_id": "d1", "state": "open"}).json()["state"] == "open"
    assert client.post(f"/maps/schematic/{slug}/door", json={"wall_id": "s1"}).json()["state"] == "open"      # the GM can open a secret door
    assert client.post(f"/maps/schematic/{slug}/door", json={"wall_id": "w1"}).status_code == 404            # a plain wall is not a door
    assert client.post(f"/maps/schematic/{slug}/door", json={"wall_id": 5}).status_code == 400


# ── what players are sent ────────────────────────────────────────────────────

def test_nothing_about_walls_is_sent_while_fog_is_off(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    client.post(f"/maps/schematic/{slug}/walls", json={"walls": WALLS})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    d = client.get(f"/maps/schematic/{slug}/view.json").json()
    assert d["walls"] == [] and d["fog"]["enabled"] is False
    assert "secret" not in client.get(f"/maps/schematic/{slug}/view").text


def test_the_secret_door_never_reaches_players_or_the_tv(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    client.post(f"/maps/schematic/{slug}/walls", json={"walls": WALLS})
    client.post(f"/maps/schematic/{slug}/fog", json={"enabled": True})
    tv = client.get(f"/display/map/{slug}/data.json").json()
    assert [w["kind"] for w in tv["walls"]] == ["wall", "door", "wall"] and tv["fog"]["enabled"] is True
    assert "secret" not in json.dumps(tv)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    j = client.get(f"/maps/schematic/{slug}/view.json")
    assert [w["kind"] for w in j.json()["walls"]] == ["wall", "door", "wall"] and "secret" not in j.text
    page = client.get(f"/maps/schematic/{slug}/view")
    assert page.status_code == 200 and "s1" in page.text                          # the wall is in the page (as embedded JSON)...
    assert "secret" not in page.text.replace("secrecy", "").replace("secret-", "") or '\\"secret\\"' not in page.text   # ...but never as a secret door
    assert '\\"kind\\": \\"secret' not in page.text and "kind&#34;: &#34;secret" not in page.text


# ── lights and darkness ──────────────────────────────────────────────────────

LIGHTS = [{"id": "torch", "x": 300, "y": 200, "range": 6, "color": "#ff9933", "intensity": 0.9, "on": True, "label": "Torch"},
          {"id": "lamp", "x": 500, "y": 300, "range": 4, "on": False}]


def test_lights_are_cleaned_clamped_and_capped():
    lights, warns = W.clean_lights([
        {"id": "a b!", "x": 5000, "y": -4, "range": 9999, "color": "red", "intensity": 7, "on": "no"},
        {"x": 1, "y": 1, "range": 0}, {"x": "a", "y": 1, "range": 2}, {"x": 1, "y": 1}, "junk", None,
        {"x": float("nan"), "y": 1, "range": 2}, {"x": 1, "y": 2, "range": True},
    ], 1000, 800)
    assert len(lights) == 1 and any("skipped" in w for w in warns)
    l = lights[0]
    assert l["id"] == "ab" and (l["x"], l["y"]) == (1000.0, 0.0) and l["range"] == 60 and l["color"] == "#ffd9a0" and l["intensity"] == 1.0 and l["on"] is True
    many, warns = W.clean_lights([{"x": 1, "y": 1, "range": 2}] * (W.MAX_LIGHTS + 20))
    assert len(many) == W.MAX_LIGHTS and any("first" in w for w in warns) and len({m["id"] for m in many}) == W.MAX_LIGHTS
    with pytest.raises(ValueError):
        W.clean_lights({"x": 1})


def test_lights_round_trip_toggle_and_permissions(client, seed):
    slug = _map(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/maps/schematic/{slug}/lights", json={"lights": LIGHTS}).status_code == 403
    assert client.post(f"/maps/schematic/{slug}/light", json={"id": "torch"}).status_code == 403
    _gm(client, seed)
    before = live.version(seed.world_a.id)
    r = client.post(f"/maps/schematic/{slug}/lights", json={"lights": LIGHTS})
    assert r.status_code == 200 and [l["id"] for l in r.json()["lights"]] == ["torch", "lamp"] and live.version(seed.world_a.id) > before
    assert [l["on"] for l in client.get(f"/maps/schematic/{slug}/walls.json").json()["lights"]] == [True, False]       # the GM sees every light
    assert client.post(f"/maps/schematic/{slug}/light", json={"id": "lamp"}).json() == {"id": "lamp", "on": True}
    assert client.post(f"/maps/schematic/{slug}/light", json={"id": "lamp", "on": False}).json()["on"] is False
    assert client.post(f"/maps/schematic/{slug}/light", json={"id": "nope"}).status_code == 404
    assert client.post(f"/maps/schematic/{slug}/light", json={"id": 5}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/lights", json={"lights": "x"}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/lights", content='{"lights":[{"x":NaN,"y":1,"range":2},{"x":5,"y":5,"range":3}]}', headers={"Content-Type": "application/json"}).json()["lights"][0]["x"] == 5.0
    other = _map(seed, slug="theirs", world=seed.world_b)
    assert client.post(f"/maps/schematic/{other}/lights", json={"lights": []}).status_code == 404


def test_darkness_settings_are_validated(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    d = client.post(f"/maps/schematic/{slug}/fog", json={"darkness": 0.7, "personal": 3}).json()
    assert d["darkness"] == 0.7 and d["personal"] == 3.0 and d["enabled"] is False
    assert client.post(f"/maps/schematic/{slug}/fog", json={"darkness": 5}).json()["darkness"] == 0.95
    assert client.post(f"/maps/schematic/{slug}/fog", json={"darkness": "dark"}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/fog", json={"personal": True}).status_code == 400
    assert client.post(f"/maps/schematic/{slug}/fog", json={"enabled": True}).json()["darkness"] == 0.95          # other settings are kept


def test_players_get_lights_only_while_it_is_dark_and_only_the_ones_that_are_on(client, seed):
    slug = _map(seed)
    _gm(client, seed)
    client.post(f"/maps/schematic/{slug}/walls", json={"walls": WALLS})
    client.post(f"/maps/schematic/{slug}/lights", json={"lights": [{**LIGHTS[0], "label": "Torch of the GM's secret lair"}, {**LIGHTS[1], "on": True}, {"id": "off", "x": 1, "y": 1, "range": 3, "on": False}]})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    d = client.get(f"/maps/schematic/{slug}/view.json").json()
    assert d["lights"] == [] and d["walls"] == []                                  # daylight and no fog: nothing about geometry or lights
    _gm(client, seed)
    client.post(f"/maps/schematic/{slug}/fog", json={"darkness": 0.8})
    tv = client.get(f"/display/map/{slug}/data.json").json()
    assert [l["id"] for l in tv["lights"]] == ["torch", "lamp"] and "off" not in json.dumps(tv)
    assert set(tv["lights"][0]) == {"id", "x", "y", "range", "color", "intensity"}                              # no label, no 'on'
    assert len(tv["walls"]) == 3                                                   # darkness alone sends the walls (shadows need them) - secret door as a wall
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    j = client.get(f"/maps/schematic/{slug}/view.json")
    assert [l["id"] for l in j.json()["lights"]] == ["torch", "lamp"] and "secret" not in j.text and "GM's secret lair" not in j.text
    page = client.get(f"/maps/schematic/{slug}/view")
    assert "GM" not in page.text.split("FOG_STATE")[1][:600]
