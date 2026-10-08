// Auto-furnishing a drawn room (the Rooms panel's ✨): chooses furniture for a room by its kind and places it where it belongs -
// beds and shelves against walls, tables in the middle, barrels in corners - never in a doorway, never overlapping, always inside
// the room (L-shaped and ragged rooms included). A bed (or barrel, table...) from the world's prop library is used when its name
// matches, else a plain shape is drawn. Pure functions, tested under Node (tests/test_map_furnish.py); the same seed always
// gives the same furniture. The rules mirror app/map_layout.py (the AI plan's furnishing); a room's furniture is replaced each
// time and is recognised by its id prefix "rf-<room id>-".
//
//   ndMapFurnish.furnish(state, spaceId, props, seed) -> [elements]       state is what map-rooms.js builds
(function (root) {
  'use strict';

  // name -> [long side, short side] in squares, shape
  var ITEMS = {
    'bed': [[2, 1], 'bed'], 'cot': [[2, 1], 'bed'], 'table': [[2, 1], 'rect'], 'bar counter': [[3, 1], 'rect'], 'counter': [[2, 1], 'rect'],
    'stove': [[1, 1], 'rect'], 'barrel': [[1, 1], 'circle'], 'crate': [[1, 1], 'rect'], 'chest': [[1, 1], 'rect'], 'shelf': [[2, 1], 'rect'],
    'bookshelf': [[2, 1], 'rect'], 'altar': [[2, 1], 'rect'], 'statue': [[1, 1], 'circle'], 'bench': [[2, 1], 'rect'], 'stool': [[1, 1], 'circle'],
    'weapon rack': [[2, 1], 'rect'], 'bucket': [[1, 1], 'circle'], 'desk': [[2, 1], 'rect'], 'pillar': [[1, 1], 'circle'],
  };
  var COLOUR = { 'bed': '#6b4b3a', 'cot': '#5a4a3a', 'table': '#7a5a3a', 'bar counter': '#6a4a2a', 'counter': '#6a5a4a', 'stove': '#4a4a4f', 'barrel': '#8a5a2b',
    'crate': '#8a6a3a', 'chest': '#9a7a2a', 'shelf': '#4a3a2a', 'bookshelf': '#4a3a2a', 'altar': '#8c8c9c', 'statue': '#9a9aa4', 'bench': '#6a5030',
    'stool': '#7a6040', 'weapon rack': '#555555', 'bucket': '#6a6a74', 'desk': '#6a4a2a', 'pillar': '#8a8a92' };
  var SYNONYMS = { 'bed': ['bed', 'cot', 'bunk'], 'cot': ['cot', 'bed', 'bunk'], 'table': ['table'], 'bar counter': ['bar', 'counter'], 'counter': ['counter', 'bar'],
    'stove': ['stove', 'oven', 'hearth', 'fireplace'], 'barrel': ['barrel', 'cask', 'keg'], 'crate': ['crate', 'box'], 'chest': ['chest', 'trunk'],
    'shelf': ['shelf', 'bookcase', 'bookshelf'], 'bookshelf': ['bookshelf', 'bookcase', 'shelf'], 'altar': ['altar'], 'statue': ['statue'], 'bench': ['bench', 'pew'],
    'stool': ['stool', 'chair'], 'weapon rack': ['rack', 'weapon'], 'bucket': ['bucket', 'pail'], 'desk': ['desk', 'table'], 'pillar': ['pillar', 'column'] };
  // kind -> [item, [min, max], where]   where: wall | centre | corner | any
  var PLAN = {
    tavern: [['bar counter', [1, 1], 'wall'], ['table', [2, 4], 'centre'], ['stool', [2, 5], 'any'], ['barrel', [2, 3], 'corner']],
    hall: [['table', [1, 3], 'centre'], ['bench', [2, 4], 'wall'], ['pillar', [0, 2], 'centre']],
    bedroom: [['bed', [1, 3], 'wall'], ['chest', [1, 1], 'corner'], ['table', [0, 1], 'wall']],
    kitchen: [['counter', [2, 3], 'wall'], ['stove', [1, 1], 'wall'], ['table', [1, 1], 'centre'], ['barrel', [1, 2], 'corner']],
    storage: [['crate', [3, 6], 'any'], ['barrel', [2, 4], 'corner'], ['shelf', [1, 3], 'wall']],
    cell: [['cot', [1, 2], 'wall'], ['bucket', [1, 1], 'corner']],
    shrine: [['altar', [1, 1], 'centre'], ['statue', [1, 2], 'corner'], ['bench', [0, 3], 'wall']],
    library: [['bookshelf', [3, 6], 'wall'], ['table', [1, 2], 'centre'], ['stool', [1, 3], 'any']],
    armory: [['weapon rack', [2, 4], 'wall'], ['crate', [1, 3], 'any'], ['table', [0, 1], 'centre']],
    cave: [['barrel', [0, 1], 'corner'], ['crate', [0, 2], 'any']],
    other: [['table', [0, 1], 'centre'], ['chest', [0, 1], 'corner']],
    corridor: [], outdoor: [],
  };

  function rng(seed) {                                    // mulberry32 over a string seed
    var h = 1779033703 ^ String(seed).length;
    for (var i = 0; i < String(seed).length; i++) { h = Math.imul(h ^ String(seed).charCodeAt(i), 3432918353); h = (h << 13) | (h >>> 19); }
    var a = h >>> 0;
    return function () { a = (a + 0x6D2B79F5) >>> 0; var t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }

  function findProp(item, props) {
    var words = SYNONYMS[item] || [item];
    for (var i = 0; i < (props || []).length; i++) {
      var p = props[i];
      if (!p || typeof p !== 'object' || typeof p.url !== 'string') continue;
      var hay = (String(p.name || '') + ' ' + String(p.tags || '')).toLowerCase().match(/[a-z]+/g) || [];
      for (var w = 0; w < words.length; w++) if (hay.indexOf(words[w]) >= 0) return p;
    }
    return null;
  }

  function at(st, x, y) { return x < 0 || y < 0 || x >= st.cols || y >= st.rows ? 0 : st.ids[y * st.cols + x]; }

  function elementsFor(item, shape, cx, cy, iw, ih, props, st, idBase) {
    var c = st.cell, px = st.ox + cx * c, py = st.oy + cy * c, pw = iw * c, ph = ih * c, inset = Math.max(2, c * 0.08);
    var prop = findProp(item, props);
    if (prop) {
      var rot = ((iw >= ih) === ((Number(prop.cells_w) || 1) >= (Number(prop.cells_h) || 1)) || iw === ih) ? 0 : 90;
      var w = pw - 2 * inset, h = ph - 2 * inset;
      if (rot) { var t = w; w = h; h = t; }
      return [{ id: idBase, type: 'image', x: px + pw / 2 - w / 2, y: py + ph / 2 - h / 2, w: w, h: h, rot: rot, href: prop.url, prop_id: prop.id, opacity: 1, fill: 'none', stroke: 'none', strokeW: 0, label: '', dash: '', layer: 'Tracks' }];
    }
    var base = { fill: COLOUR[item] || '#777777', stroke: '#1d1d22', strokeW: 1, layer: 'Tracks' };
    if (shape === 'circle') {
      var r = Math.min(pw, ph) / 2 - inset;
      return [Object.assign({ id: idBase, type: 'circle', cx: px + pw / 2, cy: py + ph / 2, rx: r, ry: r }, base)];
    }
    var out = [Object.assign({ id: idBase, type: 'rect', x: px + inset, y: py + inset, w: pw - 2 * inset, h: ph - 2 * inset }, base)];
    if (shape === 'bed') {
      out.push(ph >= pw
        ? { id: idBase + 'p', type: 'rect', x: px + pw * 0.2, y: py + inset + 2, w: pw * 0.6, h: ph * 0.22, fill: '#d8d8e2', stroke: '#1d1d22', strokeW: 1, layer: 'Tracks' }
        : { id: idBase + 'p', type: 'rect', x: px + inset + 2, y: py + ph * 0.2, w: pw * 0.22, h: ph * 0.6, fill: '#d8d8e2', stroke: '#1d1d22', strokeW: 1, layer: 'Tracks' });
    }
    return out;
  }

  function furnish(st, spaceId, props, seed) {
    var space = null;
    st.spaces.forEach(function (s) { if (s.id === spaceId) space = s; });
    if (!space) return [];
    var plan = PLAN[space.kind] || [];
    var free = {}, count = 0;
    for (var y = 0; y < st.rows; y++) for (var x = 0; x < st.cols; x++) if (at(st, x, y) === spaceId) { free[x + ',' + y] = true; count++; }
    if (!plan.length || count < 6) return [];                                 // a closet gets no furniture
    // doorways stay clear: the square inside every door / opening and the one beyond it
    Object.keys(st.marks).forEach(function (k) {
      if (st.marks[k] === 'window') return;
      var p = k.split(','), x = +p[0], y = +p[1], horiz = p[2] === 'h';
      [[0, 0], [horiz ? 0 : -1, horiz ? -1 : 0]].forEach(function (side, i) {      // the two squares either side of the edge
        var sx = x + side[0], sy = y + side[1];
        if (at(st, sx, sy) !== spaceId) return;
        var dx = horiz ? 0 : (i === 0 ? 1 : -1), dy = horiz ? (i === 0 ? 1 : -1) : 0;
        delete free[sx + ',' + sy]; delete free[(sx + dx) + ',' + (sy + dy)];
      });
    });
    var rand = rng(seed + ':' + spaceId + ':' + space.name), out = [], n = 0;
    function same(x, y) { return at(st, x, y) === spaceId; }
    plan.forEach(function (entry) {
      var item = entry[0], lo = entry[1][0], hi = entry[1][1], where = entry[2], dims = ITEMS[item][0], shape = ITEMS[item][1];
      var times = lo + Math.floor(rand() * (hi - lo + 1));
      for (var t = 0; t < times; t++) {
        var spots = [];
        [true, false].forEach(function (horizontal, oi) {
          if (!horizontal && dims[0] === dims[1]) return;
          var iw = horizontal ? dims[0] : dims[1], ih = horizontal ? dims[1] : dims[0];
          for (var cy = 0; cy <= st.rows - ih; cy++) for (var cx = 0; cx <= st.cols - iw; cx++) {
            var cells = [], ok = true;
            for (var j = 0; j < ih && ok; j++) for (var i = 0; i < iw; i++) { if (!free[(cx + i) + ',' + (cy + j)]) { ok = false; break; } cells.push([cx + i, cy + j]); }
            if (!ok) continue;
            var touchesH = cells.some(function (c) { return !same(c[0], c[1] - 1) || !same(c[0], c[1] + 1); });   // a wall above or below
            var touchesV = cells.some(function (c) { return !same(c[0] - 1, c[1]) || !same(c[0] + 1, c[1]); });   // a wall left or right
            var inner = cells.every(function (c) { for (var a = -1; a <= 1; a++) for (var b = -1; b <= 1; b++) if (!same(c[0] + a, c[1] + b)) return false; return true; });
            var corner = cells.some(function (c) { return (!same(c[0] - 1, c[1]) || !same(c[0] + 1, c[1])) && (!same(c[0], c[1] - 1) || !same(c[0], c[1] + 1)); });
            if (where === 'wall' && !(iw >= ih ? touchesH : touchesV)) continue;          // a long item lies along the wall it hugs
            if (where === 'corner' && !corner) continue;
            if (where === 'centre' && !inner) continue;
            spots.push({ cx: cx, cy: cy, iw: iw, ih: ih, cells: cells });
          }
        });
        if (!spots.length) continue;
        var pick = spots[Math.floor(rand() * spots.length)];
        pick.cells.forEach(function (c) { delete free[c[0] + ',' + c[1]]; });
        if (where === 'centre') pick.cells.forEach(function (c) { for (var a = -1; a <= 1; a++) for (var b = -1; b <= 1; b++) delete free[(c[0] + a) + ',' + (c[1] + b)]; });
        out = out.concat(elementsFor(item, shape, pick.cx, pick.cy, pick.iw, pick.ih, props, st, 'rf-' + spaceId + '-' + (n++)));
      }
    });
    return out;
  }

  root.ndMapFurnish = { furnish: furnish, PLAN: PLAN, ITEMS: ITEMS, findProp: findProp };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapFurnish;
})(typeof window !== 'undefined' ? window : globalThis);
