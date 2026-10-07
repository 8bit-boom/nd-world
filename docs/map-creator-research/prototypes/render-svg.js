// Draws a generated, furnished map as one SVG string: textured floors, stone walls with a drop shadow, doors, props. Pure string
// building, so it runs under Node (for tests and the pictures in the research) and in a browser alike. 100 units = 1 cell.
(function (root) {
  'use strict';
  var assets = typeof require === 'function' && typeof module !== 'undefined' ? require('./assets.js') : root.ndMapAssets;
  var textures = typeof require === 'function' && typeof module !== 'undefined' ? require('./textures.js') : root.ndMapTextures;
  var U = 100;

  var FLOOR_OF = { tavern: 'wood', kitchen: 'wood', library: 'wood', bedroom: 'wood', shrine: 'marble', barracks: 'stone', storeroom: 'stone', hall: 'stone', cavern: 'dirt' };

  function n(v) { return Math.round(v * 10) / 10; }
  function path(s) { return 'M' + n(s.x1 * U) + ' ' + n(s.y1 * U) + 'L' + n(s.x2 * U) + ' ' + n(s.y2 * U); }

  function door(s) {
    var horizontal = s.y1 === s.y2, cx = (s.x1 + s.x2) / 2 * U, cy = (s.y1 + s.y2) / 2 * U, open = s.state === 'open', locked = s.state === 'locked';
    var leaf = open ? '<rect x="-6" y="-46" width="12" height="92" rx="2" fill="#7a4a26" stroke="#2b1c10" stroke-width="3" transform="translate(' + (horizontal ? -40 : 0) + ' 0)"/>'
      : '<rect x="-46" y="-9" width="92" height="18" rx="3" fill="#8a5a2e" stroke="#2b1c10" stroke-width="3"/><path d="M-30 0H30" stroke="#5a3a1c" stroke-width="2"/>' + (locked ? '<circle cx="0" cy="0" r="6" fill="#d8b43a" stroke="#2b1c10" stroke-width="2"/>' : '<circle cx="24" cy="0" r="3.5" fill="#d8b43a"/>');
    var rotate = open ? (horizontal ? 0 : 90) : (horizontal ? 0 : 90);
    // an open door is drawn swung back along the wall; keep it simple: a thin leaf lying along the wall line
    if (open) leaf = '<rect x="-46" y="-5" width="92" height="10" rx="2" fill="#7a4a26" stroke="#2b1c10" stroke-width="2.5" opacity=".95" transform="translate(0 ' + (horizontal ? -22 : -22) + ')"/><path d="M-46 0H46" stroke="#1b1208" stroke-width="2" stroke-dasharray="3 5" opacity=".6"/>';
    return '<g transform="translate(' + n(cx) + ' ' + n(cy) + ') rotate(' + rotate + ')">' + leaf + '</g>';
  }
  function windowGlyph(s) {
    var horizontal = s.y1 === s.y2, cx = (s.x1 + s.x2) / 2 * U, cy = (s.y1 + s.y2) / 2 * U;
    return '<g transform="translate(' + n(cx) + ' ' + n(cy) + ') rotate(' + (horizontal ? 0 : 90) + ')"><rect x="-40" y="-8" width="80" height="16" fill="#9fc6e6" stroke="#2b1c10" stroke-width="2.5" opacity=".9"/><path d="M0 -8V8" stroke="#2b1c10" stroke-width="2"/></g>';
  }

  // map: { w, h, ids, rooms, corridorId }   segs: merged wall segments   opts: { kinds, placements, grid, noise, gmMarks }
  function render(map, segs, opts) {
    opts = opts || {};
    var W = map.w * U, H = map.h * U, out = [];
    out.push('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + ' ' + H + '" width="' + (opts.width || W) + '" height="' + Math.round((opts.width || W) * H / W) + '">');
    out.push(textures.defs());
    out.push('<rect width="' + W + '" height="' + H + '" fill="#15120d"/>');

    // floors
    map.rooms.forEach(function (r) {
      var fl = FLOOR_OF[(opts.kinds || {})[r.id]] || 'stone';
      out.push('<rect x="' + r.x * U + '" y="' + r.y * U + '" width="' + r.w * U + '" height="' + r.h * U + '" fill="url(#p-' + fl + ')"/>');
    });
    for (var y = 0; y < map.h; y++) {
      var x = 0;
      while (x < map.w) {
        if (map.ids[y * map.w + x] !== map.corridorId) { x++; continue; }
        var x0 = x; while (x < map.w && map.ids[y * map.w + x] === map.corridorId) x++;
        out.push('<rect x="' + x0 * U + '" y="' + y * U + '" width="' + (x - x0) * U + '" height="' + U + '" fill="url(#p-corridor)"/>');
      }
    }
    if (opts.noise !== false) out.push('<rect width="' + W + '" height="' + H + '" fill="#000" filter="url(#f-noise)" style="mix-blend-mode:multiply" opacity=".5"/>');

    // decor (rugs, stairs) under everything
    var placements = opts.placements || [];
    function prop(p) {
      var a = assets.ASSETS[p.asset];
      return '<g transform="translate(' + n(p.cx * U) + ' ' + n(p.cy * U) + ') rotate(' + p.rot + ') translate(' + -a.w * U / 2 + ' ' + -a.h * U / 2 + ')">' + assets.svgOf(p.asset, { shadow: !p.decor }) + '</g>';
    }
    placements.filter(function (p) { return p.decor && p.asset !== 'torch_wall'; }).forEach(function (p) { out.push(prop(p)); });

    // walls: shadow, dark edge, textured stone
    var wallSegs = segs.filter(function (s) { return s.kind === 'wall' || s.kind === 'secret'; });
    var d = wallSegs.map(path).join('');
    out.push('<path d="' + d + '" stroke="#000" stroke-opacity=".38" stroke-width="22" stroke-linecap="square" fill="none" transform="translate(4 7)"/>');
    out.push('<path d="' + d + '" stroke="#1d1a16" stroke-width="18" stroke-linecap="square" fill="none"/>');
    out.push('<path d="' + d + '" stroke="url(#p-wallstone)" stroke-width="12" stroke-linecap="square" fill="none"/>');
    segs.forEach(function (s) {
      if (s.kind === 'door') out.push(door(s));
      else if (s.kind === 'window') { out.push('<path d="' + path(s) + '" stroke="#1d1a16" stroke-width="18" stroke-linecap="square"/>'); out.push(windowGlyph(s)); }
      else if (s.kind === 'secret' && opts.gmMarks) out.push('<circle cx="' + n((s.x1 + s.x2) / 2 * U) + '" cy="' + n((s.y1 + s.y2) / 2 * U) + '" r="9" fill="#c04a7a" stroke="#2b1c10" stroke-width="2"/>');
    });

    // furniture and lights on top
    placements.filter(function (p) { return !p.decor || p.asset === 'torch_wall'; }).forEach(function (p) { out.push(prop(p)); });

    if (opts.grid) {
      var g = '';
      for (var gx = 0; gx <= map.w; gx++) g += 'M' + gx * U + ' 0V' + H;
      for (var gy = 0; gy <= map.h; gy++) g += 'M0 ' + gy * U + 'H' + W;
      out.push('<path d="' + g + '" stroke="#fff" stroke-opacity=".10" stroke-width="1.5" fill="none"/>');
    }
    out.push('</svg>');
    return out.join('\n');
  }

  root.ndMapRenderSvg = { render: render, FLOOR_OF: FLOOR_OF };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapRenderSvg;
})(typeof window !== 'undefined' ? window : globalThis);
