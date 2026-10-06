"""The dice behind the 3D tray (static/js/dice-geometry.js): shapes, which number is up, and what a notation throws.

Pure functions, so they run under Node here; the meshes, the physics and the mouse throw (static/js/dice-tray.js) were
also driven in a real browser. What matters for a dice roller is fairness and honesty: every face is reachable and equally
likely to be read, opposite faces add up like real dice, and the number shown is the number that was read."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
GEOMETRY = str(ROOT / "static/js/dice-geometry.js")


def _node(body: str):
    script = f"global.window = global; const G = require({json.dumps(GEOMETRY)});\n{body}"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _js(expr: str):
    return _node(f"console.log(JSON.stringify({expr}))")


SIDES = [4, 6, 8, 10, 12, 20]
FACES = {4: 4, 6: 6, 8: 8, 10: 10, 12: 12, 20: 20}
VERTS = {4: 4, 6: 8, 8: 6, 10: 12, 12: 20, 20: 12}
FACE_SIZE = {4: 3, 6: 4, 8: 3, 10: 4, 12: 5, 20: 3}


# ── the solids ───────────────────────────────────────────────────────────────────────────────────────

@needs_node
@pytest.mark.parametrize("sides", SIDES)
def test_each_die_has_the_right_number_of_faces_and_corners(sides):
    sh = _js(f"G.shape({sides})")
    assert len(sh["faces"]) == FACES[sides] and len(sh["vertices"]) == VERTS[sides]
    assert all(len(f["idx"]) == FACE_SIZE[sides] for f in sh["faces"])
    # Euler's formula for a convex solid: V - E + F = 2 (every edge is shared by two faces)
    edges = sum(len(f["idx"]) for f in sh["faces"]) // 2
    assert VERTS[sides] - edges + FACES[sides] == 2


@needs_node
@pytest.mark.parametrize("sides", SIDES)
def test_each_die_is_convex_with_outward_flat_counter_clockwise_faces(sides):
    result = _node(f"""
      const sh = G.shape({sides});
      const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
      const sub = (a, b) => [a[0]-b[0], a[1]-b[1], a[2]-b[2]];
      const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
      let worstFlat = 0, worstConvex = -1, outward = true, ccw = true, maxR = 0, unit = true;
      sh.vertices.forEach(p => {{ const r = Math.sqrt(dot(p, p)); maxR = Math.max(maxR, r); }});
      sh.faces.forEach(f => {{
        const n = f.normal, d0 = dot(n, sh.vertices[f.idx[0]]);
        f.idx.forEach(i => {{ worstFlat = Math.max(worstFlat, Math.abs(dot(n, sh.vertices[i]) - d0)); }});
        sh.vertices.forEach(p => {{ worstConvex = Math.max(worstConvex, dot(n, p) - d0); }});   // no vertex in front of any face
        if (dot(n, f.center) <= 0) outward = false;
        for (let k = 0; k < f.idx.length; k++) {{                                                  // winding agrees with the normal
          const a = sh.vertices[f.idx[k]], b = sh.vertices[f.idx[(k+1)%f.idx.length]], c = sh.vertices[f.idx[(k+2)%f.idx.length]];
          if (dot(cross(sub(b, a), sub(c, b)), n) <= 0) ccw = false;
        }}
      }});
      console.log(JSON.stringify({{worstFlat, worstConvex, outward, ccw, maxR}}));
    """)
    assert result["worstFlat"] < 1e-9 and result["worstConvex"] < 1e-9
    assert result["outward"] and result["ccw"]
    assert result["maxR"] == pytest.approx(1.0, abs=1e-9)


@needs_node
@pytest.mark.parametrize("sides", SIDES)
def test_every_number_appears_once_and_opposite_faces_add_up_like_real_dice(sides):
    sh = _js(f"G.shape({sides})")
    labels = sorted(f["label"] for f in sh["faces"])
    assert labels == (list(range(0, 10)) if sides == 10 else list(range(1, sides + 1)))
    want = 9 if sides == 10 else sides + 1
    for f in (sh["faces"] if sides != 4 else []):      # a tetrahedron has no opposite faces (each face is opposite a corner)
        opposite = min(sh["faces"], key=lambda g: sum(a * b for a, b in zip(f["normal"], g["normal"])))
        assert f["label"] + opposite["label"] == want
    if sides == 10:   # the "0" face is worth ten
        assert {f["label"]: f["value"] for f in sh["faces"]}[0] == 10


@needs_node
def test_unknown_dice_have_no_shape():
    assert _js("G.shape(7)") is None and _js("G.shape(100)") is None and _js("G.shape('x')") is None


# ── reading the number that is up ────────────────────────────────────────────────────────────────────

ROLL_ONTO = """
  // the quaternion that turns direction `from` onto direction `to`
  function between(from, to) {
    const d = from[0]*to[0] + from[1]*to[1] + from[2]*to[2];
    if (d < -0.999999) { const ax = Math.abs(from[0]) < 0.9 ? [1,0,0] : [0,1,0]; const c = [from[1]*ax[2]-from[2]*ax[1], from[2]*ax[0]-from[0]*ax[2], from[0]*ax[1]-from[1]*ax[0]]; const l = Math.hypot(...c); return [c[0]/l, c[1]/l, c[2]/l, 0]; }
    const c = [from[1]*to[2]-from[2]*to[1], from[2]*to[0]-from[0]*to[2], from[0]*to[1]-from[1]*to[0]];
    const q = [c[0], c[1], c[2], 1 + d], l = Math.hypot(...q);
    return q.map(x => x / l);
  }
"""


@needs_node
@pytest.mark.parametrize("sides", [6, 8, 10, 12, 20])
def test_a_die_lying_on_a_face_reads_the_face_that_points_up(sides):
    result = _node(ROLL_ONTO + f"""
      const sh = G.shape({sides}); const out = [];
      sh.faces.forEach(f => {{ const r = G.readValue(sh, between(f.normal, [0, 1, 0])); out.push([f.value, r.value, r.flat]); }});
      console.log(JSON.stringify(out));
    """)
    for shown, read, flat in result:
        assert read == shown and flat == pytest.approx(1.0, abs=1e-9)


@needs_node
def test_a_d4_reads_the_corner_that_points_up():
    """A tetrahedron lies on a face; the number is the corner pointing at the ceiling (the vertex opposite the floor face)."""
    result = _node(ROLL_ONTO + """
      const sh = G.shape(4); const out = [];
      sh.vertices.forEach((v, i) => { const r = G.readValue(sh, between(v, [0, 1, 0])); out.push([sh.vertexLabels[i], r.value, r.flat]); });
      console.log(JSON.stringify(out));
    """)
    assert sorted(shown for shown, _, _ in result) == [1, 2, 3, 4]
    for shown, read, flat in result:
        assert read == shown and flat == pytest.approx(1.0, abs=1e-9)


@needs_node
@pytest.mark.parametrize("sides", SIDES)
def test_a_die_standing_on_an_edge_does_not_read_as_flat(sides):
    flat = _node(ROLL_ONTO + f"""
      const sh = G.shape({sides}); const a = sh.faces[0], b = sh.faces.find((g, i) => i && g.idx.filter(x => a.idx.includes(x)).length >= 2);
      const mid = [a.normal[0]+b.normal[0], a.normal[1]+b.normal[1], a.normal[2]+b.normal[2]], l = Math.hypot(...mid);
      console.log(JSON.stringify(G.readValue(sh, between(sh.kind === 'vertex' ? [-mid[0]/l, -mid[1]/l, -mid[2]/l] : [mid[0]/l, mid[1]/l, mid[2]/l], [0, 1, 0])).flat));
    """)
    assert flat < 0.97


@needs_node
@pytest.mark.parametrize("sides", SIDES)
def test_every_face_is_equally_likely_to_be_read_under_random_rotations(sides):
    counts = _node(f"""
      let seed = 12345; const rnd = () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296;
      const gauss = () => Math.sqrt(-2 * Math.log(rnd() + 1e-12)) * Math.cos(2 * Math.PI * rnd());
      const sh = G.shape({sides}), counts = {{}};
      const N = 24000;
      for (let i = 0; i < N; i++) {{
        const q = [gauss(), gauss(), gauss(), gauss()], l = Math.hypot(...q);          // a uniformly random rotation
        const r = G.readValue(sh, q.map(x => x / l));
        counts[r.label] = (counts[r.label] || 0) + 1;
      }}
      console.log(JSON.stringify(counts));
    """)
    assert len(counts) == (10 if sides == 10 else sides)
    expected = 24000 / len(counts)
    assert all(abs(c - expected) < 0.15 * expected for c in counts.values()), counts


@needs_node
@pytest.mark.parametrize("sides", SIDES)
def test_every_face_has_a_right_handed_frame_to_print_its_number_in(sides):
    """u (right) x v (up) = the outward normal, both unit and flat in the face - so a numeral drawn upright on the face's
    texture reads upright and not mirrored when the die is looked at from outside."""
    r = _node(f"""
      const sh = G.shape({sides});
      const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
      const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
      let worst = 0;
      sh.faces.forEach(f => {{
        const c = cross(f.u, f.v);
        worst = Math.max(worst, Math.abs(dot(f.u, f.v)), Math.abs(dot(f.u, f.u) - 1), Math.abs(dot(f.v, f.v) - 1),
                         Math.abs(dot(f.u, f.normal)), Math.abs(dot(f.v, f.normal)),
                         Math.abs(c[0]-f.normal[0]) + Math.abs(c[1]-f.normal[1]) + Math.abs(c[2]-f.normal[2]));
      }});
      console.log(JSON.stringify(worst));
    """)
    assert r < 1e-9


@needs_node
def test_the_outline_of_a_face_is_a_flat_polygon_around_its_centre():
    out = _js("(() => { const sh = G.shape(20); return G.faceOutline(sh, sh.faces[0]); })()")
    assert len(out) == 3
    # an equilateral triangle: equal distances from the centre, and the number's top (+y) points at a corner
    radii = [(x * x + y * y) ** 0.5 for x, y in out]
    assert max(radii) - min(radii) < 1e-9
    assert any(abs(x) < 1e-9 and y > 0 for x, y in out)
    # a square face's number points at the middle of an edge, so the outline's edges line up with the numeral
    sq = _js("(() => { const sh = G.shape(6); return G.faceOutline(sh, sh.faces[0]); })()")
    assert not any(abs(x) < 1e-9 and y > 0 for x, y in sq) and sorted(round(y, 6) for _, y in sq)[0] == sorted(round(y, 6) for _, y in sq)[1]


# ── what a notation throws ───────────────────────────────────────────────────────────────────────────

@needs_node
def test_a_plain_notation_becomes_one_die_per_roll():
    p = _js("G.plan('2d6+3')")
    assert [d["sides"] for d in p["dice"]] == [6, 6] and all(d["role"] == "normal" for d in p["dice"])
    assert p["terms"][1] == {"sign": 1, "flat": 3}


@needs_node
def test_a_percentile_die_is_a_tens_die_and_a_units_die():
    p = _js("G.plan('d100')")
    assert [(d["sides"], d["role"]) for d in p["dice"]] == [(10, "tens"), (10, "units")]


@needs_node
@pytest.mark.parametrize("text,fragment", [
    ("", "Enter a dice notation"), ("hello", "Use dice notation"), ("1d7", "no physical d7"), ("3", "at least one die"),
    ("21d6", "At most 20 dice"), ("11d100", "At most 20 dice"),
])
def test_what_the_tray_cannot_throw_is_explained(text, fragment):
    assert fragment in _js(f"G.plan({json.dumps(text)})")["error"]


@needs_node
def test_combine_adds_the_modifier_and_keeps_the_rolls_in_the_logs_shape():
    r = _node("""
      const pl = G.plan('2d6+3');
      console.log(JSON.stringify(G.combine(pl, [{value: 4, label: 4}, {value: 5, label: 5}])));
    """)
    assert r["total"] == 12
    assert r["breakdown"] == [{"term": "+2d6", "rolls": [4, 5], "sum": 9}, {"term": "+3", "sum": 3}]
    assert r["values"] == [4, 5]


@needs_node
def test_combine_handles_subtracted_dice_and_several_terms():
    r = _node("""
      const pl = G.plan('d20-1d4+2d8');
      console.log(JSON.stringify(G.combine(pl, [{value: 15}, {value: 3}, {value: 6}, {value: 7}])));
    """)
    assert r["total"] == 15 - 3 + 13
    assert [b["term"] for b in r["breakdown"]] == ["+1d20", "-1d4", "+2d8"]


@needs_node
@pytest.mark.parametrize("tens,units,expected", [(0, 0, 100), (0, 7, 7), (3, 7, 37), (9, 9, 99), (9, 0, 90)])
def test_a_percentile_roll_combines_both_dice(tens, units, expected):
    r = _node(f"""
      const pl = G.plan('d100+5');
      console.log(JSON.stringify(G.combine(pl, [{{label: {tens}}}, {{label: {units}}}])));
    """)
    assert r["breakdown"][0]["rolls"] == [expected] and r["total"] == expected + 5


@needs_node
@pytest.mark.parametrize("text", ["d20", "2d6+3", "d20-1", "4d8+2d6+1", "d100", "3d4-2", "1d10+1d100-3"])
def test_the_tray_describes_a_roll_the_same_way_the_server_does(text):
    """The roll is recorded through the server's own grammar: the terms must come out identical."""
    from app.routers.dice import parse_and_roll
    server, _ = parse_and_roll(text)
    pl = _js(f"G.plan({json.dumps(text)})")
    dice = pl["dice"]
    readings, i = [], 0
    while i < len(dice):
        if dice[i]["role"] == "tens":
            readings += [{"label": 2, "value": 2}, {"label": 5, "value": 5}]; i += 2
        else:
            readings.append({"label": 1, "value": 1}); i += 1
    combined = _node(f"console.log(JSON.stringify(G.combine({json.dumps(pl)}, {json.dumps(readings)})))")
    assert [b["term"] for b in combined["breakdown"]] == [b["term"] for b in server]
