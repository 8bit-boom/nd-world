/* Sheet pages: the Sheet tab's strip of pages (characters/_sheet_pages_nav.html).
 * Blocks of the sheet carry data-sheet-page="<id>"; this shows the chosen page's blocks
 * and hides the rest, remembers the last page per character in localStorage, offers an
 * "All" view (the old long scroll) and expands everything while printing. Nothing here
 * touches form fields: hidden blocks stay in the DOM, so a Save still submits them all. */
(function () {
  'use strict';
  var nav = document.getElementById('sheet-pages');
  if (!nav) return;
  var root = nav.closest('.cs, .cx');
  if (!root) return;
  var tabs = Array.prototype.slice.call(nav.querySelectorAll('[data-sp]'));
  var ids = tabs.map(function (t) { return t.getAttribute('data-sp'); });
  var storeKey = 'ndSheetPage:' + (nav.getAttribute('data-key') || location.pathname);
  var current = ids[0];

  function read() { try { return localStorage.getItem(storeKey); } catch (e) { return null; } }
  function write(v) { try { localStorage.setItem(storeKey, v); } catch (e) { /* private mode: fine */ } }

  function show(id, remember) {
    if (ids.indexOf(id) < 0) id = ids[0];
    current = id;
    root.classList.add('sheet-paged');
    root.classList.toggle('show-all', id === 'all');
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-sp') === id;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-sheet-page]'), function (el) {
      el.classList.toggle('page-on', el.getAttribute('data-sheet-page') === id);
    });
    if (remember) write(id);
  }

  nav.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-sp]');
    if (t) show(t.getAttribute('data-sp'), true);
  });

  // Arrow keys move between pages (the usual tablist behaviour).
  nav.addEventListener('keydown', function (ev) {
    var step = ev.key === 'ArrowRight' ? 1 : ev.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    var next = tabs[(ids.indexOf(current) + step + tabs.length) % tabs.length];
    ev.preventDefault();
    show(next.getAttribute('data-sp'), true);
    next.focus();
  });

  // Printing / saving as PDF must not drop the pages that happen to be hidden.
  window.addEventListener('beforeprint', function () { root.classList.add('show-all'); });
  window.addEventListener('afterprint', function () { root.classList.toggle('show-all', current === 'all'); });

  show(read() || ids[0], false);
})();
