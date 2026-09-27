// GM Cockpit — modular window workspace (app/templates/cockpit.html renders
// the shell and the per-world data). The cockpit's job is to replace the
// GM's "ten browser tabs" with movable, resizable, closable windows that
// each host ONE live tool: the battle map, the dice tray, a party's
// vitals, the quest board, any entity's page, a scratch pad, AI chat.
//
// - The whole arrangement (windows + a library of named preset layouts)
//   autosaves per world via /api/cockpit/workspace (World.cockpit_ws_json),
//   with localStorage as the offline mirror, so the cockpit reopens exactly
//   as it was left — on any device the GM logs in from.
// - Live panels (party vitals, quests) are client renders of existing JSON
//   endpoints, refreshed by the Phase-1 live-sync bus ("nd-live" event) —
//   the same data the party page and quest board render server-side.
// - Everything else is an embedded live page in ?embed=1 chrome-less mode:
//   what the map/dice/entity pages learn later, the cockpit inherits.
// - Drag/resize deliberately mutate styles in place; a full re-render (and
//   therefore an iframe reload) only happens on add/remove/preset-switch.
(function () {
  'use strict';
  const viewport = document.getElementById('ck-viewport');
  if (!viewport) return;

  const welcome = document.getElementById('ck-welcome');
  const addOverlay = document.getElementById('ck-add-overlay');
  const addTypes = document.getElementById('ck-add-types');
  const addPicker = document.getElementById('ck-add-picker');
  const addSearch = document.getElementById('ck-add-search');
  const addList = document.getElementById('ck-add-list');
  const presetSelect = document.getElementById('ck-preset-select');
  const deletePresetBtn = document.getElementById('ck-delete-preset');

  const KIND_ICONS = { character: '👤', creature: '🐉', location: '📍', organization: '🏛',
    item: '🗡', note: '📝', event: '⚡', race: '🧬', profession: '🎭', feat: '✨' };
  const COMPACT = window.matchMedia('(max-width: 900px)');

  let panels = [];
  let presets = {};
  let zTop = 10;
  let seq = 1;
  const liveLoaders = new Map(); // panel id -> refetch fn (party/quests)
  let entityCache = null;

  function embed(path) {
    const glue = path.indexOf('?') > -1 ? '&' : '?';
    return path + glue + 'embed=1&w=' + encodeURIComponent(CK_WORLD);
  }
  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  // Panel ids are "p<seq>"; restore seq from the saved ids' numeric part.
  // (parseInt alone can't read them — "p5" parses as NaN, which used to
  // reset the counter to 1 and mint duplicate ids on the next add.)
  function idNum(id) {
    const digits = String(id).replace(/\D/g, '');
    return digits ? parseInt(digits, 10) : 0;
  }
  function recomputeSeq() {
    seq = panels.reduce(function (m, p) { return Math.max(m, idNum(p.id)); }, 0) + 1;
    zTop = panels.reduce(function (m, p) { return Math.max(m, p.z || 0); }, 10) + 1;
  }
  function newId() { return 'p' + (seq++); }

  // ── persistence ────────────────────────────────────────────────────────
  let saveTimer = null;
  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 400);
  }
  function saveNow() {
    const ws = { current: { panels: panels }, presets: presets };
    try { localStorage.setItem('nd_cockpit_ws_' + CK_WORLD, JSON.stringify(ws)); } catch (e) {}
    fetch('/api/cockpit/workspace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ws),
    }).catch(function () {});
  }

  async function load() {
    let ws = null;
    try {
      const r = await fetch('/api/cockpit/workspace');
      if (r.ok) ws = (await r.json()).workspace;
    } catch (e) {}
    if (!ws) {
      try { ws = JSON.parse(localStorage.getItem('nd_cockpit_ws_' + CK_WORLD) || 'null'); } catch (e) {}
    }
    if (ws && ws.current && Array.isArray(ws.current.panels) && ws.current.panels.length) {
      panels = ws.current.panels;
      presets = ws.presets || {};
    } else {
      panels = defaultLayout();
      presets = {};
      save();
    }
    // Drop windows whose backing object vanished since the layout was saved
    // (a deleted map, a disbanded party) rather than rendering dead panels.
    panels = panels.filter(function (p) {
      if (p.type === 'map') return CK_MAPS.some(function (m) { return m.slug === p.ref; });
      if (p.type === 'party') return CK_PARTIES.some(function (x) { return String(x.id) === String(p.ref); });
      return true;
    });
    recomputeSeq();
    // Re-mint ids on every load: guarantees uniqueness even if an older
    // build saved a workspace with duplicate ids (its counter-reset bug).
    panels.forEach(function (p) { p.id = newId(); });
    renderPresetSelect();
    render();
  }

  // ── defaults ───────────────────────────────────────────────────────────
  function defaultLayout() {
    seq = 1;
    const vw = viewport.clientWidth || 1200, vh = viewport.clientHeight || 700;
    const out = [];
    let n = 0;
    function add(type, ref, title, x, y, w, h) {
      out.push({ id: newId(), type: type, ref: ref || '', title: title || '',
        x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h),
        z: ++n, collapsed: false, data: {} });
    }
    const hasMap = CK_MAPS.length > 0;
    if (hasMap) add('map', CK_MAPS[0].slug, '🗺 ' + CK_MAPS[0].name, 8, 8, Math.min(820, vw * 0.58), vh - 16);
    const rx = hasMap ? Math.min(836, vw * 0.58) + 16 : 8;
    const rw = Math.max(300, vw - rx - 8);
    let ry = 8;
    if (CK_PARTIES.length) { add('party', String(CK_PARTIES[0].id), '❤ ' + CK_PARTIES[0].name, rx, ry, rw, 230); ry += 240; }
    add('quests', '', '📜 Quests', rx, ry, rw, 220); ry += 230;
    add('dice', '', '🎲 Dice', rx, ry, rw, Math.max(220, vh - ry - 8));
    return out;
  }

  // ── rendering ──────────────────────────────────────────────────────────
  function render() {
    viewport.querySelectorAll('.ck-win').forEach(function (el) { el.remove(); });
    liveLoaders.clear();
    panels.forEach(function (p) { viewport.appendChild(buildWin(p)); });
    panels.forEach(function (p) {
      if (p.type === 'party' || p.type === 'quests') loadLive(p);
    });
    if (welcome) welcome.style.display = panels.length ? 'none' : '';
  }

  function buildWin(p) {
    const win = document.createElement('div');
    win.className = 'ck-win' + (p.collapsed ? ' collapsed' : '');
    win.dataset.pid = p.id;
    win.style.left = p.x + 'px';
    win.style.top = p.y + 'px';
    win.style.width = p.w + 'px';
    win.style.height = p.h + 'px';
    win.style.zIndex = p.z || 10;

    const head = document.createElement('div');
    head.className = 'ck-head2';
    const title = document.createElement('span');
    title.className = 'ck-win-title';
    title.textContent = p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label;
    const maxBtn = mkBtn('⤢', 'Fill the cockpit', function () { win.classList.toggle('maxed'); });
    const colBtn = mkBtn(p.collapsed ? '▸' : '▾', 'Collapse / expand', function () {
      p.collapsed = !p.collapsed;
      win.classList.toggle('collapsed', p.collapsed);
      colBtn.textContent = p.collapsed ? '▸' : '▾';
      save();
    });
    const closeBtn = mkBtn('✕', 'Remove this panel', function () {
      panels = panels.filter(function (x) { return x.id !== p.id; });
      win.remove();
      liveLoaders.delete(p.id);
      if (welcome) welcome.style.display = panels.length ? 'none' : '';
      save();
    });
    head.append(title, maxBtn, colBtn, closeBtn);

    const body = document.createElement('div');
    body.className = 'ck-body';
    fillBody(p, body);

    const rz = document.createElement('div');
    rz.className = 'ck-resize';
    rz.title = 'Resize';

    win.append(head, body, rz);
    win.addEventListener('pointerdown', function () {
      zTop += 1;
      win.style.zIndex = zTop;
      p.z = zTop;
    });
    wireDrag(head, win, p);
    wireResize(rz, win, p);
    return win;
  }

  function mkBtn(label, tip, fn) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ck-wbtn';
    b.textContent = label;
    b.title = tip;
    b.addEventListener('click', function (e) { e.stopPropagation(); fn(e); });
    return b;
  }

  function fillBody(p, body) {
    if (p.type === 'map') {
      body.appendChild(mkIframe('/maps/schematic/' + encodeURIComponent(p.ref) + '/view'));
    } else if (p.type === 'dice') {
      body.appendChild(mkIframe('/dice'));
    } else if (p.type === 'entity') {
      body.appendChild(mkIframe('/entity/' + encodeURIComponent(p.ref)));
    } else if (p.type === 'ai') {
      body.appendChild(mkIframe('/ai'));
    } else if (p.type === 'notes') {
      const ta = document.createElement('textarea');
      ta.className = 'ck-notes';
      ta.placeholder = 'Scratch notes — HP tallies, names, reminders…';
      ta.value = (p.data && p.data.text) || '';
      let t = null;
      ta.addEventListener('input', function () {
        p.data = { text: ta.value };
        clearTimeout(t);
        t = setTimeout(save, 900);
      });
      body.appendChild(ta);
    } else {
      // party / quests — live JSON panels, reloaded by the live-sync bus
      const live = document.createElement('div');
      live.className = 'ck-live';
      live.innerHTML = '<p class="ck-empty">Loading…</p>';
      body.appendChild(live);
      liveLoaders.set(p.id, function () { loadLive(p); });
    }
  }

  function mkIframe(path) {
    const f = document.createElement('iframe');
    f.src = embed(path);
    f.loading = 'lazy';
    f.title = 'panel';
    return f;
  }

  async function loadLive(p) {
    const live = viewport.querySelector('.ck-win[data-pid="' + p.id + '"] .ck-live');
    if (!live) return;
    if (p.type === 'party') {
      try {
        const r = await fetch('/api/parties/' + encodeURIComponent(p.ref) + '/vitals');
        if (!r.ok) throw new Error();
        renderParty(await r.json(), live);
      } catch (e) { live.innerHTML = '<p class="ck-empty">Party unavailable.</p>'; }
    } else {
      try {
        const r = await fetch('/api/quests/board');
        if (!r.ok) throw new Error();
        renderQuests(await r.json(), live);
      } catch (e) { live.innerHTML = '<p class="ck-empty">Quest board unavailable.</p>'; }
    }
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function renderParty(d, live) {
    if (!(d.members || []).length) {
      live.innerHTML = '<p class="ck-empty">No members yet — <a href="/parties/' + encodeURIComponent(d.party_id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">edit the party</a>.</p>';
      return;
    }
    live.innerHTML = '<h4>' + esc(d.name) + ' · live</h4>' + d.members.map(function (m) {
      let chips = '';
      (m.resources || []).forEach(function (r) {
        chips += '<i>' + esc(r.current) + '/' + esc(r.max) + ' ' + esc(r.label) + '</i>';
      });
      let hp = '';
      if (m.max_hp) {
        const pct = Math.max(0, Math.min(100, Math.round((m.hp || 0) * 100 / m.max_hp)));
        const cls = pct <= 25 ? 'ck-hp-low' : (pct <= 55 ? 'ck-hp-mid' : 'ck-hp-ok');
        hp = '<span class="ck-hpbar"><span class="ck-hpfill ' + cls + '" style="width:' + pct + '%"></span></span>' +
          '<span class="ck-mem-hp">HP ' + esc(m.hp) + '/' + esc(m.max_hp) + (m.temp_hp ? ' (+' + esc(m.temp_hp) + ')' : '') +
          ' · AC ' + esc(m.ac || '—') + (m.down ? ' · <strong style="color:#e07">DOWN</strong>' : '') + '</span>';
      }
      const lvl = 'Lvl ' + esc(m.level) + (m.levelup ? ' ⬆' : '');
      return '<a class="ck-mem' + (m.down ? ' down' : '') + '" href="/characters/' + encodeURIComponent(m.id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">' +
        '<span class="ck-mem-row"><span>' + esc(m.name) + '</span><span class="ck-mem-lvl">' + lvl + '</span></span>' +
        (chips ? '<span class="ck-reschips">' + chips + '</span>' : '') + hp +
        ((m.conditions || []).length ? '<span class="ck-conds">' + esc(m.conditions.join(', ')) + '</span>' : '') +
        '</a>';
    }).join('');
  }

  function renderQuests(d, live) {
    if (!(d.quests || []).length) {
      live.innerHTML = '<p class="ck-empty">No active quests — <a href="/quests?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">quest board</a>.</p>';
      return;
    }
    live.innerHTML = '<h4>Active quests · live</h4>' + d.quests.map(function (q) {
      return '<a class="ck-quest" href="/quests/' + encodeURIComponent(q.id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">' +
        '<span class="ck-quest-t">' + esc(q.title) + '</span>' +
        '<span class="ck-quest-m"><i class="ck-pill">' + esc(q.category) + '</i>' +
        (q.party ? '<i class="ck-pill ck-pill-party">' + esc(q.party) + '</i>' : '') + '</span>' +
        (q.summary ? '<span class="ck-quest-s">' + esc(q.summary) + '</span>' : '') +
        '</a>';
    }).join('');
  }

  // Live-sync: any HP/XP/loot/quest change anywhere re-renders the live
  // panels (nd-live.js broadcasts the version; ndLiveRefetch is page-level,
  // these are per-window so we subscribe directly).
  window.addEventListener('nd-live', function () {
    if (document.hidden) return;
    liveLoaders.forEach(function (fn) { fn(); });
  });

  // ── drag / resize ──────────────────────────────────────────────────────
  function wireDrag(handle, win, p) {
    handle.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.ck-wbtn') || COMPACT.matches || win.classList.contains('maxed')) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      const sx = e.clientX - p.x, sy = e.clientY - p.y;
      function move(ev) {
        p.x = Math.max(0, Math.min(viewport.clientWidth - 60, ev.clientX - sx));
        p.y = Math.max(0, Math.min(viewport.clientHeight - 24, ev.clientY - sy));
        win.style.left = p.x + 'px';
        win.style.top = p.y + 'px';
      }
      function up() {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        save();
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });
  }

  function wireResize(handle, win, p) {
    handle.addEventListener('pointerdown', function (e) {
      if (COMPACT.matches || win.classList.contains('maxed')) return;
      e.preventDefault();
      e.stopPropagation();
      handle.setPointerCapture(e.pointerId);
      const sx = e.clientX - p.w, sy = e.clientY - p.h;
      function move(ev) {
        p.w = Math.max(240, Math.min(viewport.clientWidth, ev.clientX - sx));
        p.h = Math.max(140, Math.min(viewport.clientHeight, ev.clientY - sy));
        win.style.width = p.w + 'px';
        win.style.height = p.h + 'px';
      }
      function up() {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        save();
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });
  }

  // ── add-panel modal ────────────────────────────────────────────────────
  let pickerType = null;
  let pickerRows = [];

  function openAdd() {
    addOverlay.style.display = 'block';
    addTypes.style.display = '';
    addPicker.style.display = 'none';
    addSearch.value = '';
    addList.innerHTML = '';
    pickerType = null;
    pickerRows = [];
    addTypes.innerHTML = '';
    Object.keys(CK_TYPES).forEach(function (key) {
      const t = CK_TYPES[key];
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ck-type';
      b.innerHTML = '<span>' + t.icon + '</span><span>' + esc(t.label) + '<small>' + esc(t.sub) + '</small></span>';
      b.addEventListener('click', function () { chooseType(key); });
      addTypes.appendChild(b);
    });
  }
  function closeAdd() { addOverlay.style.display = 'none'; }

  function chooseType(key) {
    pickerType = key;
    if (key === 'map' || key === 'party' || key === 'entity') {
      addPicker.style.display = '';
      addSearch.value = '';
      addSearch.placeholder = key === 'entity' ? 'Search entities…' : 'Filter…';
      addList.innerHTML = '';
      if (key === 'map') {
        pickerRows = CK_MAPS.map(function (m) { return { ref: m.slug, label: '🗺 ' + m.name, sub: 'map' }; });
        fillPicker(pickerRows);
      } else if (key === 'party') {
        pickerRows = CK_PARTIES.map(function (x) { return { ref: String(x.id), label: '❤ ' + x.name, sub: 'party' }; });
        fillPicker(pickerRows);
      } else {
        pickerRows = [];
        ensureEntities().then(function () { filterEntities(''); });
      }
      addSearch.focus();
    } else {
      addPanel(key, '', '');
      closeAdd();
    }
  }

  function ensureEntities() {
    if (entityCache) return Promise.resolve();
    return fetch('/api/entities/picker').then(function (r) { return r.json(); }).then(function (d) {
      entityCache = d.entities || [];
    }).catch(function () { entityCache = []; });
  }

  function filterEntities(q) {
    q = q.trim().toLowerCase();
    if (!entityCache.length && pickerRows.length) { fillPicker(pickerRows); return; }
    const rows = entityCache.filter(function (e) {
      return !q || e.name.toLowerCase().indexOf(q) !== -1 || (e.kind || '').toLowerCase().indexOf(q) !== -1;
    }).slice(0, 60).map(function (e) {
      return { ref: String(e.id), label: (KIND_ICONS[e.kind] || '👤') + ' ' + e.name, sub: e.kind };
    });
    pickerRows = rows;
    fillPicker(rows);
  }

  function filterPicker(q) {
    if (pickerType === 'entity') { filterEntities(q); return; }
    q = q.trim().toLowerCase();
    fillPicker(pickerRows.filter(function (r) {
      return !q || r.label.toLowerCase().indexOf(q) !== -1;
    }));
  }

  function fillPicker(rows) {
    addList.innerHTML = '';
    if (!rows.length) {
      addList.innerHTML = '<p class="ck-empty" style="color:var(--text-dim);font-size:.8rem">Nothing to add yet.</p>';
      return;
    }
    rows.forEach(function (row) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ck-pick';
      b.innerHTML = '<span>' + esc(row.label) + '</span><small>' + esc(row.sub) + '</small>';
      b.addEventListener('click', function () {
        addPanel(pickerType, row.ref, row.label);
        closeAdd();
      });
      addList.appendChild(b);
    });
  }

  function addPanel(type, ref, title) {
    const t = CK_TYPES[type];
    const n = panels.length;
    const casc = (n % 6) * 28;
    const p = {
      id: newId(), type: type, ref: ref || '', title: title || (t.icon + ' ' + t.label),
      x: Math.min(48 + casc, Math.max(8, viewport.clientWidth - t.w - 8)),
      y: Math.min(40 + casc, Math.max(8, viewport.clientHeight - 160)),
      w: t.w, h: t.h, z: ++zTop, collapsed: false, data: {},
    };
    panels.push(p);
    if (welcome) welcome.style.display = 'none';
    viewport.appendChild(buildWin(p));
    if (p.type === 'party' || p.type === 'quests') loadLive(p);
    save();
  }

  // ── presets ────────────────────────────────────────────────────────────
  function renderPresetSelect(selected) {
    const names = Object.keys(presets);
    presetSelect.style.display = names.length ? '' : 'none';
    deletePresetBtn.style.display = names.length ? '' : 'none';
    presetSelect.innerHTML = '<option value="">' + (selected ? '' : 'Layout: unsaved') + '</option>' +
      names.map(function (nm) {
        return '<option value="' + esc(nm) + '"' + (nm === selected ? ' selected' : '') + '>' + esc(nm) + '</option>';
      }).join('');
  }

  document.getElementById('ck-preset-select').addEventListener('change', function () {
    const name = this.value;
    if (!name || !presets[name]) return;
    panels = clone(presets[name].panels);
    recomputeSeq();
    panels.forEach(function (p) { p.id = newId(); });
    render();
    save();
  });

  document.getElementById('ck-save-preset').addEventListener('click', function () {
    const current = presetSelect.value;
    const name = current || prompt('Name this layout (e.g. Combat, Exploration):');
    if (!name) return;
    presets[name.trim().slice(0, 40)] = clone({ panels: panels });
    renderPresetSelect(name.trim().slice(0, 40));
    save();
  });

  deletePresetBtn.addEventListener('click', function () {
    const name = presetSelect.value;
    if (!name || !presets[name]) return;
    if (!confirm('Delete the saved layout "' + name + '"?')) return;
    delete presets[name];
    renderPresetSelect();
    save();
  });

  document.getElementById('ck-add-btn').addEventListener('click', openAdd);
  document.getElementById('ck-add-close').addEventListener('click', closeAdd);
  addOverlay.addEventListener('mousedown', function (e) { if (e.target === addOverlay) closeAdd(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && addOverlay.style.display === 'block') closeAdd();
  });
  addSearch.addEventListener('input', function () {
    filterPicker(this.value);
  });

  document.getElementById('ck-reset').addEventListener('click', function () {
    if (!confirm('Reset the cockpit to the default panel set?')) return;
    panels = defaultLayout();
    render();
    save();
  });

  // Size the workspace to the REAL topbar height (the fixed 52px guess in
  // CSS is wrong whenever the nav wraps — the wrap then overflows the body
  // and the whole page scrolls).
  function fitViewport() {
    const tb = document.querySelector('.topbar');
    const wrap = document.getElementById('ck-wrap');
    if (!tb || !wrap) return;
    wrap.style.height = (window.innerHeight - tb.offsetHeight) + 'px';
    // Pull back any window the last layout left below/right of the (possibly
    // smaller) viewport, so panels can't get lost off-screen.
    let moved = false;
    panels.forEach(function (p) {
      const maxX = Math.max(8, viewport.clientWidth - 60);
      const maxY = Math.max(8, viewport.clientHeight - 24);
      if (p.x > maxX) { p.x = maxX; moved = true; }
      if (p.y > maxY) { p.y = maxY; moved = true; }
    });
    if (moved) {
      panels.forEach(function (p) {
        const win = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
        if (win) { win.style.left = p.x + 'px'; win.style.top = p.y + 'px'; }
      });
      save();
    }
  }
  window.addEventListener('resize', fitViewport);
  fitViewport();

  load();
})();
