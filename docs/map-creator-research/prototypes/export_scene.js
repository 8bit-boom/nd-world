// A generated dungeon as the ELEMENTS the existing schematic editor / player view already understand (rects, lines, tokens),
// plus the wall segments in pixels for fog-svg.js.        node export_scene.js [w] [h] [seed] [props] [cell] > scene.json
// Used by fog_spike_harness.py to drive the real /maps/schematic/{slug}/view page.
'use strict';
const gd = require('./gen-dungeon.js');
const { makeRng } = require('./rng.js');

const [w, h, seed, props, cell] = [60, 40, 101, 0, 50].map((d, i) => process.argv[2 + i] === undefined ? d : Number(process.argv[2 + i]));
const map = gd.generateDungeon({ w, h, seed }), segs = gd.wallsOf(map), rng = makeRng(seed + 7);
const px = v => v * cell;
let n = 0;
const id = p => `${p}${++n}`;
const els = [];

// floors: one rect per room, corridor cells merged into horizontal runs
map.rooms.forEach(r => els.push({ id: id('room'), type: 'rect', x: px(r.x), y: px(r.y), w: px(r.w), h: px(r.h), fill: '#2b2f3a', stroke: '#2b2f3a', strokeW: 0, opacity: 1, layer: 'Background' }));
for (let y = 0; y < h; y++) {
  let x = 0;
  while (x < w) {
    if (map.ids[y * w + x] !== map.corridorId) { x++; continue; }
    const x0 = x; while (x < w && map.ids[y * w + x] === map.corridorId) x++;
    els.push({ id: id('corr'), type: 'rect', x: px(x0), y: px(y), w: px(x - x0), h: px(1), fill: '#252a33', stroke: '#252a33', strokeW: 0, opacity: 1, layer: 'Background' });
  }
}
// walls and doors (a secret door looks like a wall to the players)
segs.forEach(s => {
  const door = s.kind === 'door';
  els.push({ id: id(door ? 'door' : 'wall'), type: 'line', x1: px(s.x1), y1: px(s.y1), x2: px(s.x2), y2: px(s.y2),
    stroke: door ? (s.state === 'open' ? '#5fbf6a' : '#e0a040') : '#9fb0c4', strokeW: door ? 6 : 4, opacity: 1, layer: 'Tracks' });
});
// scatter props inside rooms (what makes a real map heavy)
const floor = []; for (let i = 0; i < map.ids.length; i++) if (map.ids[i] >= 0) floor.push(i);
for (let i = 0; i < props; i++) {
  const c = rng.pick(floor), x = (c % w) * cell + rng.int(4, cell - 18), y = Math.floor(c / w) * cell + rng.int(4, cell - 18);
  els.push(i % 2 ? { id: id('prop'), type: 'rect', x, y, w: 12, h: 12, fill: '#5a4a3a', stroke: '#7a6a5a', strokeW: 1, opacity: 0.95, layer: 'Background' }
    : { id: id('prop'), type: 'circle', cx: x + 6, cy: y + 6, rx: 6, ry: 6, fill: '#3a6a3a', stroke: '#5a8a5a', strokeW: 1, opacity: 0.95, layer: 'Background' });
}
// tokens: the player's hero in the first room, six monsters elsewhere
const centre = r => [px(r.x + Math.floor(r.w / 2)) + cell / 2, px(r.y + Math.floor(r.h / 2)) + cell / 2];
const hero = centre(map.rooms[0]);
els.push({ id: 'tk-hero', type: 'token', x: hero[0], y: hero[1], r: 20, name: 'Hero', color: '#4488ff', source: 'pc', pc_id: '__PC__', hp: 12, max_hp: 12, opacity: 1, layer: 'Tracks' });
map.rooms.slice(1, 7).forEach((r, i) => { const c = centre(r); els.push({ id: 'tk-npc' + i, type: 'token', x: c[0], y: c[1], r: 20, name: 'Goblin ' + (i + 1), color: '#ff6666', source: 'entity', hp: 7, max_hp: 7, opacity: 1, layer: 'Tracks' }); });

console.log(JSON.stringify({
  canvas: { w: px(w), h: px(h) }, cell, elements: els, hero,
  walls: segs.map(s => ({ x1: px(s.x1), y1: px(s.y1), x2: px(s.x2), y2: px(s.y2), kind: s.kind, state: s.state })),
  rooms: map.rooms.map(r => ({ id: r.id, centre: centre(r) })),
}));
