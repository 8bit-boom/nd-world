// Connects the fog layer (map-fog.js) and the light layer (map-light.js) to a map view: the players' live view and the table's TV
// share this, so the rules (who sees, what is lit, what is hidden, what is remembered) are written once.
//
//   const fog = ndFogGlue.create({svg, tokenLayer, key, getElements, getGrid, getCanvas});
//   fog.setState({fog: {enabled, range, epoch, darkness, personal}, walls: [...], lights: [...]});   // from view.json / data.json
//   fog.apply();                                                                                     // call after every redraw (tokens are rebuilt)
//
// Who sees: every character token on the map (a token with a pc_id) - the party shares one view, as it does at a real table.
// What they see in the dark: where a light reaches - the map's lights that are on, plus the lantern each character carries
// (`personal` squares). Everything that is not a character (monsters, items, merchants) is shown only inside the party's view
// AND, when it is properly dark (darkness >= 0.5), only where there is light. Characters are always shown.
// Memory: what has been explored is kept in this browser (localStorage) per map and fog epoch; the GM's "reset fog" raises the
// epoch and every browser starts afresh. In the dark only lit squares are remembered.
(function (root) {
  'use strict';
  var MAX_LIGHTS = 12;              // drawn at once: thirty lights cost ~64 ms a frame on a throttled phone (docs/map-creator-research/PROTOTYPES.md)

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

  // which lights to draw when there are more than MAX_LIGHTS: the personal lights first, then the map's lights nearest a character
  function pickLights(personal, mapLights, pcs, centre) {
    var room = Math.max(4, MAX_LIGHTS - personal.length);
    if (mapLights.length <= room) return personal.concat(mapLights);
    var anchors = pcs.length ? pcs : [centre];
    var scored = mapLights.map(function (l) {
      var d = Infinity;
      anchors.forEach(function (a) { d = Math.min(d, Math.hypot(a.x - l.x, a.y - l.y) - l.range); });
      return { l: l, d: d };
    }).sort(function (a, b) { return a.d - b.d; });
    return personal.concat(scored.slice(0, room).map(function (s) { return s.l; }));
  }

  function create(o) {
    var fogLayer = null, lightLayer = null, saveTimer = null;
    var st = { fog: { enabled: false, range: 0, epoch: 0, darkness: 0, personal: 2 }, walls: [], lights: [], explored: null };
    var memKey = 'nd_fog_' + o.key;

    function showAllTokens() {
      Array.prototype.forEach.call(o.tokenLayer.children, function (n) { n.style.display = ''; });
    }
    function dropFog() { if (fogLayer) { fogLayer.destroy(); fogLayer = null; } }
    function dropLight() { if (lightLayer) { lightLayer.destroy(); lightLayer = null; } }
    function load() {
      try {
        var m = JSON.parse(localStorage.getItem(memKey) || 'null');
        if (m && m.epoch === st.fog.epoch && m.cell === fogLayer.cell && m.cols === fogLayer.cols && m.rows === fogLayer.rows) fogLayer.loadRuns(m.runs);
      } catch (e) { /* no memory: start with nothing explored */ }
    }
    function save() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(function () {
        if (!fogLayer) return;
        try { localStorage.setItem(memKey, JSON.stringify({ epoch: st.fog.epoch, cell: fogLayer.cell, cols: fogLayer.cols, rows: fogLayer.rows, runs: fogLayer.runs() })); } catch (e) {}
      }, 600);
    }
    function forget() { try { localStorage.removeItem(memKey); } catch (e) {} }

    function apply() {
      var f = st.fog, dark = f.darkness > 0;
      if (!f.enabled) dropFog();
      if (!dark) dropLight();
      showAllTokens();
      if (!f.enabled && !dark) return null;

      var c = o.getCanvas(), grid = o.getGrid(), segs = segmentsOf(st.walls);
      var els = o.getElements(), byId = {};
      els.forEach(function (e) { byId[e.id] = e; });
      var pcs = els.filter(function (e) { return e.type === 'token' && e.pc_id; });
      var res = { sources: pcs.length };

      // lighting first, so it sits underneath the fog
      if (dark) {
        if (!lightLayer) lightLayer = root.ndMapLight.create(o.svg, { width: c.w, height: c.h, walls: segs, darkness: f.darkness, before: fogLayer ? fogLayer.node : o.tokenLayer });
        else { lightLayer.setWalls(segs); lightLayer.setDarkness(f.darkness); }
        var personal = f.personal > 0 ? pcs.map(function (p) { return { x: p.x, y: p.y, range: f.personal * grid.cell, color: '#ffe9c0', intensity: 0.9 }; }) : [];
        var mapLights = (st.lights || []).map(function (l) { return { x: l.x, y: l.y, range: l.range * grid.cell, color: l.color, intensity: l.intensity }; });
        var picked = pickLights(personal, mapLights, pcs, { x: c.w / 2, y: c.h / 2 });
        res.lights = lightLayer.update(picked).lights;
      }

      var tokens = [];
      Array.prototype.forEach.call(o.tokenLayer.children, function (n) {
        var e = byId[n.dataset && n.dataset.id];
        if (e) tokens.push({ x: e.x, y: e.y, node: n, always: !!e.pc_id });
      });

      if (f.enabled) {
        if (fogLayer && (fogLayer.cols !== Math.ceil(c.w / fogLayer.cell) || fogLayer.rows !== Math.ceil(c.h / fogLayer.cell))) dropFog();    // the canvas was resized
        if (!fogLayer) {
          fogLayer = root.ndMapFog.create(o.svg, { width: c.w, height: c.h, cell: grid.cell, walls: segs, tokenLayer: o.tokenLayer });
          load();
        }
        fogLayer.setWalls(segs);
        // strict maps: the server's record of what the party has explored (shared by every phone and the TV)
        var ex = st.explored;
        if (ex && ex.cell === fogLayer.cell && ex.cols === fogLayer.cols && ex.rows === fogLayer.rows && fogLayer.addRuns) fogLayer.addRuns(ex.runs);
        var range = f.range > 0 ? f.range * grid.cell : Infinity;
        var sources = pcs.map(function (e) { return { x: e.x, y: e.y, range: range }; });
        var inDark = dark && f.darkness >= 0.5 && lightLayer;
        var r = fogLayer.update(sources, tokens, inDark ? function (x, y) { return lightLayer.isLit(x, y); } : null);
        res.newlyExplored = r.newlyExplored; res.hiddenTokens = r.hiddenTokens;
        if (r.newlyExplored) save();
      }
      // a creature standing in the dark is not seen: hide the characters' non-character tokens that no light reaches
      if (dark && f.darkness >= 0.5 && lightLayer) {
        tokens.forEach(function (t) { if (!t.always && t.node.style.display !== 'none' && !lightLayer.isLit(t.x, t.y)) t.node.style.display = 'none'; });
      }
      return res;
    }

    return {
      apply: apply,
      enabled: function () { return !!st.fog.enabled || st.fog.darkness > 0; },
      exploredRuns: function () { return fogLayer ? fogLayer.runs() : []; },
      setState: function (s) {
        var prev = st.fog;
        st = { fog: s && s.fog ? Object.assign({ darkness: 0, personal: 2 }, s.fog) : prev, walls: (s && s.walls) || [], lights: (s && s.lights) || [], explored: (s && s.explored) || null };
        if (st.fog.epoch !== prev.epoch) { if (fogLayer) fogLayer.clear(); forget(); }
        apply();
      },
    };
  }

  root.ndFogGlue = { create: create, segmentsOf: segmentsOf, pickLights: pickLights, MAX_LIGHTS: MAX_LIGHTS };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndFogGlue;
})(typeof window !== 'undefined' ? window : globalThis);
