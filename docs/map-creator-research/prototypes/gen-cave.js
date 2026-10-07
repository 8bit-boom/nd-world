// A seeded cave: cellular automata on a random mask, only the largest connected cavern is kept, and the walls are the
// smoothed outline of that cavern (closed rings, diagonals instead of a staircase).
//
//   generateCave({ w, h, seed, fill, steps, style, smooth }) -> { w, h, ids, rings, seed, floorCells }
//
// ids is 0 for cavern floor, -1 for rock, so the same grid code (gridwalls.js) works for caves and for dungeons.
// style: 'chamfer' (default) - marching squares, corners cut at 45 degrees, never through a floor cell's middle
//        'lattice'           - the exact staircase along cell edges
//        'dp'                - the staircase simplified with tolerance `smooth`; shorter, but a diagonal can run THROUGH the
//                              centre of a floor cell, which puts a token standing there on top of a wall (kept for comparison)
(function (root) {
  'use strict';
  var rngMod = typeof require === 'function' && typeof module !== 'undefined' ? require('./rng.js') : root.ndMapRng;
  var gw = typeof require === 'function' && typeof module !== 'undefined' ? require('./gridwalls.js') : root.ndMapGridWalls;

  function generateCave(opts) {
    opts = opts || {};
    var w = opts.w || 48, h = opts.h || 32, seed = opts.seed === undefined ? 1 : opts.seed;
    var fill = opts.fill === undefined ? 0.46 : opts.fill, steps = opts.steps === undefined ? 5 : opts.steps;
    var smooth = opts.smooth === undefined ? 0.75 : opts.smooth, style = opts.style || 'chamfer';
    var rng = rngMod.makeRng(seed), rock = new Uint8Array(w * h), x, y, i;
    for (y = 0; y < h; y++) for (x = 0; x < w; x++) rock[y * w + x] = (x === 0 || y === 0 || x === w - 1 || y === h - 1 || rng.next() < fill) ? 1 : 0;

    for (i = 0; i < steps; i++) {
      var nxt = new Uint8Array(w * h);
      for (y = 0; y < h; y++) for (x = 0; x < w; x++) {
        if (x === 0 || y === 0 || x === w - 1 || y === h - 1) { nxt[y * w + x] = 1; continue; }
        var n = 0;
        for (var dy = -1; dy <= 1; dy++) for (var dx = -1; dx <= 1; dx++) n += rock[(y + dy) * w + x + dx];
        nxt[y * w + x] = n >= 5 ? 1 : 0;
      }
      rock = nxt;
    }

    // keep the largest cavern (4-connected flood fill)
    var comp = new Int32Array(w * h).fill(-1), sizes = [], stack;
    for (i = 0; i < w * h; i++) {
      if (rock[i] || comp[i] !== -1) continue;
      var id = sizes.length, count = 0;
      stack = [i]; comp[i] = id;
      while (stack.length) {
        var c = stack.pop(), cx = c % w, cy = (c - cx) / w;
        count++;
        [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(function (d) {
          var nx = cx + d[0], ny = cy + d[1];
          if (nx < 0 || ny < 0 || nx >= w || ny >= h) return;
          var j = ny * w + nx;
          if (!rock[j] && comp[j] === -1) { comp[j] = id; stack.push(j); }
        });
      }
      sizes.push(count);
    }
    var big = 0;
    sizes.forEach(function (s, k) { if (s > sizes[big]) big = k; });
    var ids = new Int16Array(w * h).fill(-1), floorCells = 0;
    for (i = 0; i < w * h; i++) if (comp[i] === big && sizes.length) { ids[i] = 0; floorCells++; }
    var rings = style === 'chamfer' ? gw.contourRings(ids, w, h) : gw.outlineRings(ids, w, h, style === 'dp' ? smooth : 0);
    return { w: w, h: h, ids: ids, rings: rings, seed: seed, floorCells: floorCells, style: style };
  }

  // Wall segments of the smoothed rings (each ring is a closed polyline).
  function wallsOf(cave) {
    var segs = [];
    cave.rings.forEach(function (ring) {
      for (var i = 0; i + 1 < ring.length; i++) segs.push({ x1: ring[i][0], y1: ring[i][1], x2: ring[i + 1][0], y2: ring[i + 1][1], kind: 'wall', state: null });
    });
    return segs;
  }

  root.ndMapGenCave = { generateCave: generateCave, wallsOf: wallsOf };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapGenCave;
})(typeof window !== 'undefined' ? window : globalThis);
