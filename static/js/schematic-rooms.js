// The map editor's room-drawing tool (🏠) and its "Rooms" panel. The model and the generator are static/js/map-rooms.js; this file
// is the pointer handling and the panel. Painting floor makes walls appear around it; door, window and secret-door marks go on
// those walls. The result is written as ordinary elements (ids "rm-...") and wall data, and the drawing state is stored so it
// can be edited again.
//
//   ndRooms.init({svg, prevLayer, slug, getTool, setTool, getZoom, getGrid, getCanvas, getElements, setElements, redraw, save, ndWalls})
//   ndRooms.pointerDown(raw) / pointerMove(raw) / pointerUp(raw)      ndRooms.undo() / redo() -> true when it did something
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var M = root.ndMapRooms;
  var o = null, st = null, mode = 'paint', current = 0, layer = null, drag = null, hoverEdge = null;
  var undoStack = [], redoStack = [], saveTimer = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, a) { var e = document.createElementNS(NS, tag); for (var k in a) e.setAttribute(k, a[k]); return e; }
  function msg(t) { var m = $('rooms-msg'); if (m) m.textContent = t || ''; }

  // ── the state ───────────────────────────────────────────────────────────────
  function ensureState() {
    if (st) return true;
    var g = o.getGrid(), c = o.getCanvas();
    if (!g.square) { msg('Rooms need a square grid.'); showGridOffer(true); return false; }
    st = M.create(g.cell, g.ox, g.oy, c.w, c.h);
    if (!st) { msg('This canvas has too many squares for the room tool (at most 400 a side, 40,000 in all). Use bigger squares.'); return false; }
    return true;
  }
  function showGridOffer(on) { var b = $('rooms-grid-offer'); if (b) b.style.display = on ? '' : 'none'; }

  // furniture (ids "rf-<room id>-...", static/js/map-furnish.js) is part of what undo restores
  function furniture() { return o.getElements().filter(function (e) { return String(e.id).indexOf('rf-') === 0; }); }
  function snap() { return JSON.stringify({ st: M.pack(st), rf: furniture() }); }
  function snapshotState() { undoStack.push(snap()); if (undoStack.length > 60) undoStack.shift(); redoStack = []; }
  function restore(json) {
    var d = JSON.parse(json), keep = o.getElements().filter(function (e) { return String(e.id).indexOf('rf-') !== 0; });
    o.setElements(keep.concat(d.rf || []));
    st = M.unpack(d.st); if (!st.spaces.some(function (s) { return s.id === current; })) current = st.spaces.length ? st.spaces[0].id : 0; regenerate(false); panel(); }
  function undo() { if (!undoStack.length || !st) return false; redoStack.push(snap()); restore(undoStack.pop()); return true; }
  function redo() { if (!redoStack.length || !st) return false; undoStack.push(snap()); restore(redoStack.pop()); return true; }

  // ── writing the map ─────────────────────────────────────────────────────────
  function regenerate(record) {
    if (!st) return;
    var gen = M.generate(st, o.ndWalls.stateMap());
    var rest = o.getElements().filter(function (e) { return String(e.id).indexOf('rm-') !== 0; });
    o.setElements(M.elements(st, gen).concat(rest));          // floors and walls sit under everything else
    o.ndWalls.replaceByPrefix('rm-', gen.walls);
    o.redraw();
    o.save();
    saveState();
    draw();
  }
  function saveState() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      var body = st && st.spaces.length ? M.pack(st) : {};
      fetch('/maps/schematic/' + encodeURIComponent(o.slug) + '/rooms', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) { if (!r.ok) return r.json().then(function (d) { msg('Could not save the rooms: ' + (d.detail || r.status)); }); })
        .catch(function () { msg('Could not save the rooms (network).'); });
    }, 400);
  }

  // ── the overlay: hover and drag previews ────────────────────────────────────
  function draw() {
    if (!layer) return;
    layer.textContent = '';
    if (!st || o.getTool() !== 'rooms') return;
    if (drag && drag.kind === 'rect') {
      var a = M.cellAt(st, drag.x0, drag.y0), b = M.cellAt(st, drag.x1, drag.y1);
      if (a && b) {
        var x0 = Math.min(a.x, b.x), y0 = Math.min(a.y, b.y), x1 = Math.max(a.x, b.x) + 1, y1 = Math.max(a.y, b.y) + 1;
        layer.appendChild(el('rect', { x: st.ox + x0 * st.cell, y: st.oy + y0 * st.cell, width: (x1 - x0) * st.cell, height: (y1 - y0) * st.cell,
          fill: mode === 'erase' ? 'rgba(255,80,80,.25)' : 'rgba(0,240,255,.25)', stroke: mode === 'erase' ? '#ff5050' : '#00f0ff', 'stroke-width': 2, 'stroke-dasharray': '6 3' }));
      }
    }
    if (hoverEdge && ['door', 'window', 'secret', 'open', 'clear'].indexOf(mode) >= 0) {
      var e = hoverEdge.split(','), x = +e[0], y = +e[1], horiz = e[2] === 'h';
      var ok = !!M.edgeSet(st)[hoverEdge];
      layer.appendChild(el('line', { x1: st.ox + x * st.cell, y1: st.oy + y * st.cell, x2: st.ox + (x + (horiz ? 1 : 0)) * st.cell, y2: st.oy + (y + (horiz ? 0 : 1)) * st.cell,
        stroke: ok ? '#ffd54f' : '#ff5050', 'stroke-width': 6, opacity: 0.8, 'stroke-linecap': 'round' }));
    }
  }

  // ── pointer ─────────────────────────────────────────────────────────────────
  function paintId() { return mode === 'erase' ? 0 : current; }
  function applyPaint(c0, c1) {
    if (mode !== 'erase' && !current) { msg('Make a room first (＋ New room), then paint.'); return; }
    snapshotState();
    var changed = M.paintRect(st, c0.x, c0.y, c1.x, c1.y, paintId());
    if (!changed) { undoStack.pop(); return; }
    regenerate(true); panel();
  }
  function pointerDown(raw) {
    if (!ensureState()) return;
    if (mode === 'paint' || mode === 'erase') {
      var c = M.cellAt(st, raw.x, raw.y);
      if (!c) return;
      drag = { kind: $('rooms-brush') && $('rooms-brush').checked ? 'brush' : 'rect', x0: raw.x, y0: raw.y, x1: raw.x, y1: raw.y, last: c };
      if (drag.kind === 'brush') { applyPaint(c, c); }
      draw();
      return;
    }
    // marks: click the wall edge nearest the pointer
    var key = M.nearestEdge(st, raw.x, raw.y), kind = mode === 'clear' ? null : mode;
    snapshotState();
    if (M.setMark(st, key, kind)) regenerate(true);
    else { undoStack.pop(); msg(kind ? 'Doors and windows go on a wall: click the outline of a room.' : 'Nothing to remove there.'); }
  }
  function pointerMove(raw) {
    if (!st) return;
    if (drag) {
      drag.x1 = raw.x; drag.y1 = raw.y;
      if (drag.kind === 'brush') { var c = M.cellAt(st, raw.x, raw.y); if (c && (c.x !== drag.last.x || c.y !== drag.last.y)) { applyPaint(c, c); drag.last = c; } }
    } else hoverEdge = ['door', 'window', 'secret', 'open', 'clear'].indexOf(mode) >= 0 ? M.nearestEdge(st, raw.x, raw.y) : null;
    draw();
  }
  function pointerUp(raw) {
    if (!drag) return;
    var d = drag; drag = null;
    if (d.kind === 'rect') {
      var a = M.cellAt(st, d.x0, d.y0), b = M.cellAt(st, raw.x, raw.y) || M.cellAt(st, d.x1, d.y1);
      if (a && b) applyPaint(a, b);
    }
    draw();
  }

  // ── the panel ───────────────────────────────────────────────────────────────
  function btn(text, title, fn) { var b = document.createElement('button'); b.type = 'button'; b.className = 'props-mini'; b.textContent = text; b.title = title || ''; b.onclick = fn; return b; }
  function panel() {
    var sel = $('rooms-space'); if (!sel) return;
    sel.textContent = '';
    (st ? st.spaces : []).forEach(function (s) { var op = document.createElement('option'); op.value = s.id; op.textContent = s.name + ' (' + s.kind + ')'; if (s.id === current) op.selected = true; sel.appendChild(op); });
    var list = $('rooms-list'); list.textContent = '';
    (st ? st.spaces : []).forEach(function (s) {
      var squares = 0; for (var i = 0; i < st.ids.length; i++) if (st.ids[i] === s.id) squares++;
      var row = document.createElement('div'); row.className = 'wall-row' + (s.id === current ? ' on' : '');
      var name = document.createElement('span'); name.textContent = s.name + ' · ' + squares + ' sq'; name.onclick = function () { current = s.id; panel(); };
      row.appendChild(name);
      var k = document.createElement('select');
      M.KINDS.forEach(function (kk) { var op = document.createElement('option'); op.value = kk; op.textContent = kk; if (kk === s.kind) op.selected = true; k.appendChild(op); });
      k.onchange = function () { snapshotState(); s.kind = k.value; regenerate(true); panel(); };
      row.appendChild(k);
      row.appendChild(btn('✎', 'Rename', function () { var n = prompt('Room name:', s.name); if (n && n.trim()) { snapshotState(); s.name = n.trim().slice(0, 60); regenerate(true); panel(); } }));
      row.appendChild(btn('✨', 'Furnish this room to suit its kind (again for a new arrangement)', function () { furnishRooms([s.id]); }));
      row.appendChild(btn('✕', 'Remove this room and its floor', function () {
        if (!confirm('Remove “' + s.name + '” and its floor?')) return;
        snapshotState();
        for (var i = 0; i < st.ids.length; i++) if (st.ids[i] === s.id) st.ids[i] = 0;
        st.spaces = st.spaces.filter(function (x) { return x.id !== s.id; });
        if (current === s.id) current = st.spaces.length ? st.spaces[0].id : 0;
        dropFurniture(s.id);
        M.prune(st); regenerate(true); panel();
      }));
      list.appendChild(row);
    });
    if (!st || !st.spaces.length) { var d = document.createElement('div'); d.style.cssText = 'opacity:.55;font-size:.72rem'; d.textContent = 'No rooms yet: ＋ New room, then paint on the map.'; list.appendChild(d); }
    Array.prototype.forEach.call(document.querySelectorAll('[data-rooms-mode]'), function (b) { b.classList.toggle('on', b.dataset.roomsMode === mode); });
  }

  function dropFurniture(id) {
    var pre = 'rf-' + id + '-';
    o.setElements(o.getElements().filter(function (e) { return String(e.id).indexOf(pre) !== 0; }));
  }
  function furnishRooms(ids) {
    if (!st || !root.ndMapFurnish) return;
    msg('Furnishing…');
    fetch('/api/maps/props').then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; }).then(function (props) {
      if (!Array.isArray(props)) props = props && props.props || [];
      snapshotState();
      var seed = String(Date.now()), made = 0, rooms = 0;
      ids.forEach(function (id) {
        dropFurniture(id);
        var els = root.ndMapFurnish.furnish(st, id, props, seed + ':' + id);
        if (els.length) { o.setElements(o.getElements().concat(els)); made += els.length; rooms++; }
      });
      if (!made) { undoStack.pop(); msg('Nothing to place: closets, corridors and outdoor areas stay bare, and so do rooms under 6 squares.'); return; }
      regenerate(true);
      msg('Placed ' + made + ' pieces in ' + rooms + ' room' + (rooms === 1 ? '' : 's') + '. ✨ again for a different arrangement; Ctrl+Z undoes it.');
    });
  }

  function newRoom() {
    if (!ensureState()) return;
    var name = prompt('Name of the new room:', 'Room ' + (st.spaces.length + 1));
    if (!name || !name.trim()) return;
    var kind = $('rooms-kind').value;
    snapshotState();
    current = M.addSpace(st, name.trim(), kind);
    if (mode !== 'paint') mode = 'paint';
    panel(); saveState();
    msg('Painting “' + name.trim() + '”: drag a rectangle on the map.');
  }

  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-rooms-mode]'), function (b) { b.onclick = function () { mode = b.dataset.roomsMode; hoverEdge = null; panel(); draw(); if (o.getTool() !== 'rooms') o.setTool('rooms'); }; });
    var sel = $('rooms-space'); if (sel) sel.onchange = function () { current = +sel.value; panel(); };
    var fa = $('rooms-furnish-all'); if (fa) fa.onclick = function () { if (st) furnishRooms(st.spaces.map(function (x) { return x.id; })); };
    var nw = $('rooms-new'); if (nw) nw.onclick = newRoom;
    var go = $('rooms-grid-offer'); if (go) go.onclick = function () { o.useSquareGrid(); };
    var kd = $('rooms-kind'); if (kd) { kd.textContent = ''; M.KINDS.forEach(function (k) { var op = document.createElement('option'); op.value = k; op.textContent = k; kd.appendChild(op); }); }
  }

  function init(opts) {
    o = opts; M = root.ndMapRooms;
    layer = el('g', { id: 'rooms-layer', 'pointer-events': 'none' });
    o.svg.insertBefore(layer, o.prevLayer);
    bind(); panel();
    fetch('/maps/schematic/' + encodeURIComponent(o.slug) + '/rooms.json').then(function (r) { return r.ok ? r.json() : {}; }).then(function (d) {
      if (d && d.cols) { st = M.unpack(d); current = st.spaces.length ? st.spaces[0].id : 0; panel(); }
    }).catch(function () {});
  }
  function activate() { if (ensureState()) { showGridOffer(false); msg(st.spaces.length ? '' : 'Start with ＋ New room.'); } draw(); }

  root.ndRooms = { init: init, activate: activate, pointerDown: pointerDown, pointerMove: pointerMove, pointerUp: pointerUp, undo: undo, redo: redo, draw: draw };
})(window);
