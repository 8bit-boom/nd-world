"""Regression tests for two schematic shape-rendering gaps in
static/js/schematic-render.js, per explicit request ("when you bake
geometric forms (example circle) you should be able to choose color of this
form and there must be indicators on how big this form is in hexes/feet"):

1. AOE elements (Burst/Cone/Line templates) stored their color in a bespoke
   `color` field, but the property panel's Fill/Stroke pickers (shared by
   every other shape type) write to `fill`/`stroke` — so the pickers
   rendered and looked interactive for a selected AOE, but changing them had
   no visible effect at all; AOE color was silently stuck at its creation-
   time default forever. Fixed by having AOE read fill/stroke like every
   other shape.
2. A plain circle had no real-world-unit size indicator anywhere — only the
   property bar's raw pixel radius when selected. AOE bursts already showed
   their radius in feet/hexes baked onto the map (via pxToUnits, the same
   grid-unit conversion the Measure tool uses); circles got the same
   treatment.

Same execution technique as test_schematic_hex_grid.py: this is pure
client-side JS with no server route to exercise and no JS runtime in the
rest of this suite, but Node is available here (and on the CI runner), so
this actually executes makeElSVG with a minimal DOM shim rather than just
grepping source text.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_RENDER_JS = Path(__file__).parent.parent / "static" / "js" / "schematic-render.js"

_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');

function makeEl(tag) {
  const attrs = {};
  const children = [];
  return {
    _tag: tag, _attrs: attrs, _children: children,
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => attrs[k],
    appendChild: (c) => { children.push(c); },
  };
}
global.document = { createElementNS: (ns, tag) => makeEl(tag) };
global.layerState = {};

const src = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(src, { filename: 'schematic-render.js' });

// A 50px-per-cell square grid where each cell is 5ft — same shape a GM
// would configure via the Grid Settings dialog.
global.gridType = 'square';
global.gridConfig = { cell_size: 50, unit_per_cell: 5, unit_label: 'ft' };

function textsOf(g) {
  return g._children.filter(c => c._tag === 'text').map(c => c._children.length ? c._children[0] : c._attrs.__text);
}
// text elements set .textContent, which our shim doesn't model as a real
// DOM property — patch it onto the object so `lt.textContent = x` works.
function withTextContent(el) {
  Object.defineProperty(el, 'textContent', {
    get() { return this._attrs.__text; },
    set(v) { this._attrs.__text = v; },
  });
  return el;
}
const origCreate = global.document.createElementNS;
global.document.createElementNS = (ns, tag) => withTextContent(origCreate(ns, tag));

const results = {};

// ── AOE: fill/stroke actually drive the rendered color ──────────────────
const aoeDefault = makeElSVG({ id:'a1', type:'aoe', shape:'circle', x1:0, y1:0, x2:100, y2:0 });
const aoeCircle = aoeDefault._children.find(c => c._tag === 'circle');
results.aoe_default_fill = aoeCircle._attrs.fill;

const aoeCustom = makeElSVG({ id:'a2', type:'aoe', shape:'circle', x1:0, y1:0, x2:100, y2:0,
  fill:'#3388ff', stroke:'#ffaa00' });
const aoeCustomCircle = aoeCustom._children.find(c => c._tag === 'circle');
results.aoe_custom_fill = aoeCustomCircle._attrs.fill;
results.aoe_custom_stroke = aoeCustomCircle._attrs.stroke;

// Legacy elements saved before this fix only have `color` — must still render
// with their original color rather than silently reverting to the hardcoded
// default now that AOE reads fill/stroke.
const aoeLegacy = makeElSVG({ id:'a3', type:'aoe', shape:'circle', x1:0, y1:0, x2:100, y2:0,
  color:'#00ff00' });
const aoeLegacyCircle = aoeLegacy._children.find(c => c._tag === 'circle');
results.aoe_legacy_color_fill = aoeLegacyCircle._attrs.fill;

// ── Circle: fill still works, and a feet/hex radius indicator now exists ──
const circle = makeElSVG({ id:'c1', type:'circle', cx:200, cy:200, rx:50, ry:50, fill:'#22cc88' });
const ellipse = circle._children.find(c => c._tag === 'ellipse');
results.circle_fill = ellipse._attrs.fill;
results.circle_texts = textsOf(circle);

// Grid-less canvas: no unit to convert to, so no size text should appear
// (the property bar's raw-pixel readout is still the fallback there).
global.gridType = 'none';
const circleNoGrid = makeElSVG({ id:'c2', type:'circle', cx:200, cy:200, rx:50, ry:50 });
results.circle_no_grid_texts = textsOf(circleNoGrid);

console.log(JSON.stringify(results));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available in this environment")
def test_aoe_color_and_circle_size_indicator():
    result = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, "--", str(_RENDER_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node script failed: {result.stderr}"
    r = json.loads(result.stdout.strip().splitlines()[-1])

    assert r["aoe_default_fill"] == "#ff6666"
    assert r["aoe_custom_fill"] == "#3388ff"
    assert r["aoe_custom_stroke"] == "#ffaa00"
    assert r["aoe_legacy_color_fill"] == "#00ff00"

    assert r["circle_fill"] == "#22cc88"
    assert any("5.0 ft" in t for t in r["circle_texts"]), r["circle_texts"]
    assert r["circle_no_grid_texts"] == []
