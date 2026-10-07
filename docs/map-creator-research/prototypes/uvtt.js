// Universal VTT (.dd2vtt / .uvtt / .df2vtt) in and out.
//
// Everything the format says here was read off a REAL Dungeondraft 1.0.1.3 export (format 0.3) and two independent
// converters - see docs/map-creator-research/FORMATS.md for the evidence and its limits:
//   resolution.map_origin / map_size   in squares; coordinates elsewhere in the file are ABSOLUTE squares, so a point's pixel
//                                      is (x - map_origin.x) * pixels_per_grid
//   line_of_sight                      polylines of {x, y}; closed rings repeat the first point; cut where a door sits
//   objects_line_of_sight              outlines of objects that block light (pillars, furniture), 0.3
//   portals                            { position, bounds: [{x,y},{x,y}], rotation (radians), closed, freestanding }; a window
//                                      is just another portal in the file
//   environment                        { baked_lighting, ambient_light: "aarrggbb" (no #) }
//   lights                             { position, range (squares), intensity, color "aarrggbb", shadows }
//   image                              base64 PNG/WEBP of the whole map
// This module works on the data only; reading/writing files and the image are the caller's job.
(function (root) {
  'use strict';

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function isPt(p) { return p && isNum(p.x) && isNum(p.y); }

  // Problems with a parsed file, as plain strings. `errors` stop an import, `warnings` are things worth telling the GM.
  function validate(d) {
    var errors = [], warnings = [];
    if (!d || typeof d !== 'object') return { errors: ['not a JSON object'], warnings: warnings };
    if (!isNum(d.format)) errors.push('format must be a number (0.2 or 0.3)');
    else if (d.format > 0.3) warnings.push('format ' + d.format + ' is newer than 0.3; unknown parts are ignored');
    var r = d.resolution;
    if (!r || !r.map_size || !isNum(r.map_size.x) || !isNum(r.map_size.y) || r.map_size.x <= 0 || r.map_size.y <= 0) errors.push('resolution.map_size must be positive numbers');
    if (!r || !isNum(r.pixels_per_grid) || r.pixels_per_grid <= 0) errors.push('resolution.pixels_per_grid must be a positive number');
    else if (r.pixels_per_grid !== Math.round(r.pixels_per_grid)) warnings.push('pixels_per_grid is not a whole number');
    if (r && r.map_origin && !(isNum(r.map_origin.x) && isNum(r.map_origin.y))) errors.push('resolution.map_origin must be numbers');
    ['line_of_sight', 'objects_line_of_sight'].forEach(function (k) {
      if (d[k] === undefined) return;
      if (!Array.isArray(d[k])) { errors.push(k + ' must be a list of polylines'); return; }
      d[k].forEach(function (line, i) {
        if (!Array.isArray(line) || line.length < 2 || !line.every(isPt)) errors.push(k + '[' + i + '] must be a list of at least two {x, y} points');
      });
    });
    (d.portals || []).forEach(function (p, i) {
      if (!p || !isPt(p.position) || !Array.isArray(p.bounds) || p.bounds.length !== 2 || !p.bounds.every(isPt)) errors.push('portals[' + i + '] needs a position and two bounds points');
    });
    (d.lights || []).forEach(function (l, i) {
      if (!l || !isPt(l.position) || !isNum(l.range)) errors.push('lights[' + i + '] needs a position and a range');
      else if (typeof l.color === 'string' && !/^[0-9a-fA-F]{8}$/.test(l.color)) warnings.push('lights[' + i + '].color is not "aarrggbb"');
    });
    if (d.format === 0.3 && d.objects_line_of_sight === undefined) warnings.push('format 0.3 files normally carry objects_line_of_sight');
    if (typeof d.image !== 'string' || !d.image.length) warnings.push('no embedded image');
    return { errors: errors, warnings: warnings };
  }

  // Read a parsed file into plain map data in SQUARES relative to the map's own top-left corner (origin removed).
  function parse(d) {
    var v = validate(d);
    if (v.errors.length) { var e = new Error('Not a usable Universal VTT file: ' + v.errors[0]); e.problems = v.errors; throw e; }
    var r = d.resolution, ox = (r.map_origin && r.map_origin.x) || 0, oy = (r.map_origin && r.map_origin.y) || 0;
    function pt(p) { return [round6(p.x - ox), round6(p.y - oy)]; }
    function line(l) { return l.map(pt); }
    return {
      ppg: r.pixels_per_grid, w: r.map_size.x, h: r.map_size.y, origin: { x: ox, y: oy }, format: d.format,
      walls: (d.line_of_sight || []).map(line),
      objectWalls: (d.objects_line_of_sight || []).map(line),
      portals: (d.portals || []).map(function (p) {
        return { x: round6(p.position.x - ox), y: round6(p.position.y - oy), a: pt(p.bounds[0]), b: pt(p.bounds[1]),
          rotation: isNum(p.rotation) ? p.rotation : 0, closed: p.closed !== false, freestanding: !!p.freestanding };
      }),
      lights: (d.lights || []).map(function (l) {
        var c = typeof l.color === 'string' && /^[0-9a-fA-F]{8}$/.test(l.color) ? l.color : 'ffffffff';
        return { x: round6(l.position.x - ox), y: round6(l.position.y - oy), range: l.range, intensity: isNum(l.intensity) ? l.intensity : 1,
          color: '#' + c.slice(2).toLowerCase(), alpha: parseInt(c.slice(0, 2), 16) / 255, shadows: l.shadows !== false };
      }),
      env: { bakedLighting: !!(d.environment && d.environment.baked_lighting), ambient: (d.environment && d.environment.ambient_light) || 'ffffffff' },
      image: typeof d.image === 'string' ? d.image : null,
      warnings: v.warnings,
    };
  }

  function round6(n) { return Math.round(n * 1e6) / 1e6; }

  // Plain map data back into a file. `map` is what parse() returns (or what fromGrid() builds); `image` is base64.
  function build(map, image, opts) {
    opts = opts || {};
    var ox = map.origin ? map.origin.x : 0, oy = map.origin ? map.origin.y : 0;
    function pt(p) { return { x: round6(p[0] + ox), y: round6(p[1] + oy) }; }
    return {
      format: opts.format || 0.3,
      resolution: { map_origin: { x: ox, y: oy }, map_size: { x: map.w, y: map.h }, pixels_per_grid: map.ppg },
      line_of_sight: map.walls.map(function (l) { return l.map(pt); }),
      objects_line_of_sight: (map.objectWalls || []).map(function (l) { return l.map(pt); }),
      portals: map.portals.map(function (p) {
        return { position: { x: round6(p.x + ox), y: round6(p.y + oy) }, bounds: [pt(p.a), pt(p.b)], rotation: round6(p.rotation || 0), closed: p.closed !== false, freestanding: !!p.freestanding };
      }),
      environment: { baked_lighting: !!(map.env && map.env.bakedLighting), ambient_light: (map.env && map.env.ambient) || 'ffffffff' },
      lights: (map.lights || []).map(function (l) {
        var a = Math.max(0, Math.min(255, Math.round((l.alpha === undefined ? 1 : l.alpha) * 255)));
        return { position: { x: round6(l.x + ox), y: round6(l.y + oy) }, range: l.range, intensity: l.intensity === undefined ? 1 : l.intensity,
          color: (a < 16 ? '0' : '') + a.toString(16) + String(l.color || '#ffffff').replace('#', '').toLowerCase(), shadows: l.shadows !== false };
      }),
      image: image || '',
    };
  }

  // A generated map (wall segments with kinds, from gridwalls.js) as plain map data ready for build().
  //   opts.windows : 'wall' (default; the file cannot say "blocks walking but not seeing") | 'open' (a see-through, walk-through gap)
  //   opts.lights  : [{ x, y, range, color, intensity, shadows }] in squares
  function fromGrid(w, h, segs, polylinesFn, opts) {
    opts = opts || {};
    var warnings = [], walls = [], portals = [];
    var plain = segs.filter(function (s) {
      if (s.kind === 'secret') { warnings.push('secret door at (' + s.x1 + ',' + s.y1 + ') exported as a plain wall - Universal VTT has no secret doors'); return true; }
      if (s.kind === 'window') {
        if ((opts.windows || 'wall') === 'open') { portals.push(portal(s, false)); return false; }
        warnings.push('window at (' + s.x1 + ',' + s.y1 + ') exported as a wall - Universal VTT cannot say "blocks movement, not sight"');
        return true;
      }
      if (s.kind === 'door') { portals.push(portal(s, s.state !== 'open')); return false; }
      return true;
    });
    walls = polylinesFn(plain);
    return { ppg: opts.ppg || 70, w: w, h: h, origin: { x: 0, y: 0 }, format: 0.3, walls: walls, objectWalls: [], portals: portals,
      lights: opts.lights || [], env: { bakedLighting: false, ambient: 'ffffffff' }, image: null, warnings: warnings };
  }
  function portal(s, closed) {
    var dx = s.x2 - s.x1, dy = s.y2 - s.y1;
    return { x: (s.x1 + s.x2) / 2, y: (s.y1 + s.y2) / 2, a: [s.x1, s.y1], b: [s.x2, s.y2], rotation: round6(Math.atan2(dy, dx)), closed: closed, freestanding: false };
  }

  // Back to wall segments with kinds, for vision and movement: polylines -> segments, portals -> doors.
  function toSegments(map) {
    var segs = [];
    function polys(list, kind) {
      list.forEach(function (l) { for (var i = 0; i + 1 < l.length; i++) segs.push({ x1: l[i][0], y1: l[i][1], x2: l[i + 1][0], y2: l[i + 1][1], kind: kind, state: null }); });
    }
    polys(map.walls, 'wall');
    polys(map.objectWalls || [], 'object');
    map.portals.forEach(function (p) { segs.push({ x1: p.a[0], y1: p.a[1], x2: p.b[0], y2: p.b[1], kind: 'door', state: p.closed ? 'closed' : 'open' }); });
    return segs;
  }

  root.ndMapUvtt = { validate: validate, parse: parse, build: build, fromGrid: fromGrid, toSegments: toSegments };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapUvtt;
})(typeof window !== 'undefined' ? window : globalThis);
