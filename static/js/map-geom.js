// Small plane-geometry helpers for fog of war and line of sight (static/js/map-vision.js, map-fog.js). Points are [x, y].
// Pure functions, tested under Node (tests/test_map_vision.py).
(function (root) {
  'use strict';

  function dist(a, b) { return Math.hypot(b[0] - a[0], b[1] - a[1]); }

  // Perpendicular distance from p to the infinite line through a and b (a == b falls back to point distance).
  function lineDist(p, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy;
    if (!l2) return dist(p, a);
    return Math.abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / Math.sqrt(l2);
  }

  // Douglas-Peucker. Keeps the two end points; a closed loop (first == last) is simplified as one open run, so its start
  // point stays - callers that care rotate the loop first.
  function simplify(points, tol) {
    if (points.length <= 2) return points.slice();
    var keep = new Array(points.length).fill(false);
    keep[0] = keep[points.length - 1] = true;
    var stack = [[0, points.length - 1]];
    while (stack.length) {
      var seg = stack.pop(), lo = seg[0], hi = seg[1], worst = -1, at = -1;
      for (var i = lo + 1; i < hi; i++) {
        var d = lineDist(points[i], points[lo], points[hi]);
        if (d > worst) { worst = d; at = i; }
      }
      if (worst > tol) { keep[at] = true; stack.push([lo, at], [at, hi]); }
    }
    return points.filter(function (_, i) { return keep[i]; });
  }

  // Signed area (positive for counter-clockwise in a y-up system, i.e. clockwise on screen).
  function signedArea(poly) {
    var s = 0;
    for (var i = 0; i < poly.length; i++) { var a = poly[i], b = poly[(i + 1) % poly.length]; s += a[0] * b[1] - b[0] * a[1]; }
    return s / 2;
  }

  function pointInPolygon(p, poly) {
    var inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > p[1]) !== (yj > p[1]) && p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // Intersection of the ray o + t*d (t >= 0) with the segment a-b: the ray parameter t, or null.
  function rayHit(o, d, a, b) {
    var ex = b[0] - a[0], ey = b[1] - a[1];
    var den = d[0] * ey - d[1] * ex;
    if (Math.abs(den) < 1e-12) return null;
    var ox = a[0] - o[0], oy = a[1] - o[1];
    var t = (ox * ey - oy * ex) / den;
    var u = (ox * d[1] - oy * d[0]) / den;
    return t >= 0 && u >= -1e-9 && u <= 1 + 1e-9 ? t : null;
  }

  function bbox(points) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    points.forEach(function (p) { if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0]; if (p[1] < y0) y0 = p[1]; if (p[1] > y1) y1 = p[1]; });
    return { x0: x0, y0: y0, x1: x1, y1: y1 };
  }

  root.ndMapGeom = { dist: dist, lineDist: lineDist, simplify: simplify, signedArea: signedArea, pointInPolygon: pointInPolygon, rayHit: rayHit, bbox: bbox };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapGeom;
})(typeof window !== 'undefined' ? window : globalThis);
