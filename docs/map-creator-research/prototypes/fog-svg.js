// Fog of war inside an EXISTING SVG map view - no new renderer. Two translucent covers sit between the map and the tokens:
//   unexplored  an opaque cover with the explored cells cut out        (never seen: black)
//   shroud      a half-opaque cover with what is visible NOW cut out   (seen before, not in view now: dimmed)
// Both are plain <rect>s whose <mask> is a white rectangle with black "holes" drawn from one <path> each, so the browser does
// the compositing; a vision update is two attribute writes. Tokens that are not in view are hidden (display:none), because
// a dimmed cover would still let them show through.
//
// What it needs from the caller (all in the map's own pixel units):
//   walls    [{ x1, y1, x2, y2, kind, state }]      see vision.js (doors: state 'open' lets sight through)
//   sources  [{ x, y, range }]                      the tokens that see (a range of Infinity sees to the nearest wall)
// Explored memory is a grid of cells (one byte each), so it stays small and can be sent to the server as run lengths.
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var vision = typeof require === 'function' && typeof module !== 'undefined' ? require('./vision.js') : root.ndMapVision;
  var geom = typeof require === 'function' && typeof module !== 'undefined' ? require('./geom.js') : root.ndMapGeom;

  function el(tag, attrs) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }

  function polyPath(poly) {
    var d = '';
    for (var i = 0; i < poly.length; i++) d += (i ? 'L' : 'M') + poly[i][0].toFixed(1) + ' ' + poly[i][1].toFixed(1);
    return d + 'Z';
  }

  // Horizontal runs of explored cells as one path of rectangles.
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

  function create(svg, opts) {
    var W = opts.width, H = opts.height, cell = opts.cell || 50, cols = Math.ceil(W / cell), rows = Math.ceil(H / cell);
    var explored = opts.explored || new Uint8Array(cols * rows), walls = opts.walls || [];
    var shroudOpacity = opts.shroudOpacity === undefined ? 0.55 : opts.shroudOpacity;
    var uid = 'ndfog' + Math.floor(Math.random() * 1e6), tokenLayer = opts.tokenLayer;

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
    g.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#000', opacity: 1, mask: 'url(#' + uid + '-exp)' }));
    g.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#000', opacity: shroudOpacity, mask: 'url(#' + uid + '-vis)' }));
    if (tokenLayer && tokenLayer.parentNode === svg) svg.insertBefore(g, tokenLayer); else svg.appendChild(g);

    var api = {
      explored: explored, cols: cols, rows: rows, cell: cell, lastPolys: [],
      setWalls: function (w) { walls = w; },
      // sources -> visibility polygons -> two path writes; returns what it did, for measuring
      update: function (sources, tokens) {
        var polys = sources.map(function (s) { return vision.visibilityPolygon([s.x, s.y], walls, { range: s.range, bounds: { x0: 0, y0: 0, x1: W, y1: H } }); });
        api.lastPolys = polys;
        visHole.setAttribute('d', polys.map(polyPath).join(''));
        var marked = 0;
        polys.forEach(function (poly) {
          var bb = geom.bbox(poly), cx0 = Math.max(0, Math.floor(bb.x0 / cell)), cx1 = Math.min(cols - 1, Math.floor(bb.x1 / cell));
          var cy0 = Math.max(0, Math.floor(bb.y0 / cell)), cy1 = Math.min(rows - 1, Math.floor(bb.y1 / cell));
          for (var cy = cy0; cy <= cy1; cy++) for (var cx = cx0; cx <= cx1; cx++) {
            if (!explored[cy * cols + cx] && geom.pointInPolygon([(cx + 0.5) * cell, (cy + 0.5) * cell], poly)) { explored[cy * cols + cx] = 1; marked++; }
          }
        });
        expHole.setAttribute('d', exploredPath(explored, cols, rows, cell));
        var hidden = 0;
        if (tokens) tokens.forEach(function (t) {
          var inView = t.always || polys.some(function (p) { return geom.pointInPolygon([t.x, t.y], p); });
          if (t.node) { t.node.style.display = inView ? '' : 'none'; }
          if (!inView) hidden++;
        });
        return { polygons: polys.length, vertices: polys.reduce(function (n, p) { return n + p.length; }, 0), newlyExplored: marked, hiddenTokens: hidden };
      },
      // explored cells as run lengths [startIndex, length, ...] - what the server would store
      runs: function () {
        var out = [], i = 0;
        while (i < explored.length) { if (!explored[i]) { i++; continue; } var s = i; while (i < explored.length && explored[i]) i++; out.push(s, i - s); }
        return out;
      },
      destroy: function () { g.remove(); defs.querySelectorAll('mask[id^="' + uid + '"]').forEach(function (m) { m.remove(); }); },
    };
    return api;
  }

  root.ndMapFog = { create: create, exploredPath: exploredPath, polyPath: polyPath };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapFog;
})(typeof window !== 'undefined' ? window : globalThis);
