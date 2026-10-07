'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const V = require('./vision.js');
const geom = require('./geom.js');
const gd = require('./gen-dungeon.js');
const gc = require('./gen-cave.js');
const { makeRng } = require('./rng.js');

const box = (x0, y0, x1, y1, kind = 'wall') => [
  { x1: x0, y1: y0, x2: x1, y2: y0, kind }, { x1: x1, y1: y0, x2: x1, y2: y1, kind }, { x1: x1, y1: y1, x2: x0, y2: y1, kind }, { x1: x0, y1: y1, x2: x0, y2: y0, kind }];
const area = poly => Math.abs(geom.signedArea(poly));

test('in an empty closed room the visible area is the whole room', () => {
  const poly = V.visibilityPolygon([2.5, 2.5], box(0, 0, 6, 4));
  assert.ok(Math.abs(area(poly) - 24) < 1e-3, String(area(poly)));
});

test('range makes the view round: radius 2 in a big room is about pi*r^2', () => {
  const poly = V.visibilityPolygon([10, 10], box(0, 0, 20, 20), { range: 2 });
  assert.ok(Math.abs(area(poly) - Math.PI * 4) / (Math.PI * 4) < 0.05, String(area(poly)));
});

test('a wall casts a shadow: a pillar hides the cells behind it', () => {
  const room = box(0, 0, 10, 10), pillar = box(4, 4, 5, 5);
  const poly = V.visibilityPolygon([1, 4.5], room.concat(pillar));
  assert.equal(geom.pointInPolygon([8, 4.5], poly), false);        // straight behind the pillar
  assert.equal(geom.pointInPolygon([8, 8], poly), true);           // clear view
  assert.equal(geom.pointInPolygon([4.5, 4.5], poly), false);      // inside the pillar
});

test('doors: closed blocks sight, open does not; a window never blocks sight; secret doors look like walls', () => {
  const shell = box(0, 0, 8, 4);
  const wallWithGap = (kind, state) => shell.concat([{ x1: 4, y1: 0, x2: 4, y2: 1.5, kind: 'wall' }, { x1: 4, y1: 2.5, x2: 4, y2: 4, kind: 'wall' }, { x1: 4, y1: 1.5, x2: 4, y2: 2.5, kind, state }]);
  const sees = segs => geom.pointInPolygon([6, 2], V.visibilityPolygon([1, 2], segs));
  assert.equal(sees(wallWithGap('door', 'closed')), false);
  assert.equal(sees(wallWithGap('door', 'locked')), false);
  assert.equal(sees(wallWithGap('door', 'open')), true);
  assert.equal(sees(wallWithGap('window', 'closed')), true);
  assert.equal(sees(wallWithGap('secret', 'closed')), false);
});

test('movement: walls, closed doors and windows block; open doors do not', () => {
  const seg = (kind, state) => [{ x1: 4, y1: 0, x2: 4, y2: 4, kind, state }];
  const move = segs => V.moveBlocked([1, 2], [7, 2], segs);
  assert.equal(move(seg('wall')), true);
  assert.equal(move(seg('door', 'closed')), true);
  assert.equal(move(seg('door', 'open')), false);
  assert.equal(move(seg('window', 'closed')), true);
  assert.equal(move(seg('secret', 'closed')), true);
  assert.equal(V.moveBlocked([1, 2], [3, 2], seg('wall')), false);   // stops short of it
});

// The polygon against an independent oracle (does the straight line to a random point cross a wall?) on real generated maps.
function oracleMismatch(segs, origin, bounds, n, seed) {
  const poly = V.visibilityPolygon(origin, segs, { bounds }), rng = makeRng(seed);
  let missing = 0, extra = 0;
  for (let i = 0; i < n; i++) {
    const p = [bounds.x0 + rng.next() * (bounds.x1 - bounds.x0), bounds.y0 + rng.next() * (bounds.y1 - bounds.y0)];
    const seen = V.canSee(origin, p, segs), inside = geom.pointInPolygon(p, poly);
    if (seen && !inside) missing++;
    if (!seen && inside) extra++;
  }
  return { missing, extra, n };
}

test('dungeon vision matches the straight-line oracle (30 maps x 3 observers x 1500 points)', () => {
  let bad = 0, total = 0;
  for (let seed = 1; seed <= 30; seed++) {
    const m = gd.generateDungeon({ w: 40, h: 28, seed }), segs = gd.wallsOf(m), rng = makeRng(seed * 7);
    for (let k = 0; k < 3; k++) {
      const r = rng.pick(m.rooms), origin = [r.x + 0.5 + rng.int(0, r.w - 1), r.y + 0.5 + rng.int(0, r.h - 1)];
      const o = oracleMismatch(segs, origin, { x0: 0, y0: 0, x1: m.w, y1: m.h }, 1500, seed + k);
      bad += o.missing + o.extra; total += o.n;
    }
  }
  assert.ok(bad / total < 0.002, `${bad} of ${total} points disagree`);
});

test('cave vision matches the oracle too (diagonal walls)', () => {
  let bad = 0, total = 0;
  for (let seed = 1; seed <= 15; seed++) {
    const c = gc.generateCave({ w: 40, h: 28, seed }), segs = gc.wallsOf(c), rng = makeRng(seed);
    const floor = []; for (let i = 0; i < c.ids.length; i++) if (c.ids[i] >= 0) floor.push(i);
    for (let k = 0; k < 3; k++) {
      const cell = rng.pick(floor), origin = [(cell % c.w) + 0.5, Math.floor(cell / c.w) + 0.5];
      const o = oracleMismatch(segs, origin, { x0: 0, y0: 0, x1: c.w, y1: c.h }, 1500, seed * 3 + k);
      bad += o.missing + o.extra; total += o.n;
    }
  }
  assert.ok(bad / total < 0.004, `${bad} of ${total} points disagree`);
});

test('what a closed door hides, opening it shows - on a generated dungeon', () => {
  const m = gd.generateDungeon({ w: 40, h: 28, seed: 7 }), segs = gd.wallsOf(m);
  const door = segs.find(s => s.kind === 'door' && s.state === 'closed');
  assert.ok(door);
  const mid = [(door.x1 + door.x2) / 2, (door.y1 + door.y2) / 2], vertical = door.x1 === door.x2;
  const near = vertical ? [mid[0] - 1.2, mid[1]] : [mid[0], mid[1] - 1.2], far = vertical ? [mid[0] + 1.2, mid[1]] : [mid[0], mid[1] + 1.2];
  const bounds = { x0: 0, y0: 0, x1: m.w, y1: m.h };
  const closed = V.visibilityPolygon(near, segs, { bounds }), opened = V.visibilityPolygon(near, segs.map(s => s === door ? { ...s, state: 'open' } : s), { bounds });
  assert.equal(geom.pointInPolygon(far, closed), false);
  assert.equal(geom.pointInPolygon(far, opened), true);
});
