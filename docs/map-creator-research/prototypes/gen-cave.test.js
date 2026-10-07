'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const gc = require('./gen-cave.js');
const geom = require('./geom.js');

test('same seed, same cave', () => {
  const a = gc.generateCave({ w: 48, h: 32, seed: 5 }), b = gc.generateCave({ w: 48, h: 32, seed: 5 });
  assert.deepEqual(Array.from(a.ids), Array.from(b.ids));
  assert.deepEqual(a.rings, b.rings);
});

test('the cave is one connected cavern with a rock border', () => {
  for (let seed = 1; seed <= 20; seed++) {
    const c = gc.generateCave({ w: 48, h: 32, seed }), { w, h, ids } = c;
    assert.ok(c.floorCells > 100, `seed ${seed}: has floor (${c.floorCells})`);
    for (let x = 0; x < w; x++) { assert.equal(ids[x], -1); assert.equal(ids[(h - 1) * w + x], -1); }
    for (let y = 0; y < h; y++) { assert.equal(ids[y * w], -1); assert.equal(ids[y * w + w - 1], -1); }
    // connectivity: flood from the first floor cell reaches every floor cell
    const start = ids.findIndex(v => v >= 0), seen = new Uint8Array(w * h), stack = [start]; seen[start] = 1;
    while (stack.length) {
      const k = stack.pop(), x = k % w, y = (k - x) / w;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) { const n = (y + dy) * w + x + dx; if (ids[n] >= 0 && !seen[n]) { seen[n] = 1; stack.push(n); } }
    }
    assert.ok(ids.every((v, i) => v < 0 || seen[i]), `seed ${seed}: one cavern`);
  }
});

test('rings are closed, and the unsmoothed rings enclose exactly the floor cells', () => {
  for (let seed = 1; seed <= 10; seed++) {
    const c = gc.generateCave({ w: 40, h: 30, seed, style: 'lattice' });
    c.rings.forEach(r => assert.deepEqual(r[0], r[r.length - 1]));
    const net = c.rings.reduce((s, r) => s + geom.signedArea(r.slice(0, -1)), 0);   // outer ring minus holes (opposite winding)
    assert.equal(Math.abs(net), c.floorCells, `seed ${seed}`);
  }
});

test('smoothing cuts the number of wall segments a lot and keeps the enclosed area close', () => {
  const raw = gc.generateCave({ w: 60, h: 40, seed: 9, style: 'lattice' }), sm = gc.generateCave({ w: 60, h: 40, seed: 9, style: 'dp', smooth: 0.75 });
  const count = c => c.rings.reduce((n, r) => n + r.length - 1, 0), area = c => Math.abs(c.rings.reduce((s, r) => s + geom.signedArea(r.slice(0, -1)), 0));
  assert.ok(count(sm) < count(raw) * 0.8, `${count(sm)} vs ${count(raw)}`);
  assert.ok(Math.abs(area(sm) - area(raw)) / area(raw) < 0.08, `area ${area(sm)} vs ${area(raw)}`);
  assert.equal(gc.wallsOf(sm).length, count(sm));
});

// the distance from a floor cell's centre to the nearest wall segment
function nearestWall(c, segs) {
  const d = (px, py, s) => {
    const dx = s.x2 - s.x1, dy = s.y2 - s.y1, l2 = dx * dx + dy * dy, t = l2 ? Math.max(0, Math.min(1, ((px - s.x1) * dx + (py - s.y1) * dy) / l2)) : 0;
    return Math.hypot(px - (s.x1 + t * dx), py - (s.y1 + t * dy));
  };
  let worst = Infinity;
  for (let i = 0; i < c.ids.length; i++) if (c.ids[i] >= 0) { const px = (i % c.w) + 0.5, py = Math.floor(i / c.w) + 0.5; for (const s of segs) worst = Math.min(worst, d(px, py, s)); }
  return worst;
}

test('chamfered walls never come nearer than 0.35 cells to the centre of a floor cell; simplified ones do', () => {
  let dpBad = 0;
  for (let seed = 1; seed <= 12; seed++) {
    const chamfer = gc.generateCave({ w: 40, h: 28, seed }), dp = gc.generateCave({ w: 40, h: 28, seed, style: 'dp' });
    assert.ok(nearestWall(chamfer, gc.wallsOf(chamfer)) >= 0.35 - 1e-9, `seed ${seed}`);
    if (nearestWall(dp, gc.wallsOf(dp)) < 0.1) dpBad++;
  }
  assert.ok(dpBad >= 1, 'the simplified outline should put a wall through a floor-cell centre on at least one of these caves');
});

test('chamfer rings separate floor-cell centres from rock-cell centres exactly, and are closed', () => {
  for (let seed = 1; seed <= 8; seed++) {
    const c = gc.generateCave({ w: 40, h: 28, seed }), lat = gc.generateCave({ w: 40, h: 28, seed, style: 'lattice' });
    c.rings.forEach(r => assert.deepEqual(r[0], r[r.length - 1]));
    for (let i = 0; i < c.ids.length; i++) {
      const p = [(i % c.w) + 0.5, Math.floor(i / c.w) + 0.5];
      const inside = c.rings.filter(r => geom.pointInPolygon(p, r.slice(0, -1))).length % 2 === 1;     // even-odd across all rings
      assert.equal(inside, c.ids[i] >= 0, `seed ${seed} cell ${i}`);
    }
    assert.ok(gc.wallsOf(c).length <= gc.wallsOf(lat).length, 'no more segments than the staircase');
  }
});
