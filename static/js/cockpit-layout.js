// Cockpit layout maths — pure functions, no DOM, so every screen size can be tested under Node
// (tests/test_cockpit_layout.py) and cockpit.js only has to apply the numbers.
//
// A cockpit layout is a list of windows in pixels, and the same layout follows the GM across devices. So the layout is
// saved together with the SCREEN it was arranged for ({w, h} of the workspace), and on any other screen it is
// scaled from that one to this one instead of being used as raw pixels:
//   tilePanels   - the default layout and 📌 Auto-arrange: a stage (map / combat) plus tiles, columns chosen from the width
//   scalePanels  - a layout arranged for one screen, brought to another (edge by edge, so neighbours stay neighbours)
//   fitPanels    - pull back whatever still hangs off the screen (legacy layouts saved without a screen)
//   needsScale   - is the change of screen big enough to rescale (a scrollbar appearing is not)
//   sizeFor / cascade - a window added later: sized for this screen, placed inside it
(function (root) {
  'use strict';
  var MIN_W = 240, MIN_H = 140;   // the smallest a window can be dragged to (cockpit.js uses the same floor)
  var MIN_CELL_H = 180;           // the shortest a tiled window is made; more windows than fit simply scroll
  var GAP = 8, GRID = 8;
  var COL_W = 600;                // aim for columns about this wide: 2 on a laptop, 3 on 1080p, 4 on 1440p, 6 at most
  var MAX_COLS = 6;
  var MAX_ASPECT = 1.8;           // taller than this (height / width) and a tile is a sliver ...
  var MIN_ASPECT = 0.4;           // ... flatter than this and it is a bar
  var TOLERANCE = 0.04;           // a scrollbar or a bookmarks bar is not a new screen
  var STAGE = { map: 1, wmap: 1, combat: 1 };   // the window that gets the big area

  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }
  function snap(n) { return Math.round(n / GRID) * GRID; }
  function copy(p) { return Object.assign({}, p); }
  function vpOk(v) { return !!v && isFinite(v.w) && isFinite(v.h) && v.w >= 200 && v.h >= 150; }

  function needsScale(from, to, tol) {
    if (!vpOk(from) || !vpOk(to)) return false;
    tol = tol == null ? TOLERANCE : tol;
    return Math.abs(to.w / from.w - 1) > tol || Math.abs(to.h / from.h - 1) > tol;
  }

  // Pull every window fully inside the screen horizontally (the workspace scrolls vertically, so y is left alone).
  function fitPanels(panels, vp) {
    return panels.map(function (p) {
      var q = copy(p);
      q.w = Math.min(q.w, vp.w);
      q.x = clamp(q.x, 0, Math.max(0, vp.w - q.w));
      q.y = Math.max(0, q.y);
      return q;
    });
  }

  // A layout arranged for `from`, shown on `to`. Each window's four edges are scaled and snapped separately: rounding
  // is monotonic, so two windows that did not overlap still do not (a width-and-position scale could add up to a few px).
  function scalePanels(panels, from, to) {
    if (!vpOk(from) || !vpOk(to)) return fitPanels(panels, to);
    var sx = to.w / from.w, sy = to.h / from.h;
    return fitPanels(panels.map(function (p) {
      var q = copy(p);
      var left = snap(p.x * sx), right = snap((p.x + p.w) * sx);
      var top = snap(p.y * sy), bottom = snap((p.y + p.h) * sy);
      q.x = left; q.y = top;
      q.w = Math.max(Math.min(MIN_W, to.w), right - left);
      q.h = Math.max(MIN_H, bottom - top);
      return q;
    }), to);
  }

  // ── tiling ───────────────────────────────────────────────────────────────────────────────────

  // Stack `list` (indices into out) in one column; each window's height follows what it wants (opts.pref), never below
  // MIN_CELL_H — more windows than fit make the column taller and the workspace scrolls.
  function fillColumn(out, panels, list, x, y0, w, availH, pref) {
    if (!list.length) return;
    var total = 0;
    list.forEach(function (i) { total += Math.max(1, pref(panels[i])); });
    var room = availH - GAP * (list.length - 1);
    var y = y0;
    list.forEach(function (i, k) {
      var h = Math.max(MIN_CELL_H, Math.floor(room * Math.max(1, pref(panels[i])) / total));
      // the last window in a column that fits takes the rounding remainder, so it ends exactly at the bottom
      if (k === list.length - 1 && y + h < y0 + availH && h > MIN_CELL_H) h = y0 + availH - y;
      place(out[i], x, y, w, h);
      y += h + GAP;
    });
  }

  function place(q, x, y, w, h) {
    q.x = Math.round(x); q.y = Math.round(y); q.w = Math.max(1, Math.round(w)); q.h = Math.max(1, Math.round(h));
    q.collapsed = false;
  }

  // How many columns n windows get in an area of areaW x areaH: as many as maxCols, but not so many that a window
  // becomes a tall sliver (height / width over MAX_ASPECT) or so few that it is a flat bar (under MIN_ASPECT), and — among
  // the counts that look right — the one that leaves the fewest empty slots in the last row (the larger count wins a tie).
  // `stacked`: the windows go into independent columns (counts may differ by one) rather than into aligned rows.
  // Returns {c, violation}: how far the best choice still is from looking right (0 = fine).
  function gridColumns(n, maxCols, areaW, areaH, stacked) {
    var best = 1, bestV = Infinity, bestEmpty = Infinity;
    for (var c = Math.max(1, Math.min(maxCols, n)); c >= 1; c--) {
      var cw = (areaW - GAP * (c - 1)) / c;
      var tall, flat;
      if (stacked) {
        var few = Math.max(1, Math.floor(n / c)), many = Math.ceil(n / c);
        tall = (areaH - GAP * (few - 1)) / few / cw;
        flat = (areaH - GAP * (many - 1)) / many / cw;
      } else {
        var rows = Math.ceil(n / c);
        tall = flat = (areaH - GAP * (rows - 1)) / rows / cw;
      }
      var v = Math.round((Math.max(0, tall - MAX_ASPECT) + Math.max(0, MIN_ASPECT - flat)) * 100);
      var empty = stacked ? 0 : Math.ceil(n / c) * c - n;
      if (v < bestV || (v === bestV && empty < bestEmpty)) { best = c; bestV = v; bestEmpty = empty; }
    }
    return { c: best, violation: bestV };
  }

  // Aligned rows of c columns inside an area; a short last row stretches across the width.
  function placeGrid(out, list, x0, y0, areaW, areaH, c) {
    var rows = Math.ceil(list.length / c);
    var ch = Math.max(MIN_CELL_H, (areaH - GAP * (rows - 1)) / rows);
    for (var r = 0; r < rows; r++) {
      var inRow = Math.min(c, list.length - r * c);
      var cw = (areaW - GAP * (inRow - 1)) / inRow;
      for (var k = 0; k < inRow; k++) place(out[list[r * c + k]], x0 + k * (cw + GAP), y0 + r * (ch + GAP), cw, ch);
    }
  }

  // Lay `panels` out for a screen of vp {w, h}: the first map / world map / combat window is the STAGE (about half the
  // width, the full height); the others tile the remaining area. Without a stage — or with a single window — it is a
  // plain grid. opts.pref(panel) is how tall a window likes to be.
  function tilePanels(panels, vp, opts) {
    var pref = (opts && opts.pref) || function () { return 300; };
    var n = panels.length;
    if (!n) return [];
    var W = Math.max(MIN_W, vp.w), H = Math.max(MIN_H, vp.h);
    var inner = W - 2 * GAP, innerH = H - 2 * GAP;
    var cols = clamp(Math.round(W / COL_W), 1, MAX_COLS);
    var out = panels.map(copy);

    var stage = -1;
    for (var i = 0; i < n; i++) if (STAGE[panels[i].type]) { stage = i; break; }

    if (n === 1) { place(out[0], GAP, GAP, inner, innerH); return out; }

    if (stage < 0) {                                   // a plain grid
      var all = [];
      for (var g = 0; g < n; g++) all.push(g);
      placeGrid(out, all, GAP, GAP, inner, innerH, gridColumns(n, cols, inner, innerH, false).c);
      return out;
    }

    var rest = [];
    for (var j = 0; j < n; j++) if (j !== stage) rest.push(j);

    if (cols === 1) {                                  // too narrow for side by side: stage on top, the rest below
      var sh = Math.max(MIN_H * 2, Math.round(innerH * 0.62));
      place(out[stage], GAP, GAP, inner, sh);
      fillColumn(out, panels, rest, GAP, GAP + sh + GAP, inner, Math.max(rest.length * MIN_CELL_H, innerH - sh - GAP), pref);
      return out;
    }

    // The stage takes about half the columns (58% of a two-column screen), but never more than the rest of the windows
    // can fill: with few of them the stage gets the spare columns.
    var wantRest = cols - (cols === 2 ? 1 : clamp(Math.round(cols / 2), 1, cols - 1));
    var restCols = Math.min(wantRest, rest.length);
    var usable = inner - GAP;
    var share = cols === 2 ? 0.58 : (cols - restCols) / cols;
    var stageW = Math.round(usable * share);
    var restW = usable - stageW;
    place(out[stage], GAP, GAP, stageW, innerH);

    // ... and the rest tile their own area: stacked columns (each window as tall as it likes) or aligned rows, whichever
    // looks better there — a handful of windows beside a wide stage read better as a small grid than as flat bars.
    var stacked = gridColumns(rest.length, restCols, restW, innerH, true);
    var aligned = gridColumns(rest.length, restCols, restW, innerH, false);
    if (aligned.violation < stacked.violation) {
      placeGrid(out, rest, GAP + stageW + GAP, GAP, restW, innerH, aligned.c);
      return out;
    }
    var nc = stacked.c;
    var colW = (restW - GAP * (nc - 1)) / nc;
    var columns = [], heights = [];
    for (var m = 0; m < nc; m++) { columns.push([]); heights.push(0); }
    rest.forEach(function (idx) {                      // fewest windows first (no column gets a lone window while another holds three), then the shortest
      var best = 0;
      for (var q = 1; q < nc; q++) {
        if (columns[q].length < columns[best].length ||
            (columns[q].length === columns[best].length && heights[q] < heights[best])) best = q;
      }
      columns[best].push(idx);
      heights[best] += pref(panels[idx]);
    });
    columns.forEach(function (list, q) {
      fillColumn(out, panels, list, GAP + stageW + GAP + q * (colW + GAP), GAP, colW, innerH, pref);
    });
    return out;
  }

  // ── a window added later ─────────────────────────────────────────────────────────────────────

  // The type's usual size {w, h} for a 1500x820 workspace, scaled to this screen and never bigger than it.
  function sizeFor(spec, vp) {
    var f = clamp(Math.min(vp.w / 1500, vp.h / 820), 0.7, 1.8);
    var maxW = Math.max(1, vp.w - 2 * GAP), maxH = Math.max(MIN_H, vp.h - 2 * GAP);
    var w = Math.max(Math.min(MIN_W, maxW), Math.min(Math.round(spec.w * f), maxW));
    var h = Math.max(MIN_H, Math.min(Math.round(spec.h * f), maxH));
    return { w: w, h: h };
  }

  // Where the n-th added window goes: stepped down-right from the top-left, kept inside the screen.
  function cascade(n, size, vp) {
    var step = (n % 6) * 28;
    return {
      x: Math.max(0, Math.min(48 + step, vp.w - size.w - GAP)),
      y: Math.max(0, Math.min(40 + step, vp.h - 160)),
    };
  }

  root.ndCockpitLayout = {
    MIN_W: MIN_W, MIN_H: MIN_H, GRID: GRID, GAP: GAP,
    vpOk: vpOk, needsScale: needsScale, fitPanels: fitPanels, scalePanels: scalePanels,
    tilePanels: tilePanels, sizeFor: sizeFor, cascade: cascade,
  };
})(typeof window !== 'undefined' ? window : globalThis);
