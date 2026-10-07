/* Zoom / pan / pinch maths for a map drawn in an SVG viewBox. Pure functions (no DOM), tested under Node.
 *
 * A "view" is {x, y, w, h}: the part of the canvas (canvas units) that is visible. `bounds` is {w, h}, the whole canvas.
 * The aspect ratio of a view never changes, so a view is fully described by its centre and a zoom factor (1 = the whole
 * canvas fits). Every function returns a NEW view and never produces a non-finite number, whatever it is given. */
(function (root) {
  'use strict';

  var MIN_ZOOM = 1;      // the whole map
  var MAX_ZOOM = 12;     // 12x closer than "fit"

  function num(v, fallback) { return (typeof v === 'number' && isFinite(v)) ? v : fallback; }
  function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

  function fit(bounds) {
    var w = Math.max(1, num(bounds && bounds.w, 1)), h = Math.max(1, num(bounds && bounds.h, 1));
    return { x: 0, y: 0, w: w, h: h };
  }

  function zoomOf(view, bounds) {
    var b = fit(bounds);
    return b.w / Math.max(1e-9, num(view && view.w, b.w));
  }

  // keep the view inside the canvas (a view as large as the canvas is pinned to 0,0)
  function constrain(view, bounds) {
    var b = fit(bounds);
    var z = clamp(zoomOf(view, b), MIN_ZOOM, MAX_ZOOM);
    var w = b.w / z, h = b.h / z;
    return {
      x: clamp(num(view && view.x, 0), 0, b.w - w),
      y: clamp(num(view && view.y, 0), 0, b.h - h),
      w: w, h: h,
    };
  }

  // zoom by `factor` keeping the canvas point (cx, cy) fixed on screen
  function zoomAt(view, bounds, factor, cx, cy) {
    var b = fit(bounds), v = constrain(view, b);
    var f = num(factor, 1);
    if (f <= 0) f = 1;
    var z = clamp(zoomOf(v, b) * f, MIN_ZOOM, MAX_ZOOM);
    var w = b.w / z, h = b.h / z;
    var px = clamp(num(cx, v.x + v.w / 2), v.x, v.x + v.w), py = clamp(num(cy, v.y + v.h / 2), v.y, v.y + v.h);
    // the point keeps its relative position inside the window
    var rx = (px - v.x) / v.w, ry = (py - v.y) / v.h;
    return constrain({ x: px - rx * w, y: py - ry * h, w: w, h: h }, b);
  }

  // move by a screen-space delta; `screen` is {w, h} of the element showing the map, in pixels
  function panBy(view, bounds, dxPx, dyPx, screen) {
    var b = fit(bounds), v = constrain(view, b);
    var sw = Math.max(1, num(screen && screen.w, 1)), sh = Math.max(1, num(screen && screen.h, 1));
    // the map is shown "meet": one scale for both axes
    var scale = Math.min(sw / v.w, sh / v.h);
    return constrain({ x: v.x - num(dxPx, 0) / scale, y: v.y - num(dyPx, 0) / scale, w: v.w, h: v.h }, b);
  }

  // two fingers: from the previous pair of screen points to the new pair. Points are {x, y} in screen pixels RELATIVE
  // to the map element; the result zooms by the change in distance and pans by the movement of the midpoint.
  function pinch(view, bounds, prev, next, screen) {
    var b = fit(bounds), v = constrain(view, b);
    var sw = Math.max(1, num(screen && screen.w, 1)), sh = Math.max(1, num(screen && screen.h, 1));
    function dist(p) { return Math.hypot(num(p[0].x, 0) - num(p[1].x, 0), num(p[0].y, 0) - num(p[1].y, 0)); }
    function mid(p) { return { x: (num(p[0].x, 0) + num(p[1].x, 0)) / 2, y: (num(p[0].y, 0) + num(p[1].y, 0)) / 2 }; }
    var d0 = dist(prev), d1 = dist(next);
    var m0 = mid(prev), m1 = mid(next);
    var scale = Math.min(sw / v.w, sh / v.h);
    // canvas point under the previous midpoint ("meet" centres the map in the element)
    var offX = (sw - v.w * scale) / 2, offY = (sh - v.h * scale) / 2;
    var cx = v.x + (m0.x - offX) / scale, cy = v.y + (m0.y - offY) / scale;
    var out = d0 > 1 ? zoomAt(v, b, d1 / d0, cx, cy) : v;
    return panBy(out, b, m1.x - m0.x, m1.y - m0.y, screen);
  }

  // centre the view on a canvas point at the current zoom (used to follow your own token)
  function centerOn(view, bounds, cx, cy) {
    var b = fit(bounds), v = constrain(view, b);
    return constrain({ x: num(cx, v.x + v.w / 2) - v.w / 2, y: num(cy, v.y + v.h / 2) - v.h / 2, w: v.w, h: v.h }, b);
  }

  // convert a screen point (relative to the map element) to canvas coordinates under "meet" scaling
  function toCanvas(view, screen, px, py) {
    var sw = Math.max(1, num(screen && screen.w, 1)), sh = Math.max(1, num(screen && screen.h, 1));
    var scale = Math.min(sw / view.w, sh / view.h);
    var offX = (sw - view.w * scale) / 2, offY = (sh - view.h * scale) / 2;
    return { x: view.x + (num(px, 0) - offX) / scale, y: view.y + (num(py, 0) - offY) / scale };
  }

  function toAttr(view) { return [view.x, view.y, view.w, view.h].map(function (n) { return Math.round(n * 100) / 100; }).join(' '); }

  root.ndViewport = {
    fit: fit, zoomOf: zoomOf, constrain: constrain, zoomAt: zoomAt, panBy: panBy, pinch: pinch,
    centerOn: centerOn, toCanvas: toCanvas, toAttr: toAttr, MIN_ZOOM: MIN_ZOOM, MAX_ZOOM: MAX_ZOOM,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndViewport;
})(typeof window !== 'undefined' ? window : globalThis);
