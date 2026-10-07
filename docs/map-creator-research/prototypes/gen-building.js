// A building from a room GRAPH, the kind of thing a language model can describe reliably ("a smugglers' den: a bar, two
// storerooms, a hidden back room behind the bar") while deterministic code does the geometry, which models are poor at.
//
//   generateBuilding({ w, h, seed, rooms: [{ name, area, kind }], links: [[a, b, 'door' | 'open' | 'secret']], entrance })
//     -> { w, h, ids, rooms: [{ id, name, kind, x, y, w, h }], marks, open, unmet, seed }
//
// Layout is a slicing treemap: the rooms are split into two groups of near-equal area and the footprint is cut across its
// longer side in that proportion, recursively. Doors go on the middle of the wall two linked rooms share; a link between
// rooms that do not touch is reported in `unmet` (the caller can add a hallway room and try again) - nothing is guessed.
(function (root) {
  'use strict';
  var rngMod = typeof require === 'function' && typeof module !== 'undefined' ? require('./rng.js') : root.ndMapRng;
  var gw = typeof require === 'function' && typeof module !== 'undefined' ? require('./gridwalls.js') : root.ndMapGridWalls;

  function layout(items, x, y, w, h, out) {
    if (items.length === 1) { out.push({ item: items[0], x: x, y: y, w: w, h: h }); return; }
    var total = items.reduce(function (s, i) { return s + i.area; }, 0), acc = 0, cut = 1;
    for (var i = 0; i < items.length - 1; i++) {                       // first group reaches about half of the area
      acc += items[i].area; cut = i + 1;
      if (acc >= total / 2) break;
    }
    var a = items.slice(0, cut), b = items.slice(cut);
    var share = a.reduce(function (s, i) { return s + i.area; }, 0) / total;
    if (w >= h) {
      var wa = Math.min(w - 2, Math.max(2, Math.round(w * share)));
      layout(a, x, y, wa, h, out); layout(b, x + wa, y, w - wa, h, out);
    } else {
      var ha = Math.min(h - 2, Math.max(2, Math.round(h * share)));
      layout(a, x, y, w, ha, out); layout(b, x, y + ha, w, h - ha, out);
    }
  }

  function generateBuilding(opts) {
    var w = opts.w, h = opts.h, rng = rngMod.makeRng(opts.seed === undefined ? 1 : opts.seed);
    var items = opts.rooms.map(function (r, i) { return { i: i, name: r.name, kind: r.kind || 'room', area: r.area || 12 }; })
      .sort(function (p, q) { return q.area - p.area || p.i - q.i; });
    var placed = [];
    layout(items, 0, 0, w, h, placed);
    var ids = new Int16Array(w * h).fill(-1), rooms = [];
    placed.forEach(function (p) {
      var id = p.item.i;
      rooms[id] = { id: id, name: p.item.name, kind: p.item.kind, x: p.x, y: p.y, w: p.w, h: p.h };
      for (var yy = p.y; yy < p.y + p.h; yy++) for (var xx = p.x; xx < p.x + p.w; xx++) ids[yy * w + xx] = id;
    });

    // all candidate edges between two rooms (or a room and the outside)
    function sharedEdges(a, b) {
      var out = [];
      for (var y = 0; y <= h; y++) for (var x = 0; x < w; x++) {
        var up = gw.idAt(ids, w, h, x, y - 1), dn = gw.idAt(ids, w, h, x, y);
        if ((up === a && dn === b) || (up === b && dn === a)) out.push({ key: gw.ek('h', x, y), x: x, y: y });
      }
      for (y = 0; y < h; y++) for (x = 0; x <= w; x++) {
        var lf = gw.idAt(ids, w, h, x - 1, y), rt = gw.idAt(ids, w, h, x, y);
        if ((lf === a && rt === b) || (lf === b && rt === a)) out.push({ key: gw.ek('v', x, y), x: x, y: y });
      }
      return out;
    }

    var marks = new Map(), open = new Set(), unmet = [];
    (opts.links || []).forEach(function (l) {
      var edges = sharedEdges(l[0], l[1]);
      if (!edges.length) { unmet.push(l); return; }
      var e = edges[Math.floor(edges.length / 2)], kind = l[2] || 'door';
      if (kind === 'open') open.add(e.key);
      else marks.set(e.key, { kind: kind === 'secret' ? 'secret' : 'door', state: 'closed' });
    });
    if (opts.entrance !== undefined) {
      var ext = sharedEdges(opts.entrance, -1);
      if (ext.length) { var pick = ext[rng.int(0, ext.length - 1)]; marks.set(pick.key, { kind: 'door', state: 'closed' }); }
      else unmet.push([opts.entrance, -1, 'door']);
    }
    // a window on the outside wall of some rooms, never on a door
    rooms.forEach(function (r) {
      if (!r || r.id === opts.entrance || !rng.chance(0.6)) return;
      var cands = sharedEdges(r.id, -1).filter(function (e) { return !marks.has(e.key); });
      if (cands.length >= 3) marks.set(cands[Math.floor(cands.length / 2)].key, { kind: 'window', state: 'closed' });
    });
    return { w: w, h: h, ids: ids, rooms: rooms, marks: marks, open: open, unmet: unmet, seed: opts.seed };
  }

  function wallsOf(map) { return gw.mergeEdges(gw.collectEdges(map.ids, map.w, map.h, { open: map.open, marks: map.marks })); }

  root.ndMapGenBuilding = { generateBuilding: generateBuilding, wallsOf: wallsOf };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapGenBuilding;
})(typeof window !== 'undefined' ? window : globalThis);
