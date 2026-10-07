// A seeded dungeon: rooms placed without overlap, joined by a minimum spanning tree of corridors plus a few extra loops,
// doors where a corridor meets a room. The result is a grid of space ids (see gridwalls.js) - walls follow from it.
//
//   generateDungeon({ w, h, seed, rooms, minRoom, maxRoom, loops, doorChance })
//     -> { w, h, ids, rooms: [{ id, x, y, w, h, cx, cy }], corridorId, marks, open, entrances, seed }
//
// The same seed and options always give the same dungeon (tests/determinism), which is also what lets a GM keep a seed
// and regenerate one room later.
(function (root) {
  'use strict';
  var rngMod = typeof require === 'function' && typeof module !== 'undefined' ? require('./rng.js') : root.ndMapRng;
  var gw = typeof require === 'function' && typeof module !== 'undefined' ? require('./gridwalls.js') : root.ndMapGridWalls;

  function edgeBetween(c1, c2) {
    // two cells that share a side -> the key of the edge between them
    if (c1[0] !== c2[0]) return gw.ek('v', Math.max(c1[0], c2[0]), c1[1]);
    return gw.ek('h', c1[0], Math.max(c1[1], c2[1]));
  }

  function generateDungeon(opts) {
    opts = opts || {};
    var w = opts.w || 40, h = opts.h || 30, seed = opts.seed === undefined ? 1 : opts.seed;
    var want = opts.rooms || Math.max(4, Math.round(w * h / 90)), minRoom = opts.minRoom || 4, maxRoom = opts.maxRoom || 9;
    var loops = opts.loops === undefined ? 0.15 : opts.loops, doorChance = opts.doorChance === undefined ? 0.75 : opts.doorChance;
    var rng = rngMod.makeRng(seed), ids = new Int16Array(w * h).fill(-1), rooms = [];

    // 1. rooms, never closer than two cells to another so a corridor can pass between them
    for (var tries = 0; tries < want * 40 && rooms.length < want; tries++) {
      var rw = rng.int(minRoom, maxRoom), rh = rng.int(minRoom - 1, maxRoom - 2);
      if (rw + 4 > w || rh + 4 > h) continue;
      var rx = rng.int(2, w - rw - 2), ry = rng.int(2, h - rh - 2);
      var clash = rooms.some(function (o) { return rx < o.x + o.w + 2 && rx + rw + 2 > o.x && ry < o.y + o.h + 2 && ry + rh + 2 > o.y; });
      if (clash) continue;
      rooms.push({ id: rooms.length, x: rx, y: ry, w: rw, h: rh, cx: rx + Math.floor(rw / 2), cy: ry + Math.floor(rh / 2) });
    }
    rooms.forEach(function (r) { for (var y = r.y; y < r.y + r.h; y++) for (var x = r.x; x < r.x + r.w; x++) ids[y * w + x] = r.id; });
    var corridorId = rooms.length;

    // 2. which rooms get joined: a minimum spanning tree (Prim) over the centres, then a few extra edges for loops
    var joins = [], inTree = [0], rest = rooms.slice(1).map(function (r) { return r.id; });
    function d(a, b) { return Math.abs(rooms[a].cx - rooms[b].cx) + Math.abs(rooms[a].cy - rooms[b].cy); }
    while (rest.length) {
      var best = null;
      inTree.forEach(function (a) { rest.forEach(function (b) { var dd = d(a, b); if (!best || dd < best.d) best = { a: a, b: b, d: dd }; }); });
      joins.push([best.a, best.b]); inTree.push(best.b); rest.splice(rest.indexOf(best.b), 1);
    }
    rooms.forEach(function (r) {
      var near = rooms.filter(function (o) { return o.id !== r.id; }).sort(function (p, q) { return d(r.id, p.id) - d(r.id, q.id); }).slice(0, 2);
      near.forEach(function (o) {
        var dup = joins.some(function (j) { return (j[0] === r.id && j[1] === o.id) || (j[0] === o.id && j[1] === r.id); });
        if (!dup && rng.chance(loops)) joins.push([r.id, o.id]);
      });
    });

    // 3. carve corridors as L-shaped paths between the centres; cells inside rooms stay room cells
    var entranceKeys = new Set();
    joins.forEach(function (j) {
      var A = rooms[j[0]], B = rooms[j[1]], horizFirst = rng.chance(0.5), path = [], x = A.cx, y = A.cy;
      path.push([x, y]);
      function stepX() { while (x !== B.cx) { x += x < B.cx ? 1 : -1; path.push([x, y]); } }
      function stepY() { while (y !== B.cy) { y += y < B.cy ? 1 : -1; path.push([x, y]); } }
      if (horizFirst) { stepX(); stepY(); } else { stepY(); stepX(); }
      path.forEach(function (c, i) {
        var cur = ids[c[1] * w + c[0]];
        if (cur === -1) ids[c[1] * w + c[0]] = corridorId;
        if (i > 0) {
          var p = path[i - 1], pid = ids[p[1] * w + p[0]], cid = ids[c[1] * w + c[0]];
          var pRoom = pid >= 0 && pid < corridorId, cRoom = cid >= 0 && cid < corridorId;
          if (pRoom !== cRoom && (pRoom || cRoom) && pid !== cid) entranceKeys.add(edgeBetween(p, c));
        }
      });
    });

    // 4. doors where a corridor meets a room; the rest stay as open archways
    var marks = new Map(), open = new Set(), entrances = [];
    Array.from(entranceKeys).sort().forEach(function (key) {
      entrances.push(key);
      if (rng.chance(doorChance)) {
        var roll = rng.next(), kind = roll < 0.06 ? 'secret' : 'door';
        var state = kind === 'secret' ? 'closed' : roll < 0.55 ? 'closed' : roll < 0.9 ? 'open' : 'locked';
        marks.set(key, { kind: kind, state: state });
      } else {
        open.add(key);
      }
    });

    return { w: w, h: h, ids: ids, rooms: rooms, corridorId: corridorId, marks: marks, open: open, entrances: entrances, seed: seed };
  }

  // The wall segments of a generated map (merged, integer cell coordinates).
  function wallsOf(map) {
    return gw.mergeEdges(gw.collectEdges(map.ids, map.w, map.h, { open: map.open, marks: map.marks }));
  }

  root.ndMapGenDungeon = { generateDungeon: generateDungeon, wallsOf: wallsOf, edgeBetween: edgeBetween };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapGenDungeon;
})(typeof window !== 'undefined' ? window : globalThis);
