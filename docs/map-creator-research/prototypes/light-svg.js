// Coloured lights with falloff and real shadows inside the same SVG, no canvas and no WebGL:
//   darkness  a dark cover whose <mask> is white with each light's visibility polygon painted as BLACK that fades out with
//             distance (a radial gradient of black with falling opacity), so overlapping lights simply add up and the cover
//             thins out where there is light. (Painting the holes opaque black-to-white and blending them with `darken` looked
//             right in a toy page but did not punch holes inside the real page - plain alpha has no such dependency.)
//   tint      each light's polygon again, filled with a colour gradient, blended with `screen`, for the coloured glow
// A light is { x, y, range, color: '#rrggbb', intensity: 0..1 } in the map's own pixel units; shadows come from vision.js.
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var vision = typeof require === 'function' && typeof module !== 'undefined' ? require('./vision.js') : root.ndMapVision;

  function el(tag, attrs) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function polyPath(poly) { var d = ''; for (var i = 0; i < poly.length; i++) d += (i ? 'L' : 'M') + poly[i][0].toFixed(1) + ' ' + poly[i][1].toFixed(1); return d + 'Z'; }

  function create(svg, opts) {
    var W = opts.width, H = opts.height, walls = opts.walls || [], darkness = opts.darkness === undefined ? 0.8 : opts.darkness;
    var uid = 'ndlight' + Math.floor(Math.random() * 1e6);
    var defs = svg.querySelector('defs') || svg.insertBefore(el('defs', {}), svg.firstChild);
    var mask = el('mask', { id: uid + '-m', maskUnits: 'userSpaceOnUse', x: 0, y: 0, width: W, height: H });
    mask.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#fff' }));
    var holes = el('g', {});
    mask.appendChild(holes);
    defs.appendChild(mask);
    var g = el('g', { id: uid, 'pointer-events': 'none' });
    g.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: opts.color || '#04050c', opacity: darkness, mask: 'url(#' + uid + '-m)' }));
    var tint = el('g', {});
    g.appendChild(tint);
    if (opts.before && opts.before.parentNode === svg) svg.insertBefore(g, opts.before); else svg.appendChild(g);

    function gradient(id, cx, cy, r, stops) {
      var gr = el('radialGradient', { id: id, gradientUnits: 'userSpaceOnUse', cx: cx, cy: cy, r: r });
      stops.forEach(function (s) { gr.appendChild(el('stop', { offset: s[0], 'stop-color': s[1], 'stop-opacity': s[2] === undefined ? 1 : s[2] })); });
      return gr;
    }

    return {
      setWalls: function (w) { walls = w; },
      update: function (lights) {
        while (holes.firstChild) holes.removeChild(holes.firstChild);
        while (tint.firstChild) tint.removeChild(tint.firstChild);
        defs.querySelectorAll('radialGradient[id^="' + uid + '"]').forEach(function (n) { n.remove(); });
        var verts = 0;
        lights.forEach(function (l, i) {
          var poly = vision.visibilityPolygon([l.x, l.y], walls, { range: l.range, bounds: { x0: 0, y0: 0, x1: W, y1: H } });
          verts += poly.length;
          var d = polyPath(poly), k = l.intensity === undefined ? 1 : l.intensity;
          defs.appendChild(gradient(uid + '-h' + i, l.x, l.y, l.range, [[0, '#000', k], [0.55, '#000', k * 0.5], [1, '#000', 0]]));
          defs.appendChild(gradient(uid + '-t' + i, l.x, l.y, l.range, [[0, l.color, 0.35], [1, l.color, 0]]));
          holes.appendChild(el('path', { d: d, fill: 'url(#' + uid + '-h' + i + ')' }));
          tint.appendChild(el('path', { d: d, fill: 'url(#' + uid + '-t' + i + ')', style: 'mix-blend-mode:screen' }));
        });
        return { lights: lights.length, vertices: verts };
      },
      destroy: function () { g.remove(); mask.remove(); defs.querySelectorAll('radialGradient[id^="' + uid + '"]').forEach(function (n) { n.remove(); }); },
    };
  }

  root.ndMapLight = { create: create };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapLight;
})(typeof window !== 'undefined' ? window : globalThis);
