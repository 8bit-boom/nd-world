// Fog of war inside the EXISTING SVG map views (the players' live view and the table's TV) - no new renderer.
// Two covers sit between the map and the tokens:
//   unexplored  opaque black with the explored cells cut out        (never seen)
//   shroud      half-opaque black with what is visible NOW cut out  (seen before, not in view now)
// Both are plain <rect>s masked by a white rectangle with black "holes" from ONE <path> each, so the browser composites and a
// vision update is two attribute writes. Tokens that are not in view are hidden (display:none): a dimmed cover would still
// let them show through.
//
// This is TABLE-TRUST fog: the browser is given the walls and decides what it may show, so it keeps honest players honest and
// is not a secrecy boundary (a player who reads the page source can see the walls; a SECRET door is already sent as a plain
// wall, see app/map_walls.py). What a browser has explored is remembered in that browser (localStorage), and the GM's
// "reset fog" bumps an epoch that makes every browser forget it.
//
//   ndMapFog.create(svg, {width, height, cell, walls, tokenLayer})  -> api
//   api.update(sources, tokens)   sources: [{x, y, range}]  (range Infinity = to the nearest wall)
//                                 tokens:  [{x, y, node, always}]  nodes outside every view are hidden
//   api.setWalls(walls)           api.runs() / api.loadRuns(runs)   api.clear()   api.destroy()
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var isNode = typeof require === 'function' && typeof module !== 'undefined';
  var vision = isNode ? require('./map-vision.js') : root.ndMapVision;
  var geom = isNode ? require('./map-geom.js') : root.ndMapGeom;

  function el(tag, attrs) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }

  function polyPath(poly) {
    var d = '';
    for (var i = 0; i < poly.length; i++) d += (i ? 'L' : 'M') + poly[i][0].toFixed(1) + ' ' + poly[i][1].toFixed(1);
    return d + 'Z';
  }

  // horizontal runs of explored cells as one path of rectangles
  function exploredPath(cells, cols, rows, cell) {
    var d = '';
    for (var y = 0; y < rows; y++) {
      var x = 0;
      while (x < cols) {
        if (!cells[y * cols + x]) { x++; continue; }
        var x0 = x;
        while (x < cols && cells[y * cols + x]) x++;
        d += 'M' + x0 * cell + ' ' + y * cell + 'h' + (x - x0) * cell + 'v' + cell + 'h' + -(x - x0) * cell + 'z';
      }
    }
    return d;
  }

  // explored cells -> [start, length, start, length, ...]; and back (bounds-checked: stored data is never trusted)
  function toRuns(cells) {
    var out = [], i = 0;
    while (i < cells.length) { if (!cells[i]) { i++; continue; } var s = i; while (i < cells.length && cells[i]) i++; out.push(s, i - s); }
    return out;
  }
  function fromRuns(runs, size) {
    var cells = new Uint8Array(size);
    if (!Array.isArray(runs)) return cells;
    for (var i = 0; i + 1 < runs.length; i += 2) {
      var s = runs[i], n = runs[i + 1];
      if (!(Number.isInteger(s) && Number.isInteger(n)) || s < 0 || n < 0) continue;
      for (var k = s; k < Math.min(size, s + n); k++) cells[k] = 1;
    }
    return cells;
  }

  // The cell grid the explored memory lives on. Never smaller than 20 px, and never more than ~40k cells, so a huge canvas
  // with a tiny grid cannot make the memory (or the path that draws it) enormous.
  function memoryCell(width, height, cell) {
    var c = Math.max(20, cell || 50);
    while (Math.ceil(width / c) * Math.ceil(height / c) > 40000) c *= 2;
    return c;
  }

  function create(svg, opts) {
    var W = opts.width, H = opts.height, cell = memoryCell(W, H, opts.cell), cols = Math.ceil(W / cell), rows = Math.ceil(H / cell);
    var explored = new Uint8Array(cols * rows), walls = opts.walls || [];
    var shroudOpacity = opts.shroudOpacity === undefined ? 0.55 : opts.shroudOpacity;
    var uid = 'ndfog' + Math.floor(Math.random() * 1e9), tokenLayer = opts.tokenLayer;

    var defs = svg.querySelector('defs') || svg.insertBefore(el('defs', {}), svg.firstChild);
    function mask(id) {
      var m = el('mask', { id: id, maskUnits: 'userSpaceOnUse', x: 0, y: 0, width: W, height: H });
      m.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#fff' }));
      var p = el('path', { fill: '#000', d: '' });
      m.appendChild(p);
      defs.appendChild(m);
      return p;
    }
    var visHole = mask(uid + '-vis'), expHole = mask(uid + '-exp');
    var g = el('g', { id: uid, 'pointer-events': 'none' });
    g.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#000', mask: 'url(#' + uid + '-exp)' }));
    g.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#000', opacity: shroudOpacity, mask: 'url(#' + uid + '-vis)' }));
    if (tokenLayer && tokenLayer.parentNode === svg) svg.insertBefore(g, tokenLayer); else svg.appendChild(g);

    var api = {
      cols: cols, rows: rows, cell: cell, explored: explored, lastPolys: [],
      setWalls: function (w) { walls = w || []; },
      update: function (sources, tokens) {
        var polys = (sources || []).map(function (s) {
          return vision.visibilityPolygon([s.x, s.y], walls, { range: s.range, bounds: { x0: 0, y0: 0, x1: W, y1: H } });
        });
        api.lastPolys = polys;
        visHole.setAttribute('d', polys.map(polyPath).join(''));
        var marked = 0;
        polys.forEach(function (poly) {
          if (poly.length < 3) return;
          var bb = geom.bbox(poly);
          var cx0 = Math.max(0, Math.floor(bb.x0 / cell)), cx1 = Math.min(cols - 1, Math.floor(bb.x1 / cell));
          var cy0 = Math.max(0, Math.floor(bb.y0 / cell)), cy1 = Math.min(rows - 1, Math.floor(bb.y1 / cell));
          for (var cy = cy0; cy <= cy1; cy++) for (var cx = cx0; cx <= cx1; cx++) {
            if (!explored[cy * cols + cx] && geom.pointInPolygon([(cx + 0.5) * cell, (cy + 0.5) * cell], poly)) { explored[cy * cols + cx] = 1; marked++; }
          }
        });
        expHole.setAttribute('d', exploredPath(explored, cols, rows, cell));
        var hidden = 0;
        (tokens || []).forEach(function (t) {
          var inView = t.always || polys.some(function (p) { return geom.pointInPolygon([t.x, t.y], p); });
          if (t.node) t.node.style.display = inView ? '' : 'none';
          if (!inView) hidden++;
        });
        return { polygons: polys.length, newlyExplored: marked, hiddenTokens: hidden };
      },
      runs: function () { return toRuns(explored); },
      loadRuns: function (runs) { explored.set(fromRuns(runs, explored.length)); },
      clear: function () { explored.fill(0); expHole.setAttribute('d', ''); },
      destroy: function () {
        g.remove();
        defs.querySelectorAll('mask[id^="' + uid + '"]').forEach(function (m) { m.remove(); });
      },
    };
    return api;
  }

  root.ndMapFog = { create: create, exploredPath: exploredPath, polyPath: polyPath, toRuns: toRuns, fromRuns: fromRuns, memoryCell: memoryCell };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapFog;
})(typeof window !== 'undefined' ? window : globalThis);
