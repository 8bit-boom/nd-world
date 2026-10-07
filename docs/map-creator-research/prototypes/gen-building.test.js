'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const gb = require('./gen-building.js');
const gw = require('./gridwalls.js');

const TAVERN = {
  w: 24, h: 16, seed: 4, entrance: 0,
  rooms: [{ name: 'Common room', area: 90 }, { name: 'Kitchen', area: 40 }, { name: 'Cellar stair', area: 20 }, { name: 'Storeroom', area: 30 }, { name: 'Back room', area: 24 }],
  links: [[0, 1, 'door'], [0, 3, 'door'], [1, 2, 'open'], [3, 4, 'secret']],
};

test('the rooms tile the footprint with no gaps and no overlaps', () => {
  const b = gb.generateBuilding(TAVERN);
  assert.ok(Array.from(b.ids).every(v => v >= 0), 'no cell left outside');
  assert.equal(b.rooms.reduce((n, r) => n + r.w * r.h, 0), b.w * b.h);
  assert.equal(new Set(Array.from(b.ids)).size, TAVERN.rooms.length);
});

test('room areas follow the requested proportions', () => {
  const b = gb.generateBuilding(TAVERN), total = TAVERN.rooms.reduce((n, r) => n + r.area, 0);
  b.rooms.forEach(r => {
    const want = TAVERN.rooms[r.id].area / total, got = (r.w * r.h) / (b.w * b.h);
    assert.ok(Math.abs(want - got) < 0.08, `${r.name}: wanted ${want.toFixed(2)}, got ${got.toFixed(2)}`);
  });
});

test('a link between rooms that share a wall gets its door, a link between rooms that do not touch is reported', () => {
  const b = gb.generateBuilding(TAVERN);
  const doors = [...b.marks.values()].filter(m => m.kind === 'door').length, secrets = [...b.marks.values()].filter(m => m.kind === 'secret').length;
  assert.ok(doors >= 1);
  const wanted = TAVERN.links.length, met = wanted - b.unmet.length;
  assert.equal(b.marks.size - [...b.marks.values()].filter(m => m.kind === 'window').length + b.open.size, met + 1 /* entrance */);
  assert.ok(secrets <= 1);
  const far = gb.generateBuilding({ ...TAVERN, links: [[0, 1, 'door'], [1, 4, 'door']] });
  // whether 1 and 4 touch depends on the layout; whatever is reported must really not share an edge
  far.unmet.forEach(([a, c]) => {
    if (c === -1) return;
    const shared = gw.collectEdges(far.ids, far.w, far.h).some(e => (e.a === a && e.b === c) || (e.a === c && e.b === a));
    assert.equal(shared, false);
  });
});

test('the entrance is a door on the outside wall, windows never replace doors', () => {
  const b = gb.generateBuilding(TAVERN);
  const edges = gw.collectEdges(b.ids, b.w, b.h, { open: b.open, marks: b.marks });
  const exteriorDoors = edges.filter(e => e.kind === 'door' && (e.a === -1 || e.b === -1));
  assert.equal(exteriorDoors.length, 1);
  assert.ok([exteriorDoors[0].a, exteriorDoors[0].b].includes(TAVERN.entrance));
  edges.filter(e => e.kind === 'window').forEach(e => assert.ok(e.a === -1 || e.b === -1));
});

test('same seed, same building', () => {
  const a = gb.generateBuilding(TAVERN), b = gb.generateBuilding(TAVERN);
  assert.deepEqual([...a.marks], [...b.marks]);
  assert.deepEqual(Array.from(a.ids), Array.from(b.ids));
});
