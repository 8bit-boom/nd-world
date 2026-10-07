'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const U = require('./uvtt.js');
const gd = require('./gen-dungeon.js');
const gw = require('./gridwalls.js');

const REAL = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'dungeondraft-1.0.1.3-sample.uvtt.json'), 'utf8'));

test('the real Dungeondraft export is valid and reads as expected', () => {
  const v = U.validate(REAL);
  assert.deepEqual(v.errors, []);
  assert.deepEqual(v.warnings, ['no embedded image']);          // the image was left out of the fixture on purpose
  const m = U.parse(REAL);
  assert.equal(m.ppg, 256); assert.equal(m.w, 10); assert.equal(m.h, 10);
  assert.deepEqual(m.origin, { x: 2, y: 1 });
  assert.equal(m.walls.length, 4); assert.equal(m.objectWalls.length, 1); assert.equal(m.portals.length, 2); assert.equal(m.lights.length, 2);
});

test('coordinates in the file are absolute: the map-relative values are the file values minus map_origin', () => {
  const m = U.parse(REAL);
  assert.deepEqual(m.walls[0], [[5, 1], [5, 1.5]]);              // file: (7,2)-(7,2.5), origin (2,1)
  assert.deepEqual([m.portals[0].x, m.portals[0].y], [5, 2]);     // file: (7,3)
  const xs = REAL.line_of_sight.flat().map(p => p.x), ys = REAL.line_of_sight.flat().map(p => p.y);
  assert.ok(Math.min(...xs) >= 2 && Math.max(...xs) <= 12 && Math.min(...ys) >= 1 && Math.max(...ys) <= 11);   // inside origin..origin+size
});

test('portals: the closed door and the open window both come through; rotation is in radians', () => {
  const m = U.parse(REAL);
  assert.equal(m.portals[0].closed, true); assert.equal(m.portals[1].closed, false);
  assert.ok(Math.abs(m.portals[0].rotation - Math.PI / 2) < 1e-5);
});

test('lights: ARGB "aarrggbb" becomes #rrggbb plus alpha, and back', () => {
  const m = U.parse(REAL);
  assert.equal(m.lights[0].color, '#eccd8b'); assert.equal(m.lights[0].alpha, 1);
  assert.equal(m.lights[1].color, '#4dd569');
  const back = U.build(m, 'IMG');
  assert.equal(back.lights[0].color, 'ffeccd8b');
});

test('read -> write gives the same file (everything but the image, numbers to 6 decimals)', () => {
  const back = U.build(U.parse(REAL), '');
  const strip = d => JSON.parse(JSON.stringify({ ...d, image: '' }));
  const norm = o => JSON.parse(JSON.stringify(o, (k, v) => typeof v === 'number' ? Math.round(v * 1e6) / 1e6 : v));
  assert.deepEqual(norm(strip(back)), norm(strip(REAL)));
});

test('a writer for a file with no map_origin / no lights still reads (the optional parts default)', () => {
  const m = U.parse({ format: 0.2, resolution: { map_size: { x: 4, y: 3 }, pixels_per_grid: 70 }, line_of_sight: [[{ x: 0, y: 0 }, { x: 4, y: 0 }]], portals: [], image: 'x' });
  assert.deepEqual(m.origin, { x: 0, y: 0 }); assert.equal(m.lights.length, 0); assert.equal(m.objectWalls.length, 0);
});

test('bad files are refused with a reason, odd ones are accepted with a warning', () => {
  assert.throws(() => U.parse({ format: 0.3 }), /resolution/);
  assert.throws(() => U.parse({ format: 'x', resolution: { map_size: { x: 1, y: 1 }, pixels_per_grid: 70 } }), /format/);
  assert.throws(() => U.parse({ format: 0.3, resolution: { map_size: { x: 1, y: 1 }, pixels_per_grid: 70 }, line_of_sight: [[{ x: 0, y: 0 }]] }), /line_of_sight/);
  assert.throws(() => U.parse({ format: 0.3, resolution: { map_size: { x: 1, y: 1 }, pixels_per_grid: 70 }, line_of_sight: [[{ x: NaN, y: 0 }, { x: 1, y: 1 }]] }), /line_of_sight/);
  const v = U.validate({ format: 0.4, resolution: { map_size: { x: 1, y: 1 }, pixels_per_grid: 70.5 }, image: 'x', objects_line_of_sight: [] });
  assert.deepEqual(v.errors, []); assert.equal(v.warnings.length, 2);
});

// -- a generated map out and back: no wall gained, none lost ---------------------------------------------------
function unitEdges(segs) {                 // every wall as unit lattice edges, with its kind
  const out = new Map();
  segs.forEach(s => {
    const n = Math.max(Math.abs(s.x2 - s.x1), Math.abs(s.y2 - s.y1)), dx = Math.sign(s.x2 - s.x1), dy = Math.sign(s.y2 - s.y1);
    for (let i = 0; i < n; i++) {
      const x1 = s.x1 + dx * i, y1 = s.y1 + dy * i, x2 = x1 + dx, y2 = y1 + dy;
      const k = x1 < x2 || (x1 === x2 && y1 < y2) ? `${x1},${y1}-${x2},${y2}` : `${x2},${y2}-${x1},${y1}`;
      out.set(k, s.kind || 'wall');
    }
  });
  return out;
}

test('generated dungeon -> UVTT -> read back: the same walls, the same doors (20 seeds)', () => {
  for (let seed = 1; seed <= 20; seed++) {
    const m = gd.generateDungeon({ w: 40, h: 28, seed }), segs = gd.wallsOf(m);
    const data = U.fromGrid(m.w, m.h, segs, gw.polylines, { ppg: 70 });
    const file = U.build(data, 'IMG'), back = U.parse(JSON.parse(JSON.stringify(file)));
    const again = U.toSegments(back);
    const want = unitEdges(segs.filter(s => s.kind !== 'secret' && s.kind !== 'window').map(s => ({ ...s, kind: s.kind === 'door' ? 'door' : 'wall' })));
    const secretsAsWalls = unitEdges(segs.filter(s => s.kind === 'secret').map(s => ({ ...s, kind: 'wall' })));
    secretsAsWalls.forEach((v, k) => want.set(k, v));
    const got = unitEdges(again.map(s => ({ ...s, kind: s.kind === 'door' ? 'door' : 'wall' })));
    assert.deepEqual([...got].sort(), [...want].sort(), `seed ${seed}`);
    const doors = segs.filter(s => s.kind === 'door').length;
    assert.equal(back.portals.length, doors);
    assert.equal(back.portals.filter(p => p.closed).length, segs.filter(s => s.kind === 'door' && s.state !== 'open').length);
    assert.deepEqual(U.validate(file).errors, []);
  }
});

test('windows: exported as walls by default (with a warning) or as open gaps on request; secret doors become walls with a warning', () => {
  const segs = [{ x1: 0, y1: 0, x2: 3, y2: 0, kind: 'wall' }, { x1: 3, y1: 0, x2: 4, y2: 0, kind: 'window', state: 'closed' }, { x1: 4, y1: 0, x2: 5, y2: 0, kind: 'secret', state: 'closed' }];
  const asWall = U.fromGrid(8, 4, segs, gw.polylines, {}), asOpen = U.fromGrid(8, 4, segs, gw.polylines, { windows: 'open' });
  assert.equal(asWall.portals.length, 0); assert.equal(asWall.warnings.length, 2);
  assert.equal(asWall.walls.length, 1); assert.deepEqual(asWall.walls[0], [[0, 0], [5, 0]]);     // one straight run: wall, window and secret door joined
  assert.equal(asOpen.portals.length, 1); assert.equal(asOpen.portals[0].closed, false);
});
