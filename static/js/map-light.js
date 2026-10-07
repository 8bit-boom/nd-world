// Lights and darkness inside the EXISTING SVG map views (the players' live view and the table's TV) - no new renderer.
//   darkness  a dark cover whose <mask> is white with each light's visibility polygon painted as BLACK that fades out with
//             distance (a radial gradient of black with falling opacity), so overlapping lights add up and the cover thins out
//             where there is light. (Opaque black-to-white holes blended with `darken` looked right in a toy page but did not
//             punch holes inside the real page; plain alpha has no such dependency.)
//   tint      each light's polygon again, filled with a colour gradient and blended with `screen`: the coloured glow
// Shadows come from the same visibility maths as fog of war (map-vision.js): a wall stops light.
//
//   ndMapLight.create(svg, {width, height, walls, darkness, before})  -> api
//   api.update(lights)   lights: [{x, y, range (px), color '#rrggbb', intensity 0..1}]  -> {lights, vertices}
//   api.isLit(x, y)      is the point inside any light from the last update (within its range)?
//   api.setWalls(w)  api.setDarkness(d)  api.destroy()
// Cost: measured in docs/map-creator-research/PROTOTYPES.md - about 2 ms for one light, 11 ms for thirty on a laptop, and thirty
// is too many for a phone, so callers cap how many lights are drawn at once.
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var isNode = typeof require === 'function' && typeof module !== 'undefined';
  var vision = isNode ? require('./map-vision.js') : root.ndMapVision;
  var geom = isNode ? require('./map-geom.js') : root.ndMapGeom;

  function el(tag, attrs) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function polyPath(poly) { var d = ''; for (var i = 0; i < poly.length; i++) d += (i ? 'L' : 'M') + poly[i][0].toFixed(1) + ' ' + poly[i][1].toFixed(1); return d + 'Z'; }
  var COLOUR = /^#[0-9a-fA-F]{6}$/;

  function create(svg, opts) {
    var W = opts.width, H = opts.height, walls = opts.walls || [], darkness = opts.darkness === undefined ? 0.8 : opts.darkness;
    var uid = 'ndlight' + Math.floor(Math.random() * 1e9), polys = [];
    var defs = svg.querySelector('defs') || svg.insertBefore(el('defs', {}), svg.firstChild);
    var mask = el('mask', { id: uid + '-m', maskUnits: 'userSpaceOnUse', x: 0, y: 0, width: W, height: H });
    mask.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#fff' }));
    var holes = el('g', {});
    mask.appendChild(holes);
    defs.appendChild(mask);
    var g = el('g', { id: uid, 'pointer-events': 'none' });
    var cover = el('rect', { x: 0, y: 0, width: W, height: H, fill: opts.color || '#04050c', opacity: darkness, mask: 'url(#' + uid + '-m)' });
    g.appendChild(cover);
    var tint = el('g', {});
    g.appendChild(tint);
    if (opts.before && opts.before.parentNode === svg) svg.insertBefore(g, opts.before); else svg.appendChild(g);

    function gradient(id, cx, cy, r, stops) {
      var gr = el('radialGradient', { id: id, gradientUnits: 'userSpaceOnUse', cx: cx, cy: cy, r: r });
      stops.forEach(function (s) { gr.appendChild(el('stop', { offset: s[0], 'stop-color': s[1], 'stop-opacity': s[2] === undefined ? 1 : s[2] })); });
      return gr;
    }
    function clearGradients() { defs.querySelectorAll('radialGradient[id^="' + uid + '"]').forEach(function (n) { n.remove(); }); }

    return {
      node: g,
      setWalls: function (w) { walls = w || []; },
      setDarkness: function (d) { darkness = d; cover.setAttribute('opacity', d); },
      update: function (lights) {
        while (holes.firstChild) holes.removeChild(holes.firstChild);
        while (tint.firstChild) tint.removeChild(tint.firstChild);
        clearGradients();
        polys = [];
        var verts = 0;
        (lights || []).forEach(function (l, i) {
          var poly = vision.visibilityPolygon([l.x, l.y], walls, { range: l.range, bounds: { x0: 0, y0: 0, x1: W, y1: H } });
          verts += poly.length;
          polys.push({ poly: poly, x: l.x, y: l.y, range: l.range });
          var color = COLOUR.test(l.color || '') ? l.color : '#ffd9a0';
          var k = l.intensity === undefined ? 1 : Math.min(1, Math.max(0.1, l.intensity));
          defs.appendChild(gradient(uid + '-h' + i, l.x, l.y, l.range, [[0, '#000', k], [0.55, '#000', k * 0.5], [1, '#000', 0]]));
          defs.appendChild(gradient(uid + '-t' + i, l.x, l.y, l.range, [[0, color, 0.35 * k], [1, color, 0]]));
          holes.appendChild(el('path', { d: polyPath(poly), fill: 'url(#' + uid + '-h' + i + ')' }));
          tint.appendChild(el('path', { d: polyPath(poly), fill: 'url(#' + uid + '-t' + i + ')', style: 'mix-blend-mode:screen' }));
        });
        return { lights: polys.length, vertices: verts };
      },
      // lit = inside a light's visibility polygon and within 90% of its range (the last tenth is the dim edge)
      isLit: function (x, y) {
        for (var i = 0; i < polys.length; i++) {
          var p = polys[i];
          if (Math.hypot(x - p.x, y - p.y) <= p.range * 0.9 && geom.pointInPolygon([x, y], p.poly)) return true;
        }
        return false;
      },
      destroy: function () { g.remove(); mask.remove(); clearGradients(); },
    };
  }

  root.ndMapLight = { create: create };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapLight;
})(typeof window !== 'undefined' ? window : globalThis);
