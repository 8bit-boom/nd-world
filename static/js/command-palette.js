// GM/player command palette (Ctrl-K / Cmd-K): fuzzy jump to any page in the
// nav plus any entity in the active world, without reaching for the mouse.
// Self-contained by design — injects its own DOM/styles so it can ship as
// one file included from base.html, using the app's existing CSS variables
// so it follows the active theme/world accent.
// Entity results come from /api/entities/picker (player-safe and
// visibility-filtered server-side, so a player's palette only ever offers
// entities they can already see); fetched once on first open, not on page
// load, so pages that never open the palette pay nothing.
//
// A page can plug into it through window.ndPalette (the GM cockpit does): addProvider(fn) adds
// commands - fn(query) returns [{label, sub, kw, run}] - and setEntityOpener / setPageOpener make an
// entity / a nav page open somewhere other than by navigating away. A page INSIDE a frame (a cockpit
// window) forwards Ctrl-K to the nearest ancestor page that has such providers, because a palette
// confined to a 500px window is no use.
(function () {
  var overlay = null, input = null, listEl = null, items = [], selected = 0, prevFocus = null;
  var navIndex = null, entityIndex = null;
  var providers = [], entityOpener = null, pageOpener = null;

  // Phase-4 GM actions: keyboard-first access to the cockpit and the
  // float panels (nd-float.js) — "⇱ Float: Dice" from any page floats the
  // dice tray without touching the mouse. Actions run in place instead of
  // navigating (see go()).
  function buildActions() {
    var actions = [];
    // the cockpit has its own commands for all of this (a window of the dice is the same thing, and "open the cockpit" is a reload)
    if (document.getElementById('ck-wrap')) return actions;
    if (document.body && document.body.dataset.worldSlug) {
      actions.push({ label: '🎛 GM Cockpit', sub: 'session dashboard', href: '/cockpit' });
    }
    if (window.ndFloat) {
      actions.push({ label: '⇱ Float: Dice', sub: 'pop out', run: function () { ndFloat('/dice', { title: '🎲 Dice', w: 480, h: 680 }); } });
      actions.push({ label: '⇱ Float: Talk to NPCs', sub: 'pop out', run: function () { ndFloat('/npc-talk', { title: '💬 Talk to NPCs', w: 820, h: 640 }); } });
      actions.push({ label: '⇱ Float: AI Chat', sub: 'pop out', run: function () { ndFloat('/ai', { title: '🤖 AI Chat', w: 640, h: 760 }); } });
      // Float whatever the GM is looking at right now (map viewer, party
      // detail, a session page, ...) — the panel keeps live-syncing while
      // they browse elsewhere.
      var here = location.pathname + location.search;
      if (here !== '/' && here.indexOf('/logout') !== 0) {
        actions.push({ label: '⇱ Float this page', sub: 'pop out', run: function () { ndFloat(here, { title: document.title, w: 640, h: 720 }); } });
      }
    }
    return actions;
  }

  function buildNavIndex() {
    var out = [];
    document.querySelectorAll('#nav-kinds a, .topbar a.logo, .search-form').forEach(function (a) {
      if (a.classList.contains('search-form')) return;
      var label = a.textContent.replace(/\s+/g, ' ').trim();
      var href = a.getAttribute('href');
      if (label && href && href !== '#') out.push({ label: label, href: href, sub: 'page', page: true });
    });
    return out;
  }

  function loadEntities(cb) {
    if (entityIndex) return cb();
    fetch('/api/entities/picker').then(function (r) {
      return r.ok ? r.json() : { entities: [] };
    }).then(function (d) {
      entityIndex = (d.entities || []).map(function (e) {
        return { label: e.name, href: '/entity/' + e.id, sub: e.folder ? e.folder : e.kind, entity: { id: e.id, name: e.name, kind: e.kind } };
      });
      cb();
    }).catch(function () { entityIndex = []; cb(); });
  }

  function ensureDom() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Command palette');
    // above every cockpit layer (a maximised window is 99999, its dialogs 100000-100003, the lightbox 100010)
    overlay.style.cssText = 'display:none;position:fixed;inset:0;z-index:100020;background:rgba(0,0,0,.55);padding-top:12vh;';
    overlay.innerHTML =
      '<div style="max-width:560px;margin:0 auto;background:var(--bg2,#222);border:1px solid var(--border,#444);border-radius:8px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.5)">' +
      '<input id="nd-palette-input" role="combobox" aria-expanded="true" aria-controls="nd-palette-list" aria-autocomplete="list" aria-label="Command palette" ' +
      'placeholder="Jump to a page, entity or window — or type a command…" autocomplete="off" spellcheck="false" ' +
      'style="width:100%;box-sizing:border-box;background:var(--bg3,#2a2a2a);border:none;border-bottom:1px solid var(--border,#444);color:var(--text,#eee);padding:.8rem 1rem;font-size:1rem;font-family:var(--font,inherit);outline:none"/>' +
      '<div id="nd-palette-list" role="listbox" style="max-height:50vh;overflow-y:auto"></div>' +
      '<div style="padding:.4rem .9rem;font-size:.7rem;color:var(--text-dim,#888);border-top:1px solid var(--border,#444)">↑↓ navigate · ↵ open · shift+↵ open the page itself · esc close</div>' +
      '</div>';
    document.body.appendChild(overlay);
    input = overlay.querySelector('#nd-palette-input');
    listEl = overlay.querySelector('#nd-palette-list');
    input.addEventListener('input', render);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); go(selected, e.shiftKey); }
    });
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(); });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  }

  // ── ranking ───────────────────────────────────────────────────────────────────────────────────────────────────────
  // How well a query matches an item; 0 = not at all. Every word of the query has to match somewhere: at the start of the
  // label (best), at the start of a word in it, anywhere in it, in the small text / keywords, or as letters in order.
  var SEP = /[\s\-_.:/()·|,;+]/;
  function inOrder(w, text) {
    var at = 0;
    for (var i = 0; i < w.length; i++) { at = text.indexOf(w.charAt(i), at); if (at < 0) return false; at++; }
    return true;
  }
  function score(q, it) {
    var words = String(q).toLowerCase().split(/\s+/).filter(Boolean), total = 0;
    if (!words.length) return 1;
    var label = String(it.label).toLowerCase(), more = ((it.sub || '') + ' ' + (it.kw || '')).toLowerCase();
    for (var i = 0; i < words.length; i++) {
      var w = words[i], at = label.indexOf(w), s = 0;
      if (at === 0) s = 100;
      else if (at > 0 && SEP.test(label.charAt(at - 1))) s = 80;
      else if (at > 0) s = 60;
      else if (more.indexOf(w) !== -1) s = 30;
      else if (w.length >= 3 && inOrder(w, label)) s = 12;
      if (!s) return 0;
      total += s;
    }
    return total;
  }

  function gather(q) {
    var pool = [];
    providers.forEach(function (p) {
      try { (p(q) || []).forEach(function (it) { pool.push(it); }); } catch (e) { /* a broken provider must not take the palette down */ }
    });
    var own = (navIndex || []).concat(buildActions(), entityIndex || []);
    return pool.concat(own);
  }

  function rank(q, pool, limit) {
    if (!q) return pool.slice(0, limit);
    return pool.map(function (it, i) { return { it: it, s: score(q, it), i: i }; })
      .filter(function (r) { return r.s > 0; })
      .sort(function (a, b) { return b.s - a.s || a.i - b.i; })
      .slice(0, limit).map(function (r) { return r.it; });
  }

  function render() {
    var q = input.value.trim().toLowerCase();
    items = rank(q, gather(q), q ? 12 : 14);
    selected = 0;
    listEl.innerHTML = items.map(function (it, i) {
      return '<div role="option" id="nd-palette-opt-' + i + '" aria-selected="' + (i === 0 ? 'true' : 'false') + '" data-i="' + i + '" style="display:flex;justify-content:space-between;gap:1rem;padding:.55rem 1rem;cursor:pointer;font-size:.9rem;' +
        (i === 0 ? 'background:var(--bg3,#333);' : '') + 'color:var(--text,#eee)">' +
        '<span>' + esc(it.label) + '</span><span style="color:var(--text-dim,#888);font-size:.75rem;white-space:nowrap">' + esc(it.page && pageOpener ? 'as a window' : (it.sub || '')) + '</span></div>';
    }).join('') || '<div role="presentation" style="padding:.8rem 1rem;color:var(--text-dim,#888);font-size:.85rem">No matches</div>';
    input.setAttribute('aria-activedescendant', items.length ? 'nd-palette-opt-0' : '');
    listEl.querySelectorAll('[data-i]').forEach(function (el) {
      el.addEventListener('click', function (e) { go(parseInt(el.dataset.i, 10), e.shiftKey); });
      el.addEventListener('mousemove', function () { highlight(parseInt(el.dataset.i, 10)); });
    });
  }

  function highlight(i) {
    selected = i;
    listEl.querySelectorAll('[data-i]').forEach(function (el, j) {
      el.style.background = j === i ? 'var(--bg3,#333)' : 'transparent';
      el.setAttribute('aria-selected', j === i ? 'true' : 'false');
    });
    input.setAttribute('aria-activedescendant', 'nd-palette-opt-' + i);
    var cur = listEl.querySelector('[data-i="' + i + '"]');
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  }

  function move(d) {
    if (!items.length) return;
    highlight((selected + d + items.length) % items.length);
  }

  // Enter runs the item; Shift+Enter opens the page itself even where a host would open it as a window.
  function go(i, plain) {
    var it = items[i];
    close();
    if (!it) return;
    if (it.run) { it.run(); return; }
    if (!plain && it.entity && entityOpener) { entityOpener(it.entity); return; }
    if (!plain && it.page && pageOpener) { pageOpener(it); return; }
    location.href = it.href;
  }

  function isOpen() { return !!overlay && overlay.style.display === 'block'; }

  function open() {
    ensureDom();
    navIndex = navIndex || buildNavIndex();
    prevFocus = document.activeElement;
    loadEntities(function () {
      overlay.style.display = 'block';
      input.value = '';
      render();
      input.focus();
    });
  }

  function close() {
    if (!overlay || overlay.style.display === 'none') return;
    overlay.style.display = 'none';
    // back to where the keys were going (a cockpit window's page is the document's active element: its frame)
    if (prevFocus && prevFocus.focus && document.contains(prevFocus)) { try { prevFocus.focus(); } catch (e) { /* gone */ } }
    prevFocus = null;
  }

  function toggle() { if (isOpen()) close(); else open(); }

  // The nearest ancestor page whose palette has providers (the cockpit); else the top-most one that has a palette at all.
  // A cross-origin parent throws on .ndPalette - the loop simply stops there.
  function hostPalette() {
    var w = window, best = null;
    try {
      while (w.parent && w.parent !== w) {
        var pp = w.parent.ndPalette;
        if (!pp) break;
        best = pp;
        if (pp.hasProviders()) break;
        w = w.parent;
      }
    } catch (e) { /* cross-origin */ }
    return best;
  }

  function isPaletteKey(e) {
    return (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.code === 'KeyK' || e.key === 'k' || e.key === 'K');
  }

  window.ndPalette = {
    open: open, close: close, toggle: toggle, isOpen: isOpen,
    addProvider: function (fn) { if (typeof fn === 'function') providers.push(fn); },
    hasProviders: function () { return providers.length > 0; },
    setEntityOpener: function (fn) { entityOpener = typeof fn === 'function' ? fn : null; },
    setPageOpener: function (fn) { pageOpener = typeof fn === 'function' ? fn : null; },
    score: score,
    // every page the site's menu offers: [{label, href}] (the cockpit's "Page" window lists them)
    pages: function () { navIndex = navIndex || buildNavIndex(); return navIndex; },
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = window.ndPalette;

  document.addEventListener('keydown', function (e) {
    if (isPaletteKey(e)) {
      e.preventDefault();
      // inside a frame with nothing of its own to offer: the palette that does (the cockpit's) opens up there instead
      var host = providers.length ? null : hostPalette();
      if (host) host.toggle(); else toggle();
    } else if (e.key === 'Escape' && isOpen()) {
      close();
    }
  });
})();
