// Writes the pictures used in the research: the prop set, the textures, and a furnished dungeon.
//   node make_sheets.js OUT_DIR [seed]        then  python3 rasterize.py OUT_DIR   (Chromium turns the SVGs into PNGs)
'use strict';
const fs = require('node:fs'), path = require('node:path');
const assets = require('./assets.js'), textures = require('./textures.js'), furnish = require('./furnish.js'), R = require('./render-svg.js');
const gd = require('./gen-dungeon.js'), gw = require('./gridwalls.js'), gc = require('./gen-cave.js');
const { makeRng } = require('./rng.js');

const out = process.argv[2] || '.', seed = Number(process.argv[3] || 5);
fs.mkdirSync(out, { recursive: true });

// 1. the prop set
(function () {
  const ids = assets.ids, cols = 6, cellW = 360, cellH = 300, rows = Math.ceil(ids.length / cols);
  let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${cols * cellW} ${rows * cellH}" width="${cols * cellW / 2}" height="${rows * cellH / 2}">` + textures.defs() + `<rect width="100%" height="100%" fill="#2b2f3a"/><rect width="100%" height="100%" fill="url(#p-stone)" opacity=".55"/>`;
  ids.forEach((id, i) => {
    const a = assets.ASSETS[id], x = (i % cols) * cellW, y = Math.floor(i / cols) * cellH, s = Math.min(1.7, 200 / (Math.max(a.w, a.h) * 100));
    svg += `<g transform="translate(${x + cellW / 2} ${y + 130}) scale(${s}) translate(${-a.w * 50} ${-a.h * 50})">${assets.svgOf(id)}</g>`;
    svg += `<text x="${x + cellW / 2}" y="${y + 275}" text-anchor="middle" font-family="sans-serif" font-size="22" fill="#e6e8ee">${a.name} (${a.w}x${a.h})</text>`;
  });
  fs.writeFileSync(path.join(out, 'props.svg'), svg + '</svg>');
})();

// 2. the textures
(function () {
  const items = [['p-wood', 'wood planks'], ['p-stone', 'stone flags'], ['p-corridor', 'corridor flags'], ['p-marble', 'marble'], ['p-dirt', 'packed earth'], ['p-wallstone', 'wall blocks']];
  const filters = [['f-grass', 'grass'], ['f-water', 'water'], ['f-cave', 'cave rock']];
  let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 780" width="1200" height="780">` + textures.defs() + `<rect width="1200" height="780" fill="#1b1d23"/>`;
  items.forEach(([id, name], i) => {
    const x = 20 + (i % 3) * 390, y = 20 + Math.floor(i / 3) * 250;
    svg += `<rect x="${x}" y="${y}" width="360" height="190" fill="url(#${id})" stroke="#000" stroke-width="3"/>` +
      (id === 'p-wood' || id === 'p-stone' ? `<rect x="${x}" y="${y}" width="360" height="190" fill="#000" filter="url(#f-noise)" style="mix-blend-mode:multiply" opacity=".5"/>` : '') +
      `<text x="${x}" y="${y + 215}" font-family="sans-serif" font-size="20" fill="#e6e8ee">${name}</text>`;
  });
  filters.forEach(([id, name], i) => {
    const x = 20 + i * 390;
    svg += `<rect x="${x}" y="540" width="360" height="170" fill="#000" filter="url(#${id})"/><text x="${x}" y="735" font-family="sans-serif" font-size="20" fill="#e6e8ee">${name} (feTurbulence filter)</text>`;
  });
  fs.writeFileSync(path.join(out, 'textures.svg'), svg + '</svg>');
})();

// 3. a furnished dungeon
(function () {
  const map = gd.generateDungeon({ w: 44, h: 30, seed }), segs = gd.wallsOf(map), rng = makeRng(seed + 1000);
  const kinds = furnish.assignKinds(map.rooms, rng), placements = [], lights = [];
  // cells of each room that touch a door or an opening
  const edgeCells = new Map();
  [...map.marks.keys(), ...map.open].forEach(key => {
    const k = key[0], [ex, ey] = key.slice(1).split(',').map(Number);
    const cells = k === 'h' ? [[ex, ey - 1], [ex, ey]] : [[ex - 1, ey], [ex, ey]];
    cells.forEach(([cx, cy]) => { const id = map.ids[cy * map.w + cx]; if (id >= 0 && id < map.corridorId) (edgeCells.get(id) || edgeCells.set(id, []).get(id)).push({ x: cx, y: cy }); });
  });
  map.rooms.forEach(r => {
    const f = furnish.furnish(r, kinds[r.id], edgeCells.get(r.id) || [], makeRng(seed * 100 + r.id));
    placements.push(...f.placements); lights.push(...f.lights);
  });
  fs.writeFileSync(path.join(out, 'furnished-dungeon.svg'), R.render(map, segs, { kinds, placements, grid: true, width: 1760 }));
  fs.writeFileSync(path.join(out, 'furnished-dungeon.json'), JSON.stringify({ seed, kinds, placements: placements.length, lights: lights.length, walls: segs.length }, null, 1));
  console.log(JSON.stringify({ rooms: map.rooms.length, placements: placements.length, lights: lights.length, kinds }));
})();

// 4. a building from a room graph (what a language model would hand over), furnished
(function () {
  const gb = require('./gen-building.js');
  const spec = {
    w: 26, h: 16, seed: 4, entrance: 0,
    rooms: [{ name: 'Common room', area: 100 }, { name: 'Kitchen', area: 44 }, { name: 'Cellar stair', area: 22 }, { name: 'Storeroom', area: 34 }, { name: 'Back room', area: 30 }],
    links: [[0, 1, 'door'], [0, 3, 'door'], [1, 2, 'open'], [3, 4, 'secret']],
  };
  const b = gb.generateBuilding(spec), segs = gb.wallsOf(b);
  const kinds = { 0: 'tavern', 1: 'kitchen', 2: 'storeroom', 3: 'storeroom', 4: 'bedroom' };
  const entr = new Map();
  [...b.marks.keys(), ...b.open].forEach(key => {
    const k = key[0], [ex, ey] = key.slice(1).split(',').map(Number);
    (k === 'h' ? [[ex, ey - 1], [ex, ey]] : [[ex - 1, ey], [ex, ey]]).forEach(([cx, cy]) => {
      if (cx < 0 || cy < 0 || cx >= b.w || cy >= b.h) return;
      const id = b.ids[cy * b.w + cx]; if (id >= 0) (entr.get(id) || entr.set(id, []).get(id)).push({ x: cx, y: cy });
    });
  });
  const placements = [];
  b.rooms.forEach(r => placements.push(...furnish.furnish(r, kinds[r.id], entr.get(r.id) || [], makeRng(40 + r.id)).placements));
  const map = { w: b.w, h: b.h, ids: b.ids, rooms: b.rooms, corridorId: -99 };
  fs.writeFileSync(path.join(out, 'building.svg'), R.render(map, segs, { kinds, placements, grid: true, width: 1300, gmMarks: true }));
  console.log(JSON.stringify({ building: { rooms: b.rooms.map(r => `${r.name} ${r.w}x${r.h}`), unmet: b.unmet, walls: segs.length } }));
})();

// 5. a cave: chamfered outline, rocks, textured floor
(function () {
  const cave = gc.generateCave({ w: 48, h: 32, seed: 12 }), rng = makeRng(77), U = 100;
  const d = cave.rings.map(r => 'M' + r.slice(0, -1).map(p => `${p[0] * U} ${p[1] * U}`).join('L') + 'Z').join('');
  const floor = []; for (let i = 0; i < cave.ids.length; i++) if (cave.ids[i] >= 0) floor.push(i);
  let rocks = '';
  for (let i = 0; i < Math.floor(floor.length / 28); i++) {
    const c = rng.pick(floor), cx = (c % cave.w) + 0.5, cy = Math.floor(c / cave.w) + 0.5, a = assets.ASSETS.rock;
    rocks += `<g transform="translate(${cx * U} ${cy * U}) rotate(${rng.int(0, 3) * 90}) scale(${0.55 + rng.next() * 0.5}) translate(${-a.w * 50} ${-a.h * 50})">${assets.svgOf('rock')}</g>`;
  }
  const W = cave.w * U, H = cave.h * U;
  fs.writeFileSync(path.join(out, 'cave.svg'), `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="1300" height="${Math.round(1300 * H / W)}">` + textures.defs() +
    `<rect width="${W}" height="${H}" fill="#1a1612"/><rect width="${W}" height="${H}" fill="#000" filter="url(#f-cave)" opacity=".55"/>` +
    `<path d="${d}" fill="none" stroke="#000" stroke-opacity=".4" stroke-width="40" stroke-linejoin="round" transform="translate(5 8)"/>` +
    `<path d="${d}" fill="url(#p-dirt)" fill-rule="evenodd" stroke="#2c241b" stroke-width="30" stroke-linejoin="round"/><path d="${d}" fill="none" stroke="#5a4a38" stroke-width="8" stroke-linejoin="round"/>` +
    `<path d="${d}" fill="#000" fill-rule="evenodd" filter="url(#f-noise)" style="mix-blend-mode:multiply" opacity=".0"/>` + rocks + '</svg>');
  console.log(JSON.stringify({ cave: { floorCells: cave.floorCells, wallSegments: gc.wallsOf(cave).length, rocks: Math.floor(floor.length / 28) } }));
})();
