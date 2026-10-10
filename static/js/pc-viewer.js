/* In-page viewer for the player's character page: any app page opens on top of the character (full screen on a phone, a
 * large panel on a desktop) instead of replacing it, so a player never has to leave their character page.
 *
 * The page is shown in an iframe with ?embed=1 - the same chrome-less mode the cockpit windows use (base.html keeps it
 * through every link and form inside, so reading around works). Android's Back button / the browser Back close the
 * viewer first (one history entry per open), Esc closes it, and closing gives the iframe's memory back.
 *
 * ndPcViewer.open(href, title) - same-origin paths only; anything else is refused and left to normal navigation.
 * ndPcViewer.wants(anchor)     - true when a click on this link should open here.
 * ndPcViewer.close()           - close it (no-op when closed).
 */
(function () {
  'use strict';
  var root = null, frame = null, body = null, titleEl = null, popEl = null, loadEl = null, closeBtn = null;
  var openState = false, pushed = false, lastFocus = null, loads = 0;

  var FILE_LIKE = /\.(md|ics|pdf|zip|json|csv|txt|png|jpe?g|gif|webp|svg|mp3|mp4|wav|ogg|webm)(\?|#|$)/i;
  var NEVER = /^\/(logout|login|static|uploads|api)(\/|$)/;

  function normalise(href) {
    if (typeof href !== 'string' || href.charAt(0) !== '/' || href.charAt(1) === '/') return null;
    var u;
    try { u = new URL(href, location.origin); } catch (e) { return null; }
    if (u.origin !== location.origin || NEVER.test(u.pathname) || FILE_LIKE.test(u.pathname)) return null;
    return u;
  }

  function build() {
    if (root) return;
    root = document.createElement('div');
    root.className = 'pcv';
    root.hidden = true;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-label', 'Page viewer');
    var bar = document.createElement('div');
    bar.className = 'pcv-bar';
    closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'pcv-x';
    closeBtn.setAttribute('aria-label', 'Back to your character');
    closeBtn.textContent = '← Back';
    closeBtn.addEventListener('click', close);
    titleEl = document.createElement('span');
    titleEl.className = 'pcv-title';
    popEl = document.createElement('a');
    popEl.className = 'pcv-pop';
    popEl.target = '_blank';
    popEl.rel = 'noopener';
    popEl.title = 'Open in its own tab';
    popEl.setAttribute('aria-label', 'Open in its own tab');
    popEl.textContent = '↗';
    bar.append(closeBtn, titleEl, popEl);
    body = document.createElement('div');
    body.className = 'pcv-body';
    loadEl = document.createElement('div');
    loadEl.className = 'pcv-load';
    loadEl.textContent = 'Loading\u2026';
    body.append(loadEl);
    root.append(bar, body);
    document.body.appendChild(root);
    // A click on the dimmed area around the desktop panel closes it.
    root.addEventListener('mousedown', function (ev) { if (ev.target === root) close(); });
  }

  function onFrameLoad() {
    if (!openState) return;
    loads += 1;                         // every page the viewer shows after the first is one more history entry
    loadEl.hidden = true;
    try {
      var t = frame.contentDocument && frame.contentDocument.title;
      if (t && !titleEl.dataset.fixed) titleEl.textContent = t.replace(/\s+[\u2014-]\s+.*$/, '');
    } catch (e) { /* cross-origin: keep the given title */ }
  }

  function open(href, title) {
    var u = normalise(href);
    if (!u) return false;
    build();
    lastFocus = document.activeElement;
    var shown = new URL(u.href);
    shown.searchParams.set('embed', '1');
    titleEl.textContent = title || '';
    if (title) titleEl.dataset.fixed = '1'; else delete titleEl.dataset.fixed;
    popEl.href = u.pathname + u.search + u.hash;
    loadEl.hidden = false;
    // A FRESH iframe each time, its src set before it is inserted: the first navigation of a new frame replaces its
    // blank page, whereas changing the src of a frame already in the document adds a session-history entry - which is
    // what made the browser's Back button step through the viewer's pages instead of closing it.
    if (frame) frame.remove();
    loads = 0;
    frame = document.createElement('iframe');
    frame.className = 'pcv-frame';
    frame.title = 'Page';
    frame.setAttribute('allow', 'fullscreen');
    frame.addEventListener('load', onFrameLoad);
    frame.setAttribute('src', shown.pathname + shown.search + shown.hash);
    body.insertBefore(frame, loadEl);
    root.hidden = false;
    document.documentElement.classList.add('pcv-open');
    openState = true;
    if (!pushed) {
      try { history.pushState({ pcv: 1 }, '', location.href); pushed = true; } catch (e) { pushed = false; }
    }
    closeBtn.focus();
    return true;
  }

  function finishClose() {
    if (!openState) return;
    openState = false;
    root.hidden = true;
    if (frame) { frame.remove(); frame = null; }     // give the page's memory back (a phone keeps few tabs alive)
    document.documentElement.classList.remove('pcv-open');
    try { if (lastFocus && lastFocus.focus) lastFocus.focus(); } catch (e) { /* element gone */ }
  }

  function close() {
    if (!openState) return;
    if (pushed) {
      // Undo our own history entry plus every page the viewer navigated to since; the popstate handler then closes it.
      // If the browser does not answer (the count was off), close anyway.
      var steps = Math.max(1, loads);
      pushed = false;
      try {
        history.go(-steps);
        setTimeout(function () { if (openState) finishClose(); }, 700);
        return;
      } catch (e) { /* fall through */ }
    }
    finishClose();
  }

  window.addEventListener('popstate', function () {
    if (openState) { pushed = false; finishClose(); }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && openState) { ev.preventDefault(); close(); }
  });

  function wants(a) {
    if (!a || !a.getAttribute) return false;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) !== '/' || a.hasAttribute('download') || a.hasAttribute('data-no-viewer')) return false;
    var t = (a.getAttribute('target') || '').toLowerCase();
    if (t && t !== '_self') return false;
    return !!normalise(href);
  }

  window.ndPcViewer = { open: open, close: close, wants: wants, isOpen: function () { return openState; } };
})();
