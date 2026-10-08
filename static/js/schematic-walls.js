// The map editor's wall tool and "Walls & fog" panel (schematic.html loads this after its own script).
//
// Walls are DATA the fog of war and line of sight are computed from (see app/map_walls.py): polylines of kind wall / door /
// window / secret, drawn on top of the map for the GM only. They are saved to the server as they change.
//
//   ndWalls.init({ svg, prevLayer, slug, getTool, getZoom, snapOn, getGrid, getCanvas, getRects })
//   ndWalls.pointerDown(rawPoint) -> true when the wall tool used the click      ndWalls.pointerMove(rawPoint)
//   ndWalls.finish()  ndWalls.cancel()  ndWalls.deleteSelected() -> true when a wall was deleted
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var COLOURS = { wall: '#ff6bd6', door: '#e0a050', window: '#6bd6ff', secret: '#e0a050' };
  var KIND_LABEL = { wall: 'Wall', door: 'Door', window: 'Window', secret: 'Secret door' };
  var o = null, walls = [], fog = { enabled: false, range: 0, epoch: 0 }, layer = null;
  var drawing = null, cursor = null, selected = null, saveTimer = null, seq = 0;

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs) { var e = document.createElementNS(NS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function msg(t) { var m = $('walls-msg'); if (m) m.textContent = t || ''; }
  function newId() { seq++; return 'w' + Date.now().toString(36) + seq; }
  function kindNow() { var s = $('walls-kind'); return s ? s.value : 'wall'; }

  // ── server ──────────────────────────────────────────────────────────────────
  function post(path, body) {
    return fetch('/maps/schematic/' + encodeURIComponent(o.slug) + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); });
  }
  function save() {
    clearTimeout(saveTimer);
    msg('Saving…');
    saveTimer = setTimeout(function () {
      post('/walls', { walls: walls }).then(function (d) {
        walls = d.walls;                                  // what the server kept (clamped, de-duplicated)
        if (selected && !walls.some(function (w) { return w.id === selected; })) selected = null;
        msg(d.warnings && d.warnings.length ? d.warnings.join(' ') : 'Saved.');
        render(); list();
      }).catch(function (e) { msg('Could not save the walls: ' + e.message); });
    }, 350);
  }

  // ── drawing the overlay ─────────────────────────────────────────────────────
  function pathOf(pts) { return pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0] + ' ' + p[1]; }).join(''); }
  function render() {
    if (!layer) return;
    layer.textContent = '';
    var show = !$('walls-show') || $('walls-show').checked;
    layer.style.display = show || (o.getTool() === 'wall') ? '' : 'none';
    var sw = Math.max(3, 4 / (o.getZoom() || 1));
    walls.forEach(function (w) {
      var sel = w.id === selected, open = w.state === 'open' && (w.kind === 'door' || w.kind === 'secret');
      var a = { d: pathOf(w.pts), fill: 'none', stroke: COLOURS[w.kind] || COLOURS.wall, 'stroke-width': sw + (sel ? 3 : 0), 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: sel ? 1 : 0.85 };
      if (w.kind === 'window') a['stroke-dasharray'] = '10 5';
      if (w.kind === 'secret') a['stroke-dasharray'] = '3 4';
      if (open) { a['stroke-dasharray'] = '2 8'; a.opacity = 0.6; }
      layer.appendChild(el('path', a));
      if (w.kind === 'door' || w.kind === 'secret') {
        var m = w.pts[0], n = w.pts[w.pts.length - 1];
        var t = el('text', { x: (m[0] + n[0]) / 2, y: (m[1] + n[1]) / 2 - sw * 1.5, 'font-size': 14, fill: COLOURS[w.kind], 'text-anchor': 'middle', 'font-family': 'monospace' });
        t.textContent = (w.kind === 'secret' ? 'S ' : '') + (open ? '◌' : '▮');
        layer.appendChild(t);
      }
    });
    if (drawing) {
      var pts = drawing.pts.concat(cursor ? [cursor] : []);
      layer.appendChild(el('path', { d: pathOf(pts), fill: 'none', stroke: COLOURS[drawing.kind], 'stroke-width': sw, 'stroke-dasharray': '6 4', 'stroke-linecap': 'round' }));
      drawing.pts.forEach(function (p) { layer.appendChild(el('circle', { cx: p[0], cy: p[1], r: sw + 1, fill: '#fff' })); });
    }
  }

  // ── geometry helpers ────────────────────────────────────────────────────────
  function snap(p) {
    if (!o.snapOn()) return [Math.round(p.x), Math.round(p.y)];
    var g = o.getGrid(), step = g.square ? g.cell / 2 : 10, ox = g.square ? g.ox : 0, oy = g.square ? g.oy : 0;
    var c = o.getCanvas();
    return [Math.min(c.w, Math.max(0, Math.round((p.x - ox) / step) * step + ox)), Math.min(c.h, Math.max(0, Math.round((p.y - oy) / step) * step + oy))];
  }
  function distToSeg(px, py, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy;
    var t = l2 ? Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / l2)) : 0;
    return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
  }
  function nearestWall(p, tol) {
    var best = null, bd = tol;
    walls.forEach(function (w) {
      for (var i = 0; i + 1 < w.pts.length; i++) { var d = distToSeg(p.x, p.y, w.pts[i], w.pts[i + 1]); if (d <= bd) { bd = d; best = w; } }
    });
    return best;
  }

  // A door, window or secret door drawn on top of an existing wall must open that wall: the stretch it covers is cut out of the
  // wall (a wall that passes straight through a door would still block the view). Pure; returns a new list of walls.
  function cutWalls(list, a, b, makeId) {
    var dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy);
    if (!len) return list.slice();
    var ux = dx / len, uy = dy / len, TOL = 1.5, out = [];
    function along(p) { return (p[0] - a[0]) * ux + (p[1] - a[1]) * uy; }
    function off(p) { return Math.abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux); }
    list.forEach(function (w) {
      if (w.kind !== 'wall') { out.push(w); return; }
      var segs = [], cut = false;
      for (var i = 0; i + 1 < w.pts.length; i++) {
        var p = w.pts[i], q = w.pts[i + 1];
        if (off(p) <= TOL && off(q) <= TOL) {
          var t0 = Math.min(along(p), along(q)), t1 = Math.max(along(p), along(q));
          var lo = Math.max(t0, 0), hi = Math.min(t1, len);
          if (hi - lo > TOL) {
            cut = true;
            var at = function (t) { return [Math.round((a[0] + ux * t) * 100) / 100, Math.round((a[1] + uy * t) * 100) / 100]; };
            var forward = along(q) >= along(p);
            // the stretch nearest p, then the stretch nearest q (in the order the polyline runs)
            if (forward ? lo - t0 > TOL : t1 - hi > TOL) segs.push([p, at(forward ? lo : hi)]);
            if (forward ? t1 - hi > TOL : lo - t0 > TOL) segs.push([at(forward ? hi : lo), q]);
            continue;
          }
        }
        segs.push([p, q]);
      }
      if (!cut) { out.push(w); return; }
      // join consecutive pieces that still meet end to start back into polylines
      var cur = null, n = 0;
      segs.forEach(function (sg) {
        if (cur && cur[cur.length - 1][0] === sg[0][0] && cur[cur.length - 1][1] === sg[0][1]) cur.push(sg[1]);
        else { if (cur) out.push({ id: n++ ? makeId() : w.id, pts: cur, kind: 'wall' }); cur = [sg[0], sg[1]]; }
      });
      if (cur) out.push({ id: n++ ? makeId() : w.id, pts: cur, kind: 'wall' });
    });
    return out;
  }

  // ── the tool ────────────────────────────────────────────────────────────────
  function finish() {
    if (drawing && drawing.pts.length >= 2) {
      if (drawing.kind !== 'wall') walls = cutWalls(walls, drawing.pts[0], drawing.pts[drawing.pts.length - 1], newId);
      walls.push({ id: newId(), pts: drawing.pts, kind: drawing.kind, state: 'closed' });
      selected = walls[walls.length - 1].id;
      save();
    }
    drawing = null; cursor = null; render(); list();
  }
  function cancel() { drawing = null; cursor = null; render(); }

  function nearVertex(p, tol) {
    for (var i = 0; i < walls.length; i++) for (var j = 0; j < walls[i].pts.length; j++) {
      var v = walls[i].pts[j];
      if (Math.hypot(p.x - v[0], p.y - v[1]) <= tol) return v;
    }
    return null;
  }

  // Starting a stroke: a door/window/secret door is always drawn (it usually sits ON a wall); a wall starts from an existing
  // corner if the click is on one, selects the wall if the click is on the middle of one (Shift forces drawing), else begins.
  function pointerDown(raw, shift) {
    var tol = 10 / (o.getZoom() || 1);
    if (!drawing) {
      var kind = kindNow(), corner = nearVertex(raw, tol);
      if (kind === 'wall' && !shift && !corner) {
        var hit = nearestWall(raw, tol);
        if (hit) { selected = hit.id; render(); list(); return true; }
      }
      drawing = { kind: kind, pts: [corner ? [corner[0], corner[1]] : snap(raw)] };
      selected = null; render(); list();
      return true;
    }
    var p = snap(raw), last = drawing.pts[drawing.pts.length - 1];
    if (p[0] === last[0] && p[1] === last[1]) { finish(); return true; }            // clicking the same spot again ends the wall
    var first = drawing.pts[0];
    drawing.pts.push(p);
    if (drawing.pts.length >= 3 && drawing.kind === 'wall' && Math.hypot(p[0] - first[0], p[1] - first[1]) < 1) { finish(); return true; }   // closed the loop
    if (drawing.kind !== 'wall' && drawing.pts.length === 2) { finish(); return true; }                                                        // a door/window is one segment
    render();
    return true;
  }
  function pointerMove(raw) { if (drawing) { cursor = snap(raw); render(); } }
  function deleteSelected() {
    if (!selected) return false;
    walls = walls.filter(function (w) { return w.id !== selected; });
    selected = null; save(); render(); list();
    return true;
  }

  // ── the panel ───────────────────────────────────────────────────────────────
  function btn(text, title, fn) { var b = document.createElement('button'); b.type = 'button'; b.className = 'props-mini'; b.textContent = text; b.title = title || ''; b.onclick = fn; return b; }
  function list() {
    var box = $('walls-list'); if (!box) return;
    box.textContent = '';
    if (!walls.length) { var d = document.createElement('div'); d.style.cssText = 'opacity:.55;font-size:.72rem'; d.textContent = 'No walls yet. Pick the 🧱 tool and click to draw.'; box.appendChild(d); return; }
    walls.slice(0, 150).forEach(function (w, i) {
      var row = document.createElement('div'); row.className = 'wall-row' + (w.id === selected ? ' on' : '');
      var name = document.createElement('span'); name.textContent = (KIND_LABEL[w.kind] || 'Wall') + ' ' + (i + 1) + (w.pts.length > 2 ? ' · ' + w.pts.length + ' pts' : '');
      name.onclick = function () { selected = w.id; render(); list(); };
      row.appendChild(name);
      if (w.kind === 'door' || w.kind === 'secret') {
        row.appendChild(btn(w.state === 'open' ? '🔓 Close' : '🚪 Open', 'Open or close this door for everyone, live', function () {
          post('/door', { wall_id: w.id }).then(function (r) { w.state = r.state; render(); list(); }).catch(function (e) { msg(e.message); });
        }));
      }
      var sel = document.createElement('select');
      ['wall', 'door', 'window', 'secret'].forEach(function (k) { var op = document.createElement('option'); op.value = k; op.textContent = KIND_LABEL[k]; if (k === w.kind) op.selected = true; sel.appendChild(op); });
      sel.onchange = function () { w.kind = sel.value; if (w.kind === 'door' || w.kind === 'secret') w.state = w.state || 'closed'; else delete w.state; save(); render(); list(); };
      row.appendChild(sel);
      row.appendChild(btn('✕', 'Delete this wall', function () { selected = w.id; deleteSelected(); }));
      box.appendChild(row);
    });
    if (walls.length > 150) { var more = document.createElement('div'); more.style.cssText = 'opacity:.55;font-size:.72rem'; more.textContent = '…and ' + (walls.length - 150) + ' more (select them on the map).'; box.appendChild(more); }
  }

  function fromRects() {
    var rects = (o.getRects() || []).filter(function (r) { return r.w >= 20 && r.h >= 20; });
    if (!rects.length) { msg('There are no rooms (rectangles) on the map.'); return; }
    var have = {};
    walls.forEach(function (w) { have[JSON.stringify(w.pts)] = true; });
    var added = 0;
    rects.forEach(function (r) {
      var pts = [[r.x, r.y], [r.x + r.w, r.y], [r.x + r.w, r.y + r.h], [r.x, r.y + r.h], [r.x, r.y]].map(function (p) { return [Math.round(p[0] * 100) / 100, Math.round(p[1] * 100) / 100]; });
      if (have[JSON.stringify(pts)]) return;
      walls.push({ id: newId(), pts: pts, kind: 'wall' }); added++;
    });
    msg(added ? 'Added the outline of ' + added + ' room(s). Draw doors with the 🧱 tool set to Door.' : 'Those rooms already have walls.');
    if (added) { save(); render(); list(); }
  }

  function bindFog() {
    var en = $('walls-fog-enabled'), rg = $('walls-fog-range'), rs = $('walls-fog-reset');
    if (en) { en.checked = !!fog.enabled; en.onchange = function () { post('/fog', { enabled: en.checked }).then(function (d) { fog = d; msg(d.enabled ? 'Fog of war is ON for players and the table screen.' : 'Fog of war is off.'); }).catch(function (e) { en.checked = !en.checked; msg(e.message); }); }; }
    if (rg) { rg.value = fog.range || ''; rg.onchange = function () { post('/fog', { range: parseFloat(rg.value) || 0 }).then(function (d) { fog = d; msg(d.range ? 'Characters see ' + d.range + ' squares.' : 'Characters see as far as the walls allow.'); }).catch(function (e) { msg(e.message); }); }; }
    if (rs) rs.onclick = function () { if (!confirm('Hide everything again? Every player and the table screen forgets what they have explored.')) return; post('/fog', { reset: true }).then(function (d) { fog = d; msg('Fog reset: nobody remembers the map any more.'); }).catch(function (e) { msg(e.message); }); };
    var st = $('walls-fog-strict');
    if (st) {
      st.checked = !!fog.strict;
      st.onchange = function () {
        post('/fog', { strict: st.checked }).then(function (d) { fog = d; msg(d.strict ? (d.enabled ? 'Strict secrecy is ON: players are sent only what their characters have seen.' : 'Strict secrecy is set, but it only works while Fog of war is on.') : 'Strict secrecy is off: the fog hides the map in the browser only.'); })
          .catch(function (e) { st.checked = !st.checked; msg(e.message); });
      };
    }
    var fr = $('walls-from-rects'); if (fr) fr.onclick = fromRects;
    var clr = $('walls-clear'); if (clr) clr.onclick = function () { if (walls.length && confirm('Delete all ' + walls.length + ' walls?')) { walls = []; selected = null; save(); render(); list(); } };
    var sh = $('walls-show'); if (sh) sh.onchange = render;
  }

  // The room tool owns the walls whose id starts with `prefix`: they are replaced wholesale, every other wall (drawn by hand) is
  // untouched, and a door keeps the open/closed state it had.
  function replaceByPrefix(prefix, fresh) {
    var keep = walls.filter(function (w) { return String(w.id).indexOf(prefix) !== 0; });
    walls = keep.concat(fresh);
    if (selected && !walls.some(function (w) { return w.id === selected; })) selected = null;
    save(); render(); list();
  }
  function stateMap() {
    var m = {};
    walls.forEach(function (w) { if (w.state) m[w.id] = w.state; });
    return m;
  }

  function init(opts) {
    o = opts;
    layer = el('g', { id: 'walls-layer', 'pointer-events': 'none' });
    o.svg.insertBefore(layer, o.prevLayer);
    fetch('/maps/schematic/' + encodeURIComponent(o.slug) + '/walls.json').then(function (r) { return r.ok ? r.json() : { walls: [], fog: fog }; }).then(function (d) {
      walls = d.walls || []; fog = d.fog || fog; bindFog(); render(); list();
    }).catch(function () { bindFog(); msg('Could not load the walls.'); });
  }

  root.ndWalls = { getWalls: function () { return walls; }, cut: cutWalls, replaceByPrefix: replaceByPrefix, stateMap: stateMap, init: init, pointerDown: pointerDown, pointerMove: pointerMove, finish: finish, cancel: cancel, deleteSelected: deleteSelected, render: render };
})(window);
