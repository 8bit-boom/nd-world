"""Phase 12 regression test: renderBattleGrid's hex-tiling loop (in
static/js/schematic-render.js, shared by the GM editor and the player-facing
live view) picked its q/r iteration range from CW/CH alone, ignoring that
axial hex coordinates are sheared relative to screen space — pointy-top hexes
shear horizontally as r grows, flat-top hexes shear vertically as q grows.
Deep rows (or far columns) drift outside a range sized without accounting for
that shift, so whole rows/columns of grid lines never got drawn near the
opposite edge — a real, visible rendering gap, not just an extreme-aspect-
ratio edge case (see the "square-ish" case below, which gapped too).

This is pure client-side JS with no server route to exercise, and this test
suite has no JS runtime — everywhere else in it, that kind of fix gets a
source-level regression guard instead. Here we can do better: Node is
available in this dev environment (and ubuntu-latest GitHub Actions runners
ship it out of the box even without an explicit setup-node step), so this
test actually executes schematic-render.js's real hex math with a minimal
DOM shim and checks the rendered grid actually reaches both edges at every
height. It skips outright if `node` isn't on PATH rather than failing CI in
some future environment that doesn't have it.
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

const drawn = [];
global.document = {
  createElementNS: (ns, tag) => {
    const attrs = {};
    return { setAttribute: (k, v) => { attrs[k] = v; }, _attrs: attrs, _tag: tag };
  },
};
global.battleGridLayer = { innerHTML: '', appendChild: (el) => { drawn.push(el); } };

const src = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(src, { filename: 'schematic-render.js' });

function coversEdges(CW, CH, cellSize, orientation) {
  global.gridType = 'hex';
  global.gridConfig = { cell_size: cellSize, offset_x: 0, offset_y: 0, orientation };
  global.CW = CW; global.CH = CH;
  drawn.length = 0;
  global.renderBattleGrid();
  const w = orientation === 'flat' ? cellSize * 2 : Math.sqrt(3) * cellSize;
  const centers = drawn.map(el => {
    const pts = el._attrs.points.split(' ').map(p => p.split(',').map(Number));
    return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
  });
  const probeYs = [0, CH * 0.25, CH * 0.5, CH * 0.75, CH];
  return probeYs.every(py => {
    const anyLeft = centers.some(([cx, cy]) => Math.abs(cy - py) < w && cx < w);
    const anyRight = centers.some(([cx, cy]) => Math.abs(cy - py) < w && cx > CW - w);
    return anyLeft && anyRight;
  });
}

const cases = {
  "tall_pointy": coversEdges(2000, 6000, 50, 'pointy'),
  "wide_flat": coversEdges(6000, 2000, 50, 'flat'),
  "square_ish_pointy": coversEdges(2000, 1500, 50, 'pointy'),
};
console.log(JSON.stringify(cases));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available in this environment")
def test_hex_grid_covers_canvas_edges_at_every_height():
    result = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, "--", str(_RENDER_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node script failed: {result.stderr}"
    cases = json.loads(result.stdout.strip().splitlines()[-1])
    for name, covered in cases.items():
        assert covered, f"{name}: hex grid left a gap — some probed row never reaches both canvas edges"
