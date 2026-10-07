'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const gd = require('./gen-dungeon.js');
const gw = require('./gridwalls.js');

// can a walker get from every room to every other room, passing only through cells of the same space or through
// door / opening edges (a closed door is passable here: it is a door, not a wall)
function connected(map) {
  const { w, h, ids } = map, passable = new Set([...map.marks.keys(), ...map.open]);
  const seen = new Uint8Array(w * h), start = ids.findIndex(v => v >= 0), stack = [start];
  seen[start] = 1;
  while (stack.length) {
    const c = stack.pop(), x = c % w, y = (c - x) / w;
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = x + dx, ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
      const n = ny * w + nx;
      if (seen[n] || ids[n] < 0) continue;
      if (ids[n] !== ids[c]) {
        const key = dx !== 0 ? gw.ek('v', Math.max(x, nx), y) : gw.ek('h', x, Math.max(y, ny));
        if (!passable.has(key)) continue;
      }
      seen[n] = 1; stack.push(n);
    }
  }
  return ids.every((v, i) => v < 0 || seen[i]);
}

test('same seed, same dungeon; another seed, another dungeon', () => {
  const a = gd.generateDungeon({ w: 40, h: 28, seed: 7 }), b = gd.generateDungeon({ w: 40, h: 28, seed: 7 }), c = gd.generateDungeon({ w: 40, h: 28, seed: 8 });
  assert.deepEqual(Array.from(a.ids), Array.from(b.ids));
  assert.deepEqual([...a.marks], [...b.marks]);
  assert.notDeepEqual(Array.from(a.ids), Array.from(c.ids));
});

test('rooms never overlap, stay inside the map and keep a gap between them', () => {
  for (let seed = 1; seed <= 25; seed++) {
    const m = gd.generateDungeon({ w: 50, h: 36, seed });
    for (const r of m.rooms) {
      assert.ok(r.x >= 2 && r.y >= 2 && r.x + r.w <= m.w - 2 && r.y + r.h <= m.h - 2, `seed ${seed}: room inside`);
      for (const o of m.rooms) if (o.id < r.id) assert.ok(r.x >= o.x + o.w + 2 || r.x + r.w + 2 <= o.x || r.y >= o.y + o.h + 2 || r.y + r.h + 2 <= o.y, `seed ${seed}: gap`);
    }
  }
});

test('every room can be reached from every other (25 seeds, three sizes)', () => {
  for (const [w, h] of [[30, 20], [50, 36], [80, 60]]) {
    for (let seed = 1; seed <= 25; seed++) {
      const m = gd.generateDungeon({ w, h, seed });
      assert.ok(m.rooms.length >= 2, `seed ${seed}: has rooms`);
      assert.ok(connected(m), `${w}x${h} seed ${seed} is not connected`);
    }
  }
});

test('doors only sit between a room and a corridor, never inside a room', () => {
  for (let seed = 1; seed <= 25; seed++) {
    const m = gd.generateDungeon({ w: 50, h: 36, seed });
    const edges = gw.collectEdges(m.ids, m.w, m.h, { open: m.open, marks: m.marks });
    for (const e of edges.filter(e => e.kind !== 'wall')) {
      const roomSide = [e.a, e.b].filter(v => v >= 0 && v < m.corridorId).length, corrSide = [e.a, e.b].filter(v => v === m.corridorId).length;
      assert.equal(roomSide, 1); assert.equal(corrSide, 1);
    }
    // every mark / open key really is an edge between two different spaces
    for (const key of [...m.marks.keys(), ...m.open]) {
      const hit = gw.collectEdges(m.ids, m.w, m.h).some(e => gw.ek(e.k, e.x, e.y) === key);
      assert.ok(hit, `seed ${seed}: ${key} is not an edge`);
    }
  }
});

test('walls: merged segments are axis aligned, integer, inside the map and never inside a room', () => {
  const m = gd.generateDungeon({ w: 50, h: 36, seed: 3 }), segs = gd.wallsOf(m);
  for (const s of segs) {
    assert.ok(Number.isInteger(s.x1) && Number.isInteger(s.y1) && Number.isInteger(s.x2) && Number.isInteger(s.y2));
    assert.ok(s.x1 === s.x2 || s.y1 === s.y2);
    assert.ok(Math.min(s.x1, s.x2) >= 0 && Math.max(s.x1, s.x2) <= m.w && Math.min(s.y1, s.y2) >= 0 && Math.max(s.y1, s.y2) <= m.h);
  }
  const stats = gw.wallStats(segs);
  assert.ok(stats.byKind.wall > 0);
  assert.equal(stats.byKind.door + stats.byKind.secret, m.marks.size);
  // merging really shortens the list: far fewer segments than unit edges
  const unit = gw.collectEdges(m.ids, m.w, m.h, { open: m.open, marks: m.marks }).length;
  assert.ok(segs.length < unit * 0.6, `${segs.length} segments for ${unit} unit edges`);
});

test('a wall never separates cells of the same space (no wall inside a room)', () => {
  const m = gd.generateDungeon({ w: 40, h: 28, seed: 11 });
  for (const e of gw.collectEdges(m.ids, m.w, m.h, { open: m.open, marks: m.marks })) assert.notEqual(e.a, e.b);
});
