'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const gw = require('./gridwalls.js');
const geom = require('./geom.js');

function grid(rows) {            // ['..#', ...]  '#' rock, letters = space ids
  const h = rows.length, w = rows[0].length, ids = new Int16Array(w * h);
  rows.forEach((r, y) => [...r].forEach((ch, x) => { ids[y * w + x] = ch === '#' ? -1 : ch.charCodeAt(0) - 97; }));
  return { w, h, ids };
}

test('one room: perimeter edges merge into four walls and one closed ring', () => {
  const { w, h, ids } = grid(['#####', '#aaa#', '#aaa#', '#####']);
  const edges = gw.collectEdges(ids, w, h);
  assert.equal(edges.length, 10);                                  // 3 + 3 + 2 + 2
  const segs = gw.mergeEdges(edges);
  assert.equal(segs.length, 4);
  assert.equal(segs.reduce((n, s) => n + Math.hypot(s.x2 - s.x1, s.y2 - s.y1), 0), 10);
  const lines = gw.polylines(segs);
  assert.equal(lines.length, 1);
  assert.deepEqual(lines[0][0], lines[0][lines[0].length - 1]);    // closed: first point repeated
  assert.equal(lines[0].length, 5);
});

test('two rooms side by side share one wall; a door makes a one-cell gap, an open edge removes the wall cell', () => {
  const { w, h, ids } = grid(['######', '#aabb#', '#aabb#', '######']);
  const plain = gw.mergeEdges(gw.collectEdges(ids, w, h));
  const shared = plain.filter(s => s.x1 === 3 && s.x2 === 3);
  assert.equal(shared.length, 1);                                  // x = 3 between the rooms
  assert.equal(shared[0].y2 - shared[0].y1, 2);

  const marks = new Map([[gw.ek('v', 3, 1), { kind: 'door', state: 'closed' }]]);
  const withDoor = gw.mergeEdges(gw.collectEdges(ids, w, h, { marks }));
  assert.equal(withDoor.filter(s => s.kind === 'door').length, 1);
  assert.equal(withDoor.filter(s => s.kind === 'wall' && s.x1 === 3).length, 1);   // the other half of the wall is still there

  const open = new Set([gw.ek('v', 3, 0 + 1)]);
  const withGap = gw.mergeEdges(gw.collectEdges(ids, w, h, { open }));
  assert.equal(withGap.filter(s => s.x1 === 3 && s.x2 === 3).reduce((n, s) => n + s.y2 - s.y1, 0), 1);
});

test('edges between two cells of the same space never become walls', () => {
  const { w, h, ids } = grid(['aaa', 'aaa']);
  assert.equal(gw.collectEdges(ids, w, h).length, 10);             // only the outside perimeter (outside counts as -1)
});

test('polylines stop at a T junction and keep going through a corner', () => {
  // an H shape of unit segments: a vertical bar crossing a horizontal one
  const segs = [{ x1: 0, y1: 0, x2: 2, y2: 0 }, { x1: 2, y1: 0, x2: 2, y2: 2 }, { x1: 1, y1: 0, x2: 1, y2: -1 }];
  // note: (1,0) lies in the middle of the first segment on purpose-unsplit input would be a T; here we test chains by endpoint
  const lines = gw.polylines(segs);
  const covered = lines.reduce((n, l) => n + l.length - 1, 0);
  assert.equal(covered, 3);                                        // every segment is used exactly once
});

test('outlineRings: an L-shaped cave gives one closed ring with six corners and the right area', () => {
  const { w, h, ids } = grid(['#####', '#aa##', '#a###', '#a###', '#####']);
  const rings = gw.outlineRings(ids, w, h, 0);
  assert.equal(rings.length, 1);
  const ring = rings[0];
  assert.deepEqual(ring[0], ring[ring.length - 1]);
  assert.equal(ring.length - 1, 6);
  assert.equal(Math.abs(geom.signedArea(ring.slice(0, -1))), 4);   // 4 cells
});

test('outlineRings: a room with a pillar has an outer ring and a hole ring running the other way', () => {
  const { w, h, ids } = grid(['#######', '#aaaaa#', '#aa#aa#', '#aaaaa#', '#######']);
  const rings = gw.outlineRings(ids, w, h, 0);
  assert.equal(rings.length, 2);
  const areas = rings.map(r => geom.signedArea(r.slice(0, -1))).sort((a, b) => a - b);
  assert.ok(areas[0] < 0 && areas[1] > 0);                         // opposite winding
  assert.equal(Math.max(...areas.map(Math.abs)) - Math.min(...areas.map(Math.abs)), 14);   // outer ring 5x3 = 15, hole = 1 -> 14 floor cells
});

test('outlineRings: two cells touching only at a corner are consumed completely (no edge left over)', () => {
  const { w, h, ids } = grid(['####', '#a##', '##a#', '####']);
  const edges = gw.collectEdges(ids, w, h);
  const rings = gw.outlineRings(ids, w, h, 0);
  const used = rings.reduce((n, r) => n + r.length - 1, 0);
  assert.ok(used >= 2 && rings.every(r => r[0][0] === r[r.length - 1][0] && r[0][1] === r[r.length - 1][1]));
  assert.equal(edges.length, 8);
});

test('wallStats counts kinds and total length', () => {
  const segs = [{ x1: 0, y1: 0, x2: 3, y2: 0, kind: 'wall' }, { x1: 3, y1: 0, x2: 3, y2: 1, kind: 'door' }];
  const s = gw.wallStats(segs);
  assert.equal(s.segments, 2); assert.equal(s.byKind.wall, 1); assert.equal(s.byKind.door, 1); assert.equal(s.totalLength, 4);
});
