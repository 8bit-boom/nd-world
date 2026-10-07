// Writes the generated test maps as JSON for vision_bench.py:  node export_for_py.js > /tmp/maps.json
'use strict';
const gd = require('./gen-dungeon.js'), gc = require('./gen-cave.js');
const V = require('./vision.js');
const { makeRng } = require('./rng.js');

function observers(map, rng, n) {
  if (map.rooms) return Array.from({ length: n }, () => { const r = rng.pick(map.rooms); return [r.x + 0.5 + rng.int(0, r.w - 1), r.y + 0.5 + rng.int(0, r.h - 1)]; });
  const floor = []; for (let i = 0; i < map.ids.length; i++) if (map.ids[i] >= 0) floor.push(i);
  return Array.from({ length: n }, () => { const c = rng.pick(floor); return [(c % map.w) + 0.5, Math.floor(c / map.w) + 0.5]; });
}

const out = [];
[['dungeon', 30, 20, 100], ['dungeon', 60, 40, 101], ['dungeon', 100, 70, 102], ['dungeon', 150, 100, 103], ['cave', 100, 70, 201]].forEach(([kind, w, h, seed]) => {
  const map = kind === 'dungeon' ? gd.generateDungeon({ w, h, seed }) : gc.generateCave({ w, h, seed });
  const segs = (kind === 'dungeon' ? gd.wallsOf(map) : gc.wallsOf(map)).map(s => [s.x1, s.y1, s.x2, s.y2, 0, 0, V.blocksSight(s)]);
  out.push({ name: `${kind} ${w}x${h}`, w, h, segs, observers: observers(map, makeRng(seed + 99), 6) });
});
console.log(JSON.stringify(out));
