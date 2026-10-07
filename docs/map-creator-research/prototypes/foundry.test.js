'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const U = require('./uvtt.js');
const F = require('./foundry.js');
const gd = require('./gen-dungeon.js');
const gw = require('./gridwalls.js');

const REAL = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'dungeondraft-1.0.1.3-sample.uvtt.json'), 'utf8'));

test('real Dungeondraft sample -> Foundry scene: counts, a hand-checked coordinate, and a valid v13 document', () => {
  const r = F.toScene(U.parse(REAL), { name: 'Sample', imageSrc: 'maps/sample.png' });
  assert.deepEqual(r.stats, { walls: 7, doors: 2, lights: 2, width: 2560, height: 2560, gridSize: 256 });   // 11 points in 4 polylines = 7 segments
  // file (7,2)-(7,2.5) with origin (2,1), 256 px per square, no padding -> (5,1)-(5,1.5) squares -> pixels
  assert.deepEqual(r.scene.walls[0].c, [1280, 256, 1280, 384]);
  assert.deepEqual(F.validateScene(r.scene), []);
  assert.deepEqual(r.scene.grid, { type: 1, size: 256, color: '#000000', alpha: 0.2, distance: 5, units: 'ft' });
});

test('doors: the closed one has ds 0, the open window-portal has ds 1; both are door 1 walls', () => {
  const r = F.toScene(U.parse(REAL));
  const doors = r.scene.walls.filter(w => w.door === 1);
  assert.deepEqual(doors.map(w => w.ds), [0, 1]);
  assert.ok(r.scene.walls.filter(w => w.door === 0).every(w => w.move === 20 && w.sight === 20 && w.light === 20 && w.sound === 20 && w.dir === 0));
});

test('lights: radius in squares x grid distance, bright is half of dim, colour without the alpha pair, integer position', () => {
  const r = F.toScene(U.parse(REAL), { gridDistance: 5 }), l = r.scene.lights[0];
  assert.equal(l.config.dim, 25); assert.equal(l.config.bright, 12.5); assert.equal(l.config.color, '#eccd8b');
  assert.ok(Number.isInteger(l.x) && Number.isInteger(l.y));
  assert.equal(l.config.shadows, 0.5);                                // a number 0..1 in Foundry, the file has a boolean
  assert.equal(F.toScene(U.parse(REAL), { gridDistance: 10 }).scene.lights[0].config.dim, 50);
});

test('padding: Foundry draws the map inside a padded canvas, so every coordinate shifts by ceil(width*padding/grid)*grid', () => {
  assert.deepEqual(F.paddingOffset(2560, 2560, 256, 0.25), { x: 768, y: 768 });      // ceil(2.5) * 256
  const r = F.toScene(U.parse(REAL), { padding: 0.25 });
  assert.deepEqual(r.scene.walls[0].c, [1280 + 768, 256 + 768, 1280 + 768, 384 + 768]);
  assert.equal(r.scene.initial.x, 1280 + 768);
});

test('light-blocking objects: skipped with a warning by default; imported as walls that block sight only, or everything', () => {
  const m = U.parse(REAL), n = m.objectWalls.reduce((s, l) => s + l.length - 1, 0);
  const skip = F.toScene(m), sight = F.toScene(m, { objectWalls: 'sight-only' }), all = F.toScene(m, { objectWalls: 'block' });
  assert.equal(skip.warnings.some(w => /light-blocking/.test(w)), true);
  assert.ok(sight.scene.walls.length > skip.scene.walls.length && sight.scene.walls.length <= skip.scene.walls.length + n);
  const extra = sight.scene.walls.filter(w => w.move === 0);
  assert.ok(extra.length > 0 && extra.every(w => w.sight === 20 && w.light === 20 && w.sound === 0));
  assert.ok(all.scene.walls.filter(w => w.move === 0).length === 0);
  assert.deepEqual(F.validateScene(sight.scene), []);
});

test('a decorative round object that blocks light is one outline of 60 points: simplified to a few walls, or kept whole on request', () => {
  const m = U.parse(REAL), geom = require('./geom.js');
  assert.equal(m.objectWalls[0].length, 60);                               // 0.79 squares across, with spikes
  const base = F.toScene(m).scene.walls.length;
  const extra = o => F.toScene(m, { objectWalls: 'sight-only', ...o }).scene.walls.length - base;
  assert.equal(extra({ objectTolerance: 0 }), 59);                          // every point kept: 59 tiny walls for one object
  const def = extra({});
  assert.ok(def >= 5 && def <= 30, `${def} walls with the default tolerance`);
  assert.ok(extra({ objectTolerance: 0.15 }) <= 8);
  // the simplified outline never strays further than the tolerance from the original points
  const kept = geom.simplify(m.objectWalls[0], 0.05);
  m.objectWalls[0].forEach(p => { let best = Infinity; for (let i = 0; i + 1 < kept.length; i++) best = Math.min(best, geom.lineDist(p, kept[i], kept[i + 1])); assert.ok(best <= 0.05 + 1e-9, 'a point strayed ' + best); });
});

test('the checker catches what v13 rejects', () => {
  const ok = F.toScene(U.parse(REAL)).scene, clone = () => JSON.parse(JSON.stringify(ok));
  let s = clone(); s.walls[0].c[0] = 1.5; assert.match(F.validateScene(s)[0], /4 integers/);
  s = clone(); s.walls[0].move = 10; assert.match(F.validateScene(s)[0], /move = 10/);          // v1 of the research claimed 10 was allowed for move
  s = clone(); s.walls[0].door = 3; assert.match(F.validateScene(s)[0], /door = 3/);
  s = clone(); s.walls[0].sight = 25; assert.match(F.validateScene(s)[0], /sight = 25/);
  s = clone(); s.grid.size = 19; assert.match(F.validateScene(s)[0], /grid\.size/);
  s = clone(); s.lights[0].x = 10.5; assert.match(F.validateScene(s)[0], /x\/y must be integers/);
  s = clone(); s.lights[0].config.shadows = true; assert.match(F.validateScene(s)[0], /shadows/);
  s = clone(); s.lights[0].config.alpha = 2; assert.match(F.validateScene(s)[0], /alpha/);
});

test('generated dungeons convert to valid scenes at several grid sizes, with every door and no zero-length wall', () => {
  for (const ppg of [50, 70, 100, 140]) {
    for (let seed = 1; seed <= 8; seed++) {
      const m = gd.generateDungeon({ w: 40, h: 28, seed }), segs = gd.wallsOf(m);
      const data = U.fromGrid(m.w, m.h, segs, gw.polylines, { ppg, lights: [{ x: m.rooms[0].cx + 0.5, y: m.rooms[0].cy + 0.5, range: 6, color: '#ffaa55', intensity: 1, alpha: 1, shadows: true }] });
      const r = F.toScene(U.parse(U.build(data, 'IMG')), { padding: 0.25 });
      assert.deepEqual(F.validateScene(r.scene), [], `ppg ${ppg} seed ${seed}`);
      assert.equal(r.stats.doors, segs.filter(s => s.kind === 'door').length);
      assert.ok(r.scene.walls.every(w => w.c[0] !== w.c[2] || w.c[1] !== w.c[3]));
    }
  }
});
