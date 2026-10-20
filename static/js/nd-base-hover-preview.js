// Hover-preview popup: hover any /entity/{id} link long enough (Settings >
// Options controls whether this is on and how long) to see its full
// details without opening it in a new tab. Delegated to `document` so it
// works on links rendered anywhere — entity cards, linked-entity lists,
// search results, quick links, entity bodies — and on links added to the
// DOM later by other scripts (e.g. index.html's quick-link drag&drop),
// with no per-template wiring needed.
(function(){
  var cfg = { enabled: false, delayMs: 5000, hideDelayMs: 400, widthPx: 340, maxHeightPx: 420 };
  var hoverTimer = null;
  var hideTimer = null;
  var currentAnchor = null;
  var popupEl = null;

  function applyCfg(d) {
    cfg.enabled = !!d.enabled;
    cfg.delayMs = d.delay_ms || 5000;
    cfg.hideDelayMs = d.hide_delay_ms != null ? d.hide_delay_ms : 400;
    cfg.widthPx = d.width_px || 340;
    cfg.maxHeightPx = d.max_height_px || 420;
  }
  // base.html carries the settings (window.ND_HOVER_CFG); a page rendered without them asks /api/hover-preview/config as before
  if (window.ND_HOVER_CFG) applyCfg(window.ND_HOVER_CFG);
  else fetch('/api/hover-preview/config').then(r => r.json()).then(applyCfg).catch(() => {});

  function esc(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

  function hidePopup() {
    if (popupEl) { popupEl.remove(); popupEl = null; }
  }

  // A short grace period between the pointer leaving the anchor (or the
  // popup) and the popup actually closing, so there's time to move the
  // mouse from the link onto the popup itself without it vanishing first —
  // canceled by cancelHide() if the pointer reaches the popup, or a link,
  // before it elapses.
  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hidePopup, cfg.hideDelayMs);
  }
  function cancelHide() {
    clearTimeout(hideTimer);
  }

  function positionPopup(anchor, popup) {
    var r = anchor.getBoundingClientRect();
    document.body.appendChild(popup);
    var w = popup.offsetWidth, h = popup.offsetHeight;
    var left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    var top = r.bottom + 6;
    if (top + h > window.innerHeight - 8 && r.top - h - 6 > 0) {
      top = r.top - h - 6; // flip above if there's no room below
    }
    popup.style.left = (left + window.scrollX) + 'px';
    popup.style.top = (top + window.scrollY) + 'px';
  }

  function renderPopup(anchor, data) {
    hidePopup();
    popupEl = document.createElement('div');
    popupEl.className = 'hover-preview-popup';
    popupEl.style.width = cfg.widthPx + 'px';
    popupEl.style.maxHeight = cfg.maxHeightPx + 'px';
    popupEl.innerHTML =
      ((data.thumb_url || data.image_url) ? '<img src="' + esc(data.thumb_url || data.image_url) + '" alt="" decoding="async"/>' : '') +
      '<span class="entity-kind">' + esc(data.kind_icon) + ' ' + esc(data.kind) + '</span>' +
      (data.subtype ? '<span class="entity-sub">' + esc(data.subtype) + '</span>' : '') +
      '<h3>' + esc(data.name) + '</h3>' +
      (data.summary ? '<p class="detail-summary">' + esc(data.summary) + '</p>' : '') +
      (data.tags && data.tags.length ? '<div class="tags">' + data.tags.map(t => '<span class="tag">' + esc(t) + '</span>').join('') + '</div>' : '') +
      (data.body_html ? '<div class="prose">' + data.body_html + '</div>' : '') +
      '<a class="hp-open-link" href="/entity/' + data.id + '">Open full page →</a>';
    // Hovering the popup itself keeps it open; leaving it re-arms the same grace period.
    popupEl.addEventListener('mouseenter', cancelHide);
    popupEl.addEventListener('mouseleave', scheduleHide);
    positionPopup(anchor, popupEl);
  }

  // Shared by both pointer (mouseover/mouseout) and keyboard
  // (focusin/focusout) triggers, so a keyboard user tabbing through links
  // gets the exact same preview affordance a mouse user gets by hovering —
  // same trigger delay, same hide grace period, same popup.
  function handleAnchorEnter(a) {
    if (!cfg.enabled) return;
    if (!a || a === currentAnchor) return;
    currentAnchor = a;
    cancelHide();
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(function() {
      var m = a.getAttribute('href').match(/^\/entity\/(\d+)/);
      if (!m || currentAnchor !== a) return;
      fetch('/api/entity/' + m[1] + '/preview').then(r => r.ok ? r.json() : null).then(data => {
        if (data && currentAnchor === a) renderPopup(a, data);
      }).catch(() => {});
    }, cfg.delayMs);
  }
  function handleAnchorLeave(a) {
    if (!a || a !== currentAnchor) return;
    currentAnchor = null;
    clearTimeout(hoverTimer);
    scheduleHide();
  }

  document.addEventListener('mouseover', e => {
    handleAnchorEnter(e.target.closest('a[href^="/entity/"]'));
  });
  document.addEventListener('mouseout', e => {
    handleAnchorLeave(e.target.closest('a[href^="/entity/"]'));
  });
  document.addEventListener('focusin', e => {
    handleAnchorEnter(e.target.closest && e.target.closest('a[href^="/entity/"]'));
  });
  document.addEventListener('focusout', e => {
    handleAnchorLeave(e.target.closest && e.target.closest('a[href^="/entity/"]'));
  });
})();
