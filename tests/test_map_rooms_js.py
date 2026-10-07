"""The room generator (static/js/map-rooms.js), run under Node: "an edge between two different spaces is a wall".

The property that matters is closure: after any sequence of paints, every side of every floor square is either shared with
the same room or covered by a wall / door / window - so a drawn room can never leak, and fog of war and exports agree."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import map_rooms as R

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
MOD = str(ROOT / "static/js/map-rooms.js")


def _js(body: str):
    script = f"global.window = global; const M = require({json.dumps(MOD)});\n{body}"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


SETUP = """
const st = M.create(50, 0, 0, 500, 400);                        // 10 x 8 squares
const a = M.addSpace(st, 'Hall', 'hall'), b = M.addSpace(st, 'Cell', 'cell');
const segLen = w => Math.hypot(w.pts[1][0]-w.pts[0][0], w.pts[1][1]-w.pts[0][1]);
"""


@needs_node
def test_one_room_gets_four_walls_around_it():
    r = _js(SETUP + "M.paintRect(st,1,1,3,2,a); const g = M.generate(st); console.log(JSON.stringify({walls: g.walls.map(w=>[w.kind, w.pts]), floors: g.floors.length}))")
    assert r["floors"] == 1 and len(r["walls"]) == 4 and all(k == "wall" for k, _ in r["walls"])
    lens = sorted(abs(p[1][0] - p[0][0]) + abs(p[1][1] - p[0][1]) for _, p in r["walls"])
    assert lens == [100, 100, 150, 150]                                                    # 3x2 squares: merged into four straight walls


@needs_node
def test_two_rooms_share_one_wall_and_a_room_split_in_two_still_has_no_inner_wall():
    r = _js(SETUP + "M.paintRect(st,0,0,2,1,a); M.paintRect(st,3,0,5,1,b); const g = M.generate(st); console.log(JSON.stringify(g.walls.map(w=>w.pts)))")
    shared = [p for p in r if p[0][0] == p[1][0] == 150]
    assert len(shared) == 1                                                                # one wall between the two rooms, not two
    r2 = _js(SETUP + "M.paintRect(st,0,0,2,1,a); M.paintRect(st,3,0,5,1,a); console.log(JSON.stringify(M.generate(st).walls.length))")
    assert r2 == 4                                                                         # the same room on both sides: no wall in between


@needs_node
def test_a_door_opens_the_wall_and_open_removes_it_and_marks_only_go_on_walls():
    r = _js(SETUP + """
M.paintRect(st,0,0,2,1,a);
console.log(JSON.stringify({
  onWall: M.setMark(st, '3,0,v', 'door'),                 // the east edge of square (2,0)
  inside: M.setMark(st, '1,0,v', 'door'),                 // inside the room: not a wall
  open: M.setMark(st, '0,2,h', 'open'),
  g: M.generate(st).walls.map(w => [w.kind, w.state || null, w.pts]),
}))""")
    assert r["onWall"] is True and r["inside"] is False
    kinds = [k for k, _, _ in r["g"]]
    assert kinds.count("door") == 1 and "open" not in kinds
    door = next(w for w in r["g"] if w[0] == "door")
    assert door[1] == "closed" and door[2] == [[150, 0], [150, 50]]
    # the east wall is one square tall (the door is all of it); the bottom loses the open square, so 2 pieces; plus top and west
    walls = [p for k, _, p in r["g"] if k == "wall"]
    assert len(walls) == 4 and not any(p == [[150, 0], [150, 100]] for p in walls)


@needs_node
def test_door_state_survives_regeneration_and_secret_doors_stay_walls_to_players():
    r = _js(SETUP + """
M.paintRect(st,0,0,2,1,a); M.setMark(st,'3,0,v','door'); M.setMark(st,'0,0,h','secret');
const first = M.generate(st).walls.find(w => w.kind === 'door');
const again = M.generate(st, {[first.id]: 'open'});
const els = M.elements(st, again);
console.log(JSON.stringify({ id: first.id, state: again.walls.find(w => w.id === first.id).state, secret: again.walls.find(w => w.kind === 'secret').state,
  hidden: els.filter(e => e.hidden).length, wallUnder: els.filter(e => e.type === 'line' && e.stroke === '#cfd6e6').length }))""")
    assert r["state"] == "open" and r["secret"] == "closed" and r["hidden"] == 1
    assert r["wallUnder"] >= 4                                                             # the secret door has a plain wall drawn under it for players


@needs_node
def test_erasing_removes_floor_walls_and_stale_marks():
    r = _js(SETUP + """
M.paintRect(st,0,0,2,1,a); M.setMark(st,'3,0,v','door');
M.paintRect(st,2,0,2,1,0);                                 // erase the column the door was on
console.log(JSON.stringify({ marks: Object.keys(st.marks), g: M.generate(st).walls.map(w => w.kind) }))""")
    assert r["marks"] == [] and set(r["g"]) == {"wall"}


@needs_node
def test_floors_are_merged_into_few_rectangles_and_one_label_per_room():
    r = _js(SETUP + """
M.paintRect(st,0,0,4,3,a); M.paintRect(st,1,1,2,2,0);       // a ring with a hole
const g = M.generate(st); const els = M.elements(st, g);
console.log(JSON.stringify({ rects: g.floors.length, labels: els.filter(e => e.label).map(e => e.label), area: g.floors.reduce((s,r)=>s+r.w*r.h,0) }))""")
    assert r["area"] == 20 - 4 and r["rects"] <= 4 and r["labels"] == ["Hall"]


@needs_node
def test_every_floor_side_is_closed_after_random_paints():
    r = _js(SETUP + """
let seed = 12345; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
let leaks = 0, checked = 0;
for (let round = 0; round < 60; round++) {
  const x0 = Math.floor(rnd()*10), y0 = Math.floor(rnd()*8), x1 = Math.floor(rnd()*10), y1 = Math.floor(rnd()*8), pick = Math.floor(rnd()*3);
  M.paintRect(st, x0, y0, x1, y1, pick === 0 ? 0 : pick === 1 ? a : b);
  const edges = M.edgeSet(st), g = M.generate(st);
  // every wall edge must be covered by a generated wall, mark or an 'open' gap; collect covered unit edges from the output
  const covered = {};
  g.walls.forEach(w => { const [p, q] = w.pts; const horiz = p[1] === q[1];
    const n = Math.round((horiz ? Math.abs(q[0]-p[0]) : Math.abs(q[1]-p[1])) / 50);
    for (let i = 0; i < n; i++) { const x = horiz ? Math.min(p[0],q[0])/50 + i : p[0]/50, y = horiz ? p[1]/50 : Math.min(p[1],q[1])/50 + i; covered[x + ',' + y + ',' + (horiz ? 'h' : 'v')] = true; } });
  Object.keys(edges).forEach(k => { checked++; if (!covered[k] && st.marks[k] !== 'open') leaks++; });
  Object.keys(covered).forEach(k => { if (!edges[k]) leaks++; });                       // no wall where there is no edge
}
console.log(JSON.stringify({ leaks, checked }))""")
    assert r["leaks"] == 0 and r["checked"] > 100


@needs_node
def test_the_stored_form_round_trips_and_python_accepts_it():
    d = _js(SETUP + "M.paintRect(st,0,0,2,1,a); M.paintRect(st,5,3,7,4,b); M.setMark(st,'3,0,v','door'); console.log(JSON.stringify(M.pack(st)))")
    state, warnings = R.clean_rooms(d)                                                    # the server's validator accepts what the browser writes
    assert state["cols"] == 10 and state["rows"] == 8 and len(state["spaces"]) == 2 and state["marks"] == [[3, 0, "v", "door"]]
    again = _js(SETUP + f"const back = M.unpack({json.dumps(d)}); console.log(JSON.stringify(M.pack(back)))")
    assert again == d


@needs_node
def test_pointer_helpers_and_limits():
    r = _js("""
const st = M.create(50, 20, 10, 520, 410);
console.log(JSON.stringify({ cols: st.cols, rows: st.rows, cell: M.cellAt(st, 70, 60), off: M.cellAt(st, 5, 5), edge: M.nearestEdge(st, 71, 85),
  edge2: M.nearestEdge(st, 95, 59), tooBig: M.create(8, 0, 0, 20000, 20000), tiny: M.create(50, 0, 0, 30, 30) }))""")
    assert (r["cols"], r["rows"]) == (10, 8) and r["cell"] == {"x": 1, "y": 1} and r["off"] is None
    assert r["edge"] == "1,1,v" and r["edge2"] == "1,1,h"                                  # nearest side of the square under the pointer
    assert r["tooBig"] is None and r["tiny"] is None
