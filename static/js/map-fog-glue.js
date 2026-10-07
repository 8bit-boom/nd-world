// Connects the fog layer (map-fog.js) to a map view: the players' live view and the table's TV share this, so the rules
// (who sees, what is hidden, what is remembered) are written once.
//
//   const fog = ndFogGlue.create({svg, tokenLayer, key, getElements, getGrid, getCanvas});
//   fog.setState({fog: {enabled, range, epoch}, walls: [...]});   // from view.json / data.json; re-applies
//   fog.apply();                                                   // call after every redraw (tokens are rebuilt)
//
// Who sees: every character token on the map (a token with a pc_id) - the party shares one view, as it does at a real table.
// Everything else (monsters, items, merchants) is shown only inside that view; characters are always shown.
// Memory: what has been explored is kept in this browser (localStorage) per map and fog epoch; the GM's "reset fog" raises the
// epoch and every browser starts afresh.
(function (root) {
  'use strict';

  function segmentsOf(walls) {
    var out = [];
    (walls || []).forEach(function (w) {
      var pts = (w && w.pts) || [], kind = w && w.kind === 'secret' ? 'wall' : (w && w.kind) || 'wall';
      for (var i = 0; i + 1 < pts.length; i++) {
        out.push({ x1: pts[i][0], y1: pts[i][1], x2: pts[i + 1][0], y2: pts[i + 1][1], kind: kind, state: (w && w.state) || 'closed' });
      }
    });
    return out;
  }

  function create(o) {
    var layer = null, st = { fog: { enabled: false, range: 0, epoch: 0 }, walls: [] }, saveTimer = null;
    var memKey = 'nd_fog_' + o.key;

    function showAllTokens() {
      Array.prototype.forEach.call(o.tokenLayer.children, function (n) { n.style.display = ''; });
    }
    function drop() {
      if (layer) { layer.destroy(); layer = null; }
      showAllTokens();
    }
    function load() {
      try {
        var m = JSON.parse(localStorage.getItem(memKey) || 'null');
        if (m && m.epoch === st.fog.epoch && m.cell === layer.cell && m.cols === layer.cols && m.rows === layer.rows) layer.loadRuns(m.runs);
      } catch (e) { /* no memory: start with nothing explored */ }
    }
    function save() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(function () {
        if (!layer) return;
        try { localStorage.setItem(memKey, JSON.stringify({ epoch: st.fog.epoch, cell: layer.cell, cols: layer.cols, rows: layer.rows, runs: layer.runs() })); } catch (e) {}
      }, 600);
    }
    function forget() { try { localStorage.removeItem(memKey); } catch (e) {} }

    function apply() {
      if (!st.fog.enabled) { if (layer) drop(); return null; }
      var c = o.getCanvas(), grid = o.getGrid();
      if (layer && (layer.cols !== Math.ceil(c.w / layer.cell) || layer.rows !== Math.ceil(c.h / layer.cell))) drop();   // the canvas was resized
      if (!layer) {
        layer = root.ndMapFog.create(o.svg, { width: c.w, height: c.h, cell: grid.cell, walls: segmentsOf(st.walls), tokenLayer: o.tokenLayer });
        load();
      }
      layer.setWalls(segmentsOf(st.walls));
      var els = o.getElements(), byId = {};
      els.forEach(function (e) { byId[e.id] = e; });
      var range = st.fog.range > 0 ? st.fog.range * grid.cell : Infinity;
      var sources = els.filter(function (e) { return e.type === 'token' && e.pc_id; }).map(function (e) { return { x: e.x, y: e.y, range: range }; });
      var tokens = [];
      Array.prototype.forEach.call(o.tokenLayer.children, function (n) {
        var e = byId[n.dataset && n.dataset.id];
        if (e) tokens.push({ x: e.x, y: e.y, node: n, always: !!e.pc_id });
      });
      var res = layer.update(sources, tokens);
      res.sources = sources.length;
      if (res.newlyExplored) save();
      return res;
    }

    return {
      apply: apply,
      enabled: function () { return !!st.fog.enabled; },
      setState: function (s) {
        var prev = st.fog;
        st = { fog: s && s.fog ? s.fog : prev, walls: (s && s.walls) || [] };
        if (st.fog.epoch !== prev.epoch) { if (layer) layer.clear(); forget(); }
        if (!st.fog.enabled && layer) drop();
        apply();
      },
    };
  }

  root.ndFogGlue = { create: create, segmentsOf: segmentsOf };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndFogGlue;
})(typeof window !== 'undefined' ? window : globalThis);
