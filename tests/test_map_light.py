"""Lights, darkness and what is lit (static/js/map-light.js and the glue in map-fog-glue.js), run under Node with a tiny DOM shim.

At the table: a creature in the dark is not seen, a light reaches as far as its range and no further than a wall, a light that
is switched off lights nothing, the party's own lantern lights its surroundings, and in the dark only lit squares are
remembered as explored."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
JS = ROOT / "static" / "js"

SHIM = f"""
global.window = global;
const V = require({json.dumps(str(JS / 'map-vision.js'))}); const G = require({json.dumps(str(JS / 'map-geom.js'))});
const F = require({json.dumps(str(JS / 'map-fog.js'))}); const L = require({json.dumps(str(JS / 'map-light.js'))});
const glue = require({json.dumps(str(JS / 'map-fog-glue.js'))});
function node(tag){{ return {{ tag, attrs:{{}}, children:[], style:{{}}, dataset:{{}}, parentNode:null,
  setAttribute(k,v){{ this.attrs[k]=String(v); }}, getAttribute(k){{ return this.attrs[k]; }},
  appendChild(c){{ c.parentNode=this; this.children.push(c); return c; }},
  insertBefore(c,ref){{ c.parentNode=this; const i=this.children.indexOf(ref); this.children.splice(i<0?this.children.length:i,0,c); return c; }},
  removeChild(c){{ this.children=this.children.filter(x=>x!==c); c.parentNode=null; }},
  get firstChild(){{ return this.children[0] || null; }},
  remove(){{ if(this.parentNode){{ this.parentNode.children=this.parentNode.children.filter(x=>x!==this); }} }},
  querySelector(sel){{ return sel==='defs' ? (this.children.find(c=>c.tag==='defs')||null) : null; }},
  querySelectorAll(){{ return []; }} }}; }}
global.document = {{ createElementNS:(ns,tag)=>node(tag) }};
global.localStorage = (()=>{{ const m={{}}; return {{ getItem:k=>m[k]||null, setItem:(k,v)=>{{m[k]=v}}, removeItem:k=>{{delete m[k]}} }}; }})();
// a 600 x 400 room with walls all round, and a wall across the middle at x=300 with no gap
const walls = [{{pts:[[0,0],[600,0],[600,400],[0,400],[0,0]],kind:'wall'}}, {{pts:[[300,0],[300,400]],kind:'wall'}}];
"""


def _js(body: str):
    out = subprocess.run(["node", "-e", SHIM + "\n" + body], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_a_light_reaches_its_range_and_stops_at_walls():
    r = _js("""
const svg = node('svg'); const layer = L.create(svg, {width:600, height:400, walls: glue.segmentsOf(walls), darkness:0.8});
layer.update([{x:100, y:200, range:150, color:'#ffaa00', intensity:1}]);
console.log(JSON.stringify({ near: layer.isLit(150,200), far: layer.isLit(260,200), beyondWall: layer.isLit(310,200), outsideRange: layer.isLit(100,60) }))""")
    assert r == {"near": True, "far": False, "beyondWall": False, "outsideRange": False}      # 150 range, 90% reach = 135; the wall at 300 stops the rest


@needs_node
def test_lights_add_up_and_no_lights_light_nothing():
    r = _js("""
const svg = node('svg'); const layer = L.create(svg, {width:600, height:400, walls: [], darkness:0.8});
layer.update([]);
const none = layer.isLit(100,100);
layer.update([{x:100,y:100,range:60,color:'#fff',intensity:1},{x:400,y:100,range:60,color:'#fff',intensity:1}]);
console.log(JSON.stringify({ none, a: layer.isLit(100,100), b: layer.isLit(400,100), between: layer.isLit(250,100) }))""")
    assert r == {"none": False, "a": True, "b": True, "between": False}


@needs_node
def test_a_hostile_light_cannot_break_the_layer():
    r = _js("""
const svg = node('svg'); const layer = L.create(svg, {width:600, height:400, walls: glue.segmentsOf(walls), darkness:0.8});
const out = layer.update([{x:100,y:100,range:80,color:'url(javascript:alert(1))',intensity:99},{x:100,y:100,range:80,color:'#fff',intensity:-5}]);
console.log(JSON.stringify({ n: out.lights, colours: svg.children[1].children.length >= 0 }))""")
    assert r["n"] == 2


@needs_node
def test_the_light_cap_keeps_the_personal_lights_and_the_lights_nearest_the_party():
    r = _js("""
const personal = [{x:50,y:50,range:100},{x:60,y:50,range:100}];
const many = []; for (let i = 0; i < 30; i++) many.push({x: 100 + i * 40, y: 50, range: 50, id: i});
const picked = glue.pickLights(personal, many, [{x:50,y:50}], {x:300,y:200});
console.log(JSON.stringify({ n: picked.length, ids: picked.filter(l => l.id !== undefined).map(l => l.id) }))""")
    assert r["n"] == 12 and r["ids"] == list(range(10))                      # 2 personal + the 10 map lights nearest the characters
    few = _js("console.log(JSON.stringify(glue.pickLights([], [{x:1,y:1,range:2}], [], {x:0,y:0}).length))")
    assert few == 1


STATE = """
const svg = node('svg'); const tokenLayer = node('g'); svg.appendChild(tokenLayer);
let els = [];
const addToken = (id, x, y, pc) => { els.push({id, type:'token', x, y, pc_id: pc ? 1 : null}); const n = node('g'); n.dataset.id = id; tokenLayer.appendChild(n); return n; };
const g = glue.create({svg, tokenLayer, key:'t', getElements:()=>els, getGrid:()=>({cell:50}), getCanvas:()=>({w:600,h:400})});
const shown = n => n.style.display === '' ? 'shown' : n.style.display;
"""


@needs_node
def test_in_the_dark_a_creature_is_seen_only_where_a_light_reaches():
    r = _js(STATE + """
const hero = addToken('hero', 100, 200, true), near = addToken('near', 150, 200, false), far = addToken('far', 280, 200, false), other = addToken('other', 450, 200, false);
const settings = { enabled:false, range:0, epoch:0, darkness:0.85, personal:2 };
g.setState({fog: settings, walls, lights: []});
const a = [near, far, other, hero].map(shown);                       // only the lantern (2 squares = 100 px) lights anything
g.setState({fog: settings, walls, lights: [{x:450, y:200, range:3, color:'#ff9933', intensity:1}]});
const b = [near, far, other, hero].map(shown);                       // a torch on the far side of the wall lights 'other'
g.setState({fog: {...settings, darkness:0.3}, walls, lights: []});
const c = [near, far, other, hero].map(shown);                       // barely dark: nobody is hidden
g.setState({fog: {...settings, darkness:0}, walls, lights: []});
const d = [near, far, other, hero].map(shown);
g.setState({fog: {...settings, personal:0}, walls, lights: []});
const e = [near, far, other, hero].map(shown);                       // no lantern, no lights: only the characters remain
console.log(JSON.stringify({a, b, c, d, e}))""")
    assert r["a"] == ["shown", "none", "none", "shown"]
    assert r["b"] == ["shown", "none", "shown", "shown"]
    assert r["c"] == ["shown"] * 4 and r["d"] == ["shown"] * 4
    assert r["e"] == ["none", "none", "none", "shown"]


@needs_node
def test_a_light_that_is_off_is_never_sent_so_it_lights_nothing():
    # the server drops lights that are off (tests/test_map_walls.py), so the browser's list simply lacks it
    r = _js(STATE + """
const hero = addToken('hero', 100, 200, true), t = addToken('t', 450, 200, false);
g.setState({fog:{enabled:false,range:0,epoch:0,darkness:0.9,personal:2}, walls, lights:[]});
console.log(JSON.stringify(shown(t)))""")
    assert r == "none"


@needs_node
def test_fog_and_darkness_together_remember_only_lit_squares():
    r = _js(STATE + """
const hero = addToken('hero', 100, 200, true);
const squares = runs => { const cols = 12; const out = []; for (let i = 0; i < runs.length; i += 2) for (let k = 0; k < runs[i+1]; k++) { const idx = runs[i] + k; out.push([(idx % cols) * 50 + 25, Math.floor(idx / cols) * 50 + 25]); } return out; };
g.setState({fog:{enabled:true,range:0,epoch:0,darkness:0.9,personal:2}, walls, lights:[]});
const dark = squares(g.exploredRuns());
g.setState({fog:{enabled:true,range:0,epoch:1,darkness:0,personal:2}, walls, lights:[]});     // a new epoch forgets; daylight
const day = squares(g.exploredRuns());
console.log(JSON.stringify({ dark, dayCount: day.length }))""")
    assert r["dark"], "the lantern lights some squares, so they are remembered"
    assert all(((x - 100) ** 2 + (y - 200) ** 2) ** 0.5 <= 90 for x, y in r["dark"])           # only inside the lantern's lit reach (90% of 100 px)
    assert r["dayCount"] > 3 * len(r["dark"])                                                  # in daylight the whole half of the room is explored


@needs_node
def test_the_layer_order_puts_light_under_fog_under_tokens():
    r = _js(STATE + """
addToken('hero', 100, 200, true);
g.setState({fog:{enabled:true,range:0,epoch:0,darkness:0.8,personal:2}, walls, lights:[]});
const order = svg.children.map(c => c === tokenLayer ? 'tokens' : (c.attrs.id || c.tag).replace(/[0-9]+/, ''));
console.log(JSON.stringify(order))""")
    assert r.index("ndlight") < r.index("ndfog") < r.index("tokens")
