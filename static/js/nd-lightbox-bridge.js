// Full-screen images from inside cockpit (and floated) windows.
//
// Every cockpit window is a whole page in an iframe (?embed=1), and an iframe's lightbox is confined to that little
// window — and the GM's controls on it (📺 second screen, 👥 send to players) are deliberately not loaded in iframes.
// So in an embedded page openLightbox() hands the image UP to the window above it (a same-site postMessage), and the
// main window shows it in the real full-screen lightbox. The cockpit page can itself be embedded (the character page's
// Cockpit tab), so a window that receives such a message passes it up again until the top window opens it.
//
// Loaded by base.html right after the lightbox's own openLightbox(); the global it replaces is the one every
// onclick="openLightbox(...)" in the app already calls.
(function (root) {
  'use strict';
  var doc = root.document;
  if (!doc || !doc.body) return;
  var embedded = doc.body.classList.contains('nd-embed') && root.parent && root.parent !== root;
  var localOpen = root.openLightbox;
  var last = { src: '', at: 0 };

  function absolute(src) {
    try { return new root.URL(src, root.location.href).href; } catch (e) { return src; }
  }
  // An image is addressed by a path on this site or an http(s) URL — never javascript:, data: or a protocol-relative host.
  function imageAddress(s) { return typeof s === 'string' && /^(\/(?!\/)|https?:\/\/)/i.test(s); }

  if (embedded) {
    root.openLightbox = function (src, alt) {
      if (!src) return;
      var abs = absolute(src);
      var now = Date.now();
      // a page that opens the lightbox itself AND is caught by the click fallback below must not pop it up twice
      if (abs === last.src && now - last.at < 400) return;
      last = { src: abs, at: now };
      try {
        root.parent.postMessage({ type: 'nd-lightbox', src: abs, alt: String(alt || '').slice(0, 200) }, root.location.origin);
        return;
      } catch (e) { /* no window above to hand it to: fall back to the little frame's own lightbox */ }
      if (localOpen) localOpen(src, alt);
    };

    // A thumbnail that carries its full size (data-full, the project's convention) but has no click handler of its own.
    // Leave alone anything that already means something else: a link navigates, a button / a picker selects.
    doc.addEventListener('click', function (e) {
      var img = e.target;
      if (!img || img.tagName !== 'IMG' || e.defaultPrevented) return;
      if (!img.hasAttribute('data-full')) return;
      if (img.closest && img.closest('a[href], button, [onclick], [data-nd-lightbox="off"], #lightbox-overlay')) return;
      root.openLightbox(img.getAttribute('data-full'), img.alt || '');
    });
  }

  // The receiving end — in every document, so the message climbs through nested frames to the top window.
  root.addEventListener('message', function (e) {
    if (e.origin !== root.location.origin) return;
    var d = e.data;
    if (!d || typeof d !== 'object' || d.type !== 'nd-lightbox' || !imageAddress(d.src)) return;
    try { root.focus(); } catch (err) { /* so Escape closes it even though the click was inside a frame */ }
    root.openLightbox(d.src, typeof d.alt === 'string' ? d.alt.slice(0, 200) : '');
  });
})(typeof window !== 'undefined' ? window : globalThis);
