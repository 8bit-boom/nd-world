// Walls from a grid of spaces. The idea that keeps a grid map simple and exact:
//
//   ids[y * w + x]   what space cell (x, y) belongs to: -1 = solid rock / outside, 0.. = a room, a corridor network, ...
//
// A wall exists on every cell edge whose two sides belong to different spaces - nobody draws walls, they follow from where
// the rooms are ("draw rooms, walls appear"). Two things override that, both stored per edge:
//   open  - the edge stays open (an archway, a doorless corridor mouth)
//   marks - the edge is a door / secret door / window, with its state
// Everything is integer lattice coordinates (cell corners), so the result is exact and can be scaled to pixels or feet.
//
// Cell (x, y) covers [x, x+1] x [y, y+1].
//   H(x, y): horizontal edge on lattice row y, between cell (x, y-1) above and cell (x, y) below, spanning x..x+1
//   V(x, y): vertical edge on lattice column x, between cell (x-1, y) left and cell (x, y) right, spanning y..y+1
(function (root) {
  'use strict';
  var geom = typeof require === 'function' && typeof module !== 'undefined' ? require('./geom.js') : root.ndMapGeom;

  function ek(k, x, y) { return k + x + ',' + y; }
  function idAt(ids, w, h, x, y) { return x < 0 || y < 0 || x >= w || y >= h ? -1 : ids[y * w + x]; }

  // Every edge that separates two different spaces, minus the open ones. A marked edge carries its kind and state.
  //   opts.open  : Set of edge keys (ek) that stay open
  //   opts.marks : Map edge key -> { kind: 'door' | 'secret' | 'window', state?: 'closed' | 'open' | 'locked' }
  function collectEdges(ids, w, h, opts) {
    opts = opts || {};
    var open = opts.open || new Set(), marks = opts.marks || new Map(), out = [], x, y;
    function add(k, ex, ey, a, b) {
      if (a === b) return;
      var key = ek(k, ex, ey);
      if (open.has(key)) return;
      var m = marks.get(key);
      out.push({ k: k, x: ex, y: ey, a: a, b: b, kind: m ? m.kind : 'wall', state: m ? (m.state || 'closed') : null });
    }
    for (y = 0; y <= h; y++) for (x = 0; x < w; x++) add('h', x, y, idAt(ids, w, h, x, y - 1), idAt(ids, w, h, x, y));
    for (y = 0; y < h; y++) for (x = 0; x <= w; x++) add('v', x, y, idAt(ids, w, h, x - 1, y), idAt(ids, w, h, x, y));
    return out;
  }

  // Plain wall edges merge into the longest straight segments they form ("fewer, longer segments" is what VTTs want);
  // doors, secret doors and windows stay one cell wide, each its own segment.
  function mergeEdges(edges) {
    var segs = [], runs = {};
    edges.forEach(function (e) {
      var x1 = e.x, y1 = e.y, x2 = e.k === 'h' ? e.x + 1 : e.x, y2 = e.k === 'h' ? e.y : e.y + 1;
      if (e.kind !== 'wall') { segs.push({ x1: x1, y1: y1, x2: x2, y2: y2, kind: e.kind, state: e.state }); return; }
      var line = e.k === 'h' ? e.y : e.x, pos = e.k === 'h' ? e.x : e.y;
      (runs[e.k + line] = runs[e.k + line] || { k: e.k, line: line, pos: [] }).pos.push(pos);
    });
    Object.keys(runs).forEach(function (key) {
      var r = runs[key], p = r.pos.sort(function (a, b) { return a - b; }), start = p[0], prev = p[0];
      function flush(end) {
        if (r.k === 'h') segs.push({ x1: start, y1: r.line, x2: end + 1, y2: r.line, kind: 'wall', state: null });
        else segs.push({ x1: r.line, y1: start, x2: r.line, y2: end + 1, kind: 'wall', state: null });
      }
      for (var i = 1; i < p.length; i++) {
        if (p[i] !== prev + 1) { flush(prev); start = p[i]; }
        prev = p[i];
      }
      flush(prev);
    });
    return segs;
  }

  // Remove points that lie on the straight line between their neighbours (keeps both ends; a closed ring keeps its start).
  function dropCollinear(line) {
    var out = [line[0]];
    for (var i = 1; i < line.length - 1; i++) {
      var a = out[out.length - 1], b = line[i], n = line[i + 1];
      if (Math.abs((b[0] - a[0]) * (n[1] - b[1]) - (b[1] - a[1]) * (n[0] - b[0])) > 1e-9) out.push(b);
    }
    out.push(line[line.length - 1]);
    return out;
  }

  // Chains of wall segments that meet end to end: a polyline continues through a corner where exactly two segments meet and
  // stops at a T or a cross. A closed ring comes back with its first point repeated at the end, the way Universal VTT's
  // line_of_sight writes it.
  function polylines(segs) {
    var adj = new Map();
    function key(x, y) { return x + ',' + y; }
    function push(k, v) { var a = adj.get(k); if (a) a.push(v); else adj.set(k, [v]); }
    segs.forEach(function (s, i) {
      push(key(s.x1, s.y1), { i: i, x: s.x2, y: s.y2 });
      push(key(s.x2, s.y2), { i: i, x: s.x1, y: s.y1 });
    });
    var used = new Array(segs.length).fill(false), out = [];
    function walk(sx, sy, first) {
      var line = [[sx, sy]], cx = sx, cy = sy, step = first;
      while (step) {
        used[step.i] = true;
        cx = step.x; cy = step.y; line.push([cx, cy]);
        var here = adj.get(key(cx, cy));
        if (here.length !== 2) break;
        step = here.find(function (n) { return !used[n.i]; });
      }
      return line;
    }
    adj.forEach(function (list, k) {            // open chains and junctions first
      if (list.length === 2) return;
      var xy = k.split(',').map(Number);
      list.forEach(function (n) { if (!used[n.i]) out.push(walk(xy[0], xy[1], n)); });
    });
    segs.forEach(function (s, i) {              // what is left are closed rings
      if (!used[i]) out.push(walk(s.x1, s.y1, { i: i, x: s.x2, y: s.y2 }));
    });
    return out.map(dropCollinear);              // a straight run that was cut into pieces (wall, window, wall) is one line again
  }

  // Outline rings of a mask (caves): the boundary between "space" and "rock" as closed rings, optionally smoothed by
  // simplifying the staircase into diagonals. Every space id counts as the same space here. Rings keep a consistent winding:
  // the space is on the right-hand side of the direction of travel (screen coordinates, y down), so outer boundaries run
  // clockwise on screen and holes counter-clockwise.
  function outlineRings(ids, w, h, tol) {
    var mask = new Int16Array(ids.length);
    for (var m = 0; m < ids.length; m++) mask[m] = ids[m] >= 0 ? 0 : -1;
    var edges = collectEdges(mask, w, h), next = new Map();
    function key(x, y) { return x + ',' + y; }
    edges.forEach(function (e) {
      // direction so that the cell with id >= 0 is on the left
      var spaceIsB = e.b >= 0 ? true : false;       // b = below (h) or right (v)
      var p1, p2;
      if (e.k === 'h') { p1 = [e.x, e.y]; p2 = [e.x + 1, e.y]; if (!spaceIsB) { var t = p1; p1 = p2; p2 = t; } }
      else { p1 = [e.x, e.y]; p2 = [e.x, e.y + 1]; if (spaceIsB) { var t2 = p1; p1 = p2; p2 = t2; } }
      var k = key(p1[0], p1[1]);
      (next.get(k) || next.set(k, []).get(k)).push(p2);
    });
    var rings = [];
    while (next.size) {
      var startKey = next.keys().next().value, ring = [], cur = startKey.split(',').map(Number), guard = 0;
      ring.push(cur);
      for (;;) {
        var list = next.get(key(cur[0], cur[1]));
        if (!list || !list.length) break;
        var prev = ring.length > 1 ? ring[ring.length - 2] : null, pick = 0;
        if (list.length > 1 && prev) {                 // pinch point: keep turning the same way (right-hand rule)
          var dx = cur[0] - prev[0], dy = cur[1] - prev[1], best = -Infinity;
          list.forEach(function (c, i) {
            var cross = dx * (c[1] - cur[1]) - dy * (c[0] - cur[0]);
            if (cross > best) { best = cross; pick = i; }
          });
        }
        var nxt = list.splice(pick, 1)[0];
        if (!list.length) next.delete(key(cur[0], cur[1]));
        cur = nxt; ring.push(cur);
        if (cur[0] === ring[0][0] && cur[1] === ring[0][1]) break;
        if (++guard > 4 * w * h + 16) break;
      }
      // merge straight runs, then (optionally) smooth
      var pts = [ring[0]];
      for (var i = 1; i < ring.length - 1; i++) {
        var a = pts[pts.length - 1], b = ring[i], c = ring[i + 1];
        if ((b[0] - a[0]) * (c[1] - b[1]) !== (b[1] - a[1]) * (c[0] - b[0])) pts.push(b);
      }
      pts.push(ring[ring.length - 1]);
      if (tol && pts.length > 8) pts = geom.simplify(pts, tol);
      if (pts.length >= 4) rings.push(pts);
    }
    return rings;
  }

  // Marching squares on the cell CENTRES: the outline runs through the midpoints of the cell edges that separate space from
  // rock, so a straight wall stays exactly on its lattice line and a corner is cut by a 45-degree chamfer. Unlike simplifying
  // the staircase (outlineRings with a tolerance), a chamfer can never pass through the middle of a floor cell, so a token
  // standing at a cell centre is never on a wall (>= 0.35 cells away). Two floor cells touching only at a corner stay apart.
  // Returns closed rings (first point repeated) with collinear points merged.
  function contourRings(ids, w, h) {
    function on(x, y) { return x >= 0 && y >= 0 && x < w && y < h && ids[y * w + x] >= 0 ? 1 : 0; }
    var segs = [], mi, mj;
    for (mj = -1; mj < h; mj++) for (mi = -1; mi < w; mi++) {
      var tl = on(mi, mj), tr = on(mi + 1, mj), br = on(mi + 1, mj + 1), bl = on(mi, mj + 1), c = tl * 8 + tr * 4 + br * 2 + bl;
      if (c === 0 || c === 15) continue;
      var T = [mi + 1, mj + 0.5], B = [mi + 1, mj + 1.5], L = [mi + 0.5, mj + 1], R = [mi + 1.5, mj + 1], pairs;
      switch (c) {
        case 1: pairs = [[L, B]]; break;           case 2: pairs = [[B, R]]; break;           case 3: pairs = [[L, R]]; break;
        case 4: pairs = [[T, R]]; break;           case 5: pairs = [[T, R], [L, B]]; break;   case 6: pairs = [[T, B]]; break;
        case 7: pairs = [[T, L]]; break;           case 8: pairs = [[T, L]]; break;           case 9: pairs = [[T, B]]; break;
        case 10: pairs = [[T, L], [B, R]]; break;  case 11: pairs = [[T, R]]; break;          case 12: pairs = [[L, R]]; break;
        case 13: pairs = [[B, R]]; break;          case 14: pairs = [[L, B]]; break;
      }
      pairs.forEach(function (p) { segs.push({ x1: p[0][0], y1: p[0][1], x2: p[1][0], y2: p[1][1] }); });
    }
    return polylines(segs);
  }

  function wallStats(segs) {
    var n = { wall: 0, door: 0, secret: 0, window: 0, other: 0 }, len = 0;
    segs.forEach(function (s) { n[s.kind] !== undefined ? n[s.kind]++ : n.other++; len += Math.hypot(s.x2 - s.x1, s.y2 - s.y1); });
    return { segments: segs.length, byKind: n, totalLength: len };
  }

  root.ndMapGridWalls = { ek: ek, idAt: idAt, collectEdges: collectEdges, mergeEdges: mergeEdges, polylines: polylines, outlineRings: outlineRings, contourRings: contourRings, wallStats: wallStats };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapGridWalls;
})(typeof window !== 'undefined' ? window : globalThis);
