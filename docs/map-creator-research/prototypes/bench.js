// Measurements on REALISTIC maps (generated dungeons and caves, not random wall soup): how many walls a map really has, what a
// vision update costs, how big the exports are.   node bench.js            (VP_PATH=/path/to/visibility-polygon to compare with it)
'use strict';
const zlib = require('node:zlib');
const gd = require('./gen-dungeon.js'), gc = require('./gen-cave.js'), gw = require('./gridwalls.js');
const V = require('./vision.js'), U = require('./uvtt.js'), F = require('./foundry.js');
const { makeRng } = require('./rng.js');

let vp = null;
try { if (process.env.VP_PATH) vp = require(process.env.VP_PATH); } catch (e) { console.error('visibility-polygon not loaded:', e.message); }

const ms = () => Number(process.hrtime.bigint()) / 1e6;
function median(fn, reps = 7) { fn(); const t = []; for (let i = 0; i < reps; i++) { const a = ms(); fn(); t.push(ms() - a); } t.sort((x, y) => x - y); return t[reps >> 1]; }
const f1 = n => (Math.round(n * 10) / 10).toString();
const kb = n => f1(n / 1024);

function observers(map, n, rng) {
  if (map.rooms) return Array.from({ length: n }, () => { const r = rng.pick(map.rooms); return [r.x + 0.5 + rng.int(0, r.w - 1), r.y + 0.5 + rng.int(0, r.h - 1)]; });
  const floor = []; for (let i = 0; i < map.ids.length; i++) if (map.ids[i] >= 0) floor.push(i);
  return Array.from({ length: n }, () => { const c = rng.pick(floor); return [(c % map.w) + 0.5, Math.floor(c / map.w) + 0.5]; });
}

function vpSegments(segs, w, h) {   // the library wants [[x1,y1],[x2,y2]] pairs, with the map border, intersections broken
  const pairs = segs.filter(V.blocksSight).map(s => [[s.x1, s.y1], [s.x2, s.y2]]);
  pairs.push([[0, 0], [w, 0]], [[w, 0], [w, h]], [[w, h], [0, h]], [[0, h], [0, 0]]);
  return vp.breakIntersections(pairs);
}

const rows = [];
function run(kind, w, h, seed) {
  const t0 = ms();
  const map = kind === 'dungeon' ? gd.generateDungeon({ w, h, seed }) : gc.generateCave({ w, h, seed });
  const genMs = ms() - t0;
  const segs = kind === 'dungeon' ? gd.wallsOf(map) : gc.wallsOf(map);
  const stats = gw.wallStats(segs);
  const bounds = { x0: 0, y0: 0, x1: w, y1: h }, rng = makeRng(seed + 99), obs = observers(map, 6, rng);
  const sight = segs.filter(V.blocksSight);
  const t = {
    unlimited1: median(() => V.visibilityPolygon(obs[0], segs, { bounds })),
    unlimited6: median(() => obs.forEach(o => V.visibilityPolygon(o, segs, { bounds }))),
    r16x6: median(() => obs.forEach(o => V.visibilityPolygon(o, segs, { bounds, range: 16 }))),
    r8x6: median(() => obs.forEach(o => V.visibilityPolygon(o, segs, { bounds, range: 8 }))),
  };
  if (vp) {
    const broken = vpSegments(segs, w, h);
    t.libBreak = median(() => vpSegments(segs, w, h), 3);
    t.lib1 = median(() => vp.compute(obs[0], broken));
    t.lib6 = median(() => obs.forEach(o => vp.compute(o, broken)));
  }
  // exports (no image: the PNG dominates every file and is the same whatever the walls are)
  const data = U.fromGrid(w, h, segs, gw.polylines, { ppg: 70 });
  const uvtt = JSON.stringify(U.build(data, ''));
  const scene = JSON.stringify(F.toScene(U.parse(JSON.parse(uvtt)), { padding: 0 }).scene);
  rows.push({
    kind, w, h, cells: w * h, floorCells: kind === 'dungeon' ? Array.from(map.ids).filter(v => v >= 0).length : map.floorCells,
    rooms: map.rooms ? map.rooms.length : null, genMs, segments: stats.segments, byKind: stats.byKind, sightBlockers: sight.length,
    polylines: data.walls.length, t, uvttBytes: uvtt.length, uvttGz: zlib.gzipSync(uvtt).length, sceneBytes: scene.length, sceneGz: zlib.gzipSync(scene).length,
    px70: [w * 70, h * 70],
  });
}

[[30, 20], [60, 40], [100, 70], [150, 100], [200, 140]].forEach(([w, h], i) => run('dungeon', w, h, 100 + i));
[[48, 32], [100, 70], [160, 110]].forEach(([w, h], i) => run('cave', w, h, 200 + i));

if (process.argv.includes('--json')) { console.log(JSON.stringify(rows, null, 1)); process.exit(0); }
console.log('node', process.version, '| medians of 7 runs, ms | observers: 6 per map | "lib" = visibility-polygon 1.1.0 ' + (vp ? '(loaded)' : '(not loaded)') + '\n');
console.log('| map | cells | rooms | wall segments (walls+doors) | sight blockers | UVTT polylines | gen ms | 1 view ms | 6 views ms | 6 views r=16 ms | 6 views r=8 ms |' + (vp ? ' lib 1 view | lib 6 views | lib one-off break ms |' : ''));
rows.forEach(r => {
  const d = r.byKind;
  console.log(`| ${r.kind} ${r.w}x${r.h} | ${r.cells} | ${r.rooms === null ? '-' : r.rooms} | ${r.segments} (${d.wall}+${d.door + d.secret + d.window}) | ${r.sightBlockers} | ${r.polylines} | ${f1(r.genMs)} | ${f1(r.t.unlimited1)} | ${f1(r.t.unlimited6)} | ${f1(r.t.r16x6)} | ${f1(r.t.r8x6)} |` +
    (vp ? ` ${f1(r.t.lib1)} | ${f1(r.t.lib6)} | ${f1(r.t.libBreak)} |` : ''));
});
console.log('\n| map | UVTT JSON (no image) KB | gzip KB | Foundry scene JSON KB | gzip KB | image px at 70 px/cell |');
rows.forEach(r => console.log(`| ${r.kind} ${r.w}x${r.h} | ${kb(r.uvttBytes)} | ${kb(r.uvttGz)} | ${kb(r.sceneBytes)} | ${kb(r.sceneGz)} | ${r.px70[0]} x ${r.px70[1]} |`));
