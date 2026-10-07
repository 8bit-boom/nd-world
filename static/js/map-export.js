// Exporting a map for other tools: Universal VTT (.dd2vtt, read by Foundry, Roll20 via modules, Owlbear, Arkenforge, Fantasy
// Grounds...) and a Foundry VTT v13 scene. The conversions are pure functions (tested under Node, and round-tripped through the
// Python importer in tests/test_map_export.py); the picture is rendered in the browser from the map's own SVG.
//
// The rules come from the real files and type definitions documented in docs/map-creator-research/FORMATS.md:
//   Universal VTT   every coordinate is in SQUARES; line_of_sight are polylines (walls), portals are doors
//   Foundry v13     wall.c = 4 INTEGER pixel coordinates; move is only 0|20; door 0|1|2 (none/door/secret); ds 0|1|2 (closed/open/
//                   locked); grid.size is an integer >= 20; light x/y are integers
// What cannot be said in a format is reported, never silently dropped (a secret door in Universal VTT, a window as a wall...).
//
//   ndMapExport.toUvtt({walls, cell, canvasW, canvasH, ppg, imageB64})            -> {file, warnings}
//   ndMapExport.toFoundryScene({walls, cell, canvasW, canvasH, ppg, imageSrc, name}) -> {scene, stats, warnings}
//   ndMapExport.validateScene(scene)                                               -> [problems]
//   ndMapExport.renderPng({...})  (browser)  -> base64 PNG        ndMapExport.exportUvtt / exportFoundry  (browser, download)
(function (root) {
  'use strict';

  var MAX_PIXELS = 40e6;

  function r6(n) { return Math.round(n * 1e6) / 1e6; }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  // walls with fewer than two usable points are dropped; unusable points inside a wall are skipped (the server already
  // validates, this is the belt to its braces)
  function usable(walls) {
    return (walls || []).map(function (w) {
      if (!w || !Array.isArray(w.pts)) return null;
      var pts = w.pts.filter(function (p) { return Array.isArray(p) && isNum(p[0]) && isNum(p[1]); });
      return pts.length < 2 ? null : { id: w.id, kind: w.kind, state: w.state, pts: pts };
    }).filter(Boolean);
  }

  function scaleFor(o, warnings) {
    var cell = isNum(o.cell) && o.cell > 0 ? o.cell : 50;
    var sx = o.canvasW / cell, sy = o.canvasH / cell;
    var ppg = Math.round(o.ppg || 70);
    ppg = Math.max(20, Math.min(280, ppg));
    // keep the picture within what a browser canvas and a VTT can hold
    while (Math.round(sx * ppg) * Math.round(sy * ppg) > MAX_PIXELS && ppg > 20) ppg -= 5;
    if (Math.round(sx * ppg) * Math.round(sy * ppg) > MAX_PIXELS) throw new Error('This map is too large to export as one picture (more than ' + (MAX_PIXELS / 1e6) + ' megapixels even at 20 px per square).');
    if (o.ppg && ppg !== Math.round(o.ppg)) warnings.push('The picture was made smaller (' + ppg + ' px per square) so it stays under ' + (MAX_PIXELS / 1e6) + ' megapixels.');
    return { cell: cell, sx: sx, sy: sy, ppg: ppg };
  }

  function gridWarning(o, warnings) {
    if (o.offsetX || o.offsetY) warnings.push('The map grid is offset from the top-left corner; Universal VTT and Foundry assume it starts there, so grid lines may not line up.');
    if (!o.hasGrid) warnings.push('This map has no square grid, so squares of ' + (o.cell || 50) + ' px were assumed.');
  }

  // ── Universal VTT ───────────────────────────────────────────────────────────
  function toUvtt(o) {
    var warnings = [];
    var sc = scaleFor(o, warnings);
    gridWarning(o, warnings);
    var los = [], portals = [], secrets = 0, windows = 0;
    function sq(p) { return { x: r6(p[0] / sc.cell), y: r6(p[1] / sc.cell) }; }
    usable(o.walls).forEach(function (w) {
      if (w.kind === 'door') {
        var a = w.pts[0], b = w.pts[w.pts.length - 1];
        portals.push({ position: { x: r6((a[0] + b[0]) / 2 / sc.cell), y: r6((a[1] + b[1]) / 2 / sc.cell) }, bounds: [sq(a), sq(b)],
          rotation: r6(Math.atan2(b[1] - a[1], b[0] - a[0])), closed: w.state !== 'open', freestanding: false });
        return;
      }
      if (w.kind === 'secret') secrets++;
      if (w.kind === 'window') windows++;
      los.push(w.pts.map(sq));
    });
    if (secrets) warnings.push(secrets + ' secret door(s) exported as plain walls - Universal VTT has no secret doors.');
    if (windows) warnings.push(windows + ' window(s) exported as walls - Universal VTT cannot say "blocks movement, not sight".');
    var file = {
      format: 0.3,
      resolution: { map_origin: { x: 0, y: 0 }, map_size: { x: r6(sc.sx), y: r6(sc.sy) }, pixels_per_grid: sc.ppg },
      line_of_sight: los, objects_line_of_sight: [], portals: portals,
      environment: { baked_lighting: false, ambient_light: 'ffffffff' }, lights: [], image: o.imageB64 || '',
    };
    return { file: file, warnings: warnings, ppg: sc.ppg, width: Math.round(sc.sx * sc.ppg), height: Math.round(sc.sy * sc.ppg) };
  }

  // ── Foundry VTT v13 scene ───────────────────────────────────────────────────
  function toFoundryScene(o) {
    var warnings = [];
    var sc = scaleFor(o, warnings);
    gridWarning(o, warnings);
    var width = Math.round(sc.sx * sc.ppg), height = Math.round(sc.sy * sc.ppg), k = sc.ppg / sc.cell;
    var walls = [], doors = 0, secrets = 0, windows = 0;
    function add(a, b, props) {
      var x0 = Math.round(a[0] * k), y0 = Math.round(a[1] * k), x1 = Math.round(b[0] * k), y1 = Math.round(b[1] * k);
      if (x0 === x1 && y0 === y1) return;
      var w = { c: [x0, y0, x1, y1], move: 20, sight: 20, light: 20, sound: 20, dir: 0, door: 0, ds: 0 };
      for (var key in props) w[key] = props[key];
      walls.push(w);
    }
    usable(o.walls).forEach(function (w) {
      var kind = w.kind;
      if (kind === 'door' || kind === 'secret') {
        var a = w.pts[0], b = w.pts[w.pts.length - 1];
        var before = walls.length;
        add(a, b, { door: kind === 'secret' ? 2 : 1, ds: w.state === 'open' ? 1 : 0 });
        if (walls.length > before) { if (kind === 'secret') secrets++; else doors++; }
        return;
      }
      var props = {};
      if (kind === 'window') { props = { sight: 0, light: 0 }; windows++; }     // see through and light through, but not walk through
      for (var i = 0; i + 1 < w.pts.length; i++) add(w.pts[i], w.pts[i + 1], props);
    });
    var scene = {
      name: o.name || 'Imported map', background: { src: o.imageSrc || null }, width: width, height: height, padding: 0,
      initial: { x: Math.round(width / 2), y: Math.round(height / 2), scale: 1 }, backgroundColor: '#999999',
      grid: { type: 1, size: sc.ppg, color: '#000000', alpha: 0.2, distance: o.gridDistance || 5, units: o.gridUnits || 'ft' },
      tokenVision: true, fog: { exploration: true }, environment: { darknessLevel: 0, globalLight: { enabled: false } },
      navigation: true, active: false, ownership: { default: 2 }, walls: walls, lights: [],
    };
    warnings.push('Lights, props and tokens are not part of the scene; the picture is the map.');
    return { scene: scene, warnings: warnings, stats: { walls: walls.length - doors - secrets, doors: doors, secretDoors: secrets, windows: windows, width: width, height: height, gridSize: sc.ppg } };
  }

  // the field rules of Foundry v13's schema that matter here, as a checker (empty list = valid as far as these rules go)
  function validateScene(scene) {
    var bad = [];
    function int(v) { return typeof v === 'number' && isFinite(v) && Math.floor(v) === v; }
    var g = scene.grid || {};
    if (!int(g.size) || g.size < 20) bad.push('grid.size must be an integer >= 20');
    if ([0, 1, 2, 3, 4, 5].indexOf(g.type) < 0) bad.push('grid.type must be 0-5');
    if (!(g.distance > 0)) bad.push('grid.distance must be positive');
    if (!int(scene.width) || scene.width <= 0 || !int(scene.height) || scene.height <= 0) bad.push('width/height must be positive integers');
    (scene.walls || []).forEach(function (w, i) {
      if (!Array.isArray(w.c) || w.c.length !== 4 || !w.c.every(int)) bad.push('walls[' + i + '].c must be 4 integers');
      [['move', [0, 20]], ['light', [0, 10, 20, 30, 40]], ['sight', [0, 10, 20, 30, 40]], ['sound', [0, 10, 20, 30, 40]],
        ['dir', [0, 1, 2]], ['door', [0, 1, 2]], ['ds', [0, 1, 2]]].forEach(function (f) {
        if (w[f[0]] !== undefined && f[1].indexOf(w[f[0]]) < 0) bad.push('walls[' + i + '].' + f[0] + ' = ' + w[f[0]] + ' is not allowed');
      });
    });
    return bad;
  }

  // ── the picture (browser only) ──────────────────────────────────────────────
  function toDataUrl(blob) {
    return new Promise(function (res, rej) { var fr = new FileReader(); fr.onload = function () { res(fr.result); }; fr.onerror = rej; fr.readAsDataURL(blob); });
  }
  // An SVG shown through <img> may not load other files, so every picture it uses is embedded first.
  function inlineImages(svgEl) {
    var imgs = Array.prototype.slice.call(svgEl.querySelectorAll('image'));
    return Promise.all(imgs.map(function (im) {
      var href = im.getAttribute('href') || im.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
      if (!href || href.indexOf('data:') === 0) return null;
      return fetch(href).then(function (r) { return r.ok ? r.blob() : null; }).then(function (b) { return b ? toDataUrl(b) : null; })
        .then(function (u) { if (u) im.setAttribute('href', u); else im.remove(); }).catch(function () { im.remove(); });
    }));
  }

  // o: {elements, bg, imageUrl, canvasW, canvasH, width, height}  -> base64 PNG (no "data:" prefix)
  function renderPng(o) {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('xmlns', NS); svg.setAttribute('viewBox', '0 0 ' + o.canvasW + ' ' + o.canvasH);
    svg.setAttribute('width', o.width); svg.setAttribute('height', o.height);
    var bg = document.createElementNS(NS, 'rect');
    bg.setAttribute('width', o.canvasW); bg.setAttribute('height', o.canvasH); bg.setAttribute('fill', o.bg || '#111111');
    svg.appendChild(bg);
    if (o.imageUrl) {
      var im = document.createElementNS(NS, 'image');
      im.setAttribute('href', o.imageUrl); im.setAttribute('x', 0); im.setAttribute('y', 0); im.setAttribute('width', o.canvasW); im.setAttribute('height', o.canvasH);
      im.setAttribute('preserveAspectRatio', 'none');
      svg.appendChild(im);
    }
    (o.elements || []).forEach(function (el) {
      if (!el || el.hidden || ['token', 'measure', 'aoe'].indexOf(el.type) >= 0) return;
      var g = root.makeElSVG(el);
      if (g) svg.appendChild(g);
    });
    return inlineImages(svg).then(function () {
      var url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)], { type: 'image/svg+xml' }));
      return new Promise(function (res, rej) {
        var img = new Image();
        img.onload = function () {
          var cv = document.createElement('canvas'); cv.width = o.width; cv.height = o.height;
          cv.getContext('2d').drawImage(img, 0, 0, o.width, o.height);
          URL.revokeObjectURL(url);
          res(cv.toDataURL('image/png').split(',')[1]);
        };
        img.onerror = function () { URL.revokeObjectURL(url); rej(new Error('The map could not be drawn to a picture')); };
        img.src = url;
      });
    });
  }

  function download(name, blobOrText, type) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blobOrText instanceof Blob ? blobOrText : new Blob([blobOrText], { type: type || 'application/json' }));
    a.download = name; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  }
  function b64ToBlob(b64, type) {
    var bin = atob(b64), n = bin.length, bytes = new Uint8Array(n);
    for (var i = 0; i < n; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: type });
  }

  // ctx: {slug, name, elements, walls, canvasW, canvasH, bg, imageUrl, cell, hasGrid, offsetX, offsetY, ppg}
  function prep(ctx) {
    var warnings = [];
    var sc = scaleFor(ctx, warnings);
    return renderPng({ elements: ctx.elements, bg: ctx.bg, imageUrl: ctx.imageUrl, canvasW: ctx.canvasW, canvasH: ctx.canvasH,
                       width: Math.round(sc.sx * sc.ppg), height: Math.round(sc.sy * sc.ppg) }).then(function (b64) { return { b64: b64, ppg: sc.ppg }; });
  }
  function exportUvtt(ctx) {
    return prep(ctx).then(function (p) {
      var r = toUvtt(Object.assign({}, ctx, { ppg: p.ppg, imageB64: p.b64 }));
      download((ctx.slug || 'map') + '.dd2vtt', JSON.stringify(r.file));
      return r.warnings;
    });
  }
  function exportFoundry(ctx) {
    return prep(ctx).then(function (p) {
      var src = 'nd-world/' + (ctx.slug || 'map') + '.png';
      var r = toFoundryScene(Object.assign({}, ctx, { ppg: p.ppg, imageSrc: src }));
      download((ctx.slug || 'map') + '.png', b64ToBlob(p.b64, 'image/png'));
      setTimeout(function () { download((ctx.slug || 'map') + '.foundry-scene.json', JSON.stringify(r.scene)); }, 400);
      return r.warnings.concat(['Upload ' + (ctx.slug || 'map') + '.png to Foundry\'s Data/nd-world/ folder, then import the scene JSON.']);
    });
  }

  root.ndMapExport = { toUvtt: toUvtt, toFoundryScene: toFoundryScene, validateScene: validateScene, renderPng: renderPng, exportUvtt: exportUvtt, exportFoundry: exportFoundry };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapExport;
})(typeof window !== 'undefined' ? window : globalThis);
