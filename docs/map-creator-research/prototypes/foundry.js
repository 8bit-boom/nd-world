// Universal VTT map data -> a Foundry VTT v13 Scene document (walls, doors, lights), plus a checker for the rules the v13
// schema enforces. The field rules come from the foundry-vtt-types package (types derived from Foundry's own source):
//   wall.c            4 INTEGER coordinates [x0, y0, x1, y1]
//   wall.move         0 (none) or 20 (normal) - movement has no "limited"/"proximity"/"distance"
//   wall.light/sight/sound   0 none, 10 limited, 20 normal, 30 proximity, 40 distance
//   wall.dir          0 both, 1 left, 2 right          wall.door  0 none, 1 door, 2 secret          wall.ds  0 closed, 1 open, 2 locked
//   light.x / light.y INTEGER                          light.config.shadows  a number 0..1 (not a boolean)
//   scene.grid.size   integer, at least 20             scene.grid.type  0 gridless, 1 square, 2-5 hex
// Pixel position of a map point:  (p - origin) * pixels_per_grid + paddingOffset, with
//   paddingOffset = ceil(width * padding / grid) * grid   (Foundry puts the map inside a padded canvas)
(function (root) {
  'use strict';

  var geom = typeof require === 'function' && typeof module !== 'undefined' ? require('./geom.js') : root.ndMapGeom;

  function paddingOffset(width, height, grid, padding) {
    return { x: Math.ceil((width * padding) / grid) * grid, y: Math.ceil((height * padding) / grid) * grid };
  }

  // `map` is what uvtt.parse() returns (squares relative to the map's top-left); coordinates become pixels here.
  //   opts: name, imageSrc, padding (default 0), gridDistance (default 5), gridUnits ('ft'), objectWalls ('skip' | 'block' | 'sight-only'),
  //         objectTolerance (squares; light-blocking outlines are simplified by this much, default 0.05, 0 = keep every point)
  function toScene(map, opts) {
    opts = opts || {};
    var ppg = map.ppg, padding = opts.padding === undefined ? 0 : opts.padding, dist = opts.gridDistance || 5;
    var width = Math.round(map.w * ppg), height = Math.round(map.h * ppg), off = paddingOffset(width, height, ppg, padding);
    var warnings = [], walls = [], doors = 0, lights = [];
    function px(p) { return [Math.round(p[0] * ppg + off.x), Math.round(p[1] * ppg + off.y)]; }
    function wall(a, b, props) {
      var p = px(a), q = px(b);
      if (p[0] === q[0] && p[1] === q[1]) return;                    // collapsed to a point by rounding
      var w = { c: [p[0], p[1], q[0], q[1]], move: 20, sight: 20, light: 20, sound: 20, dir: 0, door: 0, ds: 0 };
      for (var k in props) w[k] = props[k];
      walls.push(w);
    }
    map.walls.forEach(function (l) { for (var i = 0; i + 1 < l.length; i++) wall(l[i], l[i + 1]); });
    var mode = opts.objectWalls || 'skip';
    if (mode !== 'skip') {
      // objects that block light (pillars, furniture): sight and light always; walking only when asked
      var props = mode === 'sight-only' ? { move: 0, sound: 0 } : {};
      // a round pillar is drawn as ~60 points; as walls that is 59 tiny segments for one object, so outlines are simplified first
      var tol = opts.objectTolerance === undefined ? 0.05 : opts.objectTolerance;
      (map.objectWalls || []).forEach(function (l) { l = tol > 0 ? geom.simplify(l, tol) : l; for (var i = 0; i + 1 < l.length; i++) wall(l[i], l[i + 1], props); });
    } else if ((map.objectWalls || []).length) {
      warnings.push((map.objectWalls.length) + ' light-blocking object outline(s) skipped (set objectWalls to import them)');
    }
    map.portals.forEach(function (p) {
      var before = walls.length;
      wall(p.a, p.b, { door: 1, ds: p.closed ? 0 : 1 });
      if (walls.length > before) doors++;
    });
    (map.lights || []).forEach(function (l) {
      var p = px([l.x, l.y]), dim = Math.round(l.range * dist * 100) / 100;
      lights.push({ x: p[0], y: p[1], rotation: 0, walls: true, vision: false, hidden: false,
        config: { dim: dim, bright: Math.round(dim / 2 * 100) / 100, color: l.color, alpha: Math.min(1, 0.5 * (l.intensity === undefined ? 1 : l.intensity)), angle: 360,
                  luminosity: 0.5, shadows: l.shadows === false ? 0 : 0.5 } });
    });
    if (map.env && map.env.bakedLighting && lights.length) warnings.push('the image already has lighting baked in; Foundry lights add on top of it');
    if (map.portals.some(function (p) { return p.freestanding; })) warnings.push('freestanding portals (portcullis, bars) became ordinary doors');
    var scene = {
      name: opts.name || 'Imported map', background: { src: opts.imageSrc || null }, width: width, height: height, padding: padding,
      initial: { x: Math.round(width / 2 + off.x), y: Math.round(height / 2 + off.y), scale: 1 }, backgroundColor: '#999999',
      grid: { type: 1, size: ppg, color: '#000000', alpha: 0.2, distance: dist, units: opts.gridUnits || 'ft' },
      tokenVision: true, fog: { exploration: true }, environment: { darknessLevel: 0, globalLight: { enabled: false } },
      navigation: true, active: false, ownership: { default: 2 }, walls: walls, lights: lights,
    };
    return { scene: scene, stats: { walls: walls.length - doors, doors: doors, lights: lights.length, width: width, height: height, gridSize: ppg }, warnings: warnings };
  }

  // The rules above as a checker: returns a list of violations (empty = valid as far as these rules go).
  function validateScene(scene) {
    var bad = [];
    function int(v) { return typeof v === 'number' && isFinite(v) && Math.floor(v) === v; }
    var g = scene.grid || {};
    if (!int(g.size) || g.size < 20) bad.push('grid.size must be an integer >= 20');
    if (![0, 1, 2, 3, 4, 5].includes(g.type)) bad.push('grid.type must be 0-5');
    if (!(g.distance > 0)) bad.push('grid.distance must be positive');
    if (!int(scene.width) || scene.width <= 0 || !int(scene.height) || scene.height <= 0) bad.push('width/height must be positive integers');
    if (!(scene.padding >= 0 && scene.padding <= 0.5)) bad.push('padding must be 0..0.5');
    (scene.walls || []).forEach(function (w, i) {
      if (!Array.isArray(w.c) || w.c.length !== 4 || !w.c.every(int)) bad.push('walls[' + i + '].c must be 4 integers');
      [['move', [0, 20]], ['light', [0, 10, 20, 30, 40]], ['sight', [0, 10, 20, 30, 40]], ['sound', [0, 10, 20, 30, 40]],
        ['dir', [0, 1, 2]], ['door', [0, 1, 2]], ['ds', [0, 1, 2]]].forEach(function (f) {
        if (w[f[0]] !== undefined && f[1].indexOf(w[f[0]]) < 0) bad.push('walls[' + i + '].' + f[0] + ' = ' + w[f[0]] + ' is not allowed');
      });
    });
    (scene.lights || []).forEach(function (l, i) {
      if (!int(l.x) || !int(l.y)) bad.push('lights[' + i + '] x/y must be integers');
      var c = l.config || {};
      if (!(c.dim >= 0) || !(c.bright >= 0)) bad.push('lights[' + i + '].config dim/bright must be >= 0');
      if (c.shadows !== undefined && !(typeof c.shadows === 'number' && c.shadows >= 0 && c.shadows <= 1)) bad.push('lights[' + i + '].config.shadows must be a number 0..1');
      if (c.alpha !== undefined && !(c.alpha >= 0 && c.alpha <= 1)) bad.push('lights[' + i + '].config.alpha must be 0..1');
    });
    return bad;
  }

  root.ndMapFoundry = { toScene: toScene, validateScene: validateScene, paddingOffset: paddingOffset };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapFoundry;
})(typeof window !== 'undefined' ? window : globalThis);
