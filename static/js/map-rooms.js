// The room-drawing tool's model and generator (the editor's 🏠 tool, static/js/schematic-rooms.js, drives it).
//
// You paint floor on a grid of squares and give each patch a room; walls follow from that: an edge between two DIFFERENT spaces
// (a room and nothing, or a room and another room) is a wall, and an edge inside one room never is. Doors, windows and secret
// doors are marks on edges. This module turns that state into ordinary map content - floor rectangles, wall segments, door
// records - so the player view, fog of war, the TV and the exports need to know nothing about it. Pure functions, tested under
// Node (tests/test_map_rooms_js.py); the stored form is validated by app/map_rooms.py.
//
//   state   {cell, ox, oy, cols, rows, ids: Uint16Array(cols*rows), spaces: [{id, name, kind}], marks: {"x,y,h"|"x,y,v": kind}}
//   an "h" edge key is the edge along the TOP of square (x, y); a "v" key the edge along its LEFT.
(function (root) {
  'use strict';

  var MAX_SIDE = 400, MAX_CELLS = 40000;
  var FLOOR = { hall: '#3a3a4a', tavern: '#4a3a2a', bedroom: '#3a3a52', kitchen: '#4a4538', storage: '#3c352c', corridor: '#2f3138',
                cell: '#303236', shrine: '#40384f', library: '#35402f', armory: '#3b3b3b', cave: '#3a342a', outdoor: '#2f3d2f', other: '#34363f' };
  var KINDS = Object.keys(FLOOR);
  var MARKS = ['door', 'window', 'secret', 'open'];

  function create(cell, ox, oy, canvasW, canvasH) {
    cell = Math.max(8, Math.min(400, +cell || 50));
    ox = ((+ox || 0) % cell + cell) % cell; oy = ((+oy || 0) % cell + cell) % cell;
    var cols = Math.floor((canvasW - ox) / cell), rows = Math.floor((canvasH - oy) / cell);
    if (cols < 1 || rows < 1) return null;
    if (cols > MAX_SIDE || rows > MAX_SIDE || cols * rows > MAX_CELLS) return null;       // the tool needs bigger squares
    return { cell: cell, ox: ox, oy: oy, cols: cols, rows: rows, ids: new Uint16Array(cols * rows), spaces: [], marks: {} };
  }

  function at(st, x, y) { return x < 0 || y < 0 || x >= st.cols || y >= st.rows ? 0 : st.ids[y * st.cols + x]; }

  function addSpace(st, name, kind) {
    var id = 1;
    st.spaces.forEach(function (s) { if (s.id >= id) id = s.id + 1; });
    st.spaces.push({ id: id, name: String(name || ('Room ' + id)).slice(0, 60), kind: KINDS.indexOf(kind) >= 0 ? kind : 'other' });
    return id;
  }

  // paint the squares of the rectangle between two squares (inclusive) with a space id (0 erases); returns how many changed
  function paintRect(st, x0, y0, x1, y1, id) {
    var changed = 0;
    for (var y = Math.max(0, Math.min(y0, y1)); y <= Math.min(st.rows - 1, Math.max(y0, y1)); y++) {
      for (var x = Math.max(0, Math.min(x0, x1)); x <= Math.min(st.cols - 1, Math.max(x0, x1)); x++) {
        if (st.ids[y * st.cols + x] !== id) { st.ids[y * st.cols + x] = id; changed++; }
      }
    }
    if (changed) prune(st);
    return changed;
  }

  // every wall edge: between two different spaces (neighbours off the grid count as empty), as a Set of edge keys
  function edgeSet(st) {
    var set = {};
    for (var y = 0; y < st.rows; y++) for (var x = 0; x < st.cols; x++) {
      var id = st.ids[y * st.cols + x];
      if (!id) continue;
      if (at(st, x, y - 1) !== id) set[x + ',' + y + ',h'] = true;
      if (at(st, x, y + 1) !== id) set[x + ',' + (y + 1) + ',h'] = true;
      if (at(st, x - 1, y) !== id) set[x + ',' + y + ',v'] = true;
      if (at(st, x + 1, y) !== id) set[(x + 1) + ',' + y + ',v'] = true;
    }
    return set;
  }

  // marks on edges that are no longer walls (the room was erased or merged) are removed
  function prune(st) {
    var edges = edgeSet(st);
    Object.keys(st.marks).forEach(function (k) { if (!edges[k]) delete st.marks[k]; });
  }

  function setMark(st, key, kind) {
    var edges = edgeSet(st);
    if (!edges[key]) return false;                       // marks only go on walls
    if (!kind) delete st.marks[key]; else st.marks[key] = kind;
    return true;
  }

  function parseKey(k) { var p = k.split(','); return { x: +p[0], y: +p[1], a: p[2] }; }

  function px(st, x, y) { return [Math.round((st.ox + x * st.cell) * 100) / 100, Math.round((st.oy + y * st.cell) * 100) / 100]; }
  function edgePts(st, k) {
    var e = parseKey(k);
    return e.a === 'h' ? [px(st, e.x, e.y), px(st, e.x + 1, e.y)] : [px(st, e.x, e.y), px(st, e.x, e.y + 1)];
  }

  // floor squares of each space merged into as few rectangles as possible (rows of equal runs stacked)
  function floorRects(st) {
    var out = [];
    st.spaces.forEach(function (sp) {
      var runs = [];                                                      // {x, y, w}
      for (var y = 0; y < st.rows; y++) {
        var x = 0;
        while (x < st.cols) {
          if (at(st, x, y) !== sp.id) { x++; continue; }
          var x0 = x; while (x < st.cols && at(st, x, y) === sp.id) x++;
          runs.push({ x: x0, y: y, w: x - x0, h: 1 });
        }
      }
      var open = {}, rects = [];
      runs.forEach(function (r) {                                         // stack a run on the rectangle directly above it with the same x and width
        var key = r.x + ',' + r.w, up = open[key];
        if (up && up.y + up.h === r.y) { up.h++; }
        else { var n = { x: r.x, y: r.y, w: r.w, h: 1 }; open[key] = n; rects.push(n); }
      });
      var best = null;
      rects.forEach(function (r) { r.space = sp; if (!best || r.w * r.h > best.w * best.h) best = r; });
      if (best) best.label = true;
      out = out.concat(rects);
    });
    return out;
  }

  // unit edges -> long straight segments
  function mergeEdges(keys) {
    var lines = { h: {}, v: {} }, out = [];
    keys.forEach(function (k) { var e = parseKey(k); var fixed = e.a === 'h' ? e.y : e.x, pos = e.a === 'h' ? e.x : e.y; (lines[e.a][fixed] = lines[e.a][fixed] || []).push(pos); });
    ['h', 'v'].forEach(function (a) {
      Object.keys(lines[a]).map(Number).sort(function (p, q) { return p - q; }).forEach(function (fixed) {
        var ps = lines[a][fixed].sort(function (p, q) { return p - q; }), start = ps[0], prev = ps[0];
        for (var i = 1; i <= ps.length; i++) {
          if (i < ps.length && ps[i] === prev + 1) { prev = ps[i]; continue; }
          out.push({ a: a, fixed: fixed, from: start, to: prev + 1 });
          start = ps[i]; prev = ps[i];
        }
      });
    });
    return out;
  }

  // The walls and marked edges as map_walls data (ids are stable, so a door's open/closed state can be carried over), and the
  // floors as rectangles. `states` maps a mark's wall id to "open"/"closed" to keep.
  function generate(st, states) {
    states = states || {};
    var edges = edgeSet(st), plain = [], marked = [];
    Object.keys(edges).forEach(function (k) {
      var m = st.marks[k];
      if (!m) plain.push(k);
      else if (m !== 'open') marked.push(k);
    });
    var walls = mergeEdges(plain).map(function (s) {
      var a = s.a === 'h' ? [px(st, s.from, s.fixed), px(st, s.to, s.fixed)] : [px(st, s.fixed, s.from), px(st, s.fixed, s.to)];
      return { id: 'rm-w' + s.a + s.fixed + '-' + s.from, pts: a, kind: 'wall' };
    });
    marked.sort().forEach(function (k) {
      var kind = st.marks[k], id = 'rm-m' + k.replace(/,/g, '_');
      var w = { id: id, pts: edgePts(st, k), kind: kind };
      if (kind === 'door' || kind === 'secret') w.state = states[id] === 'open' ? 'open' : 'closed';
      walls.push(w);
    });
    return { floors: floorRects(st), walls: walls };
  }

  // ordinary map elements for what generate() returned: floors, wall lines and door/window leaves (all id-prefixed "rm-")
  function elements(st, gen) {
    var els = [], sw = Math.max(3, Math.round(st.cell * 0.1));
    gen.floors.forEach(function (r, i) {
      var p = px(st, r.x, r.y);
      var el = { id: 'rm-f' + i, type: 'rect', x: p[0], y: p[1], w: r.w * st.cell, h: r.h * st.cell, fill: FLOOR[r.space.kind] || FLOOR.other,
                 stroke: 'none', strokeW: 0, layer: 'Background', opacity: st.clear ? 0.12 : 1 };
      if (r.label) el.label = r.space.name;
      els.push(el);
    });
    if (st.clear) return els;                                  // a picture draws the walls and doors: only faint floors and names are added
    var COL = { door: '#c98a3d', secret: '#c98a3d', window: '#6bd6ff' };
    gen.walls.forEach(function (w, i) {
      var a = w.pts[0], b = w.pts[1];
      if (w.kind === 'wall') els.push({ id: 'rm-l' + i, type: 'line', x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: '#cfd6e6', strokeW: sw, layer: 'Tracks' });
      else {
        var pad = st.cell * 0.12, dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
        var e = { id: 'rm-k' + i, type: 'line', x1: a[0] + ux * pad, y1: a[1] + uy * pad, x2: b[0] - ux * pad, y2: b[1] - uy * pad, stroke: COL[w.kind], strokeW: sw + 2, layer: 'Tracks' };
        if (w.kind === 'secret') { e.hidden = true; e.dash = '4 3'; }
        if (w.kind === 'window') { e.dash = '6 3'; e.strokeW = sw; }
        els.push(e);
        if (w.kind === 'secret') els.push({ id: 'rm-j' + i, type: 'line', x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: '#cfd6e6', strokeW: sw, layer: 'Tracks' });   // players see a wall
      }
    });
    return els;
  }

  // pointer helpers (canvas pixels -> squares / the nearest edge of the square under the pointer)
  function cellAt(st, x, y) {
    var cx = Math.floor((x - st.ox) / st.cell), cy = Math.floor((y - st.oy) / st.cell);
    return cx < 0 || cy < 0 || cx >= st.cols || cy >= st.rows ? null : { x: cx, y: cy };
  }
  function nearestEdge(st, x, y) {
    var fx = (x - st.ox) / st.cell, fy = (y - st.oy) / st.cell, cx = Math.floor(fx), cy = Math.floor(fy);
    var d = [[fx - cx, cx + ',' + cy + ',v'], [cx + 1 - fx, (cx + 1) + ',' + cy + ',v'], [fy - cy, cx + ',' + cy + ',h'], [cy + 1 - fy, cx + ',' + (cy + 1) + ',h']];
    d.sort(function (p, q) { return p[0] - q[0]; });
    return d[0][1];
  }

  // ── stored form (app/map_rooms.py validates it) ─────────────────────────────
  function pack(st) {
    var cells = [], i = 0;
    while (i < st.ids.length) { var id = st.ids[i], n = 0; while (i < st.ids.length && st.ids[i] === id) { i++; n++; } cells.push(id, n); }
    var marks = Object.keys(st.marks).map(function (k) { var e = parseKey(k); return [e.x, e.y, e.a, st.marks[k]]; });
    return { cell: st.cell, ox: st.ox, oy: st.oy, cols: st.cols, rows: st.rows, cells: cells, spaces: st.spaces.map(function (s) { return { id: s.id, name: s.name, kind: s.kind }; }), marks: marks, clear: st.clear === true };
  }
  function unpack(d) {
    if (!d || !d.cols || !d.rows || !Array.isArray(d.cells)) return null;
    var st = { cell: d.cell, ox: d.ox || 0, oy: d.oy || 0, cols: d.cols, rows: d.rows, ids: new Uint16Array(d.cols * d.rows), spaces: (d.spaces || []).map(function (s) { return { id: s.id, name: s.name, kind: s.kind }; }), marks: {}, clear: d.clear === true };
    var i = 0;
    for (var k = 0; k + 1 < d.cells.length; k += 2) for (var n = 0; n < d.cells[k + 1] && i < st.ids.length; n++) st.ids[i++] = d.cells[k];
    (d.marks || []).forEach(function (m) { st.marks[m[0] + ',' + m[1] + ',' + m[2]] = m[3]; });
    return st;
  }

  root.ndMapRooms = { create: create, addSpace: addSpace, paintRect: paintRect, edgeSet: edgeSet, prune: prune, setMark: setMark, generate: generate,
    elements: elements, cellAt: cellAt, nearestEdge: nearestEdge, pack: pack, unpack: unpack, at: at, KINDS: KINDS, MARKS: MARKS, MAX_SIDE: MAX_SIDE, MAX_CELLS: MAX_CELLS };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapRooms;
})(typeof window !== 'undefined' ? window : globalThis);
