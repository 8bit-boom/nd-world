// Phase-2 float panels — the "YouTube PiP for GM tooling" layer.
//
// Two openers, one shared ?embed=1 chrome-less rendering of any nd-world
// page:
//
//   ndFloat(url, {title, w, h})   — classic NAMED popup window. Survives
//     every navigation of the opener, the tab, even closing the tab
//     entirely; the GM parks the map/dice/NPC-talk on a second monitor or
//     in a corner and browses nd-world in the main window. Repeated Float
//     clicks from the same page focus the existing panel instead of
//     reloading it (see the registry below).
//
//   ndFloatPip(url, {title, w, h}) — Document Picture-in-Picture
//     (documentPictureInPicture, Chromium): the same page in a small
//     ALWAYS-ON-TOP window. Falls back to ndFloat where unsupported
//     (Firefox/Safari). Honest about its lifetime: a PiP window is owned
//     by the document that opened it, so one launched from the main tab
//     closes when you navigate away — one launched from inside a floated
//     popup (a document that never navigates) lives indefinitely. Use ⇱
//     for "browse anywhere", 📌 for "keep this on top of everything".
//
// Floated pages load with ?embed=1 (base.html hides the topbar/footer and
// page-local .nd-float-btn buttons) and are ordinary same-origin pages:
// session cookies apply, live-sync (nd-live.js) works in them, forms in
// them post back to the real routes.
(function () {
  var cascade = 0;
  // Per-document registry: repeated Float clicks from the SAME page (the
  // common case — the cockpit header, a toolbar) focus the existing panel
  // instead of reloading it. Across navigations the registry is gone and
  // window.open(name) re-finds the window by name; the browser then
  // re-navigates it (a reload — accepted, since detecting "already open"
  // cross-document would need a second window.open probe, and a blank
  // probe consumes the one-popup-per-gesture allowance and gets the real
  // window blocked).
  var registry = {};

  function withEmbed(url) {
    return url + (url.indexOf('?') > -1 ? '&' : '?') + 'embed=1';
  }

  window.ndFloat = function ndFloat(url, opts) {
    opts = opts || {};
    var w = opts.w || 480, h = opts.h || 640;
    var name = opts.name || 'nd-float-' + url;
    var existing = registry[name];
    if (existing && !existing.closed) {
      try { existing.focus(); return existing; } catch (e) { /* fell behind a blocker — reopen */ }
    }
    cascade = (cascade + 1) % 6;
    var feats = 'popup=yes,width=' + w + ',height=' + h +
      ',left=' + (120 + cascade * 32) + ',top=' + (80 + cascade * 26);
    var win = null;
    try { win = window.open(withEmbed(url), name, feats); } catch (e) { win = null; }
    if (win) {
      registry[name] = win;
      try { win.focus(); } catch (e) {}
    }
    return win;
  };

  window.ndFloatPip = function ndFloatPip(url, opts) {
    opts = opts || {};
    if (!window.documentPictureInPicture || !documentPictureInPicture.requestWindow) {
      return Promise.resolve(window.ndFloat(url, opts));
    }
    return documentPictureInPicture.requestWindow({
      width: Math.min(opts.w || 480, screen.availWidth - 40),
      height: Math.min(opts.h || 640, screen.availHeight - 40),
    }).then(function (pipWin) {
      var doc = pipWin.document;
      doc.title = opts.title || 'nd-world';
      // Copy the app's stylesheets by URL (no cssRules access needed, so
      // cross-origin font stylesheets can't throw) plus inline <style>
      // blocks (pages like schematic_view.html style themselves inline).
      document.querySelectorAll('link[rel="stylesheet"]').forEach(function (link) {
        var c = doc.createElement('link');
        c.rel = 'stylesheet';
        c.href = link.href;
        doc.head.appendChild(c);
      });
      document.querySelectorAll('style').forEach(function (st) {
        try { doc.head.appendChild(st.cloneNode(true)); } catch (e) {}
      });
      doc.body.style.margin = '0';
      doc.body.style.overflow = 'hidden';
      doc.body.classList.add('nd-embed');
      var frame = doc.createElement('iframe');
      frame.src = withEmbed(url);
      frame.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;border:0;background:#0a0a0a';
      doc.body.appendChild(frame);
      return pipWin;
    }).catch(function () {
      // User gesture expired or the browser refused — degrade to a popup.
      return window.ndFloat(url, opts);
    });
  };
})();
