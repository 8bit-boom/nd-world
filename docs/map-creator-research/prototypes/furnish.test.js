'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const assets = require('./assets.js');
const furnish = require('./furnish.js');
const textures = require('./textures.js');
const R = require('./render-svg.js');
const gd = require('./gen-dungeon.js');
const { makeRng } = require('./rng.js');

const A = assets.ASSETS;

// tag balance for the tags we emit (a cheap well-formedness check; Chromium rendered all of these in the research)
function balanced(svg) {
  const stack = [], re = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(\/?)>/g;
  let m;
  while ((m = re.exec(svg))) {
    const [, close, tag, , self] = m;
    if (self) continue;
    if (!close) stack.push(tag);
    else if (stack.pop() !== tag) return false;
  }
  return stack.length === 0;
}

test('every prop is well formed: positive size, a body, a silhouette when it blocks, balanced tags', () => {
  assert.ok(assets.ids.length >= 20);
  for (const id of assets.ids) {
    const a = A[id];
    assert.ok(a.w > 0 && a.h > 0, id);
    assert.ok(a.body.length > 20, id);
    if (a.blocks && a.kind === 'furniture') assert.ok(a.sil.length > 10, `${id} needs a shadow silhouette`);
    assert.ok(balanced(assets.svgOf(id)), `${id} svg is not balanced`);
    assert.ok(!/<(script|image|foreignObject)/i.test(assets.svgOf(id)), `${id} must be plain shapes`);
  }
});

test('textures: patterns and filters are balanced and carry no external references', () => {
  const d = textures.defs();
  assert.ok(balanced(d));
  assert.ok(!/(href|src)=/.test(d) && !/url\((?!#)/.test(d), 'no external resources');
  for (const id of ['p-wood', 'p-stone', 'p-corridor', 'p-marble', 'p-dirt', 'p-wallstone', 'f-noise', 'f-grass', 'f-water', 'f-cave']) assert.ok(d.includes(`id="${id}"`), id);
});

function footprint(p) {
  const a = A[p.asset], w2 = Math.max(1, Math.round(a.w * 2)), h2 = Math.max(1, Math.round(a.h * 2)), swap = p.rot === 90 || p.rot === 270;
  const w = (swap ? h2 : w2) / 2, h = (swap ? w2 : h2) / 2;
  return { x0: p.cx - w / 2, y0: p.cy - h / 2, x1: p.cx + w / 2, y1: p.cy + h / 2 };
}
const overlap = (a, b) => a.x0 < b.x1 - 1e-9 && a.x1 > b.x0 + 1e-9 && a.y0 < b.y1 - 1e-9 && a.y1 > b.y0 + 1e-9;

function rooms(count, seed) {
  const m = gd.generateDungeon({ w: 60, h: 40, seed });
  return m.rooms.slice(0, count);
}

test('furnishing: nothing overlaps, nothing leaves the room, only known props and right-angle turns (all kinds, 30 rooms)', () => {
  for (const kind of furnish.KINDS) {
    for (let seed = 1; seed <= 30; seed++) {
      const room = rooms(8, seed)[seed % 8], f = furnish.furnish(room, kind, [], makeRng(seed));
      const blocking = f.placements.filter(p => !p.decor);
      for (const p of f.placements) {
        assert.ok(A[p.asset], `${kind}: unknown prop ${p.asset}`);
        assert.ok([0, 90, 180, 270].includes(p.rot));
        if (p.asset === 'torch_wall') continue;
        const fp = footprint(p);
        assert.ok(fp.x0 >= room.x - 1e-9 && fp.y0 >= room.y - 1e-9 && fp.x1 <= room.x + room.w + 1e-9 && fp.y1 <= room.y + room.h + 1e-9, `${kind} seed ${seed}: ${p.asset} leaves the room`);
      }
      for (let i = 0; i < blocking.length; i++) for (let j = i + 1; j < blocking.length; j++)
        assert.ok(!overlap(footprint(blocking[i]), footprint(blocking[j])), `${kind} seed ${seed}: ${blocking[i].asset} overlaps ${blocking[j].asset}`);
    }
  }
});

test('doorways stay clear: no prop within a cell and a half of a cell that touches a door', () => {
  for (const kind of furnish.KINDS) {
    for (let seed = 1; seed <= 20; seed++) {
      const room = rooms(8, seed)[seed % 8];
      const door = { x: room.x + Math.floor(room.w / 2), y: room.y };                    // a door in the middle of the top wall
      const f = furnish.furnish(room, kind, [door], makeRng(seed));
      const zone = { x0: door.x + 0.5 - 1.5, y0: door.y + 0.5 - 1.5, x1: door.x + 0.5 + 1.5, y1: door.y + 0.5 + 1.5 };
      for (const p of f.placements.filter(p => !p.decor)) assert.ok(!overlap(footprint(p), zone), `${kind} seed ${seed}: ${p.asset} blocks the doorway`);
    }
  }
});

test('same seed, same furniture; different seed, different furniture', () => {
  const room = rooms(3, 4)[0], a = furnish.furnish(room, 'tavern', [], makeRng(1)), b = furnish.furnish(room, 'tavern', [], makeRng(1)), c = furnish.furnish(room, 'tavern', [], makeRng(2));
  assert.deepEqual(a.placements, b.placements);
  assert.notDeepEqual(a.placements, c.placements);
});

test('a room big enough gets what its kind promises', () => {
  const big = { id: 0, x: 2, y: 2, w: 12, h: 8 };
  const has = (kind, asset) => furnish.furnish(big, kind, [], makeRng(3)).placements.some(p => p.asset === asset);
  assert.ok(has('tavern', 'bar_counter') && has('tavern', 'table_round') && has('tavern', 'fireplace'));
  assert.ok(has('library', 'bookshelf') && has('library', 'table_long'));
  assert.ok(has('shrine', 'altar') && has('shrine', 'brazier') && has('shrine', 'pillar'));
  assert.ok(has('barracks', 'bed_single') && has('barracks', 'weapon_rack'));
  assert.ok(has('storeroom', 'crate') && has('storeroom', 'barrel'));
  assert.ok(has('kitchen', 'cauldron') && has('kitchen', 'fireplace'));
  assert.ok(has('cavern', 'rock'));
});

test('a tiny room gets little and nothing breaks', () => {
  const tiny = { id: 0, x: 1, y: 1, w: 2, h: 2 };
  for (const kind of furnish.KINDS) { const f = furnish.furnish(tiny, kind, [], makeRng(1)); assert.ok(f.placements.length <= 6); }
});

test('torches come with lights at the same spot', () => {
  const f = furnish.furnish({ id: 0, x: 2, y: 2, w: 12, h: 8 }, 'tavern', [], makeRng(1));
  const torches = f.placements.filter(p => p.asset === 'torch_wall');
  assert.ok(torches.length >= 1 && torches.length === f.lights.length);
  torches.forEach((t, i) => { assert.equal(f.lights[i].x, t.cx); assert.equal(f.lights[i].y, t.cy); });
});

test('rendering a whole furnished dungeon gives a balanced, self-contained SVG', () => {
  const m = gd.generateDungeon({ w: 44, h: 30, seed: 5 }), segs = gd.wallsOf(m), rng = makeRng(9), kinds = furnish.assignKinds(m.rooms, rng);
  const placements = [];
  m.rooms.forEach(r => placements.push(...furnish.furnish(r, kinds[r.id], [], makeRng(r.id)).placements));
  const svg = R.render(m, segs, { kinds, placements, grid: true });
  assert.ok(balanced(svg));
  assert.ok(svg.startsWith('<svg') && svg.trimEnd().endsWith('</svg>'));
  assert.ok(!/<(script|image|foreignObject)/i.test(svg));
  assert.ok(svg.length < 200000, `${svg.length} bytes`);
  assert.equal(Object.keys(kinds).length, m.rooms.length);
});

test('assignKinds: the biggest room is the tavern, small rooms get small-room kinds', () => {
  const m = gd.generateDungeon({ w: 60, h: 40, seed: 3 }), kinds = furnish.assignKinds(m.rooms, makeRng(1));
  const biggest = m.rooms.slice().sort((a, b) => b.w * b.h - a.w * a.h)[0];
  assert.equal(kinds[biggest.id], 'tavern');
  m.rooms.filter(r => r.w * r.h < 20).forEach(r => assert.ok(['bedroom', 'storeroom'].includes(kinds[r.id])));
});
