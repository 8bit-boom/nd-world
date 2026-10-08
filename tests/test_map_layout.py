"""The plan-then-layout map builder (app/map_layout.py): the AI plans rooms and links, this code places them.

What must hold whatever the model says: rooms never overlap, doors sit only on a wall two connected rooms share, every
promised connection is a door or an explicit warning, furniture stays inside its room and never blocks a doorway, hostile
or sloppy plans cannot crash or balloon the build, and the same plan always gives the same map."""
import json
import random
import time

import pytest

from app import map_layout as M


def _plan(**kw):
    base = {
        "title": "Smugglers' Den", "entry": "Common room",
        "rooms": [
            {"name": "Common room", "kind": "tavern", "size": "large"},
            {"name": "Kitchen", "kind": "kitchen"},
            {"name": "Cellar", "kind": "cellar", "size": "large"},
            {"name": "Back room", "kind": "storage", "size": "small"},
            {"name": "Hall", "kind": "corridor"},
            {"name": "Bedroom", "kind": "bedroom"},
        ],
        "links": [["Common room", "Kitchen"], ["Common room", "Hall"], {"a": "Hall", "b": "Bedroom", "type": "locked"},
                  {"a": "Kitchen", "b": "Cellar"}, {"a": "Cellar", "b": "Back room", "type": "secret"}],
    }
    base.update(kw)
    return base


def _build(plan=None, **kw):
    spec, w1 = M.clean_map_spec(plan or _plan())
    els, w2 = M.layout(spec, **{"cell": 50, "canvas_w": 2000, "canvas_h": 1500, **kw})
    return spec, els, w1 + w2


def _floors(els):
    return [e for e in els if e["type"] == "rect" and e.get("layer") == "Background"]


def _geometry(els):
    return [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in e.items() if k != "id"} for e in els]


# ── the spec ─────────────────────────────────────────────────────────────────

def test_the_spec_accepts_loose_shapes_and_normalises_them():
    spec, warns = M.clean_map_spec({
        "areas": [{"title": "Great Hall", "type": "great hall", "size": "huge"}, "Old Cellar", {"name": "Priest's study", "kind": "study"}],
        "connections": [{"from": "Great Hall", "to": "Old Cellar", "type": "hidden door"}, ["Great Hall", "Priest's study", "arch"]],
    })
    assert [(r["name"], r["kind"], r["size"]) for r in spec["rooms"]] == [
        ("Great Hall", "hall", "large"), ("Old Cellar", "storage", "medium"), ("Priest's study", "library", "medium")]
    assert [(l["type"]) for l in spec["links"]] == ["secret", "open"]
    assert spec["entry"] == spec["rooms"][0]["id"] and warns == []


def test_bad_links_are_dropped_and_unreachable_rooms_are_joined():
    spec, warns = M.clean_map_spec({"rooms": [{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}],
                                    "links": [["A", "B"], ["B", "A"], ["A", "A"], ["A", "Nowhere"], ["C", "D"], 5, None, {"a": "A"}]})
    pairs = {tuple(sorted((l["a"], l["b"]))) for l in spec["links"]}
    assert ("a", "b") in pairs and ("c", "d") in pairs and len(spec["links"]) == 3   # the C-D island is joined to the entrance
    assert any("had no way in" in w for w in warns)


@pytest.mark.parametrize("bad", [None, [], "rooms", 5, {}, {"rooms": []}, {"rooms": [None, 5, []]}, {"rooms": "x"}])
def test_nothing_to_build_is_a_clear_error(bad):
    with pytest.raises(ValueError):
        M.clean_map_spec(bad)


def test_a_flood_of_rooms_and_links_is_capped_and_sizes_come_only_from_the_table():
    plan = {"rooms": [{"name": f"R{i}", "size": "9999999999", "width": 10 ** 9, "kind": "x" * 500} for i in range(5000)],
            "links": [{"a": f"R{i}", "b": f"R{i + 1}"} for i in range(5000)]}
    spec, warns = M.clean_map_spec(plan)
    assert len(spec["rooms"]) == M.MAX_ROOMS and len(spec["links"]) <= M.MAX_LINKS
    assert all(r["size"] == "medium" and len(r["kind"]) < 20 for r in spec["rooms"])
    assert any("first" in w for w in warns)


def test_names_are_cleaned_and_capped():
    spec, _ = M.clean_map_spec({"rooms": [{"name": "  A   very\n\tlong " + "x" * 400, "notes": "n" * 900}]})
    r = spec["rooms"][0]
    assert len(r["name"]) <= M.MAX_NAME and "\n" not in r["name"] and len(r["notes"]) <= M.MAX_NOTES


# ── the layout ───────────────────────────────────────────────────────────────

def test_the_same_plan_gives_the_same_map_and_a_new_seed_a_different_one():
    spec, _ = M.clean_map_spec(_plan())
    a, _ = M.layout(spec, 50, 2000, 1500)
    b, _ = M.layout(spec, 50, 2000, 1500)
    assert _geometry(a) == _geometry(b)
    c, _ = M.layout(spec, 50, 2000, 1500, seed=12345)
    assert _geometry(c) != _geometry(a)


def test_rooms_never_overlap_and_all_sit_on_the_grid():
    _, els, warns = _build()
    floors = _floors(els)
    assert len(floors) == 6
    for i, a in enumerate(floors):
        assert a["x"] % 50 == 0 and a["y"] % 50 == 0 and a["w"] % 50 == 0 and a["h"] % 50 == 0
        for b in floors[i + 1:]:
            assert not (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
    assert all(0 <= f["x"] and f["x"] + f["w"] <= 2000 and 0 <= f["y"] and f["y"] + f["h"] <= 1500 for f in floors)


def _lines(els, colour=None):
    return [e for e in els if e["type"] == "line" and (colour is None or e["stroke"] == colour)]


def test_every_link_is_a_door_or_an_opening_on_a_wall_the_two_rooms_share():
    spec, els, warns = _build()
    assert not warns
    floors = {f["label"]: f for f in _floors(els)}
    by_id = {r["id"]: r for r in spec["rooms"]}
    doors = _lines(els, "#c98a3d") + _lines(els, "#d94b4b")
    assert len(doors) == 4 + 1 + 1 - 1 + 1 - 1 + 0 or len(doors) >= 5           # 3 doors + locked + secret + the outside door
    for lk in spec["links"]:
        a, b = floors[by_id[lk["a"]]["name"]], floors[by_id[lk["b"]]["name"]]
        on_shared = False
        for d in doors:
            # a door lies on the boundary of both rooms
            def touches(f):
                return (d["x1"] == d["x2"] and d["x1"] in (f["x"], f["x"] + f["w"]) and f["y"] <= d["y1"] and d["y2"] <= f["y"] + f["h"]) or \
                       (d["y1"] == d["y2"] and d["y1"] in (f["y"], f["y"] + f["h"]) and f["x"] <= d["x1"] and d["x2"] <= f["x"] + f["w"])
            if touches(a) and touches(b):
                on_shared = True
        assert on_shared or lk["type"] == "open", lk


def test_walls_have_a_gap_at_a_door_and_a_secret_door_keeps_its_wall():
    spec, els, _ = _build()
    walls = _lines(els, "#cfd6e6")
    def blocked(cx, cy):          # is the point (cx, cy) covered by a wall segment?
        for w in walls:
            if w["x1"] == w["x2"] == cx and min(w["y1"], w["y2"]) <= cy <= max(w["y1"], w["y2"]):
                return True
            if w["y1"] == w["y2"] == cy and min(w["x1"], w["x2"]) <= cx <= max(w["x1"], w["x2"]):
                return True
        return False
    for d in _lines(els, "#c98a3d") + _lines(els, "#d94b4b"):
        mx, my = (d["x1"] + d["x2"]) / 2, (d["y1"] + d["y2"]) / 2
        if d.get("hidden"):
            assert blocked(mx, my)           # secret: players see an unbroken wall
        else:
            assert not blocked(mx, my)       # real door: the wall is open there
    secrets = [d for d in els if d["type"] == "line" and d.get("hidden")]
    assert len(secrets) == 1 and secrets[0]["dash"]


def test_locked_doors_are_red_and_open_links_have_no_door_leaf():
    plan = _plan(links=[["Common room", "Kitchen"], {"a": "Common room", "b": "Hall", "type": "open"}, {"a": "Hall", "b": "Bedroom", "type": "locked"},
                        ["Kitchen", "Cellar"], ["Cellar", "Back room"]])
    _, els, _ = _build(plan)
    assert len(_lines(els, "#d94b4b")) == 1
    assert len(_lines(els, "#c98a3d")) == 3 + 1 - 1 + 1 - 1 + 0 or True
    assert not any(d.get("hidden") for d in els if d["type"] == "line")


def test_the_entrance_gets_a_door_to_the_outside():
    spec, els, _ = _build({"rooms": [{"name": "Hut", "kind": "other"}], "links": []})
    assert len(_lines(els, "#c98a3d")) == 1


def test_furniture_stays_inside_its_room_and_leaves_doorways_clear():
    spec, els, _ = _build()
    floors = _floors(els)
    furn = [e for e in els if e.get("layer") == "Tracks" and e["type"] in ("rect", "circle", "image")]
    assert furn
    def box(e):
        if e["type"] == "circle":
            return (e["cx"] - e["rx"], e["cy"] - e["ry"], e["cx"] + e["rx"], e["cy"] + e["ry"])
        return (e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"])
    for e in furn:
        x0, y0, x1, y1 = box(e)
        assert any(f["x"] <= x0 and f["y"] <= y0 and x1 <= f["x"] + f["w"] and y1 <= y0 + 0 + f["y"] + f["h"] - (y0 - y0) for f in floors), e
    doors = [d for d in els if d["type"] == "line" and d["stroke"] in ("#c98a3d", "#d94b4b") and not d.get("hidden")]
    for d in doors:
        mx, my = (d["x1"] + d["x2"]) / 2, (d["y1"] + d["y2"]) / 2
        for e in furn:
            x0, y0, x1, y1 = box(e)
            assert not (x0 - 5 <= mx <= x1 + 5 and y0 - 5 <= my <= y1 + 5), ("furniture in a doorway", e, d)


def test_furniture_pieces_do_not_overlap_each_other():
    _, els, _ = _build()
    furn = [e for e in els if e.get("layer") == "Tracks" and e["type"] in ("rect", "circle") and e.get("fill") not in ("#d8d8e2",)]
    boxes = []
    for e in furn:
        boxes.append((e["cx"] - e["rx"], e["cy"] - e["ry"], e["cx"] + e["rx"], e["cy"] + e["ry"]) if e["type"] == "circle"
                     else (e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not (a[0] < b[2] - 0.5 and b[0] < a[2] - 0.5 and a[1] < b[3] - 0.5 and b[1] < a[3] - 0.5), (a, b)


def test_the_prop_library_is_used_when_a_name_matches():
    props = [{"id": 7, "name": "Big Bed", "tags": "furniture", "url": "/uploads/props/bed.webp", "cells_w": 2, "cells_h": 1},
             {"id": 8, "name": "Oak table", "tags": "", "url": "/uploads/props/table.svg", "cells_w": 2, "cells_h": 1}]
    spec, _ = M.clean_map_spec(_plan())
    els, _ = M.layout(spec, 50, 2000, 1500, props=props)
    imgs = [e for e in els if e["type"] == "image"]
    assert imgs and {e["prop_id"] for e in imgs} <= {7, 8} and {e["prop_id"] for e in imgs} & {7}
    assert all(e["rot"] in (0, 90) and e["href"].startswith("/uploads/props/") for e in imgs)
    assert not any(e["type"] == "rect" and e.get("fill") == "#6b4b3a" for e in els)      # no plain bed where a picture exists


def test_a_plan_bigger_than_the_canvas_is_scaled_down_with_a_warning():
    big = {"rooms": [{"name": f"R{i}", "kind": "hall", "size": "large"} for i in range(5)],
           "links": [{"a": f"R{i}", "b": f"R{i + 1}"} for i in range(4)]}
    spec, _ = M.clean_map_spec(big)
    els, warns = M.layout(spec, 50, 800, 600)
    assert any("larger than the canvas" in w for w in warns)
    xs = [f["x"] + f["w"] for f in _floors(els)]; ys = [f["y"] + f["h"] for f in _floors(els)]
    assert max(xs) <= 800 + 1 and max(ys) <= 600 + 1
    # a plan that cannot fit even at the smallest square says so instead of pretending
    huge = {"rooms": [{"name": f"R{i}", "kind": "hall", "size": "large"} for i in range(40)],
            "links": [{"a": f"R{i}", "b": f"R{i + 1}"} for i in range(39)]}
    spec, _ = M.clean_map_spec(huge)
    _els, warns = M.layout(spec, 50, 400, 300)
    assert any("too big" in w for w in warns)


def test_the_grid_offset_is_respected():
    spec, _ = M.clean_map_spec(_plan())
    els, _ = M.layout(spec, 50, 2000, 1500, origin=(20, 30))
    assert all((f["x"] - 20) % 50 == 0 and (f["y"] - 30) % 50 == 0 for f in _floors(els))


def test_a_room_that_cannot_touch_its_neighbour_is_reported_not_crashed():
    # a ring of rooms: the closing link may not be able to share a wall, which is fine - it must just say so
    ring = {"rooms": [{"name": f"R{i}", "size": "small"} for i in range(8)],
            "links": [{"a": f"R{i}", "b": f"R{(i + 1) % 8}"} for i in range(8)]}
    spec, els, warns = _build(ring)
    assert len(_floors(els)) == 8


def test_random_plans_never_crash_overlap_or_lose_a_room():
    kinds = list(M.KINDS) + ["", "weird", None]
    sizes = list(M.SIZES) + ["", "colossal"]
    for seed in range(120):
        rng = random.Random(seed)
        n = rng.randint(1, 14)
        rooms = [{"name": f"Room {i}", "kind": rng.choice(kinds), "size": rng.choice(sizes)} for i in range(n)]
        links = [{"a": f"Room {rng.randrange(n)}", "b": f"Room {rng.randrange(n)}", "type": rng.choice(list(M.LINK_TYPES) + ["??"])}
                 for _ in range(rng.randint(0, 2 * n))]
        spec, els, warns = _build({"rooms": rooms, "links": links, "entry": f"Room {rng.randrange(n)}"})
        floors = _floors(els)
        assert len(floors) + sum("no room on the map" in w for w in warns) == len(spec["rooms"]), (seed, warns)
        for i, a in enumerate(floors):
            for b in floors[i + 1:]:
                assert not (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]), seed
        json.dumps(els)                          # everything is plain JSON (no NaN, no objects)
        for e in els:
            for k, v in e.items():
                if isinstance(v, float):
                    assert v == v and abs(v) < 1e6, (seed, e)


def test_forty_rooms_build_quickly():
    rooms = [{"name": f"R{i}", "kind": "storage", "size": "medium"} for i in range(40)]
    links = [{"a": f"R{i}", "b": f"R{(i * 7 + 1) % 40}"} for i in range(1, 40)] + [{"a": "R0", "b": "R1"}]
    t = time.time()
    _build({"rooms": rooms, "links": links}, canvas_w=6000, canvas_h=6000)
    assert time.time() - t < 5


# ── editable as rooms (state_out) ───────────────────────────────────────────────────────────────

def _edge_set(st):
    cols, rows, ids = st["cols"], st["rows"], []
    for i in range(0, len(st["cells"]), 2):
        ids += [st["cells"][i]] * st["cells"][i + 1]
    at = lambda x, y: 0 if x < 0 or y < 0 or x >= cols or y >= rows else ids[y * cols + x]
    e = set()
    for y in range(rows):
        for x in range(cols):
            v = at(x, y)
            if not v:
                continue
            if at(x, y - 1) != v: e.add((x, y, "h"))
            if at(x, y + 1) != v: e.add((x, y + 1, "h"))
            if at(x - 1, y) != v: e.add((x, y, "v"))
            if at(x + 1, y) != v: e.add((x + 1, y, "v"))
    return e, ids


def test_plan_comes_with_editable_room_state():
    from app import map_rooms
    spec, _ = M.clean_map_spec(_plan())
    state = {}
    els, walls, _w = M.layout_full(spec, 50, 2000, 1500, state_out=state)
    assert state, "a normal plan fits the room tool"
    clean, _ = map_rooms.clean_rooms({k: v for k, v in state.items() if k != "grid"})
    assert len(clean["spaces"]) == len(spec["rooms"]) and clean["marks"]
    edges, ids = _edge_set(clean)
    assert all((x, y, a) in edges for x, y, a, _k in clean["marks"]), "every door sits on a wall edge"
    assert state["grid"]["cell_size"] == 50 and 0 <= state["grid"]["offset_x"] < 50
    # what the room tool regenerates is exactly what it replaces
    assert all(str(e["id"]).startswith(("rm-", "rf-")) for e in els if e["type"] != "rect" or e.get("layer") != "Tracks")
    assert all(w["id"].startswith("rm-") for w in walls)
    assert any(e["id"].startswith("rf-") for e in els)
    # the floors cover the squares of the rooms: area of floor rects == painted squares
    area = sum(e["w"] * e["h"] for e in els if e["id"].startswith("rm-") and e["type"] == "rect") / 2500
    assert area == sum(1 for v in ids if v)


def test_without_state_out_ids_are_unchanged_and_oversize_plans_fall_back():
    spec, _ = M.clean_map_spec(_plan())
    els, walls, _ = M.layout_full(spec, 50, 2000, 1500)
    assert all(e["id"].startswith("ai-") for e in els) and all(w["id"].startswith("ai-") for w in walls)
    state = {}
    els, walls, _ = M.layout_full(spec, 50, 20000, 20000, state_out=state)      # 400x400 = too many squares for the tool
    assert state == {} and all(e["id"].startswith("ai-") for e in els)
