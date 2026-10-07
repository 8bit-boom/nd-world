// The map editor's light tool (💡) and the darkness controls in the "Walls & fog" panel.
//
// Lights are data like walls (app/map_walls.py clean_lights): a position, a range in squares, a colour, an intensity and an
// on/off switch. Players are sent them only while darkness is above zero (see app/schematic_payload.py); the browser draws the
// glow and the shadows (static/js/map-light.js). Here the GM places and drags lights, sets the darkness and the lantern every
// character carries, flips a light on or off live, and can preview the lighting right in the editor.
//
//   ndLights.init({svg, prevLayer, slug, getTool, getZoom, snapOn, getGrid, getCanvas, getWalls})
//   ndLights.pointerDown(raw) / pointerMove(raw) / pointerUp()      ndLights.deleteSelected() -> true when a light was deleted
(function (root) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var o = null, lights = [], settings = { darkness: 0, personal: 2 }, layer = null, preview = null, previewTimer = null;
  var selected = null, drag = null, saveTimer = null, seq = 0;

  function $(id) { return document.getElementById(id); }
  function el(tag, a) { var e = document.createElementNS(NS, tag); for (var k in a) e.setAttribute(k, a[k]); return e; }
  function msg(t) { var m = $('walls-msg'); if (m) m.textContent = t || ''; }
  function newId() { seq++; return 'l' + Date.now().toString(36) + seq; }
  function cell() { var g = o.getGrid(); return g.square ? g.cell : 50; }

  function post(path, body) {
    return fetch('/maps/schematic/' + encodeURIComponent(o.slug) + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); });
  }
  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      post('/lights', { lights: lights }).then(function (d) { lights = d.lights; if (selected && !lights.some(function (l) { return l.id === selected; })) selected = null; render(); list(); }).catch(function (e) { msg('Could not save the lights: ' + e.message); });
    }, 350);
    schedulePreview();
  }

  // ── overlay: a bulb, its range ring, and the selection ──────────────────────
  function render() {
    if (!layer) return;
    layer.textContent = '';
    var showAll = o.getTool() === 'light' || ($('walls-show') && $('walls-show').checked);
    layer.style.display = showAll ? '' : 'none';
    var z = o.getZoom() || 1, c = cell();
    lights.forEach(function (l) {
      var sel = l.id === selected, r = l.range * c;
      layer.appendChild(el('circle', { cx: l.x, cy: l.y, r: r, fill: 'none', stroke: l.color, 'stroke-width': Math.max(1, 1.5 / z), 'stroke-dasharray': '4 6', opacity: l.on ? (sel ? 0.9 : 0.4) : 0.15 }));
      layer.appendChild(el('circle', { cx: l.x, cy: l.y, r: Math.max(7, 9 / z), fill: l.on ? l.color : '#222', stroke: sel ? '#fff' : '#000', 'stroke-width': Math.max(1.5, 2 / z), opacity: 0.95 }));
      var t = el('text', { x: l.x, y: l.y + 4 / z, 'text-anchor': 'middle', 'font-size': Math.max(10, 12 / z) }); t.textContent = l.on ? '💡' : '⚫';
      layer.appendChild(t);
    });
  }

  // ── editor preview of the real lighting ─────────────────────────────────────
  function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(updatePreview, 120); }
  function updatePreview() {
    var on = $('lights-preview') && $('lights-preview').checked && settings.darkness > 0;
    if (!on) { if (preview) { preview.destroy(); preview = null; } return; }
    var c = o.getCanvas(), segs = root.ndFogGlue.segmentsOf(o.getWalls());
    if (!preview) preview = root.ndMapLight.create(o.svg, { width: c.w, height: c.h, walls: segs, darkness: Math.min(0.7, settings.darkness), before: layer });
    else { preview.setWalls(segs); preview.setDarkness(Math.min(0.7, settings.darkness)); }
    preview.update(lights.filter(function (l) { return l.on; }).slice(0, 24).map(function (l) { return { x: l.x, y: l.y, range: l.range * cell(), color: l.color, intensity: l.intensity }; }));
  }

  // ── the tool ────────────────────────────────────────────────────────────────
  function snap(p) {
    if (!o.snapOn()) return { x: Math.round(p.x), y: Math.round(p.y) };
    var g = o.getGrid(), step = g.square ? g.cell / 2 : 10, ox = g.square ? g.ox : 0, oy = g.square ? g.oy : 0, c = o.getCanvas();
    return { x: Math.min(c.w, Math.max(0, Math.round((p.x - ox) / step) * step + ox)), y: Math.min(c.h, Math.max(0, Math.round((p.y - oy) / step) * step + oy)) };
  }
  function nearest(p, tol) {
    var best = null, bd = tol;
    lights.forEach(function (l) { var d = Math.hypot(p.x - l.x, p.y - l.y); if (d <= bd) { bd = d; best = l; } });
    return best;
  }
  function pointerDown(raw) {
    var hit = nearest(raw, 16 / (o.getZoom() || 1));
    if (hit) { selected = hit.id; drag = { id: hit.id, moved: false }; render(); list(); return true; }
    if (lights.length >= 60) { msg('At most 60 lights per map.'); return true; }
    var p = snap(raw);
    var l = { id: newId(), x: p.x, y: p.y, range: 6, color: '#ffd9a0', intensity: 1, on: true, label: '' };
    lights.push(l); selected = l.id;
    if (settings.darkness === 0) msg('Lights show only when the map is dark: raise Darkness below.');
    render(); list(); save();
    return true;
  }
  function pointerMove(raw) {
    if (!drag) return;
    var l = lights.find(function (x) { return x.id === drag.id; }); if (!l) return;
    var p = snap(raw); if (p.x !== l.x || p.y !== l.y) { l.x = p.x; l.y = p.y; drag.moved = true; render(); schedulePreview(); }
  }
  function pointerUp() { if (drag && drag.moved) save(); drag = null; }
  function deleteSelected() {
    if (!selected) return false;
    lights = lights.filter(function (l) { return l.id !== selected; });
    selected = null; render(); list(); save();
    return true;
  }

  // ── the panel ───────────────────────────────────────────────────────────────
  function list() {
    var box = $('lights-list'); if (!box) return;
    box.textContent = '';
    if (!lights.length) { var d = document.createElement('div'); d.style.cssText = 'opacity:.55;font-size:.72rem'; d.textContent = 'No lights yet. Pick the 💡 tool and click the map.'; box.appendChild(d); return; }
    lights.forEach(function (l, i) {
      var row = document.createElement('div'); row.className = 'wall-row' + (l.id === selected ? ' on' : '');
      var name = document.createElement('span'); name.textContent = (l.label || 'Light ' + (i + 1)); name.onclick = function () { selected = l.id; render(); list(); };
      row.appendChild(name);
      var col = document.createElement('input'); col.type = 'color'; col.value = l.color; col.style.cssText = 'width:26px;height:22px;padding:0;border:0;background:none';
      col.oninput = function () { l.color = col.value; render(); schedulePreview(); }; col.onchange = save;
      row.appendChild(col);
      var rg = document.createElement('input'); rg.type = 'number'; rg.min = 0.5; rg.max = 60; rg.step = 0.5; rg.value = l.range; rg.style.width = '3.4rem'; rg.title = 'Range in squares';
      rg.onchange = function () { l.range = Math.max(0.5, Math.min(60, parseFloat(rg.value) || 6)); render(); save(); };
      row.appendChild(rg);
      var sw = document.createElement('button'); sw.type = 'button'; sw.className = 'props-mini'; sw.textContent = l.on ? 'On' : 'Off'; sw.title = 'Switch this light on or off, live for everyone';
      sw.onclick = function () { post('/light', { id: l.id }).then(function (r) { l.on = r.on; render(); list(); schedulePreview(); }).catch(function (e) { msg(e.message); }); };
      row.appendChild(sw);
      var x = document.createElement('button'); x.type = 'button'; x.className = 'props-mini'; x.textContent = '✕'; x.title = 'Delete this light';
      x.onclick = function () { selected = l.id; deleteSelected(); };
      row.appendChild(x);
      box.appendChild(row);
    });
  }

  function bind() {
    var dk = $('lights-dark'), dv = $('lights-dark-val'), pr = $('lights-personal'), pv = $('lights-preview');
    function show() { if (dv) dv.textContent = Math.round(settings.darkness * 100) + '%'; }
    if (dk) {
      dk.value = Math.round(settings.darkness * 100); show();
      dk.oninput = function () { settings.darkness = dk.value / 100; show(); schedulePreview(); };
      dk.onchange = function () { post('/fog', { darkness: dk.value / 100 }).then(function (d) { settings.darkness = d.darkness; show(); msg(d.darkness ? 'Dark where no light reaches. Characters carry a lantern of ' + d.personal + ' squares.' : 'Daylight: lights do nothing.'); }).catch(function (e) { msg(e.message); }); };
    }
    if (pr) { pr.value = settings.personal; pr.onchange = function () { post('/fog', { personal: parseFloat(pr.value) || 0 }).then(function (d) { settings.personal = d.personal; }).catch(function (e) { msg(e.message); }); }; }
    if (pv) pv.onchange = updatePreview;
  }

  function init(opts) {
    o = opts;
    layer = el('g', { id: 'lights-layer', 'pointer-events': 'none' });
    o.svg.insertBefore(layer, o.prevLayer);
    fetch('/maps/schematic/' + encodeURIComponent(o.slug) + '/walls.json').then(function (r) { return r.ok ? r.json() : {}; }).then(function (d) {
      lights = d.lights || []; if (d.fog) settings = { darkness: d.fog.darkness || 0, personal: d.fog.personal === undefined ? 2 : d.fog.personal };
      bind(); render(); list();
    }).catch(function () { bind(); });
  }

  root.ndLights = { init: init, pointerDown: pointerDown, pointerMove: pointerMove, pointerUp: pointerUp, deleteSelected: deleteSelected, render: render, update: updatePreview };
})(window);
