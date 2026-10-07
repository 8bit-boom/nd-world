// What a token can see and where it can walk, computed from wall segments {x1, y1, x2, y2, kind, state}.
//
//   blocksSight(seg) / blocksMove(seg)  - one rule for every kind of wall, so fog and movement never disagree:
//        wall: blocks both | secret door: blocks both until found | door: blocks both unless open |
//        window: blocks movement, not sight
//   visibilityPolygon(origin, segs, { range, bounds }) -> [[x, y], ...]  the area the origin can see, counter-clockwise by angle
//   canSee(a, b, segs) / moveBlocked(a, b, segs)       straight-line tests (a move that crosses a blocking wall is refused)
//
// The polygon is the textbook angular sweep (cast a ray at every wall end point, a hair to each side, keep the nearest hit).
// This version tests every ray against every candidate wall, so it is exact and short but O(rays x walls); `range` culls the
// walls to those near the origin first. docs/map-creator-research/PROTOTYPES.md has the measured cost on realistic maps.
// Pure functions, tested under Node (tests/test_map_vision.py); the SVG layer that draws the result is map-fog.js.
(function (root) {
  'use strict';

  function blocksSight(s) {
    if (s.kind === 'window') return false;
    if (s.kind === 'door') return s.state !== 'open';
    return true;
  }
  function blocksMove(s) {
    if (s.kind === 'door') return s.state !== 'open';
    return true;
  }

  // Ray o + t*d against segment (ax,ay)-(bx,by): t >= 0 or Infinity.
  function hit(ox, oy, dx, dy, ax, ay, bx, by) {
    var ex = bx - ax, ey = by - ay, den = dx * ey - dy * ex;
    if (den > -1e-12 && den < 1e-12) return Infinity;
    var px = ax - ox, py = ay - oy, t = (px * ey - py * ex) / den, u = (px * dy - py * dx) / den;
    return t >= 0 && u >= 0 && u <= 1 ? t : Infinity;
  }

  var EPS = 1e-6;

  function visibilityPolygon(origin, segs, opts) {
    opts = opts || {};
    var ox = origin[0], oy = origin[1], range = opts.range === undefined ? Infinity : opts.range, b = opts.bounds;
    var x0, y0, x1, y1;
    if (b) { x0 = b.x0; y0 = b.y0; x1 = b.x1; y1 = b.y1; }
    else { var r = isFinite(range) ? range : 1e6; x0 = ox - r; y0 = oy - r; x1 = ox + r; y1 = oy + r; }
    if (isFinite(range)) { x0 = Math.max(x0, ox - range); y0 = Math.max(y0, oy - range); x1 = Math.min(x1, ox + range); y1 = Math.min(y1, oy + range); }

    // candidate walls: sight-blocking and touching the box; the box itself closes the polygon
    var cand = [], i;
    for (i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (!blocksSight(s)) continue;
      if (Math.max(s.x1, s.x2) < x0 || Math.min(s.x1, s.x2) > x1 || Math.max(s.y1, s.y2) < y0 || Math.min(s.y1, s.y2) > y1) continue;
      cand.push(s);
    }
    cand.push({ x1: x0, y1: y0, x2: x1, y2: y0 }, { x1: x1, y1: y0, x2: x1, y2: y1 }, { x1: x1, y1: y1, x2: x0, y2: y1 }, { x1: x0, y1: y1, x2: x0, y2: y0 });

    var seen = new Set(), angles = [];
    function addAngle(px, py) {
      var key = px + ',' + py;
      if (seen.has(key)) return;
      seen.add(key);
      var a = Math.atan2(py - oy, px - ox);
      angles.push(a - EPS, a, a + EPS);
    }
    cand.forEach(function (s) { addAngle(s.x1, s.y1); addAngle(s.x2, s.y2); });
    if (isFinite(range)) {                                  // a round view needs rays between the wall end points as well
      var steps = opts.arcSteps || 64;
      for (i = 0; i < steps; i++) angles.push(-Math.PI + (2 * Math.PI * i) / steps);
    }

    var pts = angles.map(function (a) {
      var dx = Math.cos(a), dy = Math.sin(a), best = Infinity;
      for (var k = 0; k < cand.length; k++) {
        var c = cand[k], t = hit(ox, oy, dx, dy, c.x1, c.y1, c.x2, c.y2);
        if (t < best) best = t;
      }
      if (isFinite(range) && best > range) best = range;     // a round view of radius `range`
      return { a: a, x: ox + dx * best, y: oy + dy * best };
    });
    pts.sort(function (p, q) { return p.a - q.a; });
    return pts.map(function (p) { return [p.x, p.y]; });
  }

  // Do a and b see each other: no sight-blocking wall strictly between them.
  function canSee(a, b, segs) {
    var dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy);
    if (!len) return true;
    for (var i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (!blocksSight(s)) continue;
      var t = hit(a[0], a[1], dx / len, dy / len, s.x1, s.y1, s.x2, s.y2);
      if (t > 1e-9 && t < len - 1e-9) return false;
    }
    return true;
  }

  function moveBlocked(a, b, segs) {
    var dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy);
    if (!len) return false;
    for (var i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (!blocksMove(s)) continue;
      var t = hit(a[0], a[1], dx / len, dy / len, s.x1, s.y1, s.x2, s.y2);
      if (t > 1e-9 && t < len - 1e-9) return true;
    }
    return false;
  }

  root.ndMapVision = { blocksSight: blocksSight, blocksMove: blocksMove, visibilityPolygon: visibilityPolygon, canSee: canSee, moveBlocked: moveBlocked };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapVision;
})(typeof window !== 'undefined' ? window : globalThis);
