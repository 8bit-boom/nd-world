// "Populate this room": rule-based furnishing, deterministic from a seed. The kind of room (tavern, library, shrine, barracks,
// bedroom, storeroom, kitchen, hall, cavern) picks a recipe; a recipe puts props against walls or in the middle using a
// half-cell occupancy grid, so nothing overlaps, nothing leaves the room and the doorways stay clear.
//
//   furnish(room, kind, entrances, rng) -> { placements: [{ asset, cx, cy, rot, decor }], lights: [{ x, y, range, color }] }
//     room       { id, x, y, w, h }                        cells
//     entrances  [{ x, y }]                                cells of THIS room that touch a door or opening
//     placement  centre (cx, cy) in cells and rotation in degrees (0, 90, 180, 270); props are drawn facing south
// A language model can pick `kind` ("the smuggler's den: a tavern with a hidden back room"); it never has to place a prop.
(function (root) {
  'use strict';
  var assetsMod = typeof require === 'function' && typeof module !== 'undefined' ? require('./assets.js') : root.ndMapAssets;
  var A = assetsMod.ASSETS;

  var KINDS = ['tavern', 'library', 'shrine', 'barracks', 'kitchen', 'bedroom', 'storeroom', 'hall', 'cavern'];
  var ROT = { N: 0, E: 90, S: 180, W: 270 };

  function furnish(room, kind, entrances, rng) {
    var W2 = room.w * 2, H2 = room.h * 2, occ = [], res = [], placements = [], lights = [], x, y;
    for (y = 0; y < H2; y++) { occ.push(new Array(W2).fill(false)); res.push(new Array(W2).fill(false)); }
    // keep the doorways clear: everything within one and a half cells of an entrance is off limits
    (entrances || []).forEach(function (e) {
      var ex = (e.x - room.x) * 2 + 1, ey = (e.y - room.y) * 2 + 1;
      for (y = 0; y < H2; y++) for (x = 0; x < W2; x++) if (Math.max(Math.abs(x + 0.5 - ex), Math.abs(y + 0.5 - ey)) <= 3) res[y][x] = true;
    });

    function dims(id, rot) {
      var a = A[id], w2 = Math.max(1, Math.round(a.w * 2)), h2 = Math.max(1, Math.round(a.h * 2));
      return rot === 90 || rot === 270 ? [h2, w2] : [w2, h2];
    }
    function free(x0, y0, w2, h2) {
      if (x0 < 0 || y0 < 0 || x0 + w2 > W2 || y0 + h2 > H2) return false;
      for (var j = y0; j < y0 + h2; j++) for (var i = x0; i < x0 + w2; i++) if (occ[j][i] || res[j][i]) return false;
      return true;
    }
    function put(id, x0, y0, rot, decor) {
      var d = dims(id, rot);
      if (!decor) for (var j = y0; j < y0 + d[1]; j++) for (var i = x0; i < x0 + d[0]; i++) occ[j][i] = true;
      placements.push({ asset: id, cx: room.x + (x0 + d[0] / 2) / 2, cy: room.y + (y0 + d[1] / 2) / 2, rot: rot, decor: !!decor });
      return true;
    }
    function tryPut(id, x0, y0, rot) { var d = dims(id, rot); return free(x0, y0, d[0], d[1]) ? put(id, x0, y0, rot, false) : false; }
    // a prop with its back to a wall; `along` is the position along that wall in half cells
    function against(side, id, along) {
      var rot = ROT[side], d = dims(id, rot), x0, y0;
      if (side === 'N') { x0 = along; y0 = 0; } else if (side === 'S') { x0 = along; y0 = H2 - d[1]; }
      else if (side === 'W') { x0 = 0; y0 = along; } else { x0 = W2 - d[0]; y0 = along; }
      return tryPut(id, x0, y0, rot);
    }
    function wallLen(side) { return side === 'N' || side === 'S' ? W2 : H2; }
    function middle(side, id) { var d = dims(id, ROT[side]), len = wallLen(side), size = side === 'N' || side === 'S' ? d[0] : d[1]; return Math.floor((len - size) / 2); }
    // walk a wall and drop props from `ids` (cycled) wherever they fit, leaving `gap` half cells between them
    function lineAlong(side, ids, gap, from, to) {
      var len = wallLen(side), pos = from === undefined ? 1 : from, end = to === undefined ? len - 1 : to, i = 0;
      while (pos < end) {
        var id = ids[i % ids.length], d = dims(id, ROT[side]), size = side === 'N' || side === 'S' ? d[0] : d[1];
        if (pos + size > end) break;
        if (against(side, id, pos)) pos += size + gap; else pos += 1;
        i++;
      }
    }
    function randomFree(id, rot, margin, tries) {
      var d = dims(id, rot);
      for (var t = 0; t < (tries || 40); t++) {
        var x0 = rng.int(margin, Math.max(margin, W2 - d[0] - margin)), y0 = rng.int(margin, Math.max(margin, H2 - d[1] - margin));
        if (free(x0, y0, d[0], d[1])) return [x0, y0];
      }
      return null;
    }
    function table(id, margin) {                      // a table with chairs on all four sides
      var p = randomFree(id, 0, margin); if (!p) return false;
      var d = dims(id, 0), x0 = p[0], y0 = p[1];
      put(id, x0, y0, 0, false);
      var mx = x0 + Math.floor((d[0] - 1) / 2), my = y0 + Math.floor((d[1] - 1) / 2);
      if (rng.chance(0.9)) tryPut('chair', mx, y0 - 1, 0);
      if (rng.chance(0.9)) tryPut('chair', mx, y0 + d[1], 180);
      if (rng.chance(0.9)) tryPut('chair', x0 - 1, my, 270);
      if (rng.chance(0.9)) tryPut('chair', x0 + d[0], my, 90);
      return true;
    }
    function torch(side, along) {
      var rot = ROT[side], px = side === 'N' || side === 'S' ? room.x + along / 2 + 0.25 : (side === 'W' ? room.x + 0.15 : room.x + room.w - 0.15);
      var py = side === 'W' || side === 'E' ? room.y + along / 2 + 0.25 : (side === 'N' ? room.y + 0.15 : room.y + room.h - 0.15);
      placements.push({ asset: 'torch_wall', cx: px, cy: py, rot: rot, decor: true });
      lights.push({ x: px, y: py, range: 6, color: '#ffaa55', intensity: 1 });
    }
    function rug(rot) {
      var d = dims('rug', rot);
      if (room.w * 2 < d[0] + 4 || room.h * 2 < d[1] + 4) return;
      placements.push({ asset: 'rug', cx: room.x + room.w / 2, cy: room.y + room.h / 2, rot: rot, decor: true });
    }
    var long = room.w >= room.h ? ['N', 'S'] : ['W', 'E'], short = room.w >= room.h ? ['W', 'E'] : ['N', 'S'];

    switch (kind) {
      case 'tavern': {
        var barSide = rng.pick(long);
        if (wallLen(barSide) >= 12) against(barSide, 'bar_counter', Math.floor((wallLen(barSide) - 8) / 2));
        var fireSide = rng.pick(short); against(fireSide, 'fireplace', middle(fireSide, 'fireplace'));
        lineAlong(rng.pick(short.concat(long).filter(function (s) { return s !== barSide && s !== fireSide; })), ['barrel', 'barrel', 'crate'], 0, 1, 8);
        rug(0);
        var n = Math.max(2, Math.min(6, Math.floor(room.w * room.h / 14)));
        for (var i = 0; i < n; i++) table('table_round', 3);
        torch(long[1] === barSide ? long[0] : long[1], Math.floor(wallLen(long[0]) / 3)); torch(long[1] === barSide ? long[0] : long[1], Math.floor(2 * wallLen(long[0]) / 3));
        break;
      }
      case 'library': {
        lineAlong(long[0], ['bookshelf'], 0, 2, wallLen(long[0]) - 2);
        lineAlong(short[0], ['bookshelf'], 0, 2, wallLen(short[0]) - 2);
        rug(0);
        var tp = randomFree('table_long', long[0] === 'N' ? 0 : 90, 4);
        if (tp) { var rot = long[0] === 'N' ? 0 : 90, dd = dims('table_long', rot); put('table_long', tp[0], tp[1], rot, false);
          for (var k = 1; k < dd[0] - 1; k += 2) { if (rot === 0) { tryPut('chair', tp[0] + k, tp[1] - 1, 0); tryPut('chair', tp[0] + k, tp[1] + dd[1], 180); } }
          if (rot === 90) for (var k2 = 1; k2 < dd[1] - 1; k2 += 2) { tryPut('chair', tp[0] - 1, tp[1] + k2, 270); tryPut('chair', tp[0] + dd[0], tp[1] + k2, 90); } }
        torch(long[1], Math.floor(wallLen(long[1]) / 2));
        break;
      }
      case 'shrine': {
        var side = long[0] === 'N' ? 'N' : 'W'; against(side, 'altar', middle(side, 'altar'));
        var d2 = dims('altar', ROT[side]), alt = side === 'N' ? [middle('N', 'altar'), 0] : [0, middle('W', 'altar')];
        var bx1 = side === 'N' ? alt[0] - 2 : 0, by1 = side === 'N' ? 0 : alt[1] - 2, bx2 = side === 'N' ? alt[0] + d2[0] + 1 : 0, by2 = side === 'N' ? 0 : alt[1] + d2[1] + 1;
        tryPut('brazier', Math.max(0, bx1), Math.max(0, by1), 0); tryPut('brazier', bx2, by2, 0);
        var step = 6;
        if (side === 'N') for (var py = 6; py < H2 - 2; py += step) { tryPut('pillar', 2, py, 0); tryPut('pillar', W2 - 4, py, 0); }
        else for (var px2 = 6; px2 < W2 - 2; px2 += step) { tryPut('pillar', px2, 2, 0); tryPut('pillar', px2, H2 - 4, 0); }
        rug(side === 'N' ? 90 : 0);
        torch(side === 'N' ? 'W' : 'N', Math.floor(wallLen(side === 'N' ? 'W' : 'N') / 2)); torch(side === 'N' ? 'E' : 'S', Math.floor(wallLen(side === 'N' ? 'E' : 'S') / 2));
        break;
      }
      case 'barracks': {
        var bedSides = long;
        bedSides.forEach(function (s) {
          var len = wallLen(s), pos = 1;
          while (pos + 2 <= len - 1) {
            if (against(s, 'bed_single', pos)) { var bd = dims('bed_single', ROT[s]); var cx0 = s === 'N' ? pos : s === 'S' ? pos : (s === 'W' ? bd[0] : W2 - bd[0] - 1), cy0 = s === 'W' ? pos : s === 'E' ? pos : (s === 'N' ? bd[1] : H2 - bd[1] - 1);
              tryPut('chest', cx0, cy0, ROT[s] === 0 || ROT[s] === 180 ? ROT[s] : ROT[s]); }
            pos += 4;
          }
        });
        against(short[0], 'weapon_rack', middle(short[0], 'weapon_rack'));
        var cp = randomFree('table_small', 0, 4); if (cp) { put('table_small', cp[0], cp[1], 0, false); tryPut('stool', cp[0] - 1, cp[1], 270); tryPut('stool', cp[0] + 2, cp[1] + 1, 90); }
        torch(short[1], Math.floor(wallLen(short[1]) / 2));
        break;
      }
      case 'bedroom': {
        var bs = rng.pick(short.concat(long));
        against(bs, 'bed_single', Math.max(1, Math.floor(wallLen(bs) / 3)));
        var bp = placements[placements.length - 1];
        if (bp && bp.asset === 'bed_single') { var bdm = dims('bed_single', ROT[bs]); var fx = bs === 'W' ? bdm[0] : bs === 'E' ? W2 - bdm[0] - 1 : Math.round((bp.cx - room.x) * 2 - bdm[0] / 2), fy = bs === 'N' ? bdm[1] : bs === 'S' ? H2 - bdm[1] - 1 : Math.round((bp.cy - room.y) * 2 - bdm[1] / 2);
          tryPut('chest', fx, fy, ROT[bs]); }
        var tp2 = randomFree('table_small', 0, 1); if (tp2) { put('table_small', tp2[0], tp2[1], 0, false); tryPut('stool', tp2[0] + 2, tp2[1], 90); }
        rug(0); torch(rng.pick(['N', 'S', 'E', 'W']), 2);
        break;
      }
      case 'storeroom': {
        // keep a one-cell aisle through the middle, fill the rest
        for (y = Math.floor(H2 / 2) - 1; y <= Math.floor(H2 / 2); y++) for (x = 0; x < W2; x++) res[y][x] = true;
        ['N', 'S', 'W', 'E'].forEach(function (s) { lineAlong(s, ['crate', 'barrel', 'sack', 'crate', 'barrel'], rng.int(0, 1), rng.int(0, 2)); });
        for (var q = 0; q < 3; q++) { var pcr = randomFree(rng.pick(['crate', 'barrel', 'hay']), 0, 1); if (pcr) put(rng.pick(['crate', 'barrel']), pcr[0], pcr[1], 0, false); }
        break;
      }
      case 'kitchen': {
        var fs = rng.pick(short); against(fs, 'fireplace', middle(fs, 'fireplace'));
        var cp2 = randomFree('cauldron', 0, 2); if (cp2) put('cauldron', cp2[0], cp2[1], 0, false);
        var tl = randomFree('table_long', 0, 2); if (tl) { put('table_long', tl[0], tl[1], 0, false); for (var s2 = 1; s2 < 6; s2 += 2) { tryPut('stool', tl[0] + s2, tl[1] - 1, 0); tryPut('stool', tl[0] + s2, tl[1] + 2, 180); } }
        lineAlong(rng.pick(long), ['barrel', 'sack', 'crate'], 0, 1, 8);
        torch(long[0], Math.floor(wallLen(long[0]) / 2));
        break;
      }
      case 'cavern': {
        var rocks = Math.max(1, Math.floor(room.w * room.h / 18));
        for (var r = 0; r < rocks; r++) { var rp = randomFree('rock', 0, 1); if (rp) put('rock', rp[0], rp[1], 0, false); }
        break;
      }
      default: {                                        // hall
        if (room.w >= 7 && room.h >= 5) for (var hx = 3; hx < W2 - 3; hx += 6) { tryPut('pillar', hx, 2, 0); tryPut('pillar', hx, H2 - 4, 0); }
        rug(0);
        torch(long[0], Math.floor(wallLen(long[0]) / 2)); torch(long[1], Math.floor(wallLen(long[1]) / 2));
      }
    }
    // decor first, then furniture, so rugs sit under everything
    placements.sort(function (a, b) { return (b.decor ? 1 : 0) - (a.decor ? 1 : 0); });
    return { placements: placements, lights: lights };
  }

  // Which rooms get which recipe: the biggest is the tavern, then the rest by size.
  function assignKinds(rooms, rng) {
    var order = rooms.slice().sort(function (a, b) { return b.w * b.h - a.w * a.h || a.id - b.id; }), kinds = {};
    var big = ['tavern', 'library', 'shrine', 'barracks', 'kitchen'], small = ['bedroom', 'storeroom', 'storeroom', 'bedroom'];
    order.forEach(function (r, i) { kinds[r.id] = r.w * r.h < 20 ? small[i % small.length] : (i < big.length ? big[i] : rng.pick(['hall', 'bedroom', 'storeroom'])); });
    return kinds;
  }

  root.ndMapFurnish = { furnish: furnish, assignKinds: assignKinds, KINDS: KINDS };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapFurnish;
})(typeof window !== 'undefined' ? window : globalThis);
