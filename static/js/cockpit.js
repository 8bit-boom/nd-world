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
// - Live panels (party vitals, quests, entity cards) are client renders of
//   existing JSON endpoints, refreshed by the Phase-1 live-sync bus
//   ("nd-live" event, debounced into one pass) — the same data the party
//   page, quest board, and hover-preview render server-side.
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
  const windowsBtn = document.getElementById('ck-windows-btn');
  const windowsPop = document.getElementById('ck-windows-pop');
  const kindSelect = document.getElementById('ck-add-kind');
  const pinBtn = document.getElementById('ck-pin-btn');

  const GRID = 8;              // snap-to-grid (px)
  const DOCK_ZONE = 28;        // edge width that triggers half-dock (px)
  const ACCENTS = ['', '#00f0ff', '#ff2d78', '#7CFC9A', '#ffd166', '#a855f7', '#ff8844', '#e0e0e0'];
  let userTouched = false;     // suppresses server reconciliation once the GM acts

  const KIND_ICONS = { character: '👤', creature: '🐉', location: '📍', organization: '🏛',
    item: '🗡', note: '📝', event: '⚡', race: '🧬', profession: '🎭', feat: '✨' };
  const COMPACT = window.matchMedia('(max-width: 900px)');
  // Every iframe window is a whole page. static/js/cockpit-frames.js loads them a few at a time, only once they
  // are showing, and gives back the page of a window hidden by the layout for a minute (collapsed window,
  // inactive phone tab) - see its header for the numbers behind this.
  const frames = ndCreateFrameLoader({ concurrency: COMPACT.matches ? 2 : 4 });
  const LOW_MEM = COMPACT.matches || !!(navigator.deviceMemory && navigator.deviceMemory <= 4);
  // ── mobile / tablet mode ─────────────────────────────────────────────
  // The SAME cockpit (GM /cockpit and Player /player-cockpit) renders an
  // app-style shell at ≤900px: one panel at a time, bottom tab bar, no
  // floating windows (drag/resize are unusable on touch anyway). Panels
  // reuse fillBody verbatim, and each section carries the same
  // .ck-win[data-pid] contract the live loaders query, so party vitals /
  // quests / cards refresh identically in both modes.
  const mqlMobile = window.matchMedia('(max-width: 900px)');
  function MOBILE() { return mqlMobile.matches; }
  let mActiveId = null;
  const mWrap = document.getElementById('ck-mwrap');
  const mContent = document.getElementById('ck-mcontent');
  const mTabs = document.getElementById('ck-mtabs');
  // Player mode (the same route, adapted server-side): a tailored panel set
  // and localStorage-only persistence — the workspace API stays GM-only,
  // because the server-side workspace is ONE shared layout per world and a
  // player saving would stomp the GM's arrangement.
  const PLAYER = CK_PLAYER_MODE === true;
  // A GM looking at the cockpit as a player does keeps that arrangement apart from their own GM cockpit
  // (same browser storage otherwise), and asks for that character's board.
  const LS_KEY = 'nd_cockpit_ws_' + CK_WORLD + (CK_VIEW_AS ? '_as_player' : '');
  const BOARD_URL = '/api/cockpit/player-board' + (CK_VIEW_AS && CK_FOCUS_PC ? '?pc=' + CK_FOCUS_PC : '');
  const PLAYER_TYPES = ['pc', 'map', 'wmap', 'dice', 'party', 'quests', 'entity',
    'ecard', 'notes', 'ai_chat', 'timer', 'calendar', 'gallery', 'audio', 'video'];
  function panelTypes() {
    if (!PLAYER) return CK_TYPES;
    const out = {};
    PLAYER_TYPES.forEach(function (k) { if (CK_TYPES[k]) out[k] = CK_TYPES[k]; });
    return out;
  }

  let panels = [];
  let presets = {};
  let zTop = 10;
  let seq = 1;
  const liveLoaders = new Map(); // panel id -> refetch fn (party/quests)
  let entityCache = null;
  const findAutoRan = new Set();  // find panels seeded by a card's 🔗 (auto-run)
  const findBusy = new Set();     // find panels with a job in flight
  const panelCleanups = new Map(); // panel id -> fn (e.g. stop a timer)
  const undoStack = [];           // last closed panels (Undo toast / Ctrl+Shift+T)
  let lastSavedAt = 0;
  let saveError = '';   // why the last server save failed ('' = it did not)

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
  let liveTimer = null;
  function save() {
    userTouched = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 400);
  }
  // Live-sync refreshes collapse into ONE pass: a burst of changes (bulk XP,
  // a loot spree) would otherwise fire one fetch per panel in parallel.
  function scheduleLiveRefresh() {
    clearTimeout(liveTimer);
    liveTimer = setTimeout(function () {
      if (document.hidden) return;
      liveLoaders.forEach(function (fn) { fn(); });
    }, 500);
  }
  function saveNow() {
    const ws = { current: { panels: panels }, presets: presets };
    try { localStorage.setItem(LS_KEY, JSON.stringify(ws)); } catch (e) {}
    if (PLAYER) return;  // server workspace is GM-only
    fetch('/api/cockpit/workspace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ws),
    }).then(function (r) {
      if (r.ok) { lastSavedAt = Date.now(); saveError = ''; refreshSavedLabel(); return; }
      // The server refused the layout (too many windows, too much text): say so instead of showing "saved".
      return r.json().catch(function () { return {}; }).then(function (d) {
        saveError = d.detail || ('HTTP ' + r.status);
        refreshSavedLabel();
        toast('Layout not saved on the server: ' + saveError + ' (it is still kept in this browser).');
      });
    }).catch(function () { saveError = 'offline'; refreshSavedLabel(); });
  }

  function adopt(ws) {
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
      if (p.type === 'wmap') return (CK_WORLD_MAPS || []).some(function (m) { return m.slug === p.ref; });
      if (p.type === 'party') return CK_PARTIES.some(function (x) { return String(x.id) === String(p.ref); });
      return true;
    });
    if (panels.length > CK_MAX_PANELS) {
      panels = panels.slice(0, CK_MAX_PANELS);
      toast('This layout had more than ' + CK_MAX_PANELS + ' windows; the first ' + CK_MAX_PANELS + ' are shown.');
    }
    if (PLAYER) focusMyCharacter();
    recomputeSeq();
    // Re-mint ids on every load: guarantees uniqueness even if an older
    // build saved a workspace with duplicate ids (its counter-reset bug).
    panels.forEach(function (p) { p.id = newId(); });
    renderPresetSelect();
    render();
    clampX();
  }

  // Opened from a character's page (?pc=): that character's My Character panel leads. A saved layout keeps
  // everything else — the first My Character panel is pointed at the character, or one is added if the
  // layout has none. (A plain /player-cockpit visit never re-adds a panel the player closed.)
  function focusMyCharacter() {
    if (!CK_FOCUS_PC) return;
    const me = CK_MY_PCS.find(function (m) { return m.id === CK_FOCUS_PC; });
    if (!me) return;
    const title = '\u{1F9D9} ' + me.name;
    const existing = panels.find(function (p) { return p.type === 'pc'; });
    if (existing) {
      existing.ref = String(me.id); existing.title = title;
      panels.splice(panels.indexOf(existing), 1);   // first in the list = first tab on a phone
      panels.unshift(existing);
      return;
    }
    const vw = viewport.clientWidth || 1200;
    panels.unshift({ id: 'p0', type: 'pc', ref: String(me.id), title: title, x: Math.max(8, vw - 368), y: 8, w: 360, h: 220,
      z: panels.reduce(function (m, p) { return Math.max(m, p.z || 0); }, 10) + 1, collapsed: false, data: {} });
  }

  async function load() {
    if (PLAYER) {
      // Players: localStorage-only persistence (server workspace is GM-only).
      let mirror = null;
      try { mirror = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch (e) {}
      adopt(mirror);
      clampX();
      return;
    }
    // Instant paint: the localStorage mirror renders immediately, so the
    // cockpit is up before the network round trip. The server copy is the
    // cross-device truth and reconciles over the mirror below — unless the
    // GM has already acted in the first moments (local actions win, and
    // their save() pushes them up).
    let mirror = null;
    try { mirror = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch (e) {}
    const haveMirror = !!(mirror && mirror.current &&
      Array.isArray(mirror.current.panels) && mirror.current.panels.length);
    if (haveMirror) { adopt(mirror); userTouched = false; }

    let ws = null;
    try {
      const r = await fetch('/api/cockpit/workspace');
      if (r.ok) ws = (await r.json()).workspace;
    } catch (e) {}

    if (userTouched) return;  // GM acted during the fetch — local state wins
    if (!ws) return;          // server unreachable/never arranged: mirror or (below) default already up
    const incoming = (ws.current && Array.isArray(ws.current.panels)) ? ws.current.panels : null;
    if (incoming && incoming.length) {
      // Adopt the server copy when it differs from the painted mirror (e.g.
      // another device arranged things); identical content skips the
      // re-render and its iframe reloads.
      const same = haveMirror &&
        JSON.stringify(incoming) === JSON.stringify(mirror.current.panels) &&
        JSON.stringify(ws.presets || {}) === JSON.stringify(mirror.presets || {});
      if (!same) adopt(ws);
    } else if (!haveMirror) {
      adopt(null);  // first-ever visit on this device → default layout
    }
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
    if (PLAYER && CK_MY_PCS.length) {
      // the player's character leads (and is the first tab in the phone shell)
      const me = CK_MY_PCS.find(function (m) { return m.id === CK_FOCUS_PC; }) || CK_MY_PCS[0];
      add('pc', String(me.id), '🧙 ' + me.name, rx, ry, rw, 200); ry += 210;
    }
    if (CK_PARTIES.length) { add('party', String(CK_PARTIES[0].id), '❤ ' + CK_PARTIES[0].name, rx, ry, rw, 230); ry += 240; }
    add('quests', '', '📜 Quests', rx, ry, rw, 220); ry += 230;
    add('dice', '', '🎲 Dice', rx, ry, rw, Math.max(220, vh - ry - 8));
    return out;
  }

  // ── rendering ──────────────────────────────────────────────────────────
  function render() {
    if (MOBILE()) { renderMobile(); return; }
    frames.forgetAll();   // every window is rebuilt: release the old frames' slots and observers
    viewport.querySelectorAll('.ck-win').forEach(function (el) { el.remove(); });
    liveLoaders.clear();
    // Re-renders re-mint ids (preset switch) or drop panels — chat state
    // keyed to ids that no longer exist is an orphan: abort any stream
    // still writing into it and free the history.
    const liveIds = new Set(panels.map(function (p) { return p.id; }));
    [chatHistory, chatAborts].forEach(function (m) {
      Array.from(m.keys()).forEach(function (k) {
        if (!liveIds.has(k)) {
          const c = chatAborts.get(k);
          if (c) c.abort();
          m.delete(k);
        }
      });
    });
    Array.from(panelCleanups.keys()).forEach(function (k) {
      if (!liveIds.has(k)) {
        panelCleanups.get(k)();
        panelCleanups.delete(k);
      }
    });
    updateStatus();
    panels.forEach(function (p) { viewport.appendChild(buildWin(p)); });
    // Everything registered a loader in fillBody — fire them all now that
    // the nodes are attached (the loaders query the DOM by panel id).
    liveLoaders.forEach(function (fn) { fn(); });
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

    if (p.accent) { win.dataset.accent = p.accent; win.style.setProperty('--ck-accent', p.accent); }
    const head = document.createElement('div');
    head.className = 'ck-head2';
    const title = document.createElement('span');
    title.className = 'ck-win-title';
    title.textContent = p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label;
    title.title = 'Double-click to rename';
    title.addEventListener('dblclick', function (e) {
      e.stopPropagation();
      const name = prompt('Panel title:', p.title || CK_TYPES[p.type].label);
      if (name === null) return;
      p.title = name.trim().slice(0, 120);
      title.textContent = p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label;
      save();
    });
    const accentBtn = mkBtn('🎨', 'Colour-code this panel', function () {
      const cur = ACCENTS.indexOf(p.accent || '');
      const next = ACCENTS[(cur + 1) % ACCENTS.length];
      p.accent = next;
      if (next) { win.dataset.accent = next; win.style.setProperty('--ck-accent', next); }
      else { delete win.dataset.accent; win.style.removeProperty('--ck-accent'); }
      save();
    });
    const reloadBtn = mkBtn('⟳', 'Reload this panel', function () {
      const frame = win.querySelector('.ck-body iframe');
      if (frame) { frames.reload(frame); return; }
      const fn = liveLoaders.get(p.id);
      if (fn) fn();
    });
    const maxBtn = mkBtn('⤢', 'Fill the cockpit (double-click the header)', function () { win.classList.toggle('maxed'); });
    const colBtn = mkBtn(p.collapsed ? '▸' : '▾', 'Collapse / expand', function () {
      p.collapsed = !p.collapsed;
      win.classList.toggle('collapsed', p.collapsed);
      colBtn.textContent = p.collapsed ? '▸' : '▾';
      save();
    });
    const closeBtn = mkBtn('✕', 'Remove this panel', function () { closePanel(p, win); });
    head.append(title, accentBtn, reloadBtn, maxBtn, colBtn, closeBtn);
    head.addEventListener('dblclick', function (e) {
      if (e.target.closest('.ck-wbtn')) return;
      win.classList.toggle('maxed');
    });

    const body = document.createElement('div');
    body.className = 'ck-body';
    fillBody(p, body);

    const rz = document.createElement('div');
    rz.className = 'ck-resize';
    rz.title = 'Resize';

    win.append(head, body, rz);
    head.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      openCtx(p, win, e.clientX, e.clientY);
    });
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
    } else if (p.type === 'wmap') {
      body.appendChild(mkIframe('/maps/' + encodeURIComponent(p.ref)));
    } else if (p.type === 'dice') {
      body.appendChild(mkIframe('/dice'));
    } else if (p.type === 'entity') {
      body.appendChild(mkIframe('/entity/' + encodeURIComponent(p.ref)));
    } else if (p.type === 'ai') {
      body.appendChild(mkIframe('/ai'));
    } else if (p.type === 'gallery') {
      body.appendChild(mkIframe('/images'));
    } else if (p.type === 'audio') {
      body.appendChild(mkIframe('/audio'));
    } else if (p.type === 'video') {
      body.appendChild(mkIframe('/video'));
    } else if (p.type === 'imagestudio') {
      body.appendChild(mkIframe('/imagestudio'));
    } else if (p.type === 'calendar') {
      body.appendChild(mkIframe('/calendar'));
    } else if (p.type === 'combat') {
      // A picked combat embeds the live tracker; an unpicked one shows the
      // in-panel picker so the GM can start from the window itself.
      if (p.ref) {
        body.appendChild(mkIframe('/combat/' + encodeURIComponent(p.ref)));
      } else {
        const live = document.createElement('div');
        live.className = 'ck-live';
        live.innerHTML = '<p class="ck-empty">Loading recent combats…</p>';
        body.appendChild(live);
        liveLoaders.set(p.id, function () { loadCombatPicker(p); });
      }
    } else if (p.type === 'tables') {
      buildTables(p, body);
    } else if (p.type === 'ecard') {
      const card = document.createElement('div');
      card.className = 'ck-card';
      card.innerHTML = '<p class="ck-empty" style="color:var(--text-dim);font-size:.8rem">Loading…</p>';
      body.appendChild(card);
      liveLoaders.set(p.id, function () { loadCard(p); });
    } else if (p.type === 'ai_chat') {
      buildAiChat(p, body);
    } else if (p.type === 'find') {
      buildFind(p, body);
    } else if (p.type === 'timer') {
      buildTimer(p, body);
    } else if (p.type === 'pc') {
      buildMyCharacter(p, body);
    } else if (p.type === 'notes') {
      const wrap = document.createElement('div');
      wrap.style.cssText = 'display:flex;flex-direction:column;height:100%;box-sizing:border-box;gap:.35rem';
      const bar = document.createElement('div');
      bar.style.cssText = 'display:flex;gap:.35rem;align-items:center;flex-wrap:wrap';
      const ta = document.createElement('textarea');
      ta.className = 'ck-notes';
      ta.style.height = 'auto';
      ta.style.flex = '1';
      ta.style.minHeight = '0';
      ta.placeholder = 'Scratch notes — HP tallies, names, reminders…';
      ta.value = (p.data && p.data.text) || '';
      let t = null;
      ta.addEventListener('input', function () {
        p.data = { text: ta.value };
        clearTimeout(t);
        t = setTimeout(save, 900);
      });
      const out = document.createElement('div');
      out.style.cssText = 'display:none;border:1px solid var(--neon);border-radius:4px;background:var(--bg3);padding:.45rem .55rem;font-size:.8rem;white-space:pre-wrap;max-height:45%;overflow-y:auto';
      const row = document.createElement('div');
      row.style.cssText = 'display:none;gap:.35rem';
      const applyBtn = document.createElement('button');
      applyBtn.type = 'button';
      applyBtn.className = 'ck-btn';
      applyBtn.textContent = '✓ Apply';
      applyBtn.title = 'Replace the notes with this result';
      applyBtn.addEventListener('click', function () {
        ta.value = out.dataset.result || '';
        ta.dispatchEvent(new Event('input'));
        out.style.display = 'none';
        row.style.display = 'none';
      });
      const discardBtn = document.createElement('button');
      discardBtn.type = 'button';
      discardBtn.className = 'ck-btn';
      discardBtn.textContent = '✕ Discard';
      discardBtn.addEventListener('click', function () {
        out.style.display = 'none';
        row.style.display = 'none';
      });
      row.append(applyBtn, discardBtn);
      async function runAssist(op) {
        const text = ta.value.trim();
        if (!text) {
          out.style.display = 'block';
          out.textContent = 'Write some notes first — there is nothing for the AI to work from.';
          return;
        }
        impBtn.disabled = expBtn.disabled = true;
        out.style.display = 'block';
        out.textContent = '✨ Working…';
        try {
          const r = await fetch('/api/ai/assist', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ op: op, surface: 'cockpit-notes', body: ta.value.slice(0, 12000), think: false }),
          });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
          out.dataset.result = d.text || '';
          out.textContent = d.text || '(empty result)';
          row.style.display = 'flex';
        } catch (e) {
          out.textContent = '⚠ ' + (e.message || 'AI assist unavailable');
        } finally {
          impBtn.disabled = expBtn.disabled = false;
        }
      }
      const impBtn = mkAiBtn('✨ Improve', 'improve', runAssist);
      const expBtn = mkAiBtn('📜 Expand', 'expand', runAssist);
      bar.append(impBtn, expBtn);
      wrap.append(bar, ta, out, row);
      body.appendChild(wrap);
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
    f.title = 'panel';
    frames.mount(f, embed(path));   // src is set by the loader when the window is showing and a slot is free
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
    } else if (PLAYER) {
      try {
        const r = await fetch(BOARD_URL);
        if (!r.ok) throw new Error();
        renderQuests(await r.json(), live);
      } catch (e) { live.innerHTML = '<p class="ck-empty">Quest board unavailable.</p>'; }
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
        if (m.hp_id && r.id === m.hp_id) return;  // the system's vital track is the HP bar below
        chips += '<i>' + esc(r.current) + '/' + esc(r.max) + ' ' + esc(r.label) + '</i>';
      });
      let hp = '';
      if (m.max_hp) {
        const pct = Math.max(0, Math.min(100, Math.round((m.hp || 0) * 100 / m.max_hp)));
        const cls = pct <= 25 ? 'ck-hp-low' : (pct <= 55 ? 'ck-hp-mid' : 'ck-hp-ok');
        hp = '<span class="ck-hpbar"><span class="ck-hpfill ' + cls + '" style="width:' + pct + '%"></span></span>' +
          '<span class="ck-mem-hp">' + esc(m.hp_label || 'HP') + ' ' + esc(m.hp) + '/' + esc(m.max_hp) + (m.temp_hp ? ' (+' + esc(m.temp_hp) + ')' : '') +
          (m.native === false ? '' : ' · AC ' + esc(m.ac || '—')) + (m.down ? ' · <strong style="color:#e07">DOWN</strong>' : '') + '</span>';
      }
      const lvl = m.native === false ? '' : 'Lvl ' + esc(m.level) + (m.levelup ? ' ⬆' : '');
      return '<a class="ck-mem' + (m.down ? ' down' : '') + '" href="/characters/' + encodeURIComponent(m.id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">' +
        '<span class="ck-mem-row"><span>' + esc(m.name) + '</span><span class="ck-mem-lvl">' + lvl + '</span></span>' +
        (chips ? '<span class="ck-reschips">' + chips + '</span>' : '') + hp +
        ((m.conditions || []).length ? '<span class="ck-conds">' + esc(m.conditions.join(', ')) + '</span>' : '') +
        '</a>';
    }).join('');
  }

  function renderQuests(d, live) {
    if (d.parties) d = { quests: d.quests || [] };  // player-board payload
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

  async function loadCard(p) {
    const live = viewport.querySelector('.ck-win[data-pid="' + p.id + '"] .ck-card');
    if (!live) return;
    try {
      const r = await fetch('/api/entity/' + encodeURIComponent(p.ref) + '/preview');
      if (!r.ok) throw new Error();
      const d = await r.json();
      live.innerHTML =
        (d.image_url ? '<img src="' + esc(d.image_url) + '" alt=""/>' : '') +
        '<span class="entity-kind">' + esc((d.kind_icon || '') + ' ' + (d.kind || '')) + '</span>' +
        '<h3>' + esc(d.name) + '</h3>' +
        (d.summary ? '<p class="detail-summary">' + esc(d.summary) + '</p>' : '') +
        ((d.tags || []).length ? '<div class="tags">' + d.tags.map(function (t) { return '<span class="tag">' + esc(t) + '</span>'; }).join('') + '</div>' : '') +
        (d.body_html ? '<div class="prose">' + d.body_html + '</div>' : '') +
        '<a href="/entity/' + encodeURIComponent(d.id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">Open full page →</a>';
      const ask = document.createElement('button');
      ask.type = 'button';
      ask.className = 'ck-btn';
      ask.style.marginTop = '.4rem';
      ask.textContent = '✨ Ask AI about this';
      ask.title = 'Opens an AI chat seeded with this entity';
      ask.addEventListener('click', function () {
        addPanel('ai_chat', String(d.id), '✨ ' + (d.name || 'entity'));
      });
      live.appendChild(ask);
      const conn = document.createElement('button');
      conn.type = 'button';
      conn.className = 'ck-btn';
      conn.style.marginTop = '.35rem';
      conn.textContent = '🔗 Find connections';
      conn.title = 'Find entities & notes connected to this — AI, thinking + RAG';
      conn.addEventListener('click', function () {
        addPanel('find', String(d.id), '🔗 ' + (d.name || 'entity'), { autoRun: true });
      });
      live.appendChild(conn);
    } catch (e) {
      live.innerHTML = '<p class="ck-empty" style="color:var(--text-dim);font-size:.8rem">Entity unavailable.</p>';
    }
  }

  async function loadCombatPicker(p) {
    const live = viewport.querySelector('.ck-win[data-pid="' + p.id + '"] .ck-live');
    if (!live) return;
    try {
      const r = await fetch('/api/combat/recent');
      if (!r.ok) throw new Error();
      const d = await r.json();
      live.innerHTML = '<h4>Pick a combat to track</h4>' + ((d.combats || []).length ?
        d.combats.map(function (c) {
          return '<a class="ck-quest" href="#" data-cid="' + esc(c.id) + '"><span class="ck-quest-t">⚔ ' + esc(c.name) + '</span>' +
            '<span class="ck-quest-m"><i class="ck-pill">round ' + esc(c.round_num) + '</i></span></a>';
        }).join('') :
        '<p class="ck-empty">No combats yet — <a href="/combat?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank">create one</a>.</p>');
      live.querySelectorAll('[data-cid]').forEach(function (a) {
        a.addEventListener('click', function (e) {
          e.preventDefault();
          const me = panels.find(function (x) { return x.id === p.id; });
          const c = (d.combats || []).find(function (x) { return String(x.id) === a.dataset.cid; });
          me.ref = a.dataset.cid;
          if (c) me.title = '⚔ ' + c.name;
          render();  // swap the picker for the embedded tracker
          save();
        });
      });
    } catch (e) {
      live.innerHTML = '<p class="ck-empty">Combats unavailable.</p>';
    }
  }

  async function buildTables(p, body) {
    const wrap = document.createElement('div');
    wrap.className = 'ck-tables';
    wrap.innerHTML = '<select></select><button type="button" class="ck-btn">🎲 Roll</button>' +
      '<div class="ck-roll-out" style="display:none"></div>';
    body.appendChild(wrap);
    const sel = wrap.querySelector('select');
    try {
      const r = await fetch('/api/tables/options');
      if (!r.ok) throw new Error();
      const d = await r.json();
      (d.tables || []).forEach(function (t) {
        const o = document.createElement('option');
        o.value = t.id;
        o.textContent = t.name + ' (' + t.entries + ')';
        sel.appendChild(o);
      });
    } catch (e) { /* select stays empty */ }
    const out = wrap.querySelector('.ck-roll-out');
    wrap.querySelector('button').addEventListener('click', async function () {
      if (!sel.value) return;
      try {
        const r = await fetch('/api/tables/' + encodeURIComponent(sel.value) + '/roll', { method: 'POST' });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.status);
        out.style.display = '';
        out.innerHTML = esc(d.result) + '<small>rolled ' + esc(d.roll) + ' / ' + esc(d.total) + '</small>';
      } catch (e) {
        out.style.display = '';
        out.textContent = 'Roll failed.';
      }
    });
  }

  function mkAiBtn(label, op, run) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ck-btn';
    b.textContent = label;
    b.addEventListener('click', function () { run(op); });
    return b;
  }

  // ── native AI chat panel ───────────────────────────────────────────────
  // Streaming client for the SAME endpoint the /ai page uses (POST
  // /api/ai/stream — SSE: token/thinking/note/error pieces, [DONE]).
  // Context-aware: a panel seeded from an entity card (ref set) folds a
  // per-question relevant excerpt of that entity (POST /api/ai/entity-
  // context — the same search the entity page's Ask AI uses) into the
  // system prompt, plus a capped list of the cockpit's open panels so the
  // copilot knows what the GM is looking at. History is per-panel and
  // ephemeral — a cockpit chat is working memory, not an archive; /ai and
  // NPC Talk are the durable surfaces.
  const chatHistory = new Map();  // panel id -> [{role, content}]
  const chatAborts = new Map();   // panel id -> AbortController

  function cleanupPanel(panelId) {
    const c = chatAborts.get(panelId);
    if (c) c.abort();  // stop tokens streaming into a window nobody sees
    chatAborts.delete(panelId);
    chatHistory.delete(panelId);
    liveLoaders.delete(panelId);
    const extra = panelCleanups.get(panelId);
    if (extra) extra();  // e.g. stop a timer's setInterval
    panelCleanups.delete(panelId);
  }

  function buildAiChat(p, body) {
    const wrap = document.createElement('div');
    wrap.className = 'ck-aichat';
    const ctx = document.createElement('div');
    ctx.className = 'ck-aichat-ctx';
    ctx.textContent = p.ref ? '🪪 ' + (p.title || ('entity #' + p.ref)) : '🌍 ' + CK_WORLD;
    const bar = document.createElement('div');
    bar.className = 'ck-aichat-bar';
    const sel = document.createElement('select');
    sel.title = 'Model';
    const defOpt = document.createElement('option');
    defOpt.value = '';
    defOpt.textContent = '— default model —';
    sel.appendChild(defOpt);
    fetch('/api/ai/models').then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      (d.models || []).slice(0, 40).forEach(function (m) {
        const o = document.createElement('option');
        o.value = m.id;
        o.textContent = (m.loaded ? '● ' : '') + (m.label || m.id);
        sel.appendChild(o);
      });
      if (d.default) sel.value = d.default;
    }).catch(function () {});
    const think = document.createElement('label');
    think.title = 'Thinking mode';
    think.innerHTML = '<input type="checkbox" checked> 🧠';
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'ck-btn';
    clear.textContent = '🗑';
    clear.title = 'Clear this conversation';
    bar.append(sel, think, clear);
    const msgs = document.createElement('div');
    msgs.className = 'ck-aichat-msgs';
    const status = document.createElement('div');
    status.className = 'ck-aichat-status';
    const inRow = document.createElement('div');
    inRow.className = 'ck-aichat-in';
    const ta = document.createElement('textarea');
    ta.rows = 2;
    ta.placeholder = 'Ask the copilot… (Enter to send, Shift+Enter for a newline)';
    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'ck-btn';
    send.textContent = 'Send';
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'ck-btn';
    stop.textContent = '⏹';
    stop.title = 'Stop generating';
    stop.style.display = 'none';
    inRow.append(ta, send, stop);
    wrap.append(ctx, bar, msgs, status, inRow);
    body.appendChild(wrap);

    const history = chatHistory.get(p.id) || [];
    chatHistory.set(p.id, history);
    function addMsg(role, text) {
      const b = document.createElement('div');
      b.className = 'ck-msg ck-msg-' + role;
      b.textContent = text;
      msgs.appendChild(b);
      msgs.scrollTop = msgs.scrollHeight;
      return b;
    }
    history.forEach(function (m) { addMsg(m.role, m.content); });

    clear.addEventListener('click', function () {
      history.length = 0;
      msgs.innerHTML = '';
      status.textContent = '';
    });
    stop.addEventListener('click', function () {
      const c = chatAborts.get(p.id);
      if (c) c.abort();
    });
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); }
    });
    send.addEventListener('click', ask);

    async function ask() {
      const text = ta.value.trim();
      if (!text || send.disabled) return;
      ta.value = '';
      status.textContent = '';
      addMsg('user', text);
      history.push({ role: 'user', content: text });
      if (history.length > 24) history.splice(0, history.length - 24);
      let sys = 'You are the GM\'s copilot inside the nd-world cockpit. Keep answers tight and table-paced — short paragraphs, concrete names, markdown ok.';
      const openTitles = panels.map(function (q) {
        return q.title || (CK_TYPES[q.type] ? CK_TYPES[q.type].label : q.type);
      }).slice(0, 8).join('; ');
      if (openTitles) sys += '\n\nOpen cockpit panels: ' + openTitles;
      if (p.ref) {
        try {
          const r = await fetch('/api/ai/entity-context', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entity_id: Number(p.ref), query: text.slice(0, 400) }),
          });
          if (r.ok) {
            const d = await r.json();
            if (d.context) sys += '\n\n=== Relevant excerpt from the focused entity ===\n' + d.context;
          }
        } catch (e) { /* context is best-effort */ }
      }
      const ctrl = new AbortController();
      chatAborts.set(p.id, ctrl);
      send.disabled = true;
      stop.style.display = '';
      let acc = '';
      let bubble = null;
      const thinkEl = document.createElement('div');
      try {
        const res = await fetch('/api/ai/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: ctrl.signal,
          body: JSON.stringify({
            messages: history, system: sys, model: sel.value || '',
            surface: 'chat', options: {}, think: think.querySelector('input').checked,
          }),
        });
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buf += dec.decode(chunk.value, { stream: true });
          const parts = buf.split('\n\n');
          buf = parts.pop();
          for (const part of parts) {
            for (const line of part.split('\n')) {
              if (!line.startsWith('data: ')) continue;
              const payload = line.slice(6);
              if (payload === '[DONE]') continue;
              let d;
              try { d = JSON.parse(payload); } catch (e) { continue; }
              if (d.token !== undefined) {
                if (!bubble) {
                  bubble = document.createElement('div');
                  bubble.className = 'ck-msg ck-msg-ai';
                  bubble.appendChild(thinkEl);
                  const span = document.createElement('span');
                  span.className = 'ck-msg-text';
                  bubble.appendChild(span);
                  msgs.appendChild(bubble);
                  msgs.scrollTop = msgs.scrollHeight;
                }
                acc += d.token;
                bubble.querySelector('.ck-msg-text').textContent = acc;
                msgs.scrollTop = msgs.scrollHeight;
              } else if (d.thinking !== undefined) {
                thinkEl.className = 'ck-msg-think';
                thinkEl.textContent = '🧠 ' + d.thinking.slice(-400);
              } else if (d.note) {
                status.textContent = 'ℹ ' + d.note;
              } else if (d.error) {
                status.textContent = '⚠ ' + d.error;
              }
            }
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') status.textContent = '⚠ ' + (e.message || 'AI unavailable');
      } finally {
        if (acc) history.push({ role: 'assistant', content: acc });
        chatAborts.delete(p.id);
        send.disabled = false;
        stop.style.display = 'none';
        msgs.scrollTop = msgs.scrollHeight;
      }
    }
  }


  // ── toasts ─────────────────────────────────────────────────────────────
  let toastHost = null;
  function toast(msg, actionLabel, actionFn) {
    if (!toastHost) {
      toastHost = document.createElement('div');
      toastHost.id = 'ck-toasts';
      document.body.appendChild(toastHost);
    }
    const t = document.createElement('div');
    t.className = 'ck-toast';
    const s = document.createElement('span');
    s.textContent = msg;
    t.appendChild(s);
    if (actionLabel && actionFn) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = actionLabel;
      b.addEventListener('click', function () { actionFn(); t.remove(); });
      t.appendChild(b);
    }
    toastHost.appendChild(t);
    setTimeout(function () { t.remove(); }, 6000);
  }

  // ── close with animation + Undo (a browser-grade convenience: one misclick
  // shouldn't cost a loaded map window) ────────────────────────────────────
  function closePanel(p, win) {
    const idx = panels.indexOf(p);
    if (idx !== -1) panels.splice(idx, 1);
    if (win) {
      win.querySelectorAll('iframe').forEach(function (f) { frames.forget(f); });
      win.classList.add('closing');
      setTimeout(function () { win.remove(); }, 130);
    }
    cleanupPanel(p.id);
    if (welcome) welcome.style.display = panels.length ? 'none' : '';
    undoStack.push({ panel: p, index: idx === -1 ? panels.length : idx });
    if (undoStack.length > 8) undoStack.shift();
    // Undo restores the window, not its live contents (an aborted chat or
    // timer starts fresh) — the arrangement is what matters.
    toast('Closed "' + (p.title || CK_TYPES[p.type].label) + '"', 'Undo', undoClose);
    updateStatus();
    save();
  }
  function undoClose() {
    const item = undoStack.pop();
    if (!item) return;
    const i = Math.min(item.index, panels.length);
    panels.splice(i, 0, item.panel);
    if (welcome) welcome.style.display = 'none';
    viewport.appendChild(buildWin(item.panel));
    const fn = liveLoaders.get(item.panel.id);
    if (fn) fn();
    updateStatus();
    save();
  }
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey && e.shiftKey && (e.key === 'T' || e.key === 't')) {
      e.preventDefault();
      undoClose();
    }
  });

  // ── shared half-dock (drag release + context menu) ─────────────────────
  function dockPanel(win, p, right) {
    p.x = right ? Math.ceil(viewport.clientWidth / 2) : 0;
    p.y = 0;
    p.w = Math.floor(viewport.clientWidth / 2);
    p.h = viewport.clientHeight;
    win.style.left = p.x + 'px'; win.style.top = p.y + 'px';
    win.style.width = p.w + 'px'; win.style.height = p.h + 'px';
  }

  // ── window context menu (right-click a title bar) ──────────────────────
  let ctxEl = null;
  function openCtx(p, win, x, y) {
    if (COMPACT.matches) return;
    if (!ctxEl) {
      ctxEl = document.createElement('div');
      ctxEl.id = 'ck-ctx';
      document.body.appendChild(ctxEl);
    }
    ctxEl.innerHTML = '';
    const items = [
      ['⟳ Reload', function () {
        const f = win.querySelector('.ck-body iframe');
        if (f) { frames.reload(f); return; }
        const fn = liveLoaders.get(p.id);
        if (fn) fn();
      }],
      ['✏ Rename…', function () {
        const name = prompt('Panel title:', p.title || CK_TYPES[p.type].label);
        if (name === null) return;
        p.title = name.trim().slice(0, 120);
        const t = win.querySelector('.ck-win-title');
        if (t) t.textContent = p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label;
        save();
      }],
      ['🎨 Accent', function () {
        const cur = ACCENTS.indexOf(p.accent || '');
        const next = ACCENTS[(cur + 1) % ACCENTS.length];
        p.accent = next;
        if (next) { win.dataset.accent = next; win.style.setProperty('--ck-accent', next); }
        else { delete win.dataset.accent; win.style.removeProperty('--ck-accent'); }
        save();
      }],
      ['⤢ Maximize', function () { win.classList.toggle('maxed'); }],
      ['◧ Dock left', function () { win.classList.remove('maxed'); dockPanel(win, p, false); save(); }],
      ['◨ Dock right', function () { win.classList.remove('maxed'); dockPanel(win, p, true); save(); }],
      'hr',
      ['✕ Close', function () { closePanel(p, win); }],
    ];
    items.forEach(function (it) {
      if (it === 'hr') { ctxEl.appendChild(document.createElement('hr')); return; }
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = it[0];
      b.addEventListener('click', function () { ctxEl.style.display = 'none'; it[1](); });
      ctxEl.appendChild(b);
    });
    ctxEl.style.display = 'block';
    ctxEl.style.left = Math.max(8, Math.min(x, window.innerWidth - ctxEl.offsetWidth - 8)) + 'px';
    ctxEl.style.top = Math.max(8, Math.min(y, window.innerHeight - ctxEl.offsetHeight - 8)) + 'px';
  }
  document.addEventListener('mousedown', function (e) {
    if (ctxEl && ctxEl.style.display === 'block' && !e.target.closest('#ck-ctx')) {
      ctxEl.style.display = 'none';
    }
  });

  // ── status bar ─────────────────────────────────────────────────────────
  function updateStatus() {
    const c = document.getElementById('ck-stat-left');
    if (c) c.textContent = '\u{1FA9F} ' + panels.length;
    const m = document.getElementById('ck-stat-mid');
    if (m) m.textContent = presetSelect.value ? '\u{1F4CB} ' + presetSelect.value : '';
  }
  function refreshSavedLabel() {
    const el = document.getElementById('ck-stat-saved');
    if (!el) return;
    if (saveError) { el.textContent = '\u26A0 not saved: ' + saveError; return; }
    if (!lastSavedAt) { el.textContent = '\u2014'; return; }
    const s = Math.round((Date.now() - lastSavedAt) / 1000);
    el.textContent = 'saved ' + (s < 60 ? s + 's' : (s < 3600 ? Math.round(s / 60) + 'm' : Math.round(s / 3600) + 'h')) + ' ago';
  }
  setInterval(refreshSavedLabel, 3000);

  // ── fullscreen (table mode) ─────────────────────────────────────────────
  const fsBtn = document.getElementById('ck-fs-btn');
  if (fsBtn) {
    fsBtn.addEventListener('click', function () {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen().catch(function () {});
    });
  }

  // ── welcome quick-start tiles ───────────────────────────────────────────
  function buildWelcomeTiles() {
    const host = document.getElementById('ck-welcome-tiles');
    if (!host || host.children.length) return;
    const quick = [];
    if (CK_MAPS.length) quick.push(['map', CK_MAPS[0].slug, '\u{1F5FA}\u{FE0F} ' + CK_MAPS[0].name]);
    if ((CK_WORLD_MAPS || []).length) quick.push(['wmap', CK_WORLD_MAPS[0].slug, '\u{1F5FA}\u{FE0F} ' + CK_WORLD_MAPS[0].name]);
    if (CK_PARTIES.length) quick.push(['party', String(CK_PARTIES[0].id), '\u2764 ' + CK_PARTIES[0].name]);
    ['pc', 'quests', 'dice', 'ai_chat', 'find', 'notes', 'timer'].forEach(function (t) {
      quick.push([t, '', '']);
    });
    quick.forEach(function (q) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ck-tile';
      const t = CK_TYPES[q[0]];
      b.innerHTML = '<span class="ck-tile-ic">' + t.icon + '</span><span>' + esc(t.label) + '</span>';
      b.addEventListener('click', function () { addPanel(q[0], q[1], q[2]); });
      host.appendChild(b);
    });
  }

  // ── timer panel (client-only, session-transient by design) ─────────────
  function buildTimer(p, body) {
    const wrap = document.createElement('div');
    wrap.className = 'ck-timer';
    const time = document.createElement('div');
    time.className = 'ck-timer-time';
    const row = document.createElement('div');
    row.className = 'ck-timer-row';
    const mins = document.createElement('input');
    mins.type = 'number'; mins.min = '1'; mins.max = '600'; mins.value = '5';
    mins.title = 'Minutes';
    const modeBtn = document.createElement('button');
    modeBtn.type = 'button'; modeBtn.className = 'ck-btn';
    modeBtn.textContent = '\u23F1 Countdown';
    const startBtn = document.createElement('button');
    startBtn.type = 'button'; startBtn.className = 'ck-btn';
    startBtn.textContent = '\u25B6 Start';
    const resetBtn = document.createElement('button');
    resetBtn.type = 'button'; resetBtn.className = 'ck-btn';
    resetBtn.textContent = '\u21BA';
    row.append(mins, modeBtn, startBtn, resetBtn);
    wrap.append(time, row);
    body.appendChild(wrap);

    let up = false, running = false, remain = 5 * 60, elapsed = 0, iv = null;
    function fmt(s) {
      s = Math.max(0, Math.round(s));
      const m = Math.floor(s / 60), r = s % 60;
      return (m < 10 ? '0' : '') + m + ':' + (r < 10 ? '0' : '') + r;
    }
    function paint() { time.textContent = fmt(up ? elapsed : remain); }
    function beep() {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        const ac = new AC();
        const o = ac.createOscillator(), g = ac.createGain();
        o.connect(g); g.connect(ac.destination);
        o.frequency.value = 880;
        g.gain.setValueAtTime(0.001, ac.currentTime);
        g.gain.exponentialRampToValueAtTime(0.25, ac.currentTime + 0.02);
        g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + 0.8);
        o.start(); o.stop(ac.currentTime + 0.85);
        setTimeout(function () { ac.close(); }, 1000);
      } catch (e) {}
    }
    function stopTick() { if (iv) { clearInterval(iv); iv = null; } running = false; }
    function resetTimer() {
      stopTick();
      elapsed = 0;
      remain = (parseInt(mins.value, 10) || 5) * 60;
      wrap.classList.remove('done');
      startBtn.textContent = '\u25B6 Start';
      paint();
    }
    modeBtn.addEventListener('click', function () {
      up = !up;
      modeBtn.textContent = up ? '\u25B6 Stopwatch' : '\u23F1 Countdown';
      mins.disabled = up;
      resetTimer();
    });
    startBtn.addEventListener('click', function () {
      if (running) { stopTick(); startBtn.textContent = '\u25B6 Resume'; return; }
      running = true;
      startBtn.textContent = '\u23F8 Pause';
      wrap.classList.remove('done');
      iv = setInterval(function () {
        if (up) { elapsed += 1; paint(); return; }
        remain -= 1;
        paint();
        if (remain <= 0) {
          stopTick();
          wrap.classList.add('done');
          startBtn.textContent = '\u25B6 Start';
          beep();
        }
      }, 1000);
    });
    resetBtn.addEventListener('click', resetTimer);
    mins.addEventListener('change', function () {
      if (!running) { remain = (parseInt(mins.value, 10) || 5) * 60; paint(); }
    });
    paint();
    // Closing/preset-switching the window must stop the interval — register
    // with the generic per-panel cleanup.
    panelCleanups.set(p.id, stopTick);
  }

  // ── AI find / connections panel ────────────────────────────────────────
  // "Find using AI": thinking + RAG search for entities & notes connected
  // to a free-text query — or, seeded from an entity card's 🔗 button, to
  // that entity. Runs as a server-side background job (POST start + poll,
  // the quest-sync pattern) because thinking + RAG regularly outlives
  // Cloudflare's ~100 s no-byte timeout. Thinking and RAG are hardcoded
  // server-side for this surface — no toggles, per design.
  function buildFind(p, body) {
    const wrap = document.createElement('div');
    wrap.className = 'ck-find';
    const chip = document.createElement('div');
    chip.className = 'ck-aichat-ctx';
    const setChip = function () {
      chip.textContent = p.ref ? '\u{1FAAA} ' + (p.title || ('entity #' + p.ref)) : '\u{1F50D} searching the whole world';
      chip.title = p.ref ? 'Click to search freely instead' : '';
      chip.style.cursor = p.ref ? 'pointer' : 'default';
    };
    setChip();
    chip.addEventListener('click', function () {
      if (!p.ref) return;
      p.ref = '';
      p.title = '\u{1F50D} AI Find';
      const t = viewport.querySelector('.ck-win[data-pid="' + p.id + '"] .ck-win-title');
      if (t) t.textContent = p.title;
      setChip();
      goBtn.textContent = '\u{1F50D} Find';
      save();
    });
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:.35rem;align-items:flex-start';
    const ta = document.createElement('textarea');
    ta.rows = 2;
    ta.placeholder = 'What are you looking for? e.g. "who in Yorm owes Vex money"';
    ta.style.cssText = 'flex:1;min-width:0;resize:none;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;font-family:inherit;font-size:.8rem;padding:.35rem .45rem;box-sizing:border-box';
    const goBtn = document.createElement('button');
    goBtn.type = 'button';
    goBtn.className = 'ck-btn';
    goBtn.textContent = p.ref ? '\u{1F517} Find connections' : '\u{1F50D} Find';
    goBtn.style.whiteSpace = 'nowrap';
    row.append(ta, goBtn);
    const status = document.createElement('div');
    status.style.cssText = 'font-size:.72rem;color:var(--text-dim);min-height:1em';
    const results = document.createElement('div');
    results.className = 'ck-find-results';
    wrap.append(chip, row, status, results);
    body.appendChild(wrap);

    function addResult(r) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ck-find-res';
      const head = document.createElement('span');
      head.textContent = (KIND_ICONS[r.kind] || '\u{1F464}') + ' ' + r.name;
      b.appendChild(head);
      const why = document.createElement('small');
      why.textContent = r.reason || '';
      b.appendChild(why);
      b.addEventListener('click', function () {
        addPanel('ecard', String(r.id), (KIND_ICONS[r.kind] || '\u{1F464}') + ' ' + r.name);
      });
      results.appendChild(b);
    }

    function finish(msg) {
      status.textContent = msg;
      findBusy.delete(p.id);
      goBtn.disabled = false;
    }

    function poll(jobId, tries) {
      if ((tries || 0) > 400) { finish('Still running after ~20 minutes — something is wrong with the AI backend.'); return; }
      fetch('/api/cockpit/ai/find/' + jobId).then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(d => {
        if (d.status === 'running') {
          const el = d.elapsed || 0, m = Math.floor(el / 60), s = el % 60;
          status.textContent = '\u23F3 The AI is reading the world\u2026 (' + (m > 0 ? m + 'm ' : '') + s + 's) \u2014 thinking + RAG on';
          setTimeout(() => poll(jobId, (tries || 0) + 1), 3000);
        } else if (d.status === 'error') {
          finish('\u26A0 ' + (d.error || 'The AI find failed.'));
        } else if (d.status === 'done') {
          (d.results || []).forEach(addResult);
          if (!(d.results || []).length) {
            results.innerHTML = '<p style="color:var(--text-dim);font-size:.78rem;margin:0">Nothing genuinely connected came back \u2014 try rephrasing, or widen the query.</p>';
          }
          finish('');
        }
      }).catch(() => {
        // a dropped poll shouldn't abandon the job — the server task keeps running
        setTimeout(() => poll(jobId, (tries || 0) + 1), 3000);
      });
    }

    async function start() {
      if (findBusy.has(p.id)) return;
      const q = ta.value.trim();
      if (!q && !p.ref) { status.textContent = 'Type what to look for first \u2014 or seed me from an entity card.'; return; }
      findBusy.add(p.id);
      goBtn.disabled = true;
      status.textContent = '\u23F3 Starting\u2026';
      results.innerHTML = '';
      try {
        const r = await fetch('/api/cockpit/ai/find/start', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: q, entity_id: p.ref ? Number(p.ref) : null }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
        poll(d.job_id, 0);
      } catch (e) {
        finish('\u26A0 ' + e.message);
      }
    }
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); start(); }
    });
    goBtn.addEventListener('click', start);
    if (p.ref && findAutoRan.has(p.id)) start();
  }

  // Live-sync: any HP/XP/loot/quest change anywhere re-renders the live
  // panels in one debounced pass (nd-live.js broadcasts the version), and
  // pulses the status-bar sync dot.
  window.addEventListener('nd-live', function () {
    const d = document.getElementById('ck-stat-live');
    if (d) {
      d.classList.add('on');
      setTimeout(function () { d.classList.remove('on'); }, 2200);
    }
    scheduleLiveRefresh();
  });

  // ── drag / resize ──────────────────────────────────────────────────────
  function wireDrag(handle, win, p) {
    handle.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.ck-wbtn') || COMPACT.matches || win.classList.contains('maxed')) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      // Y math includes scrollTop on both ends: the workspace scrolls
      // vertically now, and p.y is a content coordinate, not a screen one.
      const sx = e.clientX - p.x, sy = e.clientY - p.y + viewport.scrollTop;
      function move(ev) {
        p.x = Math.max(0, Math.min(viewport.clientWidth - 60, ev.clientX - sx));
        p.y = Math.max(0, ev.clientY - sy + viewport.scrollTop);
        win.style.left = p.x + 'px';
        win.style.top = p.y + 'px';
        // Edge-dock preview: hover near the left/right edge to see the half
        // the window will occupy on release.
        win.classList.toggle('ck-dock-l', ev.clientX <= DOCK_ZONE);
        win.classList.toggle('ck-dock-r', ev.clientX >= viewport.clientWidth - DOCK_ZONE - 1);
      }
      function up(ev) {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        if (win.classList.contains('ck-dock-l') || win.classList.contains('ck-dock-r')) {
          dockPanel(win, p, win.classList.contains('ck-dock-r'));
        } else {
          p.x = Math.round(p.x / GRID) * GRID;
          p.y = Math.round(p.y / GRID) * GRID;
          win.style.left = p.x + 'px';
          win.style.top = p.y + 'px';
        }
        win.classList.remove('ck-dock-l', 'ck-dock-r');
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
      const sx = e.clientX - p.w, sy = e.clientY - p.h + viewport.scrollTop;
      function move(ev) {
        p.w = Math.max(240, Math.min(viewport.clientWidth, ev.clientX - sx));
        p.h = Math.max(140, Math.min(8000, ev.clientY - sy + viewport.scrollTop));
        win.style.width = p.w + 'px';
        win.style.height = p.h + 'px';
      }
      function up() {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        p.w = Math.round(p.w / GRID) * GRID;
        p.h = Math.round(p.h / GRID) * GRID;
        win.style.width = p.w + 'px';
        win.style.height = p.h + 'px';
        save();
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });
  }

  // ── window list popover ───────────────────────────────────
  function renderWindowList() {
    windowsPop.innerHTML = '';
    if (!panels.length) {
      windowsPop.innerHTML = '<p style="color:var(--text-dim);font-size:.8rem;padding:.4rem .5rem">No windows open.</p>';
      return;
    }
    panels.forEach(function (p) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'ck-winrow';
      const label = document.createElement('span');
      label.textContent = p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label;
      row.appendChild(label);
      const x = document.createElement('span');
      x.className = 'ck-winrow-x';
      x.textContent = '✕';
      x.title = 'Close this window';
      x.addEventListener('click', function (e) {
        e.stopPropagation();
        const el = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
        closePanel(p, el);
        renderWindowList();
      });
      row.appendChild(x);
      row.addEventListener('click', function () {
        const el = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
        if (el) { zTop += 1; el.style.zIndex = zTop; p.z = zTop; el.classList.remove('collapsed'); }
        windowsPop.style.display = 'none';
        save();
      });
      windowsPop.appendChild(row);
    });
  }
  windowsBtn.addEventListener('click', function () {
    const open = windowsPop.style.display === 'block';
    if (!open) {
      renderWindowList();
      const r = windowsBtn.getBoundingClientRect();
      windowsPop.style.left = Math.max(8, r.right - 260) + 'px';
      windowsPop.style.top = (r.bottom + 6) + 'px';
      windowsPop.style.display = 'block';
    } else {
      windowsPop.style.display = 'none';
    }
  });
  document.addEventListener('mousedown', function (e) {
    if (windowsPop.style.display === 'block' && !e.target.closest('#ck-windows-pop') && !e.target.closest('#ck-windows-btn')) {
      windowsPop.style.display = 'none';
    }
  });
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
    Object.keys(panelTypes()).forEach(function (key) {
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
    const wantsKindFilter = (key === 'entity' || key === 'ecard');
    kindSelect.style.display = wantsKindFilter ? '' : 'none';
    if (wantsKindFilter) {
      // Rebuild the kind dropdown from what this world actually has — a GM
      // with 80 creatures filters by kind, not by scrolling sixty rows.
      kindSelect.innerHTML = '<option value="">All kinds</option>';
      ensureEntities().then(function () {
        const kinds = [];
        entityCache.forEach(function (e) {
          if (e.kind && kinds.indexOf(e.kind) === -1) kinds.push(e.kind);
        });
        kinds.sort();
        kinds.forEach(function (k) {
          const o = document.createElement('option');
          o.value = k;
          o.textContent = (KIND_ICONS[k] || '?') + ' ' + k;
          kindSelect.appendChild(o);
        });
      });
    }
    if (key === 'map' || key === 'wmap' || key === 'party' || key === 'entity' || key === 'ecard' || key === 'combat') {
      addPicker.style.display = '';
      addSearch.value = '';
      addSearch.placeholder = (key === 'entity' || key === 'ecard') ? 'Search entities…' : 'Filter…';
      addList.innerHTML = '';
      if (key === 'map') {
        pickerRows = CK_MAPS.map(function (m) { return { ref: m.slug, label: '📐 ' + m.name, sub: 'schematic' }; });
        fillPicker(pickerRows);
      } else if (key === 'wmap') {
        pickerRows = (CK_WORLD_MAPS || []).map(function (m) {
          return { ref: m.slug, label: '🗺 ' + m.name, sub: m.markers ? m.markers + ' markers' : 'map' };
        });
        fillPicker(pickerRows);
      } else if (key === 'party') {
        pickerRows = CK_PARTIES.map(function (x) { return { ref: String(x.id), label: '❤ ' + x.name, sub: 'party' }; });
        fillPicker(pickerRows);
      } else if (key === 'combat') {
        pickerRows = [];
        fillPicker([]);
        fetch('/api/combat/recent').then(function (r) { return r.json(); }).then(function (d) {
          pickerRows = (d.combats || []).map(function (c) {
            return { ref: String(c.id), label: '\u2694 ' + c.name, sub: 'round ' + c.round_num };
          });
          filterPicker(addSearch.value);
        }).catch(function () { fillPicker([]); });
      } else if (key === 'ecard' || key === 'entity') {
        pickerRows = [];
        ensureEntities().then(function () { filterEntities(''); });
      } else {
        pickerRows = [];
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
    const kind = kindSelect.value || '';
    const rows = entityCache.filter(function (e) {
      if (kind && e.kind !== kind) return false;
      return !q || e.name.toLowerCase().indexOf(q) !== -1 || (e.folder || '').toLowerCase().indexOf(q) !== -1;
    }).slice(0, 60).map(function (e) {
      return { ref: String(e.id), label: (KIND_ICONS[e.kind] || '👤') + ' ' + e.name, sub: e.folder || e.kind };
    });
    pickerRows = rows;
    fillPicker(rows);
  }

  function filterPicker(q) {
    if (pickerType === 'entity' || pickerType === 'ecard') { filterEntities(q); return; }
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

  function addPanel(type, ref, title, opts) {
    const t = CK_TYPES[type];
    // The server saves at most CK_MAX_PANELS windows per layout and rejects a bigger one whole, so the page stops
    // there too; and it warns once when the pages held in memory start to get heavy.
    const pol = ndCockpitWindowPolicy({ count: panels.length, liveFrames: frames.stats().live, max: CK_MAX_PANELS, lowMemory: LOW_MEM });
    if (!pol.allow) { toast(pol.message); return null; }
    if (pol.warn) toast(pol.message);
    const n = panels.length;
    const casc = (n % 6) * 28;
    const p = {
      id: newId(), type: type, ref: ref || '', title: title || (t.icon + ' ' + t.label),
      x: Math.min(48 + casc, Math.max(8, viewport.clientWidth - t.w - 8)),
      y: Math.min(40 + casc, Math.max(8, viewport.clientHeight - 160)),
      w: t.w, h: t.h, z: ++zTop, collapsed: false, data: {},
    };
    panels.push(p);
    if (opts && opts.autoRun) findAutoRan.add(p.id);   // must be known before buildWin: buildFind checks it
    if (welcome) welcome.style.display = 'none';
    if (MOBILE()) {
      renderMobile();
    } else {
      viewport.appendChild(buildWin(p));
    }
    const fn = liveLoaders.get(p.id);
    if (fn) fn();
    save();
    return p;
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
    clampX();
    save();
  });

  document.getElementById('ck-save-preset').addEventListener('click', function () {
    const current = presetSelect.value;
    let name = current;
    if (!name) {
      name = prompt('Name this layout (e.g. Combat, Exploration):');
      if (!name) return;
    } else if (!confirm('Overwrite the saved layout "' + current + '" with the current windows?')) {
      return;
    }
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
  // The Find-AI toolbar button is GM-only (absent in player mode) — its
  // listener must be guarded or a null addEventListener kills the whole
  // cockpit IIFE (blank workspace for players).
  const findBtn = document.getElementById('ck-find-btn');
  if (findBtn) findBtn.addEventListener('click', function () {
    // Focus an existing unseeded find panel instead of stacking duplicates.
    const existing = panels.find(function (q) { return q.type === 'find' && !q.ref; });
    if (existing) {
      const el = viewport.querySelector('.ck-win[data-pid="' + existing.id + '"]');
      if (el) { zTop += 1; el.style.zIndex = zTop; existing.z = zTop; el.classList.remove('collapsed'); }
      save();
      return;
    }
    addPanel('find', '', '\u{1F50D} AI Find');
  });
  document.getElementById('ck-add-close').addEventListener('click', closeAdd);
  addOverlay.addEventListener('mousedown', function (e) { if (e.target === addOverlay) closeAdd(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (addOverlay.style.display === 'block') closeAdd();
      if (windowsPop.style.display === 'block') windowsPop.style.display = 'none';
      if (ctxEl && ctxEl.style.display === 'block') ctxEl.style.display = 'none';
    }
  });
  addSearch.addEventListener('input', function () {
    filterPicker(this.value);
  });
  kindSelect.addEventListener('change', function () {
    filterPicker(addSearch.value);
  });

  document.getElementById('ck-reset').addEventListener('click', function () {
    if (!confirm('Reset the cockpit to the default panel set?')) return;
    panels = defaultLayout();
    render();
    save();
  });

  // ── My Character panel (player mode): the viewer's own PC at a glance ──
  let myPcCache = null;
  function buildMyCharacter(p, body) {
    const live = document.createElement('div');
    live.className = 'ck-card';
    live.innerHTML = '<p style="color:var(--text-dim);font-size:.8rem;margin:0">Loading…</p>';
    body.appendChild(live);
    liveLoaders.set(p.id, function () { loadMyPc(p, live); });
  }

  async function loadMyPc(p, live) {
    const host = viewport.querySelector('.ck-win[data-pid="' + p.id + '"] .ck-card') ||
                 document.querySelector('.ck-mpanel[data-pid="' + p.id + '"] .ck-card');
    const el = host || live;
    if (!el) return;
    try {
      if (!myPcCache) {
        const r = await fetch(BOARD_URL);
        if (!r.ok) throw new Error();
        myPcCache = await r.json();
      }
      const mine = (myPcCache.my_pcs || []).filter(function (m) { return true; });
      if (!mine.length) {
        el.innerHTML = '<p style="color:var(--text-dim);font-size:.82rem;margin:0">No Player Character assigned to you in this world yet — ask your GM.</p>';
        return;
      }
      // p.ref picks a specific PC when several; default = first
      const pc = mine.find(function (m) { return String(m.id) === String(p.ref); }) || mine[0];
      let pct = 0;
      if (pc.max_hp) pct = Math.max(0, Math.min(100, Math.round((pc.hp || 0) * 100 / pc.max_hp)));
      const cls = pct <= 25 ? 'ck-hp-low' : (pct <= 55 ? 'ck-hp-mid' : 'ck-hp-ok');
      el.innerHTML =
        '<h3>🧙 ' + esc(pc.name) + '</h3>' +
        (pc.max_hp
          ? '<span class="ck-hpbar" style="display:block;background:var(--bg2);border-radius:3px;height:8px;overflow:hidden;margin:.4rem 0">' +
            '<span class="ck-hpfill ' + cls + '" style="display:block;height:100%;width:' + pct + '%"></span></span>' +
            '<p style="font-size:.8rem;color:var(--text-dim);margin:0 0 .4rem">' + esc(pc.hp_label || 'HP') + ' ' + esc(pc.hp) + '/' + esc(pc.max_hp) +
            (pc.temp_hp ? ' (+' + esc(pc.temp_hp) + ')' : '') + (pc.native === false ? '' : ' · AC ' + esc(pc.ac || '—')) + '</p>'
          : '') +
        (pc.native === false
          ? (pc.resources || []).filter(function (r) { return r.id !== pc.hp_id; }).map(function (r) {
              return '<span style="display:inline-block;font-size:.72rem;border:1px solid var(--border);border-radius:10px;padding:.05rem .5rem;margin:0 .25rem .3rem 0">' + esc(r.label) + ' ' + esc(r.current) + '/' + esc(r.max) + '</span>';
            }).join('')
          : '<p style="font-size:.8rem;color:var(--text-dim);margin:0 0 .4rem">Lvl ' + esc(pc.level) + ' · XP ' + esc(pc.xp) +
            (pc.levelup ? ' · <strong style="color:var(--neon)">⬆ level-up ready</strong>' : '') + '</p>') +
        '<a href="/characters/' + encodeURIComponent(pc.id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank" style="color:var(--neon);font-size:.8rem;text-decoration:none">Open my full sheet →</a>' +
        (mine.length > 1
          ? '<div style="margin-top:.5rem;font-size:.75rem;color:var(--text-dim)">Other characters: ' +
            mine.filter(function (m) { return m.id !== pc.id; })
                .map(function (m) { return '<button type="button" class="ck-wbtn" data-swap="' + m.id + '" style="margin-left:.3rem">' + esc(m.name) + '</button>'; }).join('') + '</div>'
          : '');
      el.querySelectorAll('[data-swap]').forEach(function (b) {
        b.addEventListener('click', function () {
          p.ref = b.dataset.swap;
          const pc2 = mine.find(function (m) { return String(m.id) === String(p.ref); });
          if (pc2) p.title = '🧙 ' + pc2.name;
          save();
          // re-render just this panel's body
          const host2 = viewport.querySelector('.ck-win[data-pid="' + p.id + '"] .ck-body') ||
                        document.querySelector('.ck-mpanel[data-pid="' + p.id + '"] .ck-mpanel-body');
          if (host2) { host2.innerHTML = ''; fillBody(p, host2); }
        });
      });
    } catch (e) {
      el.innerHTML = '<p style="color:var(--text-dim);font-size:.8rem;margin:0">Unavailable.</p>';
    }
  }

  // ── mobile / tablet renderer ──────────────────────────────────────────
  function buildMobilePanel(p) {
    const sec = document.createElement('div');
    sec.className = 'ck-mpanel ck-win';  // .ck-win + data-pid: the live
    sec.dataset.pid = p.id;              // loaders query exactly this
    const head = document.createElement('div');
    head.className = 'ck-mpanel-head';
    const title = document.createElement('span');
    title.className = 'ck-mpanel-title';
    title.textContent = p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label;
    const reload = mkBtn('⟳', 'Reload this panel', function () {
      const f = sec.querySelector('.ck-mpanel-body iframe');
      if (f) { frames.reload(f); return; }
      const fn = liveLoaders.get(p.id);
      if (fn) fn();
    });
    const close = mkBtn('✕', 'Remove this panel', function () { closePanel(p, sec); });
    head.append(title, reload, close);
    const body = document.createElement('div');
    body.className = 'ck-mpanel-body';
    fillBody(p, body);
    sec.append(head, body);
    return sec;
  }

  function renderMobile() {
    document.body.classList.add('ck-mobile');
    document.getElementById('ck-m-world').textContent = CK_WORLD;
    if (mqlMobile.addEventListener) mqlMobile.addEventListener('change', onModeChange);
    frames.forgetAll();
    mContent.innerHTML = '';
    mTabs.innerHTML = '';
    liveLoaders.clear();
    pruneOrphanChatState();
    if (!panels.length) {
      const empty = document.createElement('div');
      empty.className = 'ck-m-empty';
      const msg = document.createElement('div');
      msg.textContent = PLAYER ? 'Pin your first panels — your parties, the quest board, dice…'
                               : 'Pin your first panels — map, vitals, quests, dice…';
      const add = document.createElement('button');
      add.type = 'button';
      add.textContent = '＋ Add panel';
      add.addEventListener('click', openAdd);
      empty.append(msg, add);
      mContent.appendChild(empty);
      updateStatus();
      return;
    }
    panels.forEach(function (p) {
      const sec = buildMobilePanel(p);
      mContent.appendChild(sec);
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'ck-mtab';
      tab.dataset.pid = p.id;
      // the panel title already carries its emoji — one icon per tab
      tab.innerHTML = '<span class="ck-mtab-ic">' + esc(shortTitle(p)) + '</span>';
      tab.addEventListener('click', function () { activateMobile(p.id); });
      mTabs.appendChild(tab);
    });
    if (!panels.some(function (p) { return p.id === mActiveId; })) mActiveId = panels[0].id;
    activateMobile(mActiveId);
    liveLoaders.forEach(function (fn) { fn(); });
    updateStatus();
  }

  function shortTitle(p) {
    const t = p.title || CK_TYPES[p.type].label;
    return t.length > 12 ? t.slice(0, 11) + '…' : t;
  }

  function activateMobile(pid) {
    mActiveId = pid;
    mContent.querySelectorAll('.ck-mpanel').forEach(function (s) {
      s.classList.toggle('active', s.dataset.pid === pid);
    });
    mTabs.querySelectorAll('.ck-mtab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.pid === pid);
    });
  }

  function pruneOrphanChatState() {
    const liveIds = new Set(panels.map(function (p) { return p.id; }));
    [chatHistory, chatAborts].forEach(function (m) {
      Array.from(m.keys()).forEach(function (k) {
        if (!liveIds.has(k)) {
          const c = chatAborts.get(k);
          if (c) c.abort();
          m.delete(k);
        }
      });
    });
    Array.from(panelCleanups.keys()).forEach(function (k) {
      if (!liveIds.has(k)) {
        panelCleanups.get(k)();
        panelCleanups.delete(k);
      }
    });
  }

  function onModeChange() {
    if (MOBILE()) {
      renderMobile();
    } else {
      document.body.classList.remove('ck-mobile');
      render();
      clampX();
    }
  }

  // ── auto-arrange (📌) ───────────────────────────────────────────────
  // Packs every window into a tidy grid, starting at the top-left and
  // flowing left-to-right, row by row. Columns fit the viewport width;
  // cell height has a 320px floor, so a grid with many windows simply
  // outgrows the screen — and the workspace scrolls down instead of
  // squeezing anything. One-time arrangement: dragging stays free after.
  function autoArrange() {
    if (COMPACT.matches || !panels.length) return;
    const vw = viewport.clientWidth, vh = viewport.clientHeight;
    const n = panels.length;
    const cols = Math.max(1, Math.min(n, Math.round(vw / 640)));
    const rows = Math.ceil(n / cols);
    const gap = 8;
    const cellW = Math.floor(vw / cols) - gap;
    const cellH = Math.max(320, Math.floor(vh / rows) - gap);
    const ordered = panels.slice().sort(function (a, b) {
      return (a.y - b.y) || (a.x - b.x);
    });
    ordered.forEach(function (p, i) {
      const col = i % cols, row = Math.floor(i / cols);
      p.x = col * (cellW + gap);
      p.y = row * (cellH + gap);
      p.w = cellW;
      p.h = cellH;
      p.collapsed = false;
      const win = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
      if (win) {
        win.style.left = p.x + 'px';
        win.style.top = p.y + 'px';
        win.style.width = p.w + 'px';
        win.style.height = p.h + 'px';
        win.classList.remove('collapsed', 'maxed');
      }
    });
    save();
  }
  pinBtn.addEventListener('click', autoArrange);

  // Size the workspace to the REAL topbar height (the fixed 52px guess in
  // CSS is wrong whenever the nav wraps — the wrap then overflows the body
  // and the whole page scrolls).
  function fitViewport() {
    const tb = document.querySelector('.topbar');
    const wrap = document.getElementById('ck-wrap');
    if (!tb || !wrap) return;
    wrap.style.height = (window.innerHeight - tb.offsetHeight) + 'px';
    clampX();
  }
  // Pull back any window the last layout left RIGHT of the viewport —
  // horizontal overflow is hidden, so a window past the right edge is
  // unreachable, not merely scrolled (vertical overflow scrolls fine).
  // Runs on resize AND after every adoption: a layout arranged on a wide
  // monitor must survive being reopened on a narrower window.
  function clampX() {
    let moved = false;
    const maxX = Math.max(8, viewport.clientWidth - 60);
    panels.forEach(function (p) {
      if (p.x > maxX) { p.x = maxX; moved = true; }
    });
    if (moved) {
      panels.forEach(function (p) {
        const win = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
        if (win) win.style.left = p.x + 'px';
      });
      save();
    }
  }

  // matchMedia's change event is the primary mode switch (rotate), but
  // some embedded webviews don't fire it on viewport changes — the resize
  // listener reconciles the body class with the actual width either way.
  function syncMode() {
    if (MOBILE() && !document.body.classList.contains('ck-mobile')) renderMobile();
    else if (!MOBILE() && document.body.classList.contains('ck-mobile')) {
      document.body.classList.remove('ck-mobile');
      render();
      clampX();
    }
  }
  window.addEventListener('resize', function () { fitViewport(); syncMode(); });
  fitViewport();
  buildWelcomeTiles();
  updateStatus();
  const mAddBtn = document.getElementById('ck-m-add');
  if (mAddBtn) mAddBtn.addEventListener('click', openAdd);
  if (MOBILE()) renderMobile();

  load();
})();
