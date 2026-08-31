// GM/player command palette (Ctrl-K / Cmd-K): fuzzy jump to any page in the
// nav plus any entity in the active world, without reaching for the mouse.
// Self-contained by design — injects its own DOM/styles so it can ship as
// one file included from base.html, using the app's existing CSS variables
// so it follows the active theme/world accent.
// Entity results come from /api/entities/picker (player-safe and
// visibility-filtered server-side, so a player's palette only ever offers
// entities they can already see); fetched once on first open, not on page
// load, so pages that never open the palette pay nothing.
(function () {
  var overlay = null, input = null, listEl = null, items = [], selected = 0;
  var navIndex = null, entityIndex = null;

  function buildNavIndex() {
    var out = [];
    document.querySelectorAll('#nav-kinds a, .topbar a.logo, .search-form').forEach(function (a) {
      if (a.classList.contains('search-form')) return;
      var label = a.textContent.replace(/\s+/g, ' ').trim();
      var href = a.getAttribute('href');
      if (label && href && href !== '#') out.push({ label: label, href: href, sub: 'page' });
    });
    return out;
  }

  function loadEntities(cb) {
    if (entityIndex) return cb();
    fetch('/api/entities/picker').then(function (r) {
      return r.ok ? r.json() : { entities: [] };
    }).then(function (d) {
      entityIndex = (d.entities || []).map(function (e) {
        return { label: e.name, href: '/entity/' + e.id, sub: e.folder ? e.folder : e.kind };
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
    overlay.style.cssText = 'display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);padding-top:12vh;';
    overlay.innerHTML =
      '<div style="max-width:560px;margin:0 auto;background:var(--bg2,#222);border:1px solid var(--border,#444);border-radius:8px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.5)">' +
      '<input id="nd-palette-input" placeholder="Jump to page or entity…" autocomplete="off" spellcheck="false" ' +
      'style="width:100%;box-sizing:border-box;background:var(--bg3,#2a2a2a);border:none;border-bottom:1px solid var(--border,#444);color:var(--text,#eee);padding:.8rem 1rem;font-size:1rem;font-family:var(--font,inherit);outline:none"/>' +
      '<div id="nd-palette-list" role="listbox" style="max-height:50vh;overflow-y:auto"></div>' +
      '<div style="padding:.4rem .9rem;font-size:.7rem;color:var(--text-dim,#888);border-top:1px solid var(--border,#444)">↑↓ navigate · ↵ open · esc close</div>' +
      '</div>';
    document.body.appendChild(overlay);
    input = overlay.querySelector('#nd-palette-input');
    listEl = overlay.querySelector('#nd-palette-list');
    input.addEventListener('input', render);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); go(selected); }
    });
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(); });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  }

  function render() {
    var q = input.value.trim().toLowerCase();
    var pool = (navIndex || []).concat(entityIndex || []);
    items = !q ? pool.slice(0, 12) : pool.filter(function (it) {
      return it.label.toLowerCase().indexOf(q) !== -1 ||
             (it.sub || '').toLowerCase().indexOf(q) !== -1;
    }).slice(0, 12);
    selected = 0;
    listEl.innerHTML = items.map(function (it, i) {
      return '<div role="option" data-i="' + i + '" style="display:flex;justify-content:space-between;gap:1rem;padding:.55rem 1rem;cursor:pointer;font-size:.9rem;' +
        (i === 0 ? 'background:var(--bg3,#333);' : '') + 'color:var(--text,#eee)">' +
        '<span>' + esc(it.label) + '</span><span style="color:var(--text-dim,#888);font-size:.75rem">' + esc(it.sub || '') + '</span></div>';
    }).join('') || '<div style="padding:.8rem 1rem;color:var(--text-dim,#888);font-size:.85rem">No matches</div>';
    listEl.querySelectorAll('[data-i]').forEach(function (el) {
      el.addEventListener('click', function () { go(parseInt(el.dataset.i, 10)); });
      el.addEventListener('mousemove', function () { highlight(parseInt(el.dataset.i, 10)); });
    });
  }

  function highlight(i) {
    selected = i;
    listEl.querySelectorAll('[data-i]').forEach(function (el, j) {
      el.style.background = j === i ? 'var(--bg3,#333)' : 'transparent';
    });
  }

  function move(d) {
    if (!items.length) return;
    highlight((selected + d + items.length) % items.length);
  }

  function go(i) {
    var it = items[i];
    if (it) location.href = it.href;
    close();
  }

  function open() {
    ensureDom();
    navIndex = navIndex || buildNavIndex();
    loadEntities(function () {
      overlay.style.display = 'block';
      input.value = '';
      render();
      input.focus();
    });
  }

  function close() {
    if (overlay) overlay.style.display = 'none';
  }

  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (overlay && overlay.style.display === 'block') close(); else open();
    } else if (e.key === 'Escape' && overlay && overlay.style.display === 'block') {
      close();
    }
  });
})();
