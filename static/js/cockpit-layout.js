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
    if (opts && opts.stage != null && opts.stage >= 0 && opts.stage < n) stage = opts.stage;   // the caller names the main window
    else for (var i = 0; i < n; i++) if (STAGE[panels[i].type]) { stage = i; break; }

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

  // ── the laptop layout ────────────────────────────────────────────────────────────────────────
  //
  // A laptop (a Vivobook is 1280-1536 CSS px wide) cannot show the nine windows a 4K monitor does: a layout arranged there
  // and merely scaled down is a row of 240px slivers. So below LAPTOP_BELOW the cockpit shows a few windows at a time, big
  // (a main window plus one or two beside it) and keeps the rest in a dock. A `parked` window is not on screen but is still
  // a window - its page, notes and settings are kept - and one click on its chip brings it back.
  //
  // Both screen classes share the windows and keep their OWN geometry: a saved panel carries the big screen's geometry in
  // x/y/w/h/z/collapsed (as it always did, so older code and saved layouts are untouched) and the laptop's in `lap`. A WORKING
  // panel (what cockpit.js drags around) holds the geometry of the CURRENT class in those same fields and stashes the other
  // one in _wide / _lap; toWorking / toSaved convert, so nothing else needs to know there are two.
  var LAPTOP_BELOW = 1700, LAPTOP_ENTER = 1640, LAPTOP_LEAVE = 1760;   // window width in CSS px; the gap is hysteresis
  var GEO = ['x', 'y', 'w', 'h', 'z', 'collapsed'];
  var PRIVATE = ['_wide', '_lap', 'parked', 'lap', 'swapped'];

  // 'laptop' or 'wide' for a window `width` CSS px wide. By WIDTH only: the browser's fullscreen (F11) changes the height,
  // not the width, so it never flips the layout, and a window snapped to half a big monitor gets the laptop layout too.
  // `prev` adds hysteresis so a sidebar opening does not flip it; `override` is the user's own choice ('laptop' / 'wide').
  function screenClass(width, prev, override) {
    if (override === 'laptop' || override === 'wide') return override;
    if (!isFinite(width) || width <= 0) return prev === 'laptop' ? 'laptop' : 'wide';
    if (prev === 'laptop') return width > LAPTOP_LEAVE ? 'wide' : 'laptop';
    if (prev === 'wide') return width < LAPTOP_ENTER ? 'laptop' : 'wide';
    return width < LAPTOP_BELOW ? 'laptop' : 'wide';
  }

  // How many windows are comfortable at once: one on a narrow screen, otherwise the main window with one beside it, and a
  // second when the side column is tall enough to be split in two.
  function laptopSlots(vp) {
    if (!vpOk(vp)) return 2;
    if (vp.w < 1000) return 1;
    return vp.h >= 600 ? 3 : 2;
  }

  // The GM's choice ('auto', 1, 2, 3 or 'all') as a number ('all' is Infinity), never more than there are windows.
  function resolveSlots(pref, vp, count) {
    var n;
    if (pref === 'all') n = Infinity;
    else if (pref === 1 || pref === 2 || pref === 3 || pref === '1' || pref === '2' || pref === '3') n = +pref;
    else n = laptopSlots(vp);
    return Math.max(1, Math.min(n, Math.max(1, count || 1)));
  }

  function visibleOf(panels) { return panels.filter(function (p) { return !p.parked; }); }
  function area(p) { return (p.w || 0) * (p.h || 0); }
  function recent(a, b) { return (b.z || 0) - (a.z || 0); }               // most recently used first
  function byPosition(a, b) { return (a.y - b.y) || (a.x - b.x); }
  function pickGeo(p) { var g = {}; GEO.forEach(function (k) { g[k] = p[k]; }); return g; }
  function putGeo(p, g) { GEO.forEach(function (k) { if (g && g[k] !== undefined) p[k] = g[k]; }); }
  function putBox(to, from) { to.x = from.x; to.y = from.y; to.w = from.w; to.h = from.h; }

  // The main window: the biggest one showing (a map / combat window wins a tie).
  function mainOf(vis) {
    var best = null;
    vis.forEach(function (p) {
      if (!best || area(p) > area(best) || (area(p) === area(best) && STAGE[p.type] && !STAGE[best.type])) best = p;
    });
    return best;
  }

  // Tile the windows that are showing: `main` big, the others beside it (`all`: the ordinary tiler, a map / combat window
  // is the stage and more windows than fit simply scroll).
  function layoutShowing(panels, vp, main, all, pref) {
    var vis = visibleOf(panels);
    if (!vis.length) return;
    var ordered = all ? vis.slice().sort(byPosition) : [main].concat(vis.filter(function (p) { return p !== main; }).sort(byPosition));
    var tiled = tilePanels(ordered, vp, { pref: pref, stage: all ? undefined : 0 });
    ordered.forEach(function (p, i) { putBox(p, tiled[i]); p.collapsed = false; });
  }

  // Make what is showing fit `slots` (too many: the least recently used, never the main one, are parked; too few while some
  // wait in the dock: the most recently used come back), then tile it.
  function retileLaptop(panels, vp, slots, pref) {
    if (!panels.length) return panels;
    var all = slots === Infinity || slots >= panels.length;
    if (all) panels.forEach(function (p) { p.parked = false; });
    var vis = visibleOf(panels);
    if (!all && vis.length > slots) {
      var keep = mainOf(vis);
      vis.filter(function (p) { return p !== keep; }).sort(recent).slice(slots - 1).forEach(function (p) { p.parked = true; });
    } else if (!all && vis.length < slots) {
      panels.filter(function (p) { return p.parked; }).sort(recent).slice(0, slots - vis.length).forEach(function (p) { p.parked = false; });
    }
    vis = visibleOf(panels);
    if (!vis.length) { panels.slice().sort(recent)[0].parked = false; vis = visibleOf(panels); }
    layoutShowing(panels, vp, mainOf(vis), all, pref);
    return panels;
  }

  // Bring a window on screen. Already showing: just to the front. Room left: tile it in (nobody else comes out of the dock).
  // Otherwise it takes the place and size of one window besides the main one, which goes to the dock: the window brought in
  // LAST if there is one (so a slot of the screen rotates through the dock while the windows beside it stay - party vitals
  // that are only glanced at are never the ones to go - and peeking at a window then bringing the other back restores
  // exactly what was there), else the least recently used. Returns what happened: 'none' | 'front' | 'tiled' | 'swapped'.
  function showLaptop(panels, id, vp, slots, pref) {
    var p = null;
    panels.forEach(function (q) { if (q.id === id) p = q; });
    if (!p) return 'none';
    p.z = panels.reduce(function (m, q) { return Math.max(m, q.z || 0); }, 0) + 1;
    p.collapsed = false;
    if (!p.parked) return 'front';
    var vis = visibleOf(panels);
    if (slots === Infinity || vis.length < slots) {
      p.parked = false;
      retileLaptop(panels, vp, slots === Infinity ? Infinity : vis.length + 1, pref);
      return 'tiled';
    }
    var main = mainOf(vis);
    var pool = vis.length > 1 ? vis.filter(function (q) { return q !== main; }) : vis;
    var victim = pool.sort(function (a, b) { return ((b.swapped || 0) - (a.swapped || 0)) || ((a.z || 0) - (b.z || 0)); })[0];
    putBox(p, victim);
    p.swapped = panels.reduce(function (m, q) { return Math.max(m, q.swapped || 0); }, 0) + 1;
    delete victim.swapped;
    victim.parked = true;
    p.parked = false;
    return 'swapped';
  }

  // Send a window to the dock; the ones left fill the space. One window always stays on screen (returns false then).
  function parkLaptop(panels, id, vp, pref) {
    var vis = visibleOf(panels);
    var p = vis.filter(function (q) { return q.id === id; })[0];
    if (!p || vis.length < 2) return false;
    p.parked = true;
    retileLaptop(panels, vp, vis.length - 1, pref);
    return true;
  }

  // Make `id` the main window: it swaps places (and size) with the one that is main now.
  function makeMain(panels, id) {
    var vis = visibleOf(panels);
    var p = vis.filter(function (q) { return q.id === id; })[0];
    var main = mainOf(vis);
    if (!p || !main || p === main) return false;
    var tmp = { x: p.x, y: p.y, w: p.w, h: p.h };
    putBox(p, main);
    putBox(main, tmp);
    return true;
  }

  // A layout arranged for another screen, on this laptop for the first time: the same windows, the map / combat window (or
  // else the one nearest the top-left) as main, the next ones beside it, everything else waiting in the dock.
  function deriveLaptop(panels, vp, slots, pref) {
    var ranked = panels.slice().sort(function (a, b) {
      return (STAGE[b.type] ? 1 : 0) - (STAGE[a.type] ? 1 : 0) || byPosition(a, b);
    });
    var n = slots === Infinity ? ranked.length : Math.min(slots, ranked.length);
    ranked.forEach(function (p, i) { p.parked = i >= n; p.z = ranked.length - i; });   // recency follows importance
    if (!ranked.length) return panels;
    layoutShowing(panels, vp, ranked[0], slots === Infinity, pref);
    ranked.forEach(function (p) { if (p.parked) putBox(p, ranked[0]); });                // somewhere sane for when it is shown
    return panels;
  }

  // Saved panels (the big screen's geometry on top, the laptop's in `lap`) → working panels for `opts.klass`
  // ('wide' | 'laptop'; laptop also needs opts.vp, opts.slots, opts.pref). `derived`: the laptop had no arrangement yet and
  // this one was made from the big screen's windows - the caller treats it as already in this screen's pixels.
  function toWorking(saved, opts) {
    var list = saved.map(function (s) {
      var p = copy(s), lap = p.lap;
      PRIVATE.forEach(function (k) { delete p[k]; });
      p._wide = pickGeo(p);
      p._lap = lap && typeof lap === 'object' ? copy(lap) : null;
      return p;
    });
    var derived = false;
    if (opts.klass === 'laptop' && list.length) {
      if (!list.some(function (p) { return p._lap; })) {
        deriveLaptop(list, opts.vp, opts.slots, opts.pref);
        derived = true;
      } else {
        list.forEach(function (p) {
          if (p._lap) { putGeo(p, p._lap); p.parked = !!p._lap.parked; } else p.parked = true;   // added on the big screen since: waits in the dock
        });
        if (!visibleOf(list).length) list.slice().sort(recent)[0].parked = false;
        var main = mainOf(visibleOf(list));
        list.forEach(function (p) { if (!p._lap) putBox(p, main); });
      }
    }
    return { panels: list, derived: derived };
  }

  // Working panels → what is saved: the current class's geometry goes where it belongs, the stashed one is kept as it was.
  function toSaved(list, klass) {
    return list.map(function (p) {
      var out = copy(p);
      PRIVATE.forEach(function (k) { delete out[k]; });
      var wide, lap;
      if (klass === 'laptop') {
        lap = pickGeo(p);
        lap.parked = !!p.parked;
        wide = p._wide || pickGeo(p);
      } else {
        wide = pickGeo(p);
        lap = p._lap || null;
      }
      putGeo(out, wide);
      if (lap) out.lap = lap;
      return out;
    });
  }

  root.ndCockpitLayout = {
    MIN_W: MIN_W, MIN_H: MIN_H, GRID: GRID, GAP: GAP, LAPTOP_BELOW: LAPTOP_BELOW,
    vpOk: vpOk, needsScale: needsScale, fitPanels: fitPanels, scalePanels: scalePanels,
    tilePanels: tilePanels, sizeFor: sizeFor, cascade: cascade,
    screenClass: screenClass, laptopSlots: laptopSlots, resolveSlots: resolveSlots, visibleOf: visibleOf, mainOf: mainOf,
    retileLaptop: retileLaptop, showLaptop: showLaptop, parkLaptop: parkLaptop, makeMain: makeMain,
    deriveLaptop: deriveLaptop, toWorking: toWorking, toSaved: toSaved,
  };
})(typeof window !== 'undefined' ? window : globalThis);
