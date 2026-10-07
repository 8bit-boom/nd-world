// Second screen (GM only) — send an image or some text to the /display window on another monitor.
// Server side: app/routers/display.py. This file is self-contained (like nd-live.js / nd-poll.js) and
// is loaded by base.html for GMs on normal (non-embed) pages.
//
//   window.ndStage.showImage(url, title, caption)   window.ndStage.showText(text, title)
//   window.ndStage.showMap(mapSlug, title)         window.ndStage.showEntity(id, mode)             window.ndStage.clear()   window.ndStage.openDisplay()
//
// What the GM gets without touching any template: a 📺 button (bottom-left) with the controls and a
// history of what was shown; a "send to screen" button that appears over any image on hover; the same
// button inside the lightbox, next to a "👥 Send to players" one (the world's Spotlight: app/routers/gallery.py); and a
// "show text" button next to any text selection. An image clicked inside a cockpit window reaches that lightbox through
// nd-lightbox-bridge.js.
(function () {
  'use strict';
  if (window.ndStage) return;
  var slug = document.body ? document.body.dataset.worldSlug : '';
  if (!slug) return;
  var W = '?w=' + encodeURIComponent(slug);
  var bc = null;
  try { bc = new BroadcastChannel('nd-stage-' + slug); } catch (e) { /* the display polls as a fallback */ }

  function el(tag, props, kids) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === 'text') n.textContent = props[k]; else if (k === 'class') n.className = props[k];
      else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), props[k]); else n.setAttribute(k, props[k]);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  // ── styles (self-contained) ───────────────────────────────────────────────
  var css = el('style', { text:
    '#nd-stage-fab{position:fixed;left:14px;bottom:var(--nd-fab-bottom,14px);z-index:950;width:42px;height:42px;border-radius:50%;border:1px solid var(--border,#444);background:var(--bg2,#181820);color:var(--text,#eee);font-size:1.15rem;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.5)}' +
    '#nd-stage-fab:hover,#nd-stage-fab.open{border-color:var(--neon,#0ff)}' +
    '#nd-stage-panel{position:fixed;left:14px;bottom:calc(var(--nd-fab-bottom,14px) + 50px);z-index:951;width:min(340px,calc(100vw - 28px));max-height:70vh;overflow:auto;background:var(--bg2,#181820);border:1px solid var(--border,#444);border-radius:8px;padding:.8rem;box-shadow:0 6px 24px rgba(0,0,0,.6);font-size:.82rem;display:none}' +
    '#nd-stage-panel.open{display:block}' +
    '#nd-stage-panel h3{margin:0 0 .5rem;font-size:.9rem;color:var(--neon,#0ff)}' +
    '#nd-stage-panel button,#nd-stage-panel select{background:var(--bg3,#22222c);color:var(--text,#eee);border:1px solid var(--border,#444);border-radius:4px;padding:.4rem .6rem;font:inherit;cursor:pointer;min-height:36px}' +
    '#nd-stage-panel button:hover{border-color:var(--neon,#0ff)}' +
    '.nd-stage-row{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.5rem;align-items:center}' +
    '.nd-stage-now{color:var(--text-dim,#999);margin:.2rem 0 .5rem}' +
    '.nd-stage-recent{display:grid;grid-template-columns:repeat(auto-fill,minmax(72px,1fr));gap:.4rem}' +
    '.nd-stage-recent button{padding:0;height:60px;overflow:hidden;position:relative;text-align:left;line-height:1.2}' +
    '.nd-stage-recent img{width:100%;height:100%;object-fit:cover;display:block}' +
    '.nd-stage-recent span{display:block;padding:.25rem;font-size:.68rem;color:var(--text-dim,#999)}' +
    '.nd-stage-tip{color:var(--text-dim,#999);font-size:.72rem;margin-top:.5rem;line-height:1.4}' +
    '.nd-stage-float{position:fixed;z-index:960;background:var(--neon,#0ff);color:#000;border:none;border-radius:4px;padding:.35rem .6rem;font:600 .78rem system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.6);display:none}' +
    '#nd-stage-toast{position:fixed;left:14px;bottom:66px;z-index:970;background:#111;color:#eee;border:1px solid var(--neon,#0ff);border-radius:6px;padding:.45rem .75rem;font:.8rem system-ui,sans-serif;opacity:0;transition:opacity .25s;pointer-events:none;max-width:70vw}' +
    '#nd-stage-toast.on{opacity:1}' +
    '#nd-lightbox-actions{position:fixed;top:14px;right:70px;z-index:10001;display:flex;gap:.5rem;flex-wrap:wrap;justify-content:flex-end;max-width:calc(100vw - 90px)}' +
    '#nd-lightbox-actions .nd-stage-float{position:static;display:block;min-height:36px;padding:.4rem .75rem}' +
    '#nd-spot-lightbtn.on{background:#ff2d78;color:#fff}' });
  document.head.appendChild(css);

  // ── API ───────────────────────────────────────────────────────────────────
  var toastTimer = null;
  function toast(msg) {
    var t = document.getElementById('nd-stage-toast');
    if (!t) { t = el('div', { id: 'nd-stage-toast', role: 'status' }); document.body.appendChild(t); }
    t.textContent = msg; t.classList.add('on');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.classList.remove('on'); }, 2200);
  }
  function send(path, body, label) {
    return fetch(path + W, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; }); })
      .then(function (d) { if (bc) bc.postMessage(d.seq); toast(label || '📺 Sent to the second screen'); refreshPanel(d); return d; })
      .catch(function (e) { toast('📺 ' + e.message); });
  }
  function toPath(src) {
    try { var u = new URL(src, location.href); return u.origin === location.origin ? u.pathname + u.search : u.href; }
    catch (e) { return src; }
  }
  var api = {
    showImage: function (url, title, caption) { return send('/api/display/show', { kind: 'image', url: toPath(url), title: title || '', caption: caption || '' }, '📺 ' + (title || 'Image') + ' → second screen'); },
    showText: function (text, title) { return send('/api/display/show', { kind: 'text', text: text, title: title || '' }, '📺 Text → second screen'); },
    showMap: function (mapSlug, title) { return send('/api/display/show', { kind: 'map', slug: mapSlug }, '📺 ' + (title || 'Map') + ' → second screen'); },
    showEntity: function (id, mode) { return send('/api/display/show', { kind: 'entity', entity_id: id, mode: mode || '' }, '📺 Sent to the second screen'); },
    clear: function () { return send('/api/display/clear', {}, '📺 Screen blanked'); },
    openDisplay: openDisplay
  };
  window.ndStage = api;

  // ── opening the display on the other monitor ──────────────────────────────
  // The window is opened SYNCHRONOUSLY inside the click (a pop-up opened after an `await` loses the
  // click's user activation and gets blocked), placed where the monitor probably is — the side the GM
  // chose — and then, where the browser can tell us (Chromium's Window Management API), moved onto the
  // real second monitor.
  function openDisplay() {
    var url = '/display' + W;
    var side = 'right'; try { side = localStorage.getItem('nd_stage_side') || 'right'; } catch (e) {}
    var l = screen.availLeft || 0, t = screen.availTop || 0, sw = screen.availWidth, sh = screen.availHeight;
    var left = side === 'right' ? l + sw + 20 : side === 'left' ? l - sw + 20 : l + 20;
    var top = side === 'below' ? t + sh + 20 : side === 'above' ? t - sh + 20 : t + 20;
    var w = window.open(url, 'nd-display', 'popup=yes,left=' + left + ',top=' + top + ',width=1280,height=720');
    if (!w) { toast('📺 Pop-up blocked — allow pop-ups for this site, then try again'); return; }
    try { w.focus(); } catch (e) {}
    toast('📺 Display opened — click it once to go fullscreen');
    if (window.getScreenDetails) {
      window.getScreenDetails().then(function (d) {
        var other = d.screens.filter(function (s) { return s !== d.currentScreen; })[0];
        if (other && w && !w.closed) { try { w.moveTo(other.availLeft, other.availTop); w.resizeTo(other.availWidth, other.availHeight); } catch (e) {} }
      }).catch(function () { /* permission denied: the side guess stands */ });
    }
  }

  // ── the floating control + history ────────────────────────────────────────
  var fab = el('button', { id: 'nd-stage-fab', type: 'button', title: 'Second screen', 'aria-label': 'Second screen controls', text: '📺' });
  var panel = el('div', { id: 'nd-stage-panel', role: 'dialog', 'aria-label': 'Second screen' });
  var nowEl = el('div', { class: 'nd-stage-now', text: 'Nothing on the screen.' });
  var recentEl = el('div', { class: 'nd-stage-recent' });
  var sideSel = el('select', { 'aria-label': 'Where is the second monitor?', onchange: function () { try { localStorage.setItem('nd_stage_side', sideSel.value); } catch (e) {} } },
    [['right', 'Monitor is to the right'], ['left', 'Monitor is to the left'], ['above', 'Monitor is above'], ['below', 'Monitor is below']]
      .map(function (o) { return el('option', { value: o[0], text: o[1] }); }));
  try { sideSel.value = localStorage.getItem('nd_stage_side') || 'right'; } catch (e) {}
  panel.appendChild(el('h3', { text: '📺 Second screen' }));
  panel.appendChild(el('div', { class: 'nd-stage-row' }, [
    el('button', { type: 'button', text: 'Open on 2nd monitor', onclick: openDisplay }),
    el('button', { type: 'button', text: 'Blank', onclick: function () { api.clear(); } })]));
  panel.appendChild(nowEl);
  panel.appendChild(el('div', { text: 'Recently shown', style: 'color:var(--text-dim,#999);font-size:.72rem;margin-bottom:.3rem' }));
  panel.appendChild(recentEl);
  panel.appendChild(el('div', { class: 'nd-stage-row', style: 'margin-top:.6rem' }, [sideSel]));
  panel.appendChild(el('div', { class: 'nd-stage-tip', text: 'Hover any image (or open it full size) and press "📺 Send to screen". Select any text and press "📺 Show text". On the display window: click for fullscreen, scroll to zoom.' }));
  document.body.appendChild(fab); document.body.appendChild(panel);

  function refreshPanel(state) {
    if (!state) return;
    nowEl.textContent = state.current ? ('On screen: ' + (state.current.title || (state.current.kind === 'image' ? 'image' : 'text card'))) : 'Nothing on the screen.';
    recentEl.textContent = '';
    (state.recent || []).forEach(function (item) {
      var b = el('button', { type: 'button', title: item.title || '' }, [
        item.kind === 'image' ? el('img', { src: item.url, alt: item.title || '', loading: 'lazy' }) : el('span', { text: '📝 ' + (item.title || 'Text') })]);
      b.addEventListener('click', function () {
        if (item.kind === 'image') api.showImage(item.url, item.title, item.caption);
        else if (item.source && item.source.type === 'entity') api.showEntity(item.source.id, 'text');
        else api.showText(item.html ? new DOMParser().parseFromString(item.html, 'text/html').body.textContent : '', item.title);
      });
      recentEl.appendChild(b);
    });
  }
  function loadPanel() { fetch('/api/display/state' + W, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(refreshPanel).catch(function () {}); }
  fab.addEventListener('click', function (e) { e.stopPropagation(); var open = panel.classList.toggle('open'); fab.classList.toggle('open', open); if (open) loadPanel(); });
  document.addEventListener('click', function (e) { if (!panel.contains(e.target) && e.target !== fab) { panel.classList.remove('open'); fab.classList.remove('open'); } });

  // ── "send to screen" over any image ───────────────────────────────────────
  var imgBtn = el('button', { type: 'button', id: 'nd-stage-imgbtn', class: 'nd-stage-float', text: '📺 Send to screen' });
  document.body.appendChild(imgBtn);
  var hoverImg = null, hideTimer = null;
  function qualifies(img) {
    if (!img || img.tagName !== 'IMG' || !img.currentSrc && !img.src) return false;
    var r = img.getBoundingClientRect();
    if (r.width < 110 || r.height < 70) return false;
    if (img.closest('#nd-stage-panel, #lightbox-overlay, nav, .topbar, header.topbar, [data-nd-stage="off"], .nd-embed')) return false;
    return true;
  }
  function fullSrc(img) {
    var a = img.closest('a[href]');
    if (a && /\.(png|jpe?g|webp|gif|avif)(\?|#|$)/i.test(a.getAttribute('href'))) return a.getAttribute('href');
    return img.getAttribute('data-full') || img.getAttribute('data-full-src') || img.currentSrc || img.src;
  }
  function titleFor(img) {
    var fig = img.closest('figure'), cap = fig && fig.querySelector('figcaption');
    var h = document.querySelector('main h1, h1');
    return (img.alt || img.title || (cap && cap.textContent) || (h && h.textContent) || '').trim().slice(0, 200);
  }
  function placeBtn(img) {
    var r = img.getBoundingClientRect();
    imgBtn.style.display = 'block';
    imgBtn.style.top = Math.max(6, r.top + 8) + 'px';
    imgBtn.style.left = Math.max(6, Math.min(window.innerWidth - imgBtn.offsetWidth - 6, r.right - imgBtn.offsetWidth - 8)) + 'px';
  }
  document.addEventListener('mouseover', function (e) {
    var img = e.target && e.target.tagName === 'IMG' ? e.target : null;
    if (img && qualifies(img)) { clearTimeout(hideTimer); hoverImg = img; placeBtn(img); }
    else if (e.target === imgBtn) { clearTimeout(hideTimer); }
  });
  document.addEventListener('mouseout', function (e) {
    if (e.target === hoverImg || e.target === imgBtn) { hideTimer = setTimeout(function () { imgBtn.style.display = 'none'; hoverImg = null; }, 350); }
  });
  window.addEventListener('scroll', function () { imgBtn.style.display = 'none'; }, { passive: true });
  imgBtn.addEventListener('click', function (e) {
    e.preventDefault(); e.stopPropagation();
    if (hoverImg) api.showImage(fullSrc(hoverImg), titleFor(hoverImg));
    imgBtn.style.display = 'none';
  });

  // ── the lightbox: send the image to the second screen, or to the players ───────────────
  var lb = document.getElementById('lightbox-overlay'), lbImg = document.getElementById('lightbox-img');
  if (lb && lbImg) {
    var bar = el('div', { id: 'nd-lightbox-actions' });
    var lbBtn = el('button', { type: 'button', id: 'nd-stage-lightbtn', class: 'nd-stage-float', text: '📺 Send to screen' });
    lbBtn.addEventListener('click', function (e) { e.stopPropagation(); if (lbImg.src) api.showImage(lbImg.src, lbImg.alt || titleFor(lbImg)); });
    var spBtn = el('button', { type: 'button', id: 'nd-spot-lightbtn', class: 'nd-stage-float', text: '👥 Send to players' });
    bar.appendChild(lbBtn); bar.appendChild(spBtn); lb.appendChild(bar);

    // The players' popup is the world's Spotlight: ONE image at a time, shown to everyone until it is cleared.
    var sending = false;                                   // is THIS image what the players are being shown?
    function spotState() {
      return fetch('/api/spotlight' + W, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).catch(function () { return {}; });
    }
    function paintSpot(on) {
      sending = !!on;
      spBtn.textContent = sending ? '⏹ Stop showing to players' : '👥 Send to players';
      spBtn.classList.toggle('on', sending);
    }
    function refreshSpot() {
      var here = lbImg.src ? toPath(lbImg.src).split('?')[0] : '';
      spotState().then(function (d) { paintSpot(!!(here && d && d.image_url && toPath(d.image_url).split('?')[0] === here)); });
    }
    spBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (!lbImg.src) return;
      var clearing = sending;
      fetch((clearing ? '/images/spotlight/clear' : '/images/spotlight') + W, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(clearing ? {} : { url: toPath(lbImg.src).split('?')[0] }) })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, status: r.status, d: d }; }); })
        .then(function (res) {
          if (!res.ok) {
            // the server only broadcasts images that belong to this world's gallery, portraits and pages
            toast(res.status === 404 ? '👥 That picture is not in this world\'s gallery, so it can\'t be sent to the players'
                                      : '👥 ' + (res.d.detail || ('HTTP ' + res.status)));
            return;
          }
          if (window.ndSpotlightSeen && res.d.version != null) window.ndSpotlightSeen(res.d.version);   // not on my own screen again
          paintSpot(!clearing);
          toast(clearing ? '👥 Players\' popup closed' : '👥 Sent to all players');
        })
        .catch(function (err) { toast('👥 ' + err.message); });
    });
    // every time the lightbox opens, show whether the players are already looking at this very image
    new MutationObserver(function () { if (lb.classList.contains('open')) { paintSpot(false); refreshSpot(); } })
      .observe(lb, { attributes: true, attributeFilter: ['class'] });
  }

  // ── "show text" next to a selection ───────────────────────────────────────
  var selBtn = el('button', { type: 'button', id: 'nd-stage-selbtn', class: 'nd-stage-float', text: '📺 Show text' });
  document.body.appendChild(selBtn);
  var selText = '';
  function editable(node) {
    var n = node && node.nodeType === 3 ? node.parentElement : node;
    return !!(n && n.closest && n.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"], #nd-stage-panel'));
  }
  function checkSelection() {
    var s = window.getSelection();
    var text = s ? s.toString().trim() : '';
    if (!s || s.rangeCount === 0 || text.length < 8 || editable(s.anchorNode) || editable(document.activeElement)) { selBtn.style.display = 'none'; return; }
    var r = s.getRangeAt(0).getBoundingClientRect();
    selText = text;
    selBtn.style.display = 'block';
    selBtn.style.top = Math.min(window.innerHeight - 40, r.bottom + 6) + 'px';
    selBtn.style.left = Math.max(6, Math.min(window.innerWidth - selBtn.offsetWidth - 6, r.left)) + 'px';
  }
  document.addEventListener('mouseup', function () { setTimeout(checkSelection, 10); });
  document.addEventListener('keyup', function (e) { if (e.shiftKey || e.key === 'Shift') checkSelection(); });
  document.addEventListener('selectionchange', function () { var s = window.getSelection(); if (!s || s.toString().trim().length < 8) selBtn.style.display = 'none'; });
  selBtn.addEventListener('mousedown', function (e) { e.preventDefault(); });   // keep the selection alive
  selBtn.addEventListener('click', function () { api.showText(selText, ''); selBtn.style.display = 'none'; });
})();
