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
  // Layout STYLE: Free windows (drag and resize anywhere) or an arrangement the cockpit keeps for you - Stage + rail, Tabs, Tiled grid.
  // The choice is the person's own: URL (?layout=) > the account (/api/view-prefs/cockpit) > this browser > Free windows.
  const STYLE_LS = 'nd_view:cockpit';
  const STYLE_INFO = [
    { id: 'free', icon: '\u25EB', label: 'Free windows', sub: 'drag and resize windows anywhere' },
    { id: 'rail', icon: '\u25A5', label: 'Stage + rail', sub: 'one big window with the others stacked beside it' },
    { id: 'tabs', icon: '\u{1F5C2}', label: 'Tabs', sub: 'one window at a time, the dock chips are the tabs' },
    { id: 'grid', icon: '\u25A6', label: 'Tiled grid', sub: 'every window the same size, nothing overlapping' },
  ];
  let style = (function () {
    const ok = function (m) { return STYLE_INFO.some(function (x) { return x.id === m; }) ? m : ''; };
    const url = ok(new URLSearchParams(location.search).get('layout') || '');
    if (url) return url;
    const acct = ok((typeof CK_STYLE_SAVED === 'object' && CK_STYLE_SAVED && CK_STYLE_SAVED.mode) || '');
    if (acct) return acct;
    try { return ok((JSON.parse(localStorage.getItem(STYLE_LS) || 'null') || {}).mode || '') || 'free'; } catch (e) { return 'free'; }
  })();
  const SCENEMEM = 'nd_cockpit_scene:';       // + world:scene:class - what the windows of a scene looked like when you last arranged them (this browser)
  let tabActive = '';                     // Tabs: the window showing
  let stageId = '';                       // Stage + rail: the window made the stage by hand ('' = the last used map / combat window)
  let currentScene = '';                  // the scene applied last (lit in the scene bar, named in the status bar); any saved layout / reset clears it
  const addOverlay = document.getElementById('ck-add-overlay');
  const addTypes = document.getElementById('ck-add-types');
  const addPicker = document.getElementById('ck-add-picker');
  const addSearch = document.getElementById('ck-add-search');
  const addList = document.getElementById('ck-add-list');
  const layoutsBtn = document.getElementById('ck-layouts-btn');
  const layoutsPop = document.getElementById('ck-layouts-pop');
  const windowsBtn = document.getElementById('ck-windows-btn');
  const windowsPop = document.getElementById('ck-windows-pop');
  const kindSelect = document.getElementById('ck-add-kind');
  const pinBtn = document.getElementById('ck-pin-btn');
  const helpOverlay = document.getElementById('ck-help');
  const helpBody = document.getElementById('ck-help-body');
  const scrollBox = document.getElementById('ck-stat-scroll');      // the status bar's  ▲ 1/2 ▼  (only while the workspace scrolls)
  const scrollUp = document.getElementById('ck-scroll-up');
  const scrollDown = document.getElementById('ck-scroll-down');
  const scrollPos = document.getElementById('ck-scroll-pos');

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
    'ecard', 'notes', 'ai_chat', 'timer', 'calendar', 'gallery', 'audio', 'video', 'page'];
  function panelTypes() {
    if (!PLAYER) {                     // "My Character" is the player's own sheet: a GM has none to show
      const gm = {};
      Object.keys(CK_TYPES).forEach(function (k) { if (k !== 'pc') gm[k] = CK_TYPES[k]; });
      return gm;
    }
    const out = {};
    PLAYER_TYPES.forEach(function (k) { if (CK_TYPES[k]) out[k] = CK_TYPES[k]; });
    return out;
  }

  // ── screen size ──────────────────────────────────────────────────────
  // The windows are pixels, and the same layout follows the GM across monitors (and a player's across the browser, the
  // character page's Cockpit tab and a phone). So a layout is saved WITH the size of the workspace it was arranged for
  // (refVp) and, opened on another screen, is scaled from that one to this one rather than used as raw pixels. The maths
  // is in cockpit-layout.js (pure, tested across screen sizes); this file only applies it.
  const LAYOUT = window.ndCockpitLayout;
  const KEYS = window.ndCockpitKeys;
  let refVp = null;                       // the workspace size panels[] is currently expressed in
  // null while the desktop workspace is hidden (the phone shell): it has no size then, so nothing is scaled or saved
  function vpNow() {
    const w = viewport.clientWidth, h = viewport.clientHeight;
    return w >= 200 && h >= 150 ? { w: w, h: h } : null;
  }
  function prefHeight(p) { return (CK_TYPES[p.type] && CK_TYPES[p.type].h) || 300; }
  // "Roomy windows" (🧩 Layouts, per browser): every tiler (auto-arrange, scenes, the laptop's tiles) keeps each tile big - about 60% of the
  // screen tall - and lets the workspace scroll for the rest, instead of squeezing every window into one screen.
  const ROOMY_KEY = 'nd_cockpit_roomy';
  function roomy() { try { return localStorage.getItem(ROOMY_KEY) === '1'; } catch (e) { return false; } }
  prefHeight.minH = function (vp) { return roomy() ? LAYOUT.roomyHeight(vp) : 0; };

  // ── the laptop layout ──────────────────────────────────────────────────────────────────────────────────────────────
  // Below ~1700px of window width (a Vivobook is 1280-1536) the cockpit shows a few windows at a time, tiled big, and keeps
  // the rest in a dock; the maths and the saved shape are in cockpit-layout.js. These three are this BROWSER's choices (they
  // are not part of the saved layout): the layout class, how many windows show at once, and whether the site's top bar stays.
  const MODE_KEY = 'nd_cockpit_mode';     // '' = by window width | 'laptop' | 'wide'
  const SLOTS_KEY = 'nd_cockpit_slots';   // 'auto' | '1' | '2' | '3' | 'all'
  const SITE_KEY = 'nd_cockpit_site';     // '1' = keep the site's top bar showing in the laptop layout
  const KEEP_KEY = 'nd_cockpit_keep';     // '0' = windows in the dock are NOT kept loaded (default: kept)
  const HINT_KEY = 'nd_cockpit_laptop_hint';
  const LOCK_KEY = 'nd_cockpit_lock';     // '1' = the windows are locked: no moving, resizing or closing by accident
  const WHEEL_KEY = 'nd_cockpit_wheel';   // '1' = the mouse wheel scrolls the WORKSPACE over a window (Alt + wheel: the page in it); default: the other way round
  const SCROLL_HINT_KEY = 'nd_cockpit_scroll_hint';   // '1' = told once how the workspace scrolls (when a window first outgrew the screen)
  function pref(key, dflt) { try { return localStorage.getItem(key) || dflt; } catch (e) { return dflt; } }
  function setPref(key, val) { try { if (val) localStorage.setItem(key, val); else localStorage.removeItem(key); } catch (e) {} }
  let klass = LAYOUT.screenClass(window.innerWidth, null, pref(MODE_KEY, ''));   // 'laptop' | 'wide'
  let otherVp = null;                     // the reference screen of the class that is NOT showing (it is saved with the layout)
  const LAPTOP_FALLBACK_VP = { w: 1280, h: 620 };   // until the real workspace has a size (the phone shell hides it)
  const WIDE_NOMINAL_VP = { w: 1800, h: 900 };      // what a window made on a laptop assumes the big screen to be
  function LAPTOP() { return klass === 'laptop' && !MOBILE(); }
  function keepLoaded() { return pref(KEEP_KEY, '') !== '0'; }
  function slotsNow(n) {
    return LAYOUT.resolveSlots(pref(SLOTS_KEY, 'auto'), vpNow() || refVp || LAPTOP_FALLBACK_VP, n == null ? panels.length : n);
  }
  function laptopOpts(n) {
    const vp = vpNow() || refVp || LAPTOP_FALLBACK_VP;
    return { klass: klass, vp: vp, slots: slotsNow(n), pref: prefHeight };
  }
  // Everything that gets stored: the windows with BOTH screens' geometry, and the screen each was arranged for.
  function savedLayout() {
    const mine = refVp || undefined;
    return { panels: LAYOUT.toSaved(panels, klass),
             vp: (klass === 'wide' ? mine : otherVp) || undefined, lvp: (klass === 'laptop' ? mine : otherVp) || undefined };
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
  let lastCombatRef = null;       // the combat the status bar follows (see refreshCombatStatus)
  const alarms = new Set();       // ids of the timers that have gone off and not been dismissed (the dock chip shows it too)
  const undoStack = [];           // last closed panels (Undo toast / Ctrl+Shift+T)
  let lastSavedAt = 0;
  let saveError = '';   // why the last server save failed ('' = it did not)

  function embed(path) {
    const glue = path.indexOf('?') > -1 ? '&' : '?';
    return path + glue + 'embed=1&w=' + encodeURIComponent(CK_WORLD);
  }
  // A "Page" window shows one page of this app: an absolute path on this site (one leading slash, no scheme, no spaces).
  function validPagePath(ref) { return typeof ref === 'string' && /^\/(?!\/)[^\s\\]{0,119}$/.test(ref); }
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
    rememberScene();                                 // (a scene that is showing keeps the arrangement you give it)
  }
  // Live-sync refreshes collapse into ONE pass: a burst of changes (bulk XP,
  // a loot spree) would otherwise fire one fetch per panel in parallel.
  function scheduleLiveRefresh() {
    clearTimeout(liveTimer);
    liveTimer = setTimeout(function () {
      if (document.hidden) return;
      liveLoaders.forEach(function (fn) { fn(); });
      refreshCombatStatus();
    }, 500);
  }
  function saveNow() {
    const now = vpNow();
    if (now) refVp = now;   // what is on screen now is what is being saved
    const ws = { current: savedLayout(), presets: presets };
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

  // Drop windows whose backing object vanished since the layout was saved
  // (a deleted map, a disbanded party) rather than rendering dead panels.
  function liveOnly(list) {
    return list.filter(function (p) {
      if (p.type === 'map') return CK_MAPS.some(function (m) { return m.slug === p.ref; });
      if (p.type === 'wmap') return (CK_WORLD_MAPS || []).some(function (m) { return m.slug === p.ref; });
      if (p.type === 'party') return CK_PARTIES.some(function (x) { return String(x.id) === String(p.ref); });
      return true;
    });
  }

  // Saved panels (the big screen's geometry on top, the laptop's in `lap`) -> working panels for the class showing now. Returns
  // the screen their geometry is in terms of, so adaptToScreen can scale it to this one, and remembers the other class's.
  function workingFrom(saved, wideVp, lapVp) {
    const r = LAYOUT.toWorking(saved, laptopOpts(saved.length));
    panels = r.panels;
    otherVp = klass === 'wide' ? lapVp : wideVp;
    if (klass === 'wide') return wideVp;
    return r.derived ? (vpNow() || refVp || LAPTOP_FALLBACK_VP) : lapVp;
  }

  function adopt(ws) {
    let from = null;       // the screen this layout was arranged for (older layouts did not record it)
    if (ws && ws.current && Array.isArray(ws.current.panels) && ws.current.panels.length) {
      presets = ws.presets || {};
      let saved = liveOnly(ws.current.panels);
      if (saved.length > CK_MAX_PANELS) {
        saved = saved.slice(0, CK_MAX_PANELS);
        toast('This layout had more than ' + CK_MAX_PANELS + ' windows; the first ' + CK_MAX_PANELS + ' are shown.');
      }
      from = workingFrom(saved, LAYOUT.vpOk(ws.current.vp) ? ws.current.vp : null, LAYOUT.vpOk(ws.current.lvp) ? ws.current.lvp : null);
    } else {
      panels = liveOnly(defaultLayout());
      presets = {};
      from = refVp;
      save();
    }
    adaptToScreen(from);   // before focusMyCharacter: the panel it adds is already in this screen's terms
    if (PLAYER) {
      const before = panels.length;
      focusMyCharacter();
      const lead = panels[0];                                      // focusMyCharacter puts the character's panel first
      if (klass === 'laptop' && lead && lead.type === 'pc') {
        if (!lead.parked && panels.length > before) lead.parked = true;   // a new one has page-sized geometry: give it a real tile
        if (lead.parked) LAYOUT.showLaptop(panels, lead.id, vpNow() || refVp || LAPTOP_FALLBACK_VP, slotsNow(), prefHeight);
      }
    }
    recomputeSeq();
    // Re-mint ids on every load: guarantees uniqueness even if an older
    // build saved a workspace with duplicate ids (its counter-reset bug).
    panels.forEach(function (p) { p.id = newId(); });
    currentPreset = '';
    currentScene = '';
    reflow(true);
    renderLayoutsMenu();
    render();
  }

  // Bring panels[] (arranged for screen `from`) to this screen: scaled when the screen really differs, otherwise just
  // pulled back inside it. Nothing is saved: the stored layout stays the one the GM arranged until they change something.
  function adaptToScreen(from) {
    const now = vpNow();
    if (!now) { if (from) refVp = from; return; }   // phone shell: remember what the layout is for; scale when the desktop shows
    if (!panels.length) return;
    const scale = !!from && LAYOUT.needsScale(from, now);
    const next = scale ? LAYOUT.scalePanels(panels, from, now) : LAYOUT.fitPanels(panels, now);
    next.forEach(function (q, i) { panels[i].x = q.x; panels[i].y = q.y; panels[i].w = q.w; panels[i].h = q.h; });
    refVp = scale || !from ? now : from;   // a change within tolerance is not a new reference: drift would add up
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
  // The windows a fresh cockpit opens with, tiled for THIS screen: the map (if any) is the stage, the rest share the
  // remaining columns — 2 columns on a laptop, 3 on 1080p, more on a big or ultrawide monitor.
  function defaultLayout() {
    seq = 1;
    const vp = vpNow() || refVp || { w: 1200, h: 700 };
    const out = [];
    let n = 0;
    function add(type, ref, title) {
      out.push({ id: newId(), type: type, ref: ref || '', title: title || '',
        x: 0, y: 0, w: 300, h: 300, z: ++n, collapsed: false, data: {} });
    }
    if (CK_MAPS.length) add('map', CK_MAPS[0].slug, '🗺 ' + CK_MAPS[0].name);
    if (PLAYER && CK_MY_PCS.length) {
      // the player's character leads (and is the first tab in the phone shell)
      const me = CK_MY_PCS.find(function (m) { return m.id === CK_FOCUS_PC; }) || CK_MY_PCS[0];
      add('pc', String(me.id), '🧙 ' + me.name);
    }
    if (CK_PARTIES.length) add('party', String(CK_PARTIES[0].id), '❤ ' + CK_PARTIES[0].name);
    add('quests', '', '📜 Quests');
    add('dice', '', '🎲 Dice');
    refVp = vp;            // (a fallback size when the desktop workspace is hidden: scaled to the real one when it shows)
    if (klass !== 'laptop') { otherVp = null; return LAYOUT.tilePanels(out, vp, { pref: prefHeight }); }
    // A laptop shows a few windows at a time: tile the big screen's version too (so the same layout opens well there later)
    // and let toWorking pick what shows here and park the rest.
    otherVp = WIDE_NOMINAL_VP;
    const wide = LAYOUT.tilePanels(out, WIDE_NOMINAL_VP, { pref: prefHeight });
    return LAYOUT.toWorking(wide, laptopOpts(wide.length)).panels;
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
    // The windows on screen first: their pages take the first free slots, the ones waiting in the dock load behind them.
    panels.filter(function (p) { return !p.parked; }).concat(panels.filter(function (p) { return p.parked; }))
      .forEach(function (p) { viewport.appendChild(buildWin(p)); });
    // Everything registered a loader in fillBody — fire them all now that
    // the nodes are attached (the loaders query the DOM by panel id).
    liveLoaders.forEach(function (fn) { fn(); });
    if (welcome) welcome.style.display = panels.length ? 'none' : '';
  }

  function buildWin(p) {
    const win = document.createElement('div');
    win.className = 'ck-win' + (p.collapsed ? ' collapsed' : '') + (p.parked ? ' ck-parked' : '');
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
      titleFrames(p, win);
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
    colBtn.dataset.role = 'collapse';
    // Out of the way, into the dock (on a laptop the others fill the space); its chip in the bar above brings it back
    const dockBtn = mkBtn('⊟', 'Put this window in the dock (click its name in the bar above to bring it back)', function () { parkPanel(p); });
    dockBtn.classList.add('ck-dockbtn');
    const closeBtn = mkBtn('✕', 'Remove this panel', function () { closePanel(p, win); });
    closeBtn.dataset.role = 'close';                 // (hidden while the windows are locked)
    // a window that is not at 100% says so; a click on it puts it back
    const zoomBadge = mkBtn('', 'Zoom — click for 100%', function () { setZoom(p, 100); });
    zoomBadge.className = 'ck-wbtn ck-zoom-badge';
    zoomBadge.hidden = true;
    head.append(title, zoomBadge, accentBtn, reloadBtn, maxBtn, colBtn, dockBtn, closeBtn);
    head.addEventListener('dblclick', function (e) {
      if (e.target.closest('.ck-wbtn')) return;
      win.classList.toggle('maxed');
    });

    const body = document.createElement('div');
    body.className = 'ck-body';
    fillBody(p, body);
    labelControls(body);
    titleFrames(p, body);

    const rz = document.createElement('div');
    rz.className = 'ck-resize';
    rz.title = 'Resize';

    win.append(head, body, rz);
    applyZoomTo(p, win);                             // (after the window is put together: it looks inside it)
    head.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      openCtx(p, win, e.clientX, e.clientY);
    });
    win.addEventListener('pointerdown', function () {
      bringToFront(p);
      setActive(p);
    });
    wireDrag(head, win, p);
    RESIZE_DIRS.forEach(function (dir) {
      if (dir === 'se') { wireResize(rz, win, p, 'se'); return; }      // the visible corner grip is the south-east handle
      const h = document.createElement('div');
      h.className = 'ck-rz ck-rz-' + dir;
      h.setAttribute('aria-hidden', 'true');
      win.appendChild(h);
      wireResize(h, win, p, dir);
    });
    return win;
  }

  // A name for every field and icon button a panel builds (its tooltip or placeholder), so a screen reader is not left with "edit text".
  function labelControls(root) {
    root.querySelectorAll('textarea, input:not([type=hidden]), select, button').forEach(function (el) {
      if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')) return;
      if (el.labels && el.labels.length) return;
      if (el.tagName === 'BUTTON' && /[A-Za-z0-9]{2,}/.test(el.textContent || '')) return;     // a button that says something already
      const t = el.getAttribute('title') || el.getAttribute('placeholder');
      if (t) el.setAttribute('aria-label', t.replace(/\s*[—(].*$/, '').replace(/…$/, '').trim().slice(0, 60) || t.slice(0, 60));
    });
  }

  function mkBtn(label, tip, fn) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ck-wbtn';
    b.textContent = label;
    b.title = tip;
    b.setAttribute('aria-label', tip);
    b.addEventListener('click', function (e) { e.stopPropagation(); fn(e); });
    return b;
  }

  // The address of a window's page, for the window types that are one (null for the ones the cockpit draws itself, or a combat
  // window with no combat picked yet). One mapping for building the window and for popping it out.
  function framePath(p) {
    const ref = encodeURIComponent(p.ref || '');
    switch (p.type) {
      case 'map': return '/maps/schematic/' + ref + '/view';
      case 'wmap': return '/maps/' + ref;
      case 'dice': return '/dice';
      case 'entity': return '/entity/' + ref;
      case 'ai': return '/ai';
      case 'gallery': return '/images';
      case 'audio': return '/audio';
      case 'video': return '/video';
      case 'imagestudio': return '/imagestudio';
      case 'calendar': return '/calendar';
      case 'combat': return p.ref ? '/combat/' + ref : null;
      case 'page': return validPagePath(p.ref) ? p.ref : null;     // a saved layout is data, not trusted code: only a path on this site
      default: return null;
    }
  }

  function fillBody(p, body) {
    const fp = framePath(p);
    if (fp) {
      body.appendChild(mkIframe(fp));
    } else if (p.type === 'page') {
      body.insertAdjacentHTML('beforeend', '<p class="ck-empty" style="padding:.8rem;color:var(--text-dim)">This window has no valid page address.</p>');
    } else if (p.type === 'combat') {
      // An unpicked combat shows the in-panel picker so the GM can start from the window itself (a picked one is a page, above).
      const live = document.createElement('div');
      live.className = 'ck-live';
      live.innerHTML = '<p class="ck-empty">Loading recent combats…</p>';
      body.appendChild(live);
      liveLoaders.set(p.id, function () { loadCombatPicker(p); });
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
        p.data = Object.assign({}, p.data, { text: ta.value });     // (merged: the window's zoom lives in data too)
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
          const r = await ndAiFetch('/api/ai/assist', {
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
      if (p.type === 'party' && !PLAYER) live.addEventListener('click', function (e) { memberClick(e, p); });
    }
  }

  function mkIframe(path) {
    const f = document.createElement('iframe');
    f.title = 'panel';
    f.addEventListener('load', function () { relayFrameEvents(f); });
    frames.mount(f, embed(path));   // src is set by the loader when the window is showing and a slot is free
    return f;
  }
  // A page inside a window is a document of its own: a press there never reaches this one. It is the same origin, so each time one
  // loads the window listens in on it - a press raises and highlights its window.
  function relayFrameEvents(f) {
    let w;
    try { w = f.contentWindow; if (!w || !w.document || w.location.href === 'about:blank') return; } catch (e) { return; }
    w.addEventListener('pointerdown', function () { activateFrameWindow(f); }, true);
    // the cockpit's keys work from inside a window too (Alt+N, Alt+1..9, F1 ...)
    w.addEventListener('keydown', function (e) { if (onKey(e)) { e.preventDefault(); e.stopPropagation(); } }, true);
    // ... and so does Alt + wheel: it scrolls the workspace, not the page under the pointer
    watchWheel(w);
  }
  function activateFrameWindow(f) {
    const w = f.closest ? f.closest('.ck-win') : null;
    const q = w ? panels.find(function (x) { return x.id === w.dataset.pid; }) : null;
    if (q && !q.parked) { bringToFront(q); setActive(q); }
  }
  // Every page in a window names itself for a screen reader (the window's title), not "panel".
  function titleFrames(p, root) {
    root.querySelectorAll('iframe').forEach(function (f) { f.title = fullTitle(p); });
  }

  // A panel's container, on the desktop workspace or in the phone shell (a different element: the live loaders used to look only
  // in the workspace, so a party / quests / card / combat-picker tab on a phone stayed on "Loading…").
  function panelHost(p, inner) {
    return viewport.querySelector('.ck-win[data-pid="' + p.id + '"] ' + inner) || document.querySelector('.ck-mpanel[data-pid="' + p.id + '"] ' + inner);
  }
  async function loadLive(p) {
    const live = panelHost(p, '.ck-live');
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
      return '<div class="ck-mem' + (m.down ? ' down' : '') + '" data-id="' + esc(m.id) + '">' +
        '<a class="ck-mem-row" href="/characters/' + encodeURIComponent(m.id) + '?w=' + encodeURIComponent(CK_WORLD) + '" target="_blank"><span>' + esc(m.name) + '</span><span class="ck-mem-lvl">' + lvl + '</span></a>' +
        (chips ? '<span class="ck-reschips">' + chips + '</span>' : '') + hp +
        ((m.conditions || []).length ? '<span class="ck-conds">' + esc(m.conditions.join(', ')) + '</span>' : '') +
        (PLAYER ? '' : memberControls(m)) +
        '</div>';
    }).join('');
  }

  // A GM's quick controls under a member's HP: damage / heal by 5 or 1 and, on an N&D character, Shock. They go through the same
  // routes the sheet's own steppers use (the N&D HP track, or the template's vital track of a custom system) and are queued per
  // character, so five quick -1 clicks are five changes in order, never a lost one.
  function memberControls(m) {
    if (!m.id || (m.native === false && !m.hp_id) || (m.native !== false && !m.max_hp)) return '';
    let h = '<span class="ck-memctl" role="group" aria-label="Change ' + esc(m.name) + '\'s ' + esc(m.hp_label || 'HP') + '" data-hp-id="' + esc(m.hp_id || '') + '">';
    [-5, -1, 1, 5].forEach(function (n) {
      const a = Math.abs(n);
      h += '<button type="button" data-act="hp" data-d="' + n + '" title="' + (n < 0 ? 'Damage ' : 'Heal ') + a + '" aria-label="' + (n < 0 ? 'Damage ' : 'Heal ') + a + '">' + (n < 0 ? '−' : '+') + a + '</button>';
    });
    if (m.native !== false && m.shock_max) {
      h += '<span class="ck-shock" title="Shock">⚡ ' + esc(m.shock || 0) + '/' + esc(m.shock_max) +
        '<button type="button" data-act="shock" data-d="-1" title="Shock −1" aria-label="Shock minus one">−</button>' +
        '<button type="button" data-act="shock" data-d="1" title="Shock +1" aria-label="Shock plus one">+</button></span>';
    }
    return h + '</span>';
  }
  const memberQueue = new Map();                   // character id -> the last change still on its way
  function memberClick(e, p) {
    const b = e.target.closest ? e.target.closest('button[data-act]') : null;
    if (!b) return;
    const card = b.closest('.ck-mem');
    const ctl = b.closest('.ck-memctl');
    if (!card || !ctl) return;
    e.preventDefault();
    const id = card.dataset.id, d = parseInt(b.dataset.d, 10) || 0;
    let url, body;
    if (b.dataset.act === 'shock') { url = '/api/characters/' + encodeURIComponent(id) + '/shock'; body = { action: 'delta', value: d }; }
    else if (ctl.dataset.hpId) { url = '/api/characters/' + encodeURIComponent(id) + '/resource'; body = { field_id: ctl.dataset.hpId, action: 'delta', value: d }; }
    else { url = '/api/characters/' + encodeURIComponent(id) + '/hp-async'; body = { action: 'delta', value: d }; }
    nudgeHpText(card, b.dataset.act, d);
    const next = (memberQueue.get(id) || Promise.resolve()).then(function () {
      return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); });
    }).catch(function (err) {
      toast('Could not change that (' + (err.message || 'offline') + ').');
    }).then(function () {
      if (memberQueue.get(id) === next) { memberQueue.delete(id); loadLive(p); }       // the truth, once the last change has landed
    });
    memberQueue.set(id, next);
  }
  // Show the change at once (the server's answer replaces it a moment later): the HP number and its bar move, clamped.
  function nudgeHpText(card, act, d) {
    if (act !== 'hp') return;
    const txt = card.querySelector('.ck-mem-hp'), fill = card.querySelector('.ck-hpfill');
    const m = txt && txt.textContent.match(/(\d+)\/(\d+)/);
    if (!m) return;
    const max = parseInt(m[2], 10), now = Math.max(0, Math.min(max, parseInt(m[1], 10) + d));
    txt.textContent = txt.textContent.replace(/(\d+)\/(\d+)/, now + '/' + max);
    if (fill && max) fill.style.width = Math.max(0, Math.min(100, Math.round(now * 100 / max))) + '%';
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
    const live = panelHost(p, '.ck-card');
    if (!live) return;
    try {
      const r = await fetch('/api/entity/' + encodeURIComponent(p.ref) + '/preview');
      if (!r.ok) throw new Error();
      const d = await r.json();
      live.innerHTML =
        (d.image_url ? '<img src="' + esc(d.image_url) + '" alt="' + esc(d.name) + '"/>' : '') +
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
    const live = panelHost(p, '.ck-live');
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
      toastHost.setAttribute('role', 'status');
      toastHost.setAttribute('aria-live', 'polite');
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
    if (tabActive === p.id) tabActive = '';
    if (stageId === p.id) stageId = '';
    if (welcome) welcome.style.display = panels.length ? 'none' : '';
    undoStack.push({ panel: p, index: idx === -1 ? panels.length : idx });
    if (undoStack.length > 8) undoStack.shift();
    // Undo restores the window, not its live contents (an aborted chat or
    // timer starts fresh) — the arrangement is what matters.
    toast('Closed "' + (p.title || CK_TYPES[p.type].label) + '"', 'Undo', undoClose);
    if (LAPTOP() && panels.length) {   // the ones still showing grow into the gap (a window waiting in the dock does not jump in)
      LAYOUT.retileLaptop(panels, lapVp(), Math.max(1, LAYOUT.visibleOf(panels).length), prefHeight);
      applyDockState();
    }
    updateStatus();
    save();
  }
  function undoClose() {
    const item = undoStack.pop();
    if (!item) return;
    const i = Math.min(item.index, panels.length);
    panels.splice(i, 0, item.panel);
    if (welcome) welcome.style.display = 'none';
    if (LAPTOP()) item.panel.parked = true;      // it takes a slot like any window brought back
    else delete item.panel.parked;               // (the big screen: it is simply there again)
    viewport.appendChild(buildWin(item.panel));
    if (LAPTOP()) { LAYOUT.showLaptop(panels, item.panel.id, lapVp(), slotsNow(), prefHeight); applyDockState(); }
    const fn = liveLoaders.get(item.panel.id);
    if (fn) fn();
    updateStatus();
    revealWin(item.panel);
    save();
  }
  document.addEventListener('keydown', function (e) { if (onKey(e)) e.preventDefault(); });

  // ── shared half-dock (drag release + context menu) ─────────────────────
  function dockPanel(win, p, right) {
    p.x = right ? Math.ceil(viewport.clientWidth / 2) : 0;
    p.y = Math.round(viewport.scrollTop);             // the half you are looking at (the workspace may be scrolled)
    p.w = Math.floor(viewport.clientWidth / 2);
    p.h = viewport.clientHeight;
    win.style.left = p.x + 'px'; win.style.top = p.y + 'px';
    win.style.width = p.w + 'px'; win.style.height = p.h + 'px';
  }

  // ── window context menu (right-click a title bar) ──────────────────────
  let ctxEl = null;
  function showCtx(items, x, y) {
    if (!ctxEl) {
      ctxEl = document.createElement('div');
      ctxEl.id = 'ck-ctx';
      document.body.appendChild(ctxEl);
    }
    ctxEl.innerHTML = '';
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
  // A dock chip's menu (laptop layout)
  function openChipCtx(p, x, y) {
    const items = [];
    if (p.parked) items.push(['👁 Show', function () { showPanel(p); }]);
    else {
      if (LAPTOP() || style === 'rail') items.push(['★ ' + (style === 'rail' ? 'Make the stage' : 'Make main window'), function () { makeMainPanel(p); }]);
      items.push(['⊟ Put in the dock', function () { parkPanel(p); }]);
    }
    if (!locked()) {
      items.push('hr');
      items.push(['✕ Close', function () { closePanel(p, winEl(p)); }]);
    }
    showCtx(items, x, y);
  }
  function openCtx(p, win, x, y) {
    if (COMPACT.matches) return;
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
        titleFrames(p, win);
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
    ];
    if (!locked()) {
      items.push(['◧ Dock left', function () { win.classList.remove('maxed'); dockPanel(win, p, false); save(); }]);
      items.push(['◨ Dock right', function () { win.classList.remove('maxed'); dockPanel(win, p, true); save(); }]);
    }
    if (LAPTOP() || style === 'rail') items.push(['★ ' + (style === 'rail' ? 'Make the stage' : 'Make main window'), function () { makeMainPanel(p); }]);
    items.push(['⊟ Put in the dock', function () { parkPanel(p); }]);
    items.push('hr');
    items.push(['🔍＋ Zoom in  (Alt +)', function () { stepZoom(p, 1); }]);
    items.push(['🔍− Zoom out  (Alt −)', function () { stepZoom(p, -1); }]);
    if (zoomOf(p) !== 100) items.push(['🔍 Back to 100%  (Alt 0)', function () { setZoom(p, 100); }]);
    if (popOutPath(p)) items.push(['⇱ Pop out to its own window', function () { popOut(p); }]);
    if (!locked()) {
      items.push('hr');
      items.push(['✕ Close', function () { closePanel(p, win); }]);
    }
    showCtx(items, x, y);
  }
  document.addEventListener('mousedown', function (e) {
    if (ctxEl && ctxEl.style.display === 'block' && !e.target.closest('#ck-ctx')) {
      ctxEl.style.display = 'none';
    }
  });

  // ── per-window zoom ────────────────────────────────────────────────────────────────────────────────────────────────
  // A window's own zoom (50-200 %, in data.zoom, saved with the layout): a page too small to read on a 4K screen grows, a wide page
  // fits a small window. The window's page (or panel) is scaled from its top-left corner inside the window and given the inverse
  // size, so it still fills the window exactly. Alt + / Alt - / Alt 0 zoom the active window; the badge in the title bar resets it.
  function zoomOf(p) {
    const z = p.data ? Number(p.data.zoom) : NaN;
    return z >= 50 && z <= 200 ? Math.round(z) : 100;
  }
  function applyZoomTo(p, win) {
    if (!win) return;
    const z = zoomOf(p);
    const el = win.querySelector('.ck-body') ? win.querySelector('.ck-body').firstElementChild : null;
    if (el) {
      if (z === 100) {
        el.style.transform = ''; el.style.transformOrigin = ''; el.style.width = ''; el.style.height = '';
      } else {
        const box = LAYOUT.zoomBox(z);
        el.style.transformOrigin = '0 0';
        el.style.transform = 'scale(' + box.scale + ')';
        el.style.width = box.size + '%';
        el.style.height = box.size + '%';
      }
    }
    const badge = win.querySelector('.ck-zoom-badge');
    if (badge) { badge.hidden = z === 100; badge.textContent = z + '%'; badge.title = 'Zoom ' + z + '% — click for 100%'; }
  }
  function setZoom(p, z) {
    z = Math.max(50, Math.min(200, Math.round(z)));
    p.data = Object.assign({}, p.data);
    if (z === 100) delete p.data.zoom; else p.data.zoom = z;
    applyZoomTo(p, winEl(p));
    save();
  }
  function stepZoom(p, dir) { setZoom(p, LAYOUT.zoomStep(zoomOf(p), dir)); }

  // ── pop out ────────────────────────────────────────────────────────────────────────────────────────────────────────
  // A window's page in a browser window of its own (nd-float.js) - for a second monitor. The window stays in the cockpit too.
  function popOutPath(p) {
    const win = winEl(p);
    const f = win ? win.querySelector('.ck-body iframe') : null;
    if (f) {
      try {                                    // where the window is NOW (it may have followed a link), without the embed flag
        const loc = f.contentWindow.location;
        if (loc.href && loc.href !== 'about:blank') {
          const u = new URL(loc.href);
          u.searchParams.delete('embed');
          return u.pathname + u.search;
        }
      } catch (e) { /* not reachable: use the address it was built with */ }
    }
    const fp = framePath(p);
    if (fp) return fp;
    if (p.type === 'party' && p.ref) return '/parties/' + encodeURIComponent(p.ref);
    if (p.type === 'quests') return '/quests';
    if (p.type === 'ecard' && p.ref) return '/entity/' + encodeURIComponent(p.ref);
    return null;
  }
  function popOut(p) {
    const path = popOutPath(p);
    if (!path || !window.ndFloat) { toast('This window cannot be popped out.'); return; }
    const w = ndFloat(path, { title: fullTitle(p), name: 'nd-cockpit-' + CK_WORLD + '-' + p.id, w: Math.max(360, Math.min(p.w || 640, 1400)), h: Math.max(300, Math.min(p.h || 640, 1000)) });
    if (!w) toast('The browser blocked the new window - allow pop-ups for this site.');
  }

  // ── lock the windows ───────────────────────────────────────────────────────────────────────────────────────────────
  // At the table a stray drag or a mis-hit ✕ costs a moment's attention. Locked (per browser): no moving, resizing, docking to an
  // edge or closing; everything else works (collapse, maximise, the dock, zoom, scenes). The ✕ buttons go, so does "Close".
  function locked() { return pref(LOCK_KEY, '') === '1'; }
  function applyLock() {
    const on = locked();
    document.body.classList.toggle('ck-locked', on);
    const row = document.getElementById('ck-lock-row');
    if (row) { row.setAttribute('aria-pressed', on ? 'true' : 'false'); row.firstElementChild.textContent = on ? '🔓 Unlock the windows' : '🔒 Lock the windows'; }
    const chip = document.getElementById('ck-lock-chip');
    if (chip) chip.hidden = !on;
  }
  function toggleLock() {
    setPref(LOCK_KEY, locked() ? '' : '1');
    applyLock();
    toast(locked() ? '🔒 Windows locked: no moving, resizing or closing (Alt+K unlocks).' : '🔓 Windows unlocked.');
  }

  // ── combat status in the status bar ────────────────────────────────────────────────────────────────────────────────
  // The round and whose turn it is, for the combat window that has a combat picked - glanceable even when that window waits in the
  // dock; a click brings it forward. Refreshed with the other live panels (the live-sync bus) and on a slow timer.
  let combatStatusAt = 0;
  async function refreshCombatStatus() {
    const el = document.getElementById('ck-stat-combat');
    if (!el) return;
    const cp = PLAYER ? null : panels.find(function (q) { return q.type === 'combat' && q.ref; });
    if (!cp) { el.hidden = true; return; }
    combatStatusAt = Date.now();
    try {
      const r = await fetch('/api/combat/' + encodeURIComponent(cp.ref) + '/state');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      const list = d.combatants || [];
      const cur = list.find(function (x) { return x.id === d.active_id; });     // by id: the list is in the order added, not turn order
      el.textContent = '⚔ Round ' + (d.round_num || 1) + (cur && cur.name ? ' · ▶ ' + cur.name : '');
      el.title = fullTitle(cp) + ' — click to bring the combat window forward';
      el.dataset.pid = cp.id;
      el.hidden = false;
    } catch (e) { el.hidden = true; }
  }
  const combatChip = document.getElementById('ck-stat-combat');
  if (combatChip) combatChip.addEventListener('click', function () {
    const q = panels.find(function (x) { return x.id === combatChip.dataset.pid; });
    if (q) { showPanel(q); focusWindow(q); }
  });
  const lockChip = document.getElementById('ck-lock-chip');
  if (lockChip) lockChip.addEventListener('click', toggleLock);
  setInterval(function () { if (!document.hidden && Date.now() - combatStatusAt > 18000) refreshCombatStatus(); }, 6000);

  // ── status bar ─────────────────────────────────────────────────────────
  function updateStatus() {
    const c = document.getElementById('ck-stat-left');
    if (c) c.textContent = '\u{1FA9F} ' + panels.length;
    const combatRef = (panels.find(function (q) { return q.type === 'combat' && q.ref; }) || {}).ref || '';
    if (combatRef !== lastCombatRef) { lastCombatRef = combatRef; refreshCombatStatus(); }     // a combat window was added, picked or closed
    const m = document.getElementById('ck-stat-mid');
    if (m) { const sc = SCENES.find(function (x) { return x.id === currentScene; }); m.textContent = currentPreset ? '\u{1F4CB} ' + currentPreset : (sc ? '\u{1F9E9} ' + sc.label : ''); }
    renderDock();
    scheduleReflow();                                // a managed layout style arranges again after every change
    scheduleScrollUi();                              // a window was added, closed or put in the dock: the workspace's height changed
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
  const fsBtns = [document.getElementById('ck-fs-btn'), document.getElementById('ck-fs-btn2')].filter(Boolean);
  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(function () {});
  }
  fsBtns.forEach(function (b) { b.addEventListener('click', toggleFullscreen); });
  document.addEventListener('fullscreenchange', function () {
    const on = !!document.fullscreenElement;
    fsBtns.forEach(function (b) {
      b.title = on ? 'Leave fullscreen' : 'Fullscreen (great at the table)';
      b.setAttribute('aria-label', on ? 'Leave fullscreen' : 'Fullscreen');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  });

  // ── welcome quick-start tiles ───────────────────────────────────────────
  function buildWelcomeTiles() {
    const host = document.getElementById('ck-welcome-tiles');
    if (!host || host.children.length) return;
    const quick = [];
    if (CK_MAPS.length) quick.push(['map', CK_MAPS[0].slug, '\u{1F5FA}\u{FE0F} ' + CK_MAPS[0].name]);
    if ((CK_WORLD_MAPS || []).length) quick.push(['wmap', CK_WORLD_MAPS[0].slug, '\u{1F5FA}\u{FE0F} ' + CK_WORLD_MAPS[0].name]);
    if (CK_PARTIES.length) quick.push(['party', String(CK_PARTIES[0].id), '\u2764 ' + CK_PARTIES[0].name]);
    ['pc', 'quests', 'dice', 'ai_chat', 'find', 'notes', 'timer'].forEach(function (t) {
      if (t === 'pc' && !PLAYER) return;
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

  // ── timer panel ────────────────────────────────────────────────────────────────────────────────────────────────────
  // The state machine is static/js/cockpit-timer.js (pure, tested): wall-clock, so a throttled background tab, a laptop that slept,
  // a reload or an Undo-close all still show the true time. The state lives in the window's data.timer (saved with the layout).
  // The alarm is loud AND visible: the time flashes, the window and its dock chip pulse, the tab title says so and a toast stays -
  // the sound alone is not enough when the tab is muted or the window is parked.
  const TIMER = window.ndCockpitTimer;
  const TIMER_PRESETS = [1, 5, 10, 15, 30];
  const baseTitle = document.title;
  function setAlarmUi(p, on) {
    if (on) alarms.add(p.id); else alarms.delete(p.id);
    const el = winEl(p) || document.querySelector('.ck-mpanel[data-pid="' + p.id + '"]');
    if (el) el.classList.toggle('ck-alarm', on);
    const chip = dockChips && dockChips.querySelector('.ck-chip[data-pid="' + p.id + '"]');
    if (chip) chip.classList.toggle('alarm', on);
    document.title = (alarms.size ? '⏰ ' : '') + baseTitle;
  }
  let timerAudio = null;
  function timerAudioReady() {                    // made on a Start click (a user gesture), so the browser lets it sound later
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!timerAudio && AC) timerAudio = new AC();
      if (timerAudio && timerAudio.state === 'suspended') timerAudio.resume();
    } catch (e) { /* silent: the flashing is the alarm */ }
  }
  function timerBeeps() {
    try {
      timerAudioReady();
      if (!timerAudio) return;
      [0, 0.45, 0.9].forEach(function (at) {
        const o = timerAudio.createOscillator(), g = timerAudio.createGain();
        const t0 = timerAudio.currentTime + at;
        o.connect(g); g.connect(timerAudio.destination);
        o.frequency.value = 880;
        g.gain.setValueAtTime(0.001, t0);
        g.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.35);
        o.start(t0); o.stop(t0 + 0.4);
      });
    } catch (e) { /* silent */ }
  }

  function buildTimer(p, body) {
    const prev = panelCleanups.get(p.id);             // a rebuilt window must not leave the old one's tick running
    if (prev) prev();
    p.data = p.data && typeof p.data === 'object' ? p.data : {};
    const t = p.data.timer = TIMER.normalize(p.data.timer);
    const wrap = document.createElement('div');
    wrap.className = 'ck-timer';
    const time = document.createElement('div');
    time.className = 'ck-timer-time';
    time.setAttribute('role', 'timer');
    const presets = document.createElement('div');
    presets.className = 'ck-timer-row ck-timer-presets';
    const row = document.createElement('div');
    row.className = 'ck-timer-row';
    function btn(label, tip) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ck-btn'; b.textContent = label; b.title = tip; b.setAttribute('aria-label', tip);
      return b;
    }
    const mins = document.createElement('input');
    mins.type = 'number'; mins.min = '1'; mins.max = String(TIMER.MAX_MINUTES); mins.value = String(t.minutes);
    mins.title = 'Minutes'; mins.setAttribute('aria-label', 'Countdown minutes');
    const modeBtn = btn('', 'Switch between a countdown and a stopwatch');
    const startBtn = btn('', 'Start / pause');
    const resetBtn = btn('↺', 'Reset');
    const plus = btn('+1m', 'Add a minute to the countdown');
    const minus = btn('−1m', 'Take a minute off the countdown');
    TIMER_PRESETS.forEach(function (n) {
      const b = btn(n + 'm', 'Start a ' + n + ' minute countdown');
      b.addEventListener('click', function () { TIMER.setMode(t, 'down'); setMinutes(n); reset(); start(); });
      presets.appendChild(b);
    });
    row.append(mins, modeBtn, startBtn, resetBtn, minus, plus);
    wrap.append(time, presets, row);
    body.appendChild(wrap);

    let iv = null;
    function setMinutes(n) { TIMER.setMinutes(t, n); mins.value = String(t.minutes); }
    function paint() {
      const up = t.mode === 'up', now = Date.now();
      time.textContent = up ? TIMER.fmt(TIMER.elapsed(t, now), true) : TIMER.fmt(TIMER.left(t, now), false);
      wrap.classList.toggle('done', t.done);
      modeBtn.textContent = up ? '▶ Stopwatch' : '⏱ Countdown';
      mins.disabled = up;
      presets.hidden = minus.hidden = plus.hidden = up;
      const label = TIMER.startLabel(t);
      startBtn.textContent = (label === 'Pause' ? '⏸ ' : '▶ ') + label;
    }
    function stopTick() { if (iv) { clearInterval(iv); iv = null; } }
    function startTick() { stopTick(); iv = setInterval(tick, 250); }
    function tick() {
      if (TIMER.expired(t, Date.now())) fire();
      paint();
    }
    function fire() {
      TIMER.expire(t);
      stopTick();
      setAlarmUi(p, true);
      timerBeeps();
      toast((p.title || '⏱ Timer') + ' — time is up', 'OK', dismiss);
      save();
    }
    function dismiss() { if (t.done) { TIMER.reset(t); setAlarmUi(p, false); paint(); save(); } }
    function reset() {
      stopTick();
      TIMER.reset(t);
      setAlarmUi(p, false);
      paint();
      save();
    }
    function start() {
      timerAudioReady();
      if (t.done) setAlarmUi(p, false);
      if (TIMER.toggle(t, Date.now()) === 'started') startTick(); else stopTick();
      paint();
      save();
    }
    function adjust(ms) {
      if (t.done) setAlarmUi(p, false);
      TIMER.adjust(t, ms, Date.now());
      paint();
      save();
    }
    modeBtn.addEventListener('click', function () { TIMER.setMode(t, t.mode === 'up' ? 'down' : 'up'); reset(); });
    startBtn.addEventListener('click', start);
    time.title = 'Click to start / pause';           // a big target (and all that is left in a short window)
    let silenced = false;                             // the touch that just silenced a flashing timer must not also start it again
    time.addEventListener('click', function () { if (silenced) { silenced = false; return; } start(); });
    resetBtn.addEventListener('click', reset);
    plus.addEventListener('click', function () { adjust(60000); });
    minus.addEventListener('click', function () { adjust(-60000); });
    mins.addEventListener('change', function () {
      setMinutes(parseInt(mins.value, 10));
      if (!t.running) { TIMER.reset(t); setAlarmUi(p, false); paint(); save(); }
    });
    wrap.addEventListener('pointerdown', function () { silenced = t.done; if (t.done) dismiss(); });   // a touch on a flashing timer silences it
    document.addEventListener('visibilitychange', paint);

    // Back at a saved timer: still running -> carry on; ran out while nobody was here -> say so, quietly (no beep: the browser
    // would not play one and it would be a surprise).
    const woke = TIMER.wake(t, Date.now());
    if (woke === 'running') startTick();
    else if (woke === 'expired' || woke === 'done') setTimeout(function () { setAlarmUi(p, true); }, 0);
    paint();
    // Closing / re-rendering the window must stop the interval - register with the generic per-panel cleanup.
    panelCleanups.set(p.id, function () {
      stopTick();
      document.removeEventListener('visibilitychange', paint);
      setAlarmUi(p, false);
    });
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

  // A picture on one of the cockpit's own cards (entity card, My Character) opens full screen, with the GM's
  // "send to second screen / to players" buttons — the pages in the other windows do the same through
  // nd-lightbox-bridge.js. In the phone shell the cards live in #ck-mcontent instead of the workspace.
  [viewport, mContent].forEach(function (host) {
    if (!host) return;
    host.addEventListener('click', function (e) {
      const img = e.target && e.target.closest ? e.target.closest('.ck-card img') : null;
      if (!img || typeof window.openLightbox !== 'function') return;
      e.preventDefault();
      window.openLightbox(img.getAttribute('data-full') || img.currentSrc || img.src, img.alt || '');
    });
  });

  // ── scrolling the workspace ──────────────────────────────────────────────────────────────────────────────────────────
  // The workspace is as tall as its lowest window: make a window taller than the screen, or keep more open than fit, and it
  // scrolls. Its scrollbar (and the wheel over a title bar or a gap) does that natively; what is added here is what the windows' own
  // pages take away - a wheel over a page scrolls THAT page - and what a drag needs: Alt + wheel over any window, Alt + Up / Down,
  // the  ▲ 1/2 ▼  in the status bar, scrolling by itself while a window is carried or stretched to the top / bottom edge (the pointer
  // cannot leave the screen, the workspace can scroll on for it), and a window brought into sight when it is shown or added.
  // The numbers are in cockpit-layout.js (pure, tested).
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function scrollRoom() { return Math.max(0, viewport.scrollHeight - viewport.clientHeight); }
  let scrollGoal = null, scrollGoalAt = 0;          // where a smooth scroll in flight is heading, so quick key presses add up
  function scrollWorkspaceTo(top, smooth) {
    const to = Math.max(0, Math.min(scrollRoom(), Math.round(top)));
    scrollGoal = to; scrollGoalAt = Date.now();
    if (smooth && !reduceMotion.matches && viewport.scrollTo) viewport.scrollTo({ top: to, behavior: 'smooth' });
    else viewport.scrollTop = to;
  }
  function scrollWorkspace(kind) {                  // 'up' | 'down' | 'top' | 'end' - Alt + arrows, the ▲ ▼ buttons
    if (viewport.classList.contains('ck-maxed')) return;
    if (scrollRoom() < 2) {
      toast('Nothing to scroll: every window fits the screen. Drag a window\'s bottom edge down to make it taller, or open more windows.');
      return;
    }
    const base = scrollGoal !== null && Date.now() - scrollGoalAt < 600 ? scrollGoal : viewport.scrollTop;
    const step = LAYOUT.pageStep(viewport.clientHeight);
    scrollWorkspaceTo(kind === 'top' ? 0 : kind === 'end' ? scrollRoom() : base + (kind === 'down' ? step : -step), true);
  }
  // A window into sight (the dock chip, the Windows list, Alt + 1..9, a window just added or reopened): left alone when it is already
  // there, else scrolled the least that shows it - for one taller than the screen, to its title bar.
  function revealWin(p, instant) {
    const el = p && !p.parked && !MOBILE() ? winEl(p) : null;
    if (!el || viewport.classList.contains('ck-maxed')) return;
    const h = p.collapsed || el.classList.contains('collapsed') ? el.offsetHeight : p.h;
    const to = LAYOUT.revealScroll(viewport.scrollTop, viewport.clientHeight, p.y, h, LAYOUT.GAP, { max: scrollRoom() });
    if (to !== null) scrollWorkspaceTo(to, !instant);
  }
  // Alt + wheel scrolls the workspace wherever the pointer is - over a window's page too, which would take the wheel for itself (a page
  // in a window reports to this from its own document: relayFrameEvents). With "Wheel scrolls the workspace" ticked (🧩 Layouts) it is the
  // other way round over a PAGE: the plain wheel scrolls the workspace and Alt + wheel gives the wheel back to the page. (The cockpit's
  // own controls and panels - the dock's chips, the menus, a notes window's text - always keep their plain wheel.)
  function wheelOnWorkspace() { return pref(WHEEL_KEY, '') === '1'; }
  function workspaceWheel(e) {
    if (e.ctrlKey || e.metaKey || MOBILE() || viewport.classList.contains('ck-maxed')) return false;
    const inPage = !!e.view && e.view !== window;                  // over a page in a window, not over the cockpit's own document
    if (e.altKey === (inPage && wheelOnWorkspace())) return false;
    const dy = LAYOUT.wheelPixels(e.deltaY, e.deltaMode, viewport.clientHeight);
    if (!dy || scrollRoom() < 1) return false;
    e.preventDefault();
    scrollGoal = null;
    viewport.scrollTop += dy;
    return true;
  }
  // Cancelling a page's own scroll takes a NON-passive wheel listener, and one makes every scroll over its page wait for the main thread -
  // which the cockpit and all its windows share (a busy map or the 3D dice would make scrolling anywhere feel laggy). So the blocking
  // listeners exist only while Alt is held: Alt going down arms them on this document and on every page in a window, Alt going up takes
  // them off, and a cheap passive listener catches an Alt pressed where this document could not see it (the first tick of such a wheel
  // scrolls the page as usual) and a plain wheel while still armed (Alt released out of sight).
  const wheelHosts = new Set();                      // this window and the pages in the windows
  let altDown = false;
  function armWheel(on) { altDown = on; syncWheel(); }
  function syncWheel() {
    wheelHosts.forEach(function (w) {
      try {
        if (w !== window && (w.closed || !w.document)) { wheelHosts.delete(w); return; }        // the window it was in is gone
        // armed while Alt is down - and, with the wheel on the workspace, the pages in the windows stay armed (no Alt needed)
        if (altDown || (w !== window && wheelOnWorkspace())) w.addEventListener('wheel', workspaceWheel, { capture: true, passive: false });
        else w.removeEventListener('wheel', workspaceWheel, { capture: true });
      } catch (e) { wheelHosts.delete(w); }
    });
  }
  function watchWheel(w) {                           // called for this window and, from relayFrameEvents, for each page that loads
    wheelHosts.add(w);
    w.addEventListener('keydown', function (e) { if (e.key === 'Alt' && !altDown) armWheel(true); }, true);     // (a held key repeats)
    w.addEventListener('keyup', function (e) { if (e.key === 'Alt') armWheel(false); }, true);
    w.addEventListener('wheel', function (e) { if (!e.ctrlKey && e.altKey !== altDown) armWheel(e.altKey); }, { capture: true, passive: true });
    syncWheel();                                     // loaded while Alt is held, or with the wheel on the workspace
  }
  watchWheel(window);
  // 🧩 Layouts → "Wheel scrolls the workspace": a per-browser choice
  function applyWheelRow() {
    const on = wheelOnWorkspace();
    const row = document.getElementById('ck-wheel-row');
    if (row) { row.setAttribute('aria-pressed', on ? 'true' : 'false'); row.firstElementChild.textContent = (on ? '\u2611 ' : '\u2610 ') + 'Wheel scrolls the workspace'; }
  }
  function applyRoomyRow() {
    const on = roomy();
    const row = document.getElementById('ck-roomy-row');
    if (row) { row.setAttribute('aria-pressed', on ? 'true' : 'false'); row.firstElementChild.textContent = (on ? '\u2611 ' : '\u2610 ') + 'Roomy windows'; }
  }
  function toggleRoomy() {
    setPref(ROOMY_KEY, roomy() ? '' : '1');
    applyRoomyRow();
    autoArrange();                                   // tidy now, so the choice shows
    toast(roomy() ? 'Roomy windows: auto-arrange, scenes and the laptop tiles keep every window big - scroll for the rest.'
                  : 'Windows are fitted to one screen again.');
  }
  function toggleWheelMode() {
    setPref(WHEEL_KEY, wheelOnWorkspace() ? '' : '1');
    applyWheelRow();
    syncWheel();
    toast(wheelOnWorkspace() ? '\u{1F5B1} The mouse wheel now scrolls the workspace, even over a window. Alt + wheel scrolls the page inside the window.'
                              : '\u{1F5B1} The mouse wheel scrolls the page under the pointer again. Alt + wheel scrolls the workspace.');
  }
  // A maximised window fills the workspace as it is now (top: 0 of the CONTENT), so it must be in sight and stay there: the workspace is
  // scrolled to the top and locked while any window is maximised, and goes back to where it was when the window is given back.
  let maxedAt = null;
  function syncMaxLock() {
    const on = !!viewport.querySelector('.ck-win.maxed');
    if (on === (maxedAt !== null)) return;
    viewport.classList.toggle('ck-maxed', on);
    if (on) { maxedAt = viewport.scrollTop; viewport.scrollTop = 0; }
    else { const back = maxedAt; maxedAt = null; viewport.scrollTop = back; }
    scheduleScrollUi();
  }
  new MutationObserver(syncMaxLock).observe(viewport, { attributes: true, attributeFilter: ['class'], subtree: true, childList: true });
  // While a window is carried or stretched, the workspace scrolls by itself when the pointer nears its top or bottom edge (or leaves
  // it) and the window follows: that is how a window is made taller than the screen, or carried down to the others. `apply(ev)`
  // redraws the drag for the last pointer event; the caller feeds update() every move and calls stop() when the button is released.
  // A workspace that fits the screen has nothing below to scroll into - and a window's bottom edge cannot pass the pointer, which
  // cannot pass the screen - so once a drag starts to scroll DOWN an invisible spacer gives the workspace half a screen of room below
  // the window (it grows as the window does); the workspace goes back to the height of its lowest window when the button is released.
  // (Not before: an ordinary drag never changes the workspace, so its scrollbar never flickers in and out under the windows.)
  let spacer = null;
  function setSpacer(bottom) {
    if (bottom === null) { if (spacer) { spacer.remove(); spacer = null; } return; }
    if (!spacer) {
      spacer = document.createElement('div');
      spacer.id = 'ck-spacer';
      spacer.setAttribute('aria-hidden', 'true');
      viewport.appendChild(spacer);
    }
    spacer.style.top = Math.round(bottom) + 'px';
  }
  function autoScroller(apply, p) {
    let ev = null, raf = 0, last = 0, acc = 0;
    function makeRoom() { setSpacer(p.y + p.h + Math.round(viewport.clientHeight * 0.5)); }
    function frame(now) {
      raf = 0;
      if (!ev) return;
      const r = viewport.getBoundingClientRect();
      const d = LAYOUT.autoScrollStep(ev.clientY, r.top, r.height);     // px per frame at 60 Hz
      if (!d) { last = 0; acc = 0; return; }
      if (d > 0 || spacer) makeRoom();
      const dt = last ? Math.min(64, now - last) : 16.7;                // ms since the last frame: 144 Hz is not 2.4x as fast, a stalled tab does not jump a screen
      last = now;
      acc += d * dt / 16.7;
      const whole = acc < 0 ? Math.ceil(acc) : Math.floor(acc);          // whole px only; the rest carries to the next frame
      acc -= whole;
      if (whole) {
        const before = viewport.scrollTop;
        viewport.scrollTop = before + whole;
        if (viewport.scrollTop !== before) apply(ev);
      }
      raf = requestAnimationFrame(frame);
    }
    return {
      update: function (e) { ev = e; if (spacer) makeRoom(); if (!raf) raf = requestAnimationFrame(frame); },
      stop: function () { ev = null; if (raf) cancelAnimationFrame(raf); raf = 0; setSpacer(null); scheduleScrollUi(); },
    };
  }
  // The status bar's  ▲ 2/3 ▼ : there while the workspace is taller than the screen, saying which screenful is showing.
  let scrollUiRaf = 0, scrollHinted = false;
  function scheduleScrollUi() { if (!scrollUiRaf) scrollUiRaf = requestAnimationFrame(updateScrollUi); }
  function updateScrollUi() {
    scrollUiRaf = 0;
    if (!scrollBox || spacer) return;                // (not while a drag has stretched the workspace: stop() updates it)
    const s = MOBILE() ? { scrollable: false } : LAYOUT.scrollPages(viewport.scrollTop, viewport.clientHeight, viewport.scrollHeight);
    scrollBox.hidden = !s.scrollable || viewport.classList.contains('ck-maxed');
    if (scrollBox.hidden) return;
    scrollPos.textContent = s.page + '/' + s.pages;
    scrollBox.title = 'Screen ' + s.page + ' of ' + s.pages + ' - the windows below are a scroll away (Alt + wheel over a window, Alt + ↑ / ↓)';
    scrollUp.disabled = s.atTop;
    scrollDown.disabled = s.atBottom;
    if (!scrollHinted && userTouched && pref(SCROLL_HINT_KEY, '') !== '1') {      // the first time a window outgrows the screen
      scrollHinted = true;
      setPref(SCROLL_HINT_KEY, '1');
      toast('The workspace scrolls now: use its scrollbar, Alt + mouse wheel over a window, Alt + ↑ / ↓, or the ▲ ▼ in the status bar.');
    }
  }
  viewport.addEventListener('scroll', scheduleScrollUi, { passive: true });
  if (scrollUp) scrollUp.addEventListener('click', function () { scrollWorkspace('up'); });
  if (scrollDown) scrollDown.addEventListener('click', function () { scrollWorkspace('down'); });
  setInterval(function () { if (!document.hidden) scheduleScrollUi(); }, 2000);      // whatever changed the workspace's height without a drag

  // ── drag / resize ──────────────────────────────────────────────────────
  // Moving and resizing go through the layout maths (cockpit-layout.js): eight handles per window, and magnetic edges - the
  // workspace's and the other windows' - with guide lines while a snap holds. Hold Ctrl while dragging for free placement
  // (no snapping, no grid).
  const RESIZE_DIRS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];
  const SNAP_PX = 8;
  function otherBoxes(p) {
    return panels.filter(function (q) { return q !== p && !q.parked && !q.collapsed; })
      .map(function (q) { return { x: q.x, y: q.y, w: q.w, h: q.h }; });
  }
  function wsSize() { return { w: viewport.clientWidth, h: viewport.clientHeight }; }

  let guideHost = null;
  function showGuides(g) {
    if (!g || (!g.x.length && !g.y.length)) { hideGuides(); return; }
    if (!guideHost) {
      guideHost = document.createElement('div');
      guideHost.id = 'ck-guides';
      viewport.appendChild(guideHost);
    }
    guideHost.textContent = '';
    g.x.forEach(function (x) {
      const l = document.createElement('div');
      l.className = 'ck-guide v';
      l.style.left = x + 'px';
      l.style.height = Math.max(viewport.scrollHeight, viewport.clientHeight) + 'px';
      guideHost.appendChild(l);
    });
    g.y.forEach(function (y) {
      const l = document.createElement('div');
      l.className = 'ck-guide h';
      l.style.top = y + 'px';
      l.style.width = viewport.clientWidth + 'px';
      guideHost.appendChild(l);
    });
  }
  function hideGuides() { if (guideHost) guideHost.textContent = ''; }

  function wireDrag(handle, win, p) {
    handle.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.ck-wbtn') || COMPACT.matches || win.classList.contains('maxed') || locked()) return;
      if (style !== 'free') { noteManaged(); return; }
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      // Y math includes scrollTop on both ends: the workspace scrolls
      // vertically, and p.y is a content coordinate, not a screen one.
      const sx = e.clientX - p.x, sy = e.clientY - p.y + viewport.scrollTop;
      let snapped = false, free = false;
      const auto = autoScroller(apply, p);
      function move(ev) {
        apply(ev);
        if (Math.abs(ev.clientX - e.clientX) + Math.abs(ev.clientY - e.clientY) > 8) auto.update(ev);   // not for a click that wobbled
      }
      function apply(ev) {
        free = ev.ctrlKey || ev.metaKey;
        const raw = { x: Math.max(0, Math.min(viewport.clientWidth - 60, ev.clientX - sx)),
                      y: Math.min(LAYOUT.MAX_Y, Math.max(0, ev.clientY - sy + viewport.scrollTop)), w: p.w, h: p.h };
        const sn = free ? null : LAYOUT.snapBox(raw, 'move', otherBoxes(p), wsSize(), SNAP_PX);
        snapped = !!sn && (sn.guides.x.length > 0 || sn.guides.y.length > 0);
        p.x = sn ? sn.x : raw.x;
        p.y = sn ? sn.y : raw.y;
        win.style.left = p.x + 'px';
        win.style.top = p.y + 'px';
        showGuides(sn ? sn.guides : null);
        // Edge-dock preview: hover near the left/right edge to see the half
        // the window will occupy on release.
        win.classList.toggle('ck-dock-l', ev.clientX <= DOCK_ZONE);
        win.classList.toggle('ck-dock-r', ev.clientX >= viewport.clientWidth - DOCK_ZONE - 1);
      }
      let ended = false;
      function up() {
        if (ended) return;                           // (pointerup, then the capture being lost: once)
        ended = true;
        auto.stop();
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        handle.removeEventListener('lostpointercapture', up);
        hideGuides();
        if (win.classList.contains('ck-dock-l') || win.classList.contains('ck-dock-r')) {
          dockPanel(win, p, win.classList.contains('ck-dock-r'));
        } else if (!snapped && !free) {              // a snapped window stays exactly where it snapped; the rest land on the grid
          p.x = Math.round(p.x / GRID) * GRID;
          p.y = Math.round(p.y / GRID) * GRID;
          win.style.left = p.x + 'px';
          win.style.top = p.y + 'px';
        }
        win.classList.remove('ck-dock-l', 'ck-dock-r');
        scheduleScrollUi();
        save();
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
      handle.addEventListener('lostpointercapture', up);     // a capture can end without a pointerup: the workspace must never be left scrolling by itself
    });
  }

  // A handle on one side or corner of a window (dir: n s e w ne nw se sw). The side opposite the one dragged never moves.
  function wireResize(handle, win, p, dir) {
    handle.addEventListener('pointerdown', function (e) {
      if (COMPACT.matches || win.classList.contains('maxed') || win.classList.contains('collapsed') || locked()) return;
      if (style !== 'free') return;
      e.preventDefault();
      e.stopPropagation();
      handle.setPointerCapture(e.pointerId);
      bringToFront(p);
      setActive(p);
      const sx = e.clientX, sy = e.clientY + viewport.scrollTop;
      const start = { x: p.x, y: p.y, w: p.w, h: p.h };
      const ws = wsSize();
      let snapped = false, free = false;
      const auto = autoScroller(apply, p);
      function move(ev) {
        apply(ev);
        if (Math.abs(ev.clientX - e.clientX) + Math.abs(ev.clientY - e.clientY) > 8) auto.update(ev);
      }
      function apply(ev) {
        free = ev.ctrlKey || ev.metaKey;
        let b = LAYOUT.resizeBox(start, ev.clientX - sx, ev.clientY + viewport.scrollTop - sy, dir, ws);
        const sn = free ? null : LAYOUT.snapBox(b, dir, otherBoxes(p), ws, SNAP_PX);
        snapped = !!sn && (sn.guides.x.length > 0 || sn.guides.y.length > 0);
        if (sn) b = sn;
        p.x = b.x; p.y = b.y; p.w = b.w; p.h = b.h;
        win.style.left = p.x + 'px'; win.style.top = p.y + 'px';
        win.style.width = p.w + 'px'; win.style.height = p.h + 'px';
        showGuides(sn ? sn.guides : null);
      }
      let ended = false;
      function up() {
        if (ended) return;
        ended = true;
        auto.stop();
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        handle.removeEventListener('lostpointercapture', up);
        hideGuides();
        if (!snapped && !free) {                       // land the edges that moved on the grid, the others stay exactly put
          const sides = LAYOUT.EDGE_SIDES[dir] || LAYOUT.EDGE_SIDES.se;
          const right = p.x + p.w, bottom = p.y + p.h;
          if (sides.left) { p.x = Math.round(p.x / GRID) * GRID; p.w = right - p.x; }
          if (sides.right) p.w = Math.round(p.w / GRID) * GRID;
          if (sides.top) { p.y = Math.round(p.y / GRID) * GRID; p.h = bottom - p.y; }
          if (sides.bottom) p.h = Math.round(p.h / GRID) * GRID;
          p.w = Math.max(LAYOUT.MIN_W, p.w); p.h = Math.max(LAYOUT.MIN_H, p.h);
          win.style.left = p.x + 'px'; win.style.top = p.y + 'px';
          win.style.width = p.w + 'px'; win.style.height = p.h + 'px';
        }
        scheduleScrollUi();
        save();
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
      handle.addEventListener('lostpointercapture', up);
    });
  }

  // ── the active window ──────────────────────────────────────────────────
  // The one you last pressed on or clicked into (also inside its page: focus moving into an iframe blurs this window), drawn
  // with a brighter frame and title bar, and raised above the others - a click inside a window's page never reaches this document.
  function setActive(p) {
    viewport.querySelectorAll('.ck-win.ck-active').forEach(function (el) { el.classList.remove('ck-active'); });
    const el = p ? viewport.querySelector('.ck-win[data-pid="' + p.id + '"]') : null;
    if (el) el.classList.add('ck-active');
  }
  function bringToFront(p) {
    const el = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
    if (el && p.z === zTop && el.style.zIndex === String(zTop)) return;
    zTop += 1;
    p.z = zTop;
    if (el) el.style.zIndex = zTop;
  }
  window.addEventListener('blur', function () {
    setTimeout(function () {
      const a = document.activeElement;
      if (a && a.tagName === 'IFRAME') activateFrameWindow(a);
    }, 0);
  });

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
      label.textContent = (p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label) + (p.parked ? '  (in the dock)' : '');
      row.appendChild(label);
      const x = document.createElement('span');
      x.className = 'ck-winrow-x';
      x.textContent = '✕';
      x.hidden = locked();
      x.title = 'Close this window';
      x.addEventListener('click', function (e) {
        e.stopPropagation();
        const el = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
        closePanel(p, el);
        renderWindowList();
      });
      row.appendChild(x);
      row.addEventListener('click', function () {
        windowsPop.style.display = 'none';
        showPanel(p);
      });
      windowsPop.appendChild(row);
    });
  }
  windowsBtn.addEventListener('click', function () {
    const open = windowsPop.style.display === 'block';
    if (!open) {
      closeLayouts(false);
      renderWindowList();
      const r = windowsBtn.getBoundingClientRect();
      windowsPop.style.left = Math.max(8, r.right - 260) + 'px';
      windowsPop.style.top = (r.bottom + 6) + 'px';
      windowsPop.style.display = 'block';
      const first = windowsPop.querySelector('.ck-winrow');          // the keyboard is in the list at once (Up / Down, Enter, Esc)
      if (first) first.focus();
    } else {
      windowsPop.style.display = 'none';
    }
  });
  windowsPop.addEventListener('keydown', function (e) {
    const rows = Array.prototype.slice.call(windowsPop.querySelectorAll('.ck-winrow'));
    const at = rows.indexOf(document.activeElement);
    let to = null;
    if (e.key === 'ArrowDown') to = rows[(at + 1) % rows.length];
    else if (e.key === 'ArrowUp') to = rows[(at - 1 + rows.length) % rows.length];
    else if (e.key === 'Home') to = rows[0];
    else if (e.key === 'End') to = rows[rows.length - 1];
    if (to) { e.preventDefault(); to.focus(); }
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
    const first = addTypes.querySelector('.ck-type');
    if (first) first.focus();
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
    if (key === 'map' || key === 'wmap' || key === 'party' || key === 'entity' || key === 'ecard' || key === 'combat' || key === 'page') {
      addPicker.style.display = '';
      addSearch.value = '';
      addSearch.placeholder = (key === 'entity' || key === 'ecard') ? 'Search entities…' : (key === 'page' ? 'Filter the pages — or type an address like /tables' : 'Filter…');
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
      } else if (key === 'page') {
        pickerRows = pageRows();
        fillPicker(pickerRows);
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
    const rows = pickerRows.filter(function (r) {
      return !q || r.label.toLowerCase().indexOf(q) !== -1;
    });
    if (pickerType === 'page' && validPagePath(q) && q.length > 1) rows.push({ ref: q, label: pageTitle(q), sub: 'this address' });
    fillPicker(rows);
  }
  // Every page the site's menu offers (the quick switcher's index), as rows for the picker.
  function pageRows() {
    const pages = window.ndPalette && window.ndPalette.pages ? window.ndPalette.pages() : [];
    return pages.filter(function (pg) { return validPagePath(pageHref(pg.href)); })
      .map(function (pg) { return { ref: pageHref(pg.href), label: pageTitle(pg.label), sub: 'page' }; });
  }
  // A menu label usually starts with its own icon ("🎲 Random Tables"); one that does not gets the page icon.
  function pageTitle(label) {
    label = String(label || '').trim();
    return /^[^\x00-\x7F]/.test(label) ? label : '📄 ' + label;
  }
  // A menu link carries the world as ?w=slug; the window adds its own (embed()), so take it off.
  function pageHref(href) {
    return String(href || '').replace(/([?&])w=[^&#]*&?/, '$1').replace(/[?&]$/, '');
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
    const vp = vpNow() || refVp || { w: 1200, h: 700 };
    const size = LAYOUT.sizeFor(t, vp);          // the type's usual size, scaled to this screen and never bigger than it
    const at = LAYOUT.cascade(panels.length, size, vp, MOBILE() ? 0 : viewport.scrollTop);   // (scrolled down: where you are looking)
    const p = {
      id: newId(), type: type, ref: ref || '', title: title || (t.icon + ' ' + t.label),
      x: at.x, y: at.y, w: size.w, h: size.h, z: ++zTop, collapsed: false, data: {},
    };
    panels.push(p);
    tabActive = p.id;
    if (LAPTOP()) {
      // the big screen's place for it (a laptop-made window still opens sensibly there) ...
      const wvp = otherVp || WIDE_NOMINAL_VP;
      const wsz = LAYOUT.sizeFor(t, wvp), wat = LAYOUT.cascade(panels.length - 1, wsz, wvp);
      p._wide = { x: wat.x, y: wat.y, w: wsz.w, h: wsz.h, z: p.z, collapsed: false };
      p.parked = true;                                 // ... and here it takes a slot (below)
    }
    if (opts && opts.autoRun) findAutoRan.add(p.id);   // must be known before buildWin: buildFind checks it
    if (welcome) welcome.style.display = 'none';
    if (MOBILE()) {
      renderMobile();
    } else {
      viewport.appendChild(buildWin(p));
      if (LAPTOP() && !(opts && opts.scene)) { LAYOUT.showLaptop(panels, p.id, lapVp(), slotsNow(), prefHeight); applyDockState(); }
    }
    const fn = liveLoaders.get(p.id);
    if (fn) fn();
    updateStatus();                                  // the window count and the dock's chips
    if (!(opts && opts.scene)) { revealWin(p); save(); }     // a scene arranges what it added and saves once
    return p;
  }

  // ── offer the Combat scene when a fight starts (opt-in, this browser) ────────────────────────────────────────────────────
  // Checked after live-sync pulses and once a minute: a combat that is running now and was not when the cockpit opened (or when this was
  // switched on) is offered once, as a message with a button - nothing moves by itself.
  const COMBAT_HINT_KEY = 'nd_cockpit_combat_hint';
  let knownRunning = null;
  const combatOffered = new Set();
  let combatCheckTimer = 0;
  function combatHintOn() { return !PLAYER && pref(COMBAT_HINT_KEY, '') === '1'; }
  async function checkCombatStart() {
    if (!combatHintOn() || MOBILE() || document.hidden) return;
    let list = [];
    try { const r = await fetch('/api/combat/recent'); if (r.ok) list = (await r.json()).combats || []; } catch (e) { return; }
    const running = list.filter(function (c) { return c.running; });
    if (knownRunning === null) { knownRunning = new Set(running.map(function (c) { return c.id; })); return; }   // what was already running is not news
    const fresh = running.filter(function (c) { return !knownRunning.has(c.id) && !combatOffered.has(c.id); });
    running.forEach(function (c) { knownRunning.add(c.id); });
    if (!fresh.length || currentScene === 'combat') return;
    combatOffered.add(fresh[0].id);
    toast('\u2694 A fight has started: ' + fresh[0].name, 'Switch to Combat', function () { applyScene(SCENES[0]); });
  }
  function scheduleCombatCheck() {
    if (!combatHintOn() || combatCheckTimer) return;
    combatCheckTimer = setTimeout(function () { combatCheckTimer = 0; checkCombatStart(); }, 1500);
  }
  function applyCombatHintRow() {
    const row = document.getElementById('ck-combathint-row');
    if (row) { row.setAttribute('aria-pressed', combatHintOn() ? 'true' : 'false'); row.firstElementChild.textContent = (combatHintOn() ? '\u2611 ' : '\u2610 ') + '\u2694 Offer Combat when a fight starts'; }
  }
  function toggleCombatHint() {
    setPref(COMBAT_HINT_KEY, combatHintOn() ? '' : '1');
    knownRunning = null;
    applyCombatHintRow();
    if (combatHintOn()) { checkCombatStart(); toast('You will be offered the Combat scene when a new fight starts.'); }
    else toast('The Combat scene is no longer offered.');
  }
  window.addEventListener('nd-live', scheduleCombatCheck);
  setInterval(checkCombatStart, 60000);
  const combatHintRow = document.getElementById('ck-combathint-row');
  if (combatHintRow) combatHintRow.addEventListener('click', function () { closeLayouts(false); toggleCombatHint(); });

  // ── a scene remembers how you arranged it ─────────────────────────────────────────────────────────────────────────────
  function sceneMemKey(id) { return SCENEMEM + CK_WORLD + ':' + id + ':' + klass; }
  let sceneMemTimer = 0;
  function rememberScene() {                         // called after every save: while a scene is showing, keep its arrangement
    if (!currentScene || style !== 'free' || MOBILE() || sceneMemTimer) return;
    sceneMemTimer = setTimeout(function () {
      sceneMemTimer = 0;
      const vp = vpNow();
      if (!currentScene || style !== 'free' || !vp) return;
      try { localStorage.setItem(sceneMemKey(currentScene), JSON.stringify(LAYOUT.rememberRects(panels, vp))); } catch (e) { /* not remembered */ }
    }, 400);
  }
  function recallScene(id) {
    try { return LAYOUT.recallRects(panels, JSON.parse(localStorage.getItem(sceneMemKey(id)) || 'null'), vpNow()); } catch (e) { return false; }
  }
  function forgetScene(id) { try { localStorage.removeItem(sceneMemKey(id)); } catch (e) { /* nothing to forget */ } }

  // ── layouts: scenes, saved layouts, auto-arrange (the 🧩 menu) ────────────────────────────────────────────────────────
  // A SAVED LAYOUT is a snapshot of the windows (both screens' arrangement, who waits in the dock); a SCENE is a recipe - show
  // these kinds of window, tile them, and put every other window in the dock - so it adapts to whatever is open and costs nothing:
  // nothing is closed, a window that is not in the scene keeps its page and comes back with one click (or the next scene).
  let currentPreset = '';                 // the saved layout applied or saved last (the status bar names it)
  const SCENES = [
    { id: 'combat',  label: '⚔ Combat',      sub: 'map · combat · party · dice',        wants: ['map', 'combat', 'party', 'dice'] },
    { id: 'explore', label: '🧭 Exploration', sub: 'world map · party · quests · notes', wants: ['wmap', 'party', 'quests', 'notes'] },
    { id: 'roleplay', label: '🎭 Roleplay',   sub: 'the open NPC · party · notes · AI chat', wants: ['entity', 'party', 'notes', 'ai_chat'] },
    { id: 'prep',    label: '📖 Prep',        sub: 'AI chat · find · notes · tables · calendar', wants: ['ai_chat', 'find', 'notes', 'tables', 'calendar'] },
  ];

  // A running timer is a moment in time, not an arrangement: a saved layout must not carry one (it would open as a stale countdown).
  function stripTimers(layout) {
    (layout.panels || []).forEach(function (q) { if (q.type === 'timer' && q.data) delete q.data.timer; });
    return layout;
  }

  function layoutsOpen() { return layoutsPop.style.display === 'block'; }
  function closeLayouts(refocus) {
    if (!layoutsOpen()) return;
    layoutsPop.style.display = 'none';
    layoutsBtn.setAttribute('aria-expanded', 'false');
    if (refocus) layoutsBtn.focus();
  }
  function openLayouts() {
    windowsPop.style.display = 'none';
    renderLayoutsMenu();
    const r = layoutsBtn.getBoundingClientRect();
    layoutsPop.style.display = 'block';
    layoutsPop.style.left = Math.max(8, Math.min(r.right - layoutsPop.offsetWidth, window.innerWidth - layoutsPop.offsetWidth - 8)) + 'px';
    layoutsPop.style.top = (r.bottom + 6) + 'px';
    layoutsBtn.setAttribute('aria-expanded', 'true');
    const first = layoutsPop.querySelector('.ck-winrow');
    if (first) first.focus();
  }
  function renderLayoutsMenu() {
    const scenesEl = document.getElementById('ck-scenes');
    if (scenesEl) {
      scenesEl.textContent = '';
      SCENES.forEach(function (sc) {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'ck-winrow'; b.setAttribute('role', 'menuitem');
        const name = document.createElement('span'); name.textContent = sc.label;
        const sub = document.createElement('small'); sub.textContent = sc.sub;
        b.append(name, sub);
        b.addEventListener('click', function () { closeLayouts(false); applyScene(sc); });
        scenesEl.appendChild(b);
      });
    }
    const savedEl = document.getElementById('ck-saved');
    if (!savedEl) return;
    savedEl.textContent = '';
    const names = Object.keys(presets);
    if (!names.length) {
      const e = document.createElement('div');
      e.className = 'ck-pop-empty';
      e.textContent = 'None yet - arrange the windows, then "Save the windows as a layout…".';
      savedEl.appendChild(e);
    }
    names.forEach(function (name) {
      const row = document.createElement('div');
      row.className = 'ck-saved-row';
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ck-winrow' + (name === currentPreset ? ' current' : ''); b.setAttribute('role', 'menuitem');
      b.textContent = name;
      b.title = 'Apply the saved layout "' + name + '"';
      b.addEventListener('click', function () { closeLayouts(false); applyPreset(name); });
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'ck-winrow ck-winrow-x'; x.textContent = '✕';
      x.title = 'Delete the saved layout "' + name + '"'; x.setAttribute('aria-label', x.title);
      x.addEventListener('click', function () { deletePreset(name); });
      row.append(b, x);
      savedEl.appendChild(row);
    });
  }

  function applyPreset(name) {
    if (!name || !presets[name]) return;
    const pr = presets[name];
    const from = workingFrom(stripTimers(clone(pr)).panels, LAYOUT.vpOk(pr.vp) ? pr.vp : null, LAYOUT.vpOk(pr.lvp) ? pr.lvp : null);
    currentPreset = name;
    currentScene = '';
    recomputeSeq();
    panels.forEach(function (p) { p.id = newId(); });
    adaptToScreen(from);   // arranged on another screen? scale it
    render();
    scrollWorkspaceTo(0, false);                     // a whole new arrangement is read from the top
    save();
  }
  function deletePreset(name) {
    if (!name || !presets[name]) return;
    if (!confirm('Delete the saved layout "' + name + '"?')) return;
    delete presets[name];
    if (currentPreset === name) currentPreset = '';
    renderLayoutsMenu();
    updateStatus();
    save();
  }
  function saveLayoutAs() {
    const typed = prompt('Save the windows as a layout named (e.g. Combat, Exploration):', currentPreset || '');
    const name = (typed || '').trim().slice(0, 40);
    if (!name) return;
    if (presets[name] && !confirm('Replace the saved layout "' + name + '" with the current windows?')) return;
    if (!presets[name] && Object.keys(presets).length >= CK_MAX_PRESETS) {
      toast('At most ' + CK_MAX_PRESETS + ' saved layouts - delete one first.');
      return;
    }
    const now = vpNow(); if (now) refVp = now;
    presets[name] = stripTimers(clone(savedLayout()));
    currentPreset = name;
    currentScene = '';
    renderLayoutsMenu();
    updateStatus();
    save();
  }

  // The window a scene wants of one kind: the one used last, else a new one (a map / party / world map needs one to exist
  // in the world; a combat takes the most recent; an entity needs a choice, so only an open one is used).
  async function sceneWindow(type) {
    const have = panels.filter(function (q) { return q.type === type; }).sort(function (a, b) { return (b.z || 0) - (a.z || 0); })[0];
    if (have) return have;
    let ref = '', title = '';
    if (type === 'map') { if (!CK_MAPS.length) return null; ref = CK_MAPS[0].slug; title = '📐 ' + CK_MAPS[0].name; }
    else if (type === 'wmap') { if (!(CK_WORLD_MAPS || []).length) return null; ref = CK_WORLD_MAPS[0].slug; title = '🗺 ' + CK_WORLD_MAPS[0].name; }
    else if (type === 'party') { if (!CK_PARTIES.length) return null; ref = String(CK_PARTIES[0].id); title = '❤ ' + CK_PARTIES[0].name; }
    else if (type === 'combat') {
      try {
        const r = await fetch('/api/combat/recent');
        const c = r.ok ? ((await r.json()).combats || [])[0] : null;
        if (c) { ref = String(c.id); title = '⚔ ' + c.name; }
      } catch (e) { /* an unpicked combat window offers its own picker */ }
    } else if (type === 'entity') return null;
    return addPanel(type, ref, title, { scene: true });
  }
  async function applyScene(sc, opts) {
    if (MOBILE() || PLAYER) return;
    const fresh = !!(opts && opts.fresh);            // the lit chip clicked again: forget the arrangement and tile afresh
    if (fresh) forgetScene(sc.id);
    const ids = [];
    for (let i = 0; i < sc.wants.length; i++) {
      const p = await sceneWindow(sc.wants[i]);
      if (p && ids.indexOf(p.id) < 0) ids.push(p.id);
    }
    if (!ids.length) { toast(sc.label + ': nothing to show yet - add a map, a party or a combat first.'); return; }
    LAYOUT.applyScene(panels, ids, { klass: klass, vp: vpNow() || refVp || LAPTOP_FALLBACK_VP, slots: LAPTOP() ? slotsNow() : Infinity, pref: prefHeight, stage: ids[0] });
    tabActive = ids[0]; stageId = ids[0];
    const recalled = style === 'free' && !fresh && recallScene(sc.id);     // your own arrangement of this scene, when it covers the windows
    reflow(true);
    currentScene = sc.id; currentPreset = '';
    paintSceneBar();
    const keep = panels.filter(function (q) { return q.parked; }).length;
    applyDockState();
    updateStatus();
    scrollWorkspaceTo(0, true);                      // a scene is tiled from the top
    ids.forEach(function (id) { const q = panels.find(function (x) { return x.id === id; }); if (q && !q.parked) flash(winEl(q)); });
    save();
    toast(sc.label + ' — ' + (panels.length - keep) + ' window' + (panels.length - keep === 1 ? '' : 's') + ' showing' + (keep ? ', ' + keep + ' in the dock' : '') + (recalled ? ' — as you arranged it (click the scene again to tile afresh)' : (fresh ? ' — tiled afresh' : '')));
  }

  // The scenes one click away: chips in the header (icon only on a laptop) and tiles on the empty cockpit; Alt + Shift + 1-4 as well.
  function paintSceneBar() {
    Array.prototype.forEach.call(document.querySelectorAll('#ck-scenebar .ck-scene'), function (b) {
      const on = b.dataset.scene === currentScene;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  function renderSceneBar() {
    if (PLAYER) return;
    const bar = document.getElementById('ck-scenebar');
    if (bar) {
      bar.textContent = '';
      SCENES.forEach(function (sc, i) {
        const at = sc.label.indexOf(' ');
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'ck-scene'; b.dataset.scene = sc.id;
        b.title = sc.label + ' \u2014 ' + sc.sub + '  (Alt + Shift + ' + (i + 1) + ')';
        b.setAttribute('aria-label', 'Scene: ' + sc.label.slice(at + 1));
        const ic = document.createElement('span'); ic.textContent = sc.label.slice(0, at);
        const tx = document.createElement('span'); tx.className = 'ck-bl'; tx.textContent = ' ' + sc.label.slice(at + 1);
        b.append(ic, tx);
        b.addEventListener('click', function () { applyScene(sc, { fresh: sc.id === currentScene }); });
        bar.appendChild(b);
      });
    }
    const host = document.getElementById('ck-welcome-scenes');
    if (host && !host.children.length) {
      SCENES.forEach(function (sc) {
        const at = sc.label.indexOf(' ');
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'ck-tile ck-scene-tile';
        const ic = document.createElement('span'); ic.className = 'ck-tile-ic'; ic.textContent = sc.label.slice(0, at);
        const nm = document.createElement('span'); nm.textContent = sc.label.slice(at + 1);
        const sub = document.createElement('small'); sub.textContent = sc.sub;
        b.append(ic, nm, sub);
        b.addEventListener('click', function () { applyScene(sc); });
        host.appendChild(b);
      });
    }
    paintSceneBar();
  }

  layoutsBtn.addEventListener('click', function () { if (layoutsOpen()) closeLayouts(false); else openLayouts(); });
  layoutsPop.addEventListener('keydown', function (e) {
    if (e.altKey) return;                            // Alt + arrows scroll the workspace
    const rows = Array.prototype.slice.call(layoutsPop.querySelectorAll('.ck-winrow'));
    const at = rows.indexOf(document.activeElement);
    let to = null;
    if (e.key === 'ArrowDown') to = rows[(at + 1) % rows.length];
    else if (e.key === 'ArrowUp') to = rows[(at - 1 + rows.length) % rows.length];
    else if (e.key === 'Home') to = rows[0];
    else if (e.key === 'End') to = rows[rows.length - 1];
    if (to) { e.preventDefault(); to.focus(); }
  });
  document.addEventListener('mousedown', function (e) {
    if (layoutsOpen() && !e.target.closest('#ck-layouts-pop') && !e.target.closest('#ck-layouts-btn')) closeLayouts(false);
  });
  document.getElementById('ck-save-preset').addEventListener('click', function () { closeLayouts(false); saveLayoutAs(); });
  const lockRow = document.getElementById('ck-lock-row');
  if (lockRow) lockRow.addEventListener('click', function () { closeLayouts(false); toggleLock(); });
  const roomyRow = document.getElementById('ck-roomy-row');
  if (roomyRow) roomyRow.addEventListener('click', function () { closeLayouts(false); toggleRoomy(); });
  const wheelRow = document.getElementById('ck-wheel-row');
  if (wheelRow) wheelRow.addEventListener('click', function () { closeLayouts(false); toggleWheelMode(); });

  document.getElementById('ck-add-btn').addEventListener('click', openAdd);
  // The Find-AI toolbar button is GM-only (absent in player mode) — its
  // listener must be guarded or a null addEventListener kills the whole
  // cockpit IIFE (blank workspace for players).
  const findBtn = document.getElementById('ck-find-btn');
  if (findBtn) findBtn.addEventListener('click', function () {
    // Focus an existing unseeded find panel instead of stacking duplicates.
    const existing = panels.find(function (q) { return q.type === 'find' && !q.ref; });
    if (existing) { showPanel(existing); return; }
    addPanel('find', '', '\u{1F50D} AI Find');
  });
  document.getElementById('ck-add-close').addEventListener('click', closeAdd);
  addOverlay.addEventListener('mousedown', function (e) { if (e.target === addOverlay) closeAdd(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (window.ndPalette && window.ndPalette.isOpen()) return;       // the palette closes itself; one Esc, one thing
      if (helpOpen()) { closeHelp(); return; }
      const addWasOpen = addOverlay.style.display === 'block';
      if (addWasOpen) closeAdd();
      if (windowsPop.style.display === 'block') { windowsPop.style.display = 'none'; windowsBtn.focus(); }
      if (layoutsOpen()) closeLayouts(true);
      if (ctxEl && ctxEl.style.display === 'block') ctxEl.style.display = 'none';
      const big = viewport.querySelector('.ck-win.maxed');          // a window filling the cockpit: Esc gives it back
      if (big && !addWasOpen) big.classList.remove('maxed');
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
    refVp = vpNow() || refVp;
    render();
    scrollWorkspaceTo(0, false);                     // the default windows are read from the top
    save();
  });

  // ── keyboard, help and the quick switcher ──────────────────────────────────────────────────────────────────────────
  // The shortcut table is static/js/cockpit-keys.js (pure and tested; the same rows fill the help overlay, so the list can never
  // disagree with the keys). A window's page is a document of its own and its keys never reach this one, so relayFrameEvents
  // listens in on each page and feeds the same handler. Ctrl+K is the site's quick switcher (command-palette.js), which this
  // page extends with its own commands, the windows, and "open as a window" for entities and pages.
  const MAC = /Mac|iPhone|iPad/.test(navigator.platform || '');
  function helpOpen() { return !!helpOverlay && helpOverlay.style.display === 'block'; }
  function modalOpen() {
    return addOverlay.style.display === 'block' || helpOpen() || !!(window.ndPalette && window.ndPalette.isOpen());
  }
  // Put the keyboard inside a window: its page when it has one (so arrow keys and PgDn scroll it), else the window itself.
  function focusWindow(p) {
    const el = winEl(p);
    if (!el) return;
    const f = el.querySelector('.ck-body iframe');
    if (f && f.contentWindow) { try { f.contentWindow.focus(); return; } catch (e) { /* not reachable: focus the frame */ } }
    if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1');
    try { el.focus({ preventScroll: true }); } catch (e) { /* hidden */ }
  }
  // The window the keys act on: the active one, else the topmost that shows.
  function activeEl() {
    const shown = Array.prototype.slice.call(viewport.querySelectorAll('.ck-win:not(.ck-parked):not(.closing)'));
    return viewport.querySelector('.ck-win.ck-active:not(.ck-parked)') ||
           shown.sort(function (a, b) { return (parseInt(b.style.zIndex, 10) || 0) - (parseInt(a.style.zIndex, 10) || 0); })[0] || null;
  }
  function activePanel() {
    const el = activeEl();
    return el ? panels.find(function (q) { return q.id === el.dataset.pid; }) || null : null;
  }
  // Fill the cockpit with the active window, or give back the one that fills it.
  function toggleMaxActive() {
    const el = viewport.querySelector('.ck-win.maxed') || activeEl();
    if (!el) { toast('Open a window first.'); return; }
    el.classList.toggle('maxed');
  }
  function runShortcut(k) {
    if (k.action === 'add') openAdd();
    else if (k.action === 'windows') windowsBtn.click();
    else if (k.action === 'layouts') layoutsBtn.click();
    else if (k.action === 'scene') { const sc = SCENES[(k.n || 1) - 1]; if (sc) applyScene(sc); }
    else if (k.action === 'lock') toggleLock();
    else if (k.action === 'zoomin' || k.action === 'zoomout' || k.action === 'zoomreset') {
      const p = activePanel();
      if (!p) { toast('Click a window first - Alt + / Alt - zoom the active one.'); return; }
      if (k.action === 'zoomreset') setZoom(p, 100); else stepZoom(p, k.action === 'zoomin' ? 1 : -1);
    }
    else if (k.action === 'show') {
      const p = panels[k.n - 1];
      if (p) { showPanel(p); focusWindow(p); }
      else toast('There is no window ' + k.n + ' (' + panels.length + ' open).');
    } else if (k.action === 'maximize') toggleMaxActive();
    else if (k.action === 'undo') { if (undoStack.length) undoClose(); else toast('Nothing to reopen.'); }
    else if (k.action === 'scrollup' || k.action === 'scrolldown' || k.action === 'scrolltop' || k.action === 'scrollend') {
      scrollWorkspace({ scrollup: 'up', scrolldown: 'down', scrolltop: 'top', scrollend: 'end' }[k.action]);
    }
    else if (k.action === 'help') toggleHelp();
  }
  // True when the key was one of ours (the caller then stops it reaching the page it came from).
  function onKey(e) {
    if (MOBILE() || !KEYS) return false;
    const k = KEYS.shortcutFor({ key: e.key, code: e.code, ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey, shift: e.shiftKey,
                                 typing: KEYS.isTypingTarget(e.target), mac: MAC });
    if (!k) return false;
    if (k.action !== 'help' && modalOpen()) return false;          // a dialog is up: keys belong to it
    if (!e.repeat) runShortcut(k);                                 // a held key must not reopen ten windows
    return true;
  }

  // ── the help overlay (F1, ?, the ⌨ button) ─────────────────────────────────────────────────────────────────────────
  const MOUSE_TIPS = [
    ['Drag a title bar', 'Move the window. It snaps to the workspace edges and to other windows (hold Ctrl for free placement).'],
    ['Drag an edge or a corner', 'Resize the window from that side.'],
    ['Drag to the left / right edge', 'Dock the window to that half of the screen.'],
    ['Drag or resize at the top / bottom edge', 'The workspace scrolls with you: make a window taller than the screen, or carry it down to the others.'],
    ['Scrollbar  /  Alt + mouse wheel', 'Scroll the workspace (the wheel alone scrolls the page in the window under it). The ▲ 1/2 ▼ in the status bar does it too.'],
    ['🧩 Layouts → Roomy windows', 'Auto-arrange, scenes and the laptop tiles keep every window big (about 60% of the screen tall) and the workspace scrolls for the rest.'],
    ['🧩 Layouts → Wheel scrolls the workspace', 'Tick it and the plain wheel scrolls the workspace even over a window; Alt + wheel then scrolls the page in the window.'],
    ['Double-click a title bar', 'Fill the cockpit with the window; again to give it back.'],
    ['Double-click the title text', 'Rename the window.'],
    ['Right-click a title bar', 'The window menu: maximize, dock, reload, rename, colour …'],
  ];
  const LAPTOP_TIPS = [
    ['Dock chips (top)', 'Click one to bring that window in, swapping out the one brought in last. Right-click: make main, put in the dock, close.'],
    ['Show 1 · 2 · 3 · All', 'How many windows share the screen. Windows in the dock keep running.'],
  ];
  function buildHelp() {
    helpBody.textContent = '';
    function section(title, rows) {
      const h = document.createElement('h3');
      h.textContent = title;
      helpBody.appendChild(h);
      rows.forEach(function (r) {
        const row = document.createElement('div');
        row.className = 'ck-help-row';
        const kb = document.createElement('kbd');
        const tip = Array.isArray(r);                       // [keys, what] pairs (mouse tips) or {keys, what} rows (the key table)
        kb.textContent = tip ? r[0] : r.keys;
        const what = document.createElement('span');
        what.textContent = tip ? r[1] : r.what;
        row.append(kb, what);
        helpBody.appendChild(row);
      });
    }
    section('Keyboard', KEYS ? KEYS.helpRows() : []);
    section('Mouse', MOUSE_TIPS);
    if (LAPTOP()) section('Laptop layout', LAPTOP_TIPS);
  }
  let helpReturnTo = null;
  function openHelp() {
    if (!helpOverlay || helpOpen()) return;
    buildHelp();
    helpReturnTo = document.activeElement;
    helpOverlay.style.display = 'block';
    const x = document.getElementById('ck-help-close');
    if (x) x.focus();
  }
  function closeHelp() {
    if (!helpOpen()) return;
    helpOverlay.style.display = 'none';
    const back = helpReturnTo;
    helpReturnTo = null;
    if (back && back.focus && document.contains(back)) { try { back.focus(); } catch (e) { /* gone */ } }
  }
  function toggleHelp() { if (helpOpen()) closeHelp(); else openHelp(); }
  if (helpOverlay) {
    document.getElementById('ck-help-close').addEventListener('click', closeHelp);
    helpOverlay.addEventListener('mousedown', function (e) { if (e.target === helpOverlay) closeHelp(); });
    const helpBtn = document.getElementById('ck-help-btn');
    if (helpBtn) helpBtn.addEventListener('click', toggleHelp);
  }

  // ── the quick switcher (Ctrl+K) ────────────────────────────────────────────────────────────────────────────────────
  // command-palette.js is deferred, so it is not there yet when this script runs: register once the page has been parsed.
  function openEntityWindow(ent) {
    const ref = String(ent.id);
    const have = panels.find(function (q) { return q.type === 'entity' && q.ref === ref; });
    const p = have || addPanel('entity', ref, (KIND_ICONS[ent.kind] || '👤') + ' ' + ent.name);
    if (!p) return;
    if (have) showPanel(p);
    focusWindow(p);
  }
  function openPageWindow(it) {
    const ref = pageHref(it.href);
    if (!validPagePath(ref)) { location.href = it.href; return; }
    const have = panels.find(function (q) { return q.type === 'page' && q.ref === ref; });
    const p = have || addPanel('page', ref, pageTitle(it.label));
    if (!p) return;
    if (have) showPanel(p);
    focusWindow(p);
  }
  function clickEl(id) { return function () { const el = document.getElementById(id); if (el) el.click(); }; }
  function paletteItems() {
    const out = [];
    const lap = LAPTOP();
    out.push({ label: '＋ Add a panel…', sub: 'Alt+N', kw: 'new window open', run: openAdd });
    out.push({ label: '🗂 List of windows', sub: 'Alt+W', kw: 'switch', run: function () { windowsBtn.click(); } });
    if (undoStack.length) out.push({ label: '↩ Reopen the closed window', sub: 'Alt+U · ' + chipLabel(undoStack[undoStack.length - 1].panel), kw: 'undo closed', run: undoClose });
    out.push({ label: '⌨ Keyboard shortcuts and tips', sub: 'F1', kw: 'help keys', run: openHelp });
    panels.forEach(function (p, i) {
      out.push({
        label: 'Show: ' + fullTitle(p),
        sub: (p.parked ? 'in the dock' : 'window') + (i < 9 ? ' · Alt+' + (i + 1) : ''),
        kw: p.type + ' ' + (CK_TYPES[p.type] ? CK_TYPES[p.type].label : ''),
        run: function () { showPanel(p); focusWindow(p); },
      });
    });
    out.push({ label: '📌 Auto-arrange the windows', sub: 'layout', kw: 'tidy grid tile', run: clickEl('ck-pin-btn') });
    out.push({ label: '💾 Save this layout…', sub: 'layout', run: clickEl('ck-save-preset') });
    STYLE_INFO.forEach(function (x) { out.push({ label: 'Layout style: ' + x.label, sub: x.sub, kw: 'arrange windows tabs grid stage rail free', run: function () { setStyle(x.id); } }); });
    if (!PLAYER) SCENES.forEach(function (sc) {
      out.push({ label: 'Scene: ' + sc.label, sub: sc.sub, kw: 'layout view arrange', run: function () { applyScene(sc); } });
    });
    Object.keys(presets).forEach(function (name) {
      out.push({ label: 'Layout: ' + name, sub: 'saved layout', kw: 'preset', run: function () { applyPreset(name); } });
    });
    out.push({ label: '🧩 Layouts menu', sub: 'Alt+L', kw: 'scenes saved presets', run: openLayouts });
    out.push({ label: locked() ? '🔓 Unlock the windows' : '🔒 Lock the windows', sub: 'Alt+K', kw: 'freeze protect accidental', run: toggleLock });
    const ap = activePanel();
    if (ap) {
      out.push({ label: '🔍＋ Zoom in: ' + fullTitle(ap), sub: 'Alt +', kw: 'bigger larger', run: function () { stepZoom(ap, 1); } });
      out.push({ label: '🔍− Zoom out: ' + fullTitle(ap), sub: 'Alt −', kw: 'smaller', run: function () { stepZoom(ap, -1); } });
      if (popOutPath(ap)) out.push({ label: '⇱ Pop out: ' + fullTitle(ap), sub: 'own window', kw: 'second monitor float detach', run: function () { popOut(ap); } });
    }
    out.push({ label: '↺ Reset to the default panels', sub: 'layout', run: clickEl('ck-reset') });
    out.push({ label: document.fullscreenElement ? '⛶ Leave fullscreen' : '⛶ Fullscreen', sub: 'F11', kw: 'table mode', run: toggleFullscreen });
    out.push({ label: lap ? '🖥 Desktop layout (free windows)' : '💻 Laptop layout (a few big windows)', sub: 'this browser', kw: 'mode screen', run: clickEl('ck-mode-btn') });
    if (lap) out.push({ label: '☰ Show / hide the site menu', sub: 'laptop', run: clickEl('ck-site-btn') });
    Object.keys(panelTypes()).forEach(function (key) {
      const t = CK_TYPES[key];
      out.push({
        label: '＋ Add: ' + t.label, sub: t.sub, kw: 'new open window ' + key,
        run: t.picker ? function () { openAdd(); chooseType(key); } : function () { const p = addPanel(key, '', ''); if (p) focusWindow(p); },
      });
    });
    return out;
  }
  function registerPalette() {
    const pal = window.ndPalette;
    if (!pal) return;
    pal.addProvider(paletteItems);
    pal.setEntityOpener(openEntityWindow);
    pal.setPageOpener(openPageWindow);
  }
  if (window.ndPalette) registerPalette(); else document.addEventListener('DOMContentLoaded', registerPalette);

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
      reclass();               // the window may have been widened across the laptop / desktop line while the phone shell showed
      adaptToScreen(refVp);
      render();
    }
  }

  // ── auto-arrange (📌) ───────────────────────────────────────────────
  // Tidies every window for THIS screen with the same tiler the default layout uses: the first map / combat window is
  // the stage and the rest share the remaining columns, in their current top-to-bottom, left-to-right order. Columns
  // follow the screen's width (2 on a laptop, 3 on 1080p, up to 6), a window is never made shorter than a readable
  // height, and a grid with many windows simply outgrows the screen — the workspace scrolls. One-time arrangement:
  // dragging stays free after.
  function autoArrange() {
    if (COMPACT.matches || !panels.length) return;
    const vp = vpNow();
    if (!vp) return;
    if (style !== 'free') { reflow(); scrollWorkspaceTo(0, true); return; }     // a managed style is always tidy: arrange again
    if (LAPTOP()) {      // the main window big, the others beside it, as many as fit (the dock keeps the rest)
      LAYOUT.retileLaptop(panels, vp, slotsNow(), prefHeight);
      refVp = vp;
      applyDockState();
      scrollWorkspaceTo(0, true);
      save();
      return;
    }
    const ordered = LAYOUT.visibleOf(panels).sort(function (a, b) { return (a.y - b.y) || (a.x - b.x); });   // what waits in the dock is left alone
    if (!ordered.length) return;
    LAYOUT.tilePanels(ordered, vp, { pref: prefHeight }).forEach(function (q, i) {
      const p = ordered[i];
      p.x = q.x; p.y = q.y; p.w = q.w; p.h = q.h; p.collapsed = false;
    });
    syncWindowStyles(true);
    refVp = vp;
    scrollWorkspaceTo(0, true);                      // tiled from the top
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
    const h = (window.innerHeight - (window.ndNavOffset ? window.ndNavOffset() : tb.offsetHeight)) + 'px';   // (a side rail takes width, not height)
    wrap.style.height = h;
    // The phone/tablet shell too: sized to a whole viewport BELOW the top bar, its tab bar sat off-screen.
    const mwrap = document.getElementById('ck-mwrap');
    if (mwrap) mwrap.style.height = h;
    scheduleAdapt();
    scheduleScrollUi();
  }

  // Write panels[]'s geometry onto the open windows (no re-render: that would reload every iframe).
  function syncWindowStyles(clearState) {
    panels.forEach(function (p) {
      const win = viewport.querySelector('.ck-win[data-pid="' + p.id + '"]');
      if (!win) return;
      win.style.left = p.x + 'px';
      win.style.top = p.y + 'px';
      win.style.width = p.w + 'px';
      win.style.height = p.h + 'px';
      if (p.z) win.style.zIndex = p.z;
      win.classList.toggle('ck-parked', !!p.parked);
      win.classList.toggle('collapsed', !!p.collapsed);
      const cb = win.querySelector('[data-role="collapse"]');
      if (cb) cb.textContent = p.collapsed ? '▸' : '▾';
      if (clearState) win.classList.remove('maxed');
    });
    scheduleScrollUi();
  }

  // ── laptop layout: the dock, parking, switching class ────────────────────────────────────────────────────────────────
  const dockEl = document.getElementById('ck-dock');
  const dockChips = document.getElementById('ck-dock-chips');
  const dockSlots = document.getElementById('ck-dock-slots');
  const modeBtn = document.getElementById('ck-mode-btn');
  const siteBtn = document.getElementById('ck-site-btn');
  const keepBox = document.getElementById('ck-keep-box');

  function winEl(p) { return viewport.querySelector('.ck-win[data-pid="' + p.id + '"]'); }
  function fullTitle(p) { return p.title || CK_TYPES[p.type].icon + ' ' + CK_TYPES[p.type].label; }
  function chipLabel(p) { const t = fullTitle(p); return t.length > 18 ? t.slice(0, 17) + '…' : t; }
  function lapVp() { return vpNow() || refVp || LAPTOP_FALLBACK_VP; }
  function raiseWin(p) {
    const el = winEl(p);
    if (el) { zTop += 1; el.style.zIndex = zTop; p.z = zTop; el.classList.remove('collapsed'); }
  }
  function flash(el) {
    if (!el) return;
    el.classList.add('ck-flash');
    setTimeout(function () { el.classList.remove('ck-flash'); }, 800);
  }

  // Geometry, parked state and the dock all follow panels[] - without a re-render, which would reload every window's page.
  function applyDockState() {
    zTop = panels.reduce(function (m, p) { return Math.max(m, p.z || 0); }, zTop);
    syncWindowStyles(true);
    renderDock();
  }

  // The chip row scrolls sideways when there are more chips than room: the wheel scrolls it, its edges fade while there is more that
  // way, and the chip just brought in is scrolled into view.
  function fadeChips() {
    const c = dockChips;
    c.style.setProperty('--fl', c.scrollLeft > 4 ? '28px' : '0px');
    c.style.setProperty('--fr', c.scrollLeft + c.clientWidth < c.scrollWidth - 4 ? '28px' : '0px');
  }
  if (dockChips) {
    dockChips.addEventListener('scroll', fadeChips);
    dockChips.addEventListener('wheel', function (e) {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || dockChips.scrollWidth <= dockChips.clientWidth) return;
      e.preventDefault();
      dockChips.scrollLeft += e.deltaY;
    }, { passive: false });
    window.addEventListener('resize', fadeChips);
  }

  // The dock is the cockpit's taskbar on both layouts: a chip per window, lit while it shows; a click brings it in (a laptop swaps
  // one out for it, the big screen just raises it); a window can wait there on either screen.
  function renderDock() {
    if (!dockEl) return;
    const lap = LAPTOP();
    dockEl.hidden = MOBILE() || !panels.length;
    const empty = document.getElementById('ck-allparked');
    if (empty) empty.hidden = MOBILE() || !panels.length || !panels.every(function (p) { return p.parked; });
    if (dockEl.hidden) return;
    dockChips.textContent = '';
    const mainWin = lap ? LAYOUT.mainOf(LAYOUT.visibleOf(panels)) : null;
    panels.forEach(function (p) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ck-chip' + (p.parked ? '' : ' on') + (p === mainWin && !p.parked ? ' main' : '') + (alarms.has(p.id) ? ' alarm' : '');
      b.setAttribute('aria-pressed', p.parked ? 'false' : 'true');
      b.dataset.pid = p.id;
      b.textContent = chipLabel(p);
      b.title = fullTitle(p) + (p.parked ? ' — in the dock, click to bring it back'
        : (p === mainWin ? ' — the main window (right-click for more)' : (lap ? ' — on screen (right-click for more)' : ' — on screen, click to bring it to the front (right-click for more)'))) +
        (panels.indexOf(p) < 9 ? ' · Alt+' + (panels.indexOf(p) + 1) : '');
      b.addEventListener('click', function () { showPanel(p); });
      b.addEventListener('contextmenu', function (e) { e.preventDefault(); openChipCtx(p, e.clientX, e.clientY); });
      dockChips.appendChild(b);
    });
    if (lap) {
      const chosen = pref(SLOTS_KEY, 'auto'), n = String(slotsNow());
      dockSlots.querySelectorAll('button').forEach(function (b) {
        const active = b.dataset.n === 'all' ? chosen === 'all' : (chosen !== 'all' && b.dataset.n === n);
        b.classList.toggle('on', active);
        b.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    }
    const keepLabel = document.getElementById('ck-keep');
    if (keepLabel) keepLabel.hidden = !lap && !panels.some(function (p) { return p.parked; });   // on the big screen: once something waits
    fadeChips();
  }
  // Scroll the chip row (only the row - never the page) so one chip is fully in sight.
  function revealChip(p) {
    if (!dockChips || !p) return;
    const b = dockChips.querySelector('.ck-chip[data-pid="' + p.id + '"]');
    if (!b) return;
    const row = dockChips.getBoundingClientRect(), r = b.getBoundingClientRect();
    if (r.left < row.left) dockChips.scrollLeft -= row.left - r.left + 12;
    else if (r.right > row.right) dockChips.scrollLeft += r.right - row.right + 12;
    fadeChips();
  }

  // Bring a window on screen (the dock chip, the Windows list, the Find button): a laptop swaps it in for the one brought in
  // last; a big screen just brings it back from the dock (if it was there) and raises it.
  function showPanel(p) {
    if (LAPTOP()) LAYOUT.showLaptop(panels, p.id, lapVp(), slotsNow(), prefHeight);
    else LAYOUT.showWide(panels, p.id);
    tabActive = p.id;
    reflow(true);
    applyDockState();
    revealChip(p);
    revealWin(p);
    flash(winEl(p));
    save();
  }
  function parkPanel(p) {
    if (LAPTOP()) {
      if (!LAYOUT.parkLaptop(panels, p.id, lapVp(), prefHeight)) { toast('One window has to stay on screen.'); return; }
    } else if (!LAYOUT.parkWide(panels, p.id)) return;
    reflow(true);
    applyDockState();
    save();
  }
  function makeMainPanel(p) {
    if (style === 'rail') { stageId = p.id; reflow(); return; }          // Stage + rail: this window becomes the stage
    if (!LAPTOP() || !LAYOUT.makeMain(panels, p.id)) return;
    applyDockState();
    save();
  }
  // How many windows show at once on this browser: 1, 2, 3 or all of them.
  function setSlots(v) {
    setPref(SLOTS_KEY, v);
    if (LAPTOP()) {
      LAYOUT.retileLaptop(panels, lapVp(), slotsNow(), prefHeight);
      reflow(true);
      applyDockState();
      scrollWorkspaceTo(0, true);
      save();
    } else renderDock();
  }
  if (dockSlots) dockSlots.addEventListener('click', function (e) {
    const b = e.target.closest('button[data-n]');
    if (b) setSlots(b.dataset.n);
  });

  // The chrome that differs: the dock, the site's top bar (hidden by default on a laptop - it is 100px of a 650px screen -
  // with a button to bring it back), the labels of the toolbar.
  function applyChrome() {
    const on = LAPTOP();
    document.body.classList.toggle('ck-laptop', on);
    document.body.classList.toggle('ck-showsite', on && pref(SITE_KEY, '') === '1');
    document.body.classList.toggle('ck-keep', !MOBILE() && keepLoaded());
    if (keepBox) keepBox.checked = keepLoaded();
    if (siteBtn) siteBtn.setAttribute('aria-pressed', pref(SITE_KEY, '') === '1' ? 'true' : 'false');
    if (modeBtn) {
      modeBtn.textContent = klass === 'laptop' ? '💻' : '🖥';
      modeBtn.title = klass === 'laptop'
        ? 'Laptop layout: a few windows at a time, the rest in the dock. Click for the free-window desktop layout (Shift+click: automatic by window width)'
        : 'Desktop layout: free windows. Click for the laptop layout - a few big windows at a time (Shift+click: automatic by window width)';
    }
    renderDock();
  }
  // Keep the windows in the dock loaded (CSS: body.ck-keep) - on by default, remembered per browser
  if (keepBox) keepBox.addEventListener('change', function () {
    setPref(KEEP_KEY, keepBox.checked ? '' : '0');
    applyChrome();
  });
  if (siteBtn) siteBtn.addEventListener('click', function () {
    setPref(SITE_KEY, pref(SITE_KEY, '') === '1' ? '' : '1');
    applyChrome();
    fitViewport();
  });

  // Change class (the window moved to another monitor, was resized across the line, or the button was pressed). The windows are
  // the same, so this changes geometry in place: no re-render, no reloaded pages, chats keep their text.
  function switchClass(next) {
    if (next === klass || MOBILE()) return;
    clearTimeout(adaptTimer);
    const saved = savedLayout();
    klass = next;
    applyChrome();
    fitViewport();                       // the laptop layout hides the site's top bar: the workspace changes height
    clearTimeout(adaptTimer);
    const wideVp = LAYOUT.vpOk(saved.vp) ? saved.vp : null, lapRef = LAYOUT.vpOk(saved.lvp) ? saved.lvp : null;
    const r = LAYOUT.toWorking(saved.panels, laptopOpts(saved.panels.length));
    r.panels.forEach(function (w, i) {
      const p = panels[i];
      ['x', 'y', 'w', 'h', 'z', 'collapsed', '_wide', '_lap'].forEach(function (k) { p[k] = w[k]; });
      if (w.parked) p.parked = true; else delete p.parked;
    });
    otherVp = klass === 'wide' ? lapRef : wideVp;
    adaptToScreen(klass === 'wide' ? wideVp : (r.derived ? lapVp() : lapRef));
    reflow(true);
    applyDockState();
    if (klass === 'laptop' && pref(HINT_KEY, '') !== '1') {
      setPref(HINT_KEY, '1');
      toast('Laptop layout: a few windows at a time. The others wait in the bar at the top — click one to bring it in. 💻 switches back.');
    }
  }
  // Has the window crossed the line between the two classes (or have we just left the phone shell)?
  function reclass() {
    if (MOBILE()) return;
    const want = LAYOUT.screenClass(window.innerWidth, klass, pref(MODE_KEY, ''));
    if (want !== klass) switchClass(want);
  }
  if (modeBtn) modeBtn.addEventListener('click', function (e) {
    if (e.shiftKey) setPref(MODE_KEY, '');                                   // back to automatic
    else setPref(MODE_KEY, klass === 'laptop' ? 'wide' : 'laptop');
    reclass();
  });

  // The workspace changed size — a window resized or maximised, fullscreen (⛶), the browser moved to another monitor,
  // devtools opened: scale the windows with it (a scrollbar or a bookmarks bar is within tolerance and does not count).
  // The scaling is not saved by itself; the next change the GM makes saves the layout for this screen.
  let adaptTimer = null;
  function scheduleAdapt() {
    clearTimeout(adaptTimer);
    adaptTimer = setTimeout(function () {
      if (MOBILE() || !panels.length || !refVp) return;
      adaptToScreen(refVp);
      syncWindowStyles(false);
      reflow();
    }, 150);
  }

  // matchMedia's change event is the primary mode switch (rotate), but
  // some embedded webviews don't fire it on viewport changes — the resize
  // listener reconciles the body class with the actual width either way.
  function syncMode() {
    if (MOBILE() && !document.body.classList.contains('ck-mobile')) renderMobile();
    else if (!MOBILE() && document.body.classList.contains('ck-mobile')) {
      document.body.classList.remove('ck-mobile');
      reclass();
      adaptToScreen(refVp);
      render();
    } else reclass();      // a plain resize: has the window crossed the line between the laptop and the desktop layout?
  }
  window.addEventListener('resize', function () { fitViewport(); syncMode(); });
  // ── layout styles: arrange again after every change, switch between them ──────────────────────────────────────────────────
  function styleInfo(id) { return STYLE_INFO.find(function (x) { return x.id === id; }); }
  function reflow(quiet) {
    if (style === 'free' || MOBILE() || !panels.length) return false;
    const vp = vpNow();
    if (!vp) return false;
    const moved = LAYOUT.arrangeStyle(panels, style, { klass: klass, vp: vp, slots: LAPTOP() ? slotsNow() : Infinity, pref: prefHeight, active: tabActive, stage: stageId });
    if (!moved) return false;
    refVp = vp;
    applyDockState();
    scheduleScrollUi();
    if (!quiet) save();
    return true;
  }
  let reflowTimer = 0;
  function scheduleReflow() {
    if (style === 'free' || reflowTimer) return;
    reflowTimer = setTimeout(function () { reflowTimer = 0; reflow(); }, 0);
  }
  let managedToldAt = 0;
  function noteManaged() {
    if (Date.now() - managedToldAt < 60000) return;
    managedToldAt = Date.now();
    toast(styleInfo(style).label + ' arranges the windows for you. Choose ◫ Free windows (the buttons in the header) to move them by hand.');
  }
  function paintStyleBar() {
    document.body.classList.toggle('ck-managed', style !== 'free');
    STYLE_INFO.forEach(function (x) { document.body.classList.toggle('ck-style-' + x.id, x.id === style); });
    Array.prototype.forEach.call(document.querySelectorAll('#ck-stylebar .ck-style'), function (b) {
      const on = b.dataset.style === style;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  function saveStyle() {
    try { localStorage.setItem(STYLE_LS, JSON.stringify({ mode: style })); } catch (e) { /* not remembered in this browser */ }
    fetch('/api/view-prefs/cockpit', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: style }) })
      .catch(function () { /* the browser copy still holds */ });
  }
  function setStyle(next) {
    if (!styleInfo(next) || next === style) return;
    const was = style;
    style = next;
    paintStyleBar();
    saveStyle();
    if (MOBILE() || !panels.length) return;
    if (style === 'free') {
      if (was === 'tabs') {                      // leaving tabs: the windows all sit full-size in one spot - tile them so the free layout is usable
        panels.forEach(function (p) { delete p.parked; });
        if (LAPTOP()) LAYOUT.retileLaptop(panels, lapVp(), slotsNow(), prefHeight); else autoArrange();
        applyDockState();
        save();
      }
    } else {
      if (!tabActive) { const vis = LAYOUT.visibleOf(panels); tabActive = (vis.sort(function (a, b) { return (b.z || 0) - (a.z || 0); })[0] || panels[0]).id; }
      if (style !== 'tabs' && was === 'tabs') panels.forEach(function (p) { if (!LAPTOP()) delete p.parked; });   // a desktop shows everything again
      reflow();
      scrollWorkspaceTo(0, true);
    }
    updateStatus();
    toast(styleInfo(style).label + ' — ' + styleInfo(style).sub + '.');
  }
  function renderStyleBar() {
    const bar = document.getElementById('ck-stylebar');
    if (!bar) return;
    bar.textContent = '';
    STYLE_INFO.forEach(function (x) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ck-style'; b.dataset.style = x.id;
      b.title = x.label + ' — ' + x.sub;
      b.setAttribute('aria-label', 'Layout style: ' + x.label);
      b.textContent = x.icon;
      b.addEventListener('click', function () { setStyle(x.id); });
      bar.appendChild(b);
    });
    paintStyleBar();
  }

  applyChrome();           // before the first measure: the laptop layout hides the site's top bar, so the workspace is taller
  applyLock();
  applyWheelRow();
  applyRoomyRow();
  applyCombatHintRow();
  checkCombatStart();
  fitViewport();
  buildWelcomeTiles();
  renderSceneBar();
  renderStyleBar();
  updateStatus();
  const mAddBtn = document.getElementById('ck-m-add');
  if (mAddBtn) mAddBtn.addEventListener('click', openAdd);
  if (MOBILE()) renderMobile();

  load();
})();
