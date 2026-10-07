"""Line of sight and fog of war maths (static/js/map-geom.js, map-vision.js, map-fog.js), run under Node.

The properties that matter at the table: walls and closed doors block sight, open doors and windows do not, windows still
block movement, the visible area is a proper polygon that grows through an opened door, the explored memory round-trips and
rejects garbage, and a token you cannot see is hidden."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
JS = ROOT / "static" / "js"


def _js(expr: str, setup: str = ""):
    script = (f"global.window = global; const V = require({json.dumps(str(JS / 'map-vision.js'))}); "
              f"const G = require({json.dumps(str(JS / 'map-geom.js'))}); const F = require({json.dumps(str(JS / 'map-fog.js'))}); "
              f"{setup}\nconsole.log(JSON.stringify({expr}))")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# a 400x300 room (100,100)-(500,400) with a doorway gap in the east wall, plus a window in the north wall
ROOM = """
const seg = (x1,y1,x2,y2,kind,state) => ({x1,y1,x2,y2,kind:kind||'wall',state:state||'closed'});
const room = (doorState) => [
  seg(100,100,500,100,'window'), seg(100,100,100,400), seg(100,400,500,400),
  seg(500,100,500,200), seg(500,200,500,300,'door',doorState), seg(500,300,500,400),
];
const area = p => Math.abs(G.signedArea(p));
"""


@needs_node
def test_blocking_rules():
    assert _js("[V.blocksSight({kind:'wall'}), V.blocksSight({kind:'door',state:'closed'}), V.blocksSight({kind:'door',state:'open'}), V.blocksSight({kind:'window'})]") == [True, True, False, False]
    assert _js("[V.blocksMove({kind:'wall'}), V.blocksMove({kind:'door',state:'closed'}), V.blocksMove({kind:'door',state:'open'}), V.blocksMove({kind:'window'})]") == [True, True, False, True]


@needs_node
def test_a_closed_room_is_seen_up_to_its_walls_and_an_open_door_lets_the_view_out():
    closed = _js("area(V.visibilityPolygon([300,250], room('closed'), {bounds:{x0:0,y0:0,x1:1000,y1:800}}))", ROOM)
    # the window does not block sight, so the view reaches past the north wall; everything else stays inside
    opened = _js("area(V.visibilityPolygon([300,250], room('open'), {bounds:{x0:0,y0:0,x1:1000,y1:800}}))", ROOM)
    assert opened > closed + 10000
    inside = _js("area(V.visibilityPolygon([300,250], room('closed').map(s=>s.kind==='window'?Object.assign({},s,{kind:'wall'}):s), {bounds:{x0:0,y0:0,x1:1000,y1:800}}))", ROOM)
    assert inside == pytest.approx(400 * 300, rel=1e-3)                                   # exactly the room


@needs_node
def test_range_limits_the_view_to_a_disc():
    a = _js("area(V.visibilityPolygon([300,250], [], {range:100, bounds:{x0:0,y0:0,x1:1000,y1:800}}))", ROOM)
    assert a == pytest.approx(3.14159265 * 100 * 100, rel=0.01)


@needs_node
def test_can_see_and_move_blocked_agree_with_the_rules():
    r = lambda expr: _js(expr, ROOM)
    assert r("V.canSee([300,250],[450,250], room('closed'))") is True
    assert r("V.canSee([300,350],[600,350], room('open'))") is False                                      # through the east wall
    assert r("V.canSee([300,250],[600,250], room('closed'))") is False                                   # through a closed door
    assert r("V.canSee([300,250],[600,250], room('open'))") is True
    assert r("V.canSee([300,150],[300,50], room('closed'))") is True                                     # a window is see-through...
    assert r("V.moveBlocked([300,150],[300,50], room('closed'))") is True                                # ...but not walk-through
    assert r("V.moveBlocked([300,250],[600,250], room('open'))") is False


@needs_node
def test_explored_memory_round_trips_and_rejects_garbage():
    assert _js("F.toRuns(Uint8Array.from([0,1,1,0,1,1,1]))") == [1, 2, 4, 3]
    assert _js("Array.from(F.fromRuns([1,2,4,3], 7))") == [0, 1, 1, 0, 1, 1, 1]
    # out-of-range, negative, fractional, non-numeric and non-array input are ignored or clipped, never thrown on
    assert _js("Array.from(F.fromRuns([5,100,-3,4,'x',2,1.5,2,null,1], 8))") == [0, 0, 0, 0, 0, 1, 1, 1]
    assert _js("Array.from(F.fromRuns('nope', 4))") == [0, 0, 0, 0] and _js("Array.from(F.fromRuns(null, 3))") == [0, 0, 0]


@needs_node
def test_the_memory_grid_stays_small_for_huge_canvases():
    assert _js("F.memoryCell(2000,1500,50)") == 50
    c = _js("F.memoryCell(100000,100000,10)")
    assert c >= 20 and (100000 // c + 1) ** 2 <= 40000 * 4


# a tiny DOM shim, enough for the fog layer: elements with attributes and children
SHIM = """
function node(tag){ return { tag, attrs:{}, children:[], style:{}, parentNode:null,
  setAttribute(k,v){ this.attrs[k]=String(v); }, getAttribute(k){ return this.attrs[k]; },
  appendChild(c){ c.parentNode=this; this.children.push(c); return c; },
  insertBefore(c,ref){ c.parentNode=this; const i=this.children.indexOf(ref); this.children.splice(i<0?this.children.length:i,0,c); return c; },
  remove(){ if(this.parentNode){ this.parentNode.children=this.parentNode.children.filter(x=>x!==this); } },
  querySelector(sel){ return sel==='defs' ? (this.children.find(c=>c.tag==='defs')||null) : null; },
  querySelectorAll(){ return []; } }; }
global.document = { createElementNS:(ns,tag)=>node(tag) };
const svg = node('svg'); const tokenLayer = node('g'); svg.appendChild(tokenLayer);
const walls = [{x1:100,y1:100,x2:500,y2:100,kind:'wall'},{x1:500,y1:100,x2:500,y2:400,kind:'wall'},
               {x1:500,y1:400,x2:100,y2:400,kind:'wall'},{x1:100,y1:400,x2:100,y2:100,kind:'wall'}];
const fog = F.create(svg, {width:1000, height:800, cell:50, walls, tokenLayer});
"""


@needs_node
def test_fog_marks_what_is_seen_and_hides_tokens_out_of_view():
    out = _js("[res, fog.runs().length > 0, inside.style.display, outside.style.display, always.style.display, svg.children[0].tag]", SHIM + """
const inside = node('g'), outside = node('g'), always = node('g');
const res = fog.update([{x:300,y:250,range:Infinity}], [{x:320,y:260,node:inside},{x:700,y:600,node:outside},{x:800,y:700,node:always,always:true}]);
""")
    res, has_runs, inside, outside, always, first = out
    assert res["polygons"] == 1 and res["newlyExplored"] > 0 and res["hiddenTokens"] == 1
    assert has_runs and inside == "" and outside == "none" and always == "" and first == "g"       # the fog layer sits before the token layer


@needs_node
def test_explored_cells_stay_explored_after_the_token_leaves():
    out = _js("[a, b, c]", SHIM + """
fog.update([{x:300,y:250,range:Infinity}], []);
const a = fog.runs().length;
fog.update([], []);                       # nobody sees anything now
const b = fog.runs().length;
fog.clear();
const c = fog.runs().length;
""".replace("#", "//"))
    a, b, c = out
    assert a > 0 and b == a and c == 0


@needs_node
def test_a_remembered_memory_is_restored():
    out = _js("fog2.runs()", SHIM + """
fog.update([{x:300,y:250,range:Infinity}], []);
const saved = fog.runs();
const fog2 = F.create(node('svg'), {width:1000, height:800, cell:50, walls});
fog2.loadRuns(saved);
""")
    assert out and len(out) % 2 == 0


# ── the editor's door-cutting rule (static/js/schematic-walls.js) ────────────

def _cut(walls, a, b):
    script = (f"global.window = global; const W = require({json.dumps(str(JS / 'schematic-walls.js'))}); "
              f"let n = 0; const mk = () => 'new' + (++n); "
              f"console.log(JSON.stringify(window.ndWalls.cut({json.dumps(walls)}, {json.dumps(a)}, {json.dumps(b)}, mk)))")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_a_door_cuts_the_wall_it_is_drawn_on():
    room = [{"id": "r", "kind": "wall", "pts": [[100, 100], [400, 100], [400, 400], [100, 400], [100, 100]]}]
    out = _cut(room, [400, 200], [400, 300])
    # the east wall is split around the door; the rest of the outline stays connected
    segs = [(p, q) for w in out for p, q in zip(w["pts"], w["pts"][1:])]
    east = sorted((min(p[1], q[1]), max(p[1], q[1])) for p, q in segs if p[0] == 400 and q[0] == 400)
    assert east == [(100, 200), (300, 400)]
    assert sum(len(w["pts"]) - 1 for w in out) == 5 and all(w["kind"] == "wall" for w in out)


@needs_node
def test_a_door_elsewhere_leaves_walls_alone_and_only_walls_are_cut():
    room = [{"id": "r", "kind": "wall", "pts": [[100, 100], [400, 100]]}, {"id": "d", "kind": "door", "pts": [[200, 100], [250, 100]], "state": "closed"}]
    assert _cut(room, [200, 300], [250, 300]) == room                        # nowhere near a wall
    out = _cut(room, [200, 100], [250, 100])
    assert [w["id"] for w in out if w["kind"] == "door"] == ["d"]               # an existing door is not a wall
    assert sorted(w["pts"][0][0] for w in out if w["kind"] == "wall") == [100, 250]


@needs_node
def test_a_door_that_covers_a_whole_wall_removes_it_and_degenerate_input_is_safe():
    assert _cut([{"id": "r", "kind": "wall", "pts": [[100, 100], [150, 100]]}], [90, 100], [160, 100]) == []
    one = [{"id": "r", "kind": "wall", "pts": [[100, 100], [150, 100]]}]
    assert _cut(one, [120, 100], [120, 100]) == one                           # a zero-length "door"


@needs_node
def test_cutting_works_whichever_way_the_wall_was_drawn():
    for pts in ([[100, 100], [400, 100]], [[400, 100], [100, 100]]):
        out = _cut([{"id": "w", "kind": "wall", "pts": pts}], [200, 100], [300, 100])
        spans = sorted((min(w["pts"][0][0], w["pts"][-1][0]), max(w["pts"][0][0], w["pts"][-1][0])) for w in out)
        assert spans == [(100, 200), (300, 400)], pts
