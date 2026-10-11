/* Copy and paste for media, on every page.
 *
 *   Ctrl+V  - a copied picture / screenshot / files go to the page's own upload field: the visible <input type=file> that accepts what was
 *             pasted (sets its files and fires "change", exactly like picking them; a page with a custom paste handler - the session media
 *             panel - keeps priority and this stays out of the way). Never while typing in a text field.
 *   Ctrl+C  - with nothing selected, copies the media under the pointer (or the focused card): a picture is copied AS a picture (paste it
 *             into another program, or into an upload field here); audio / video / an /uploads link is copied as its address, which the
 *             session media panel accepts with Ctrl+V.
 *
 * Pure helpers (acceptMatches, pastedName, addressOf, kindOfPath) are exported for the Node tests; the rest needs a browser.
 */
(function (root) {
  'use strict';

  // ── pure helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
  function extOf(name) { var m = /\.[^./\\]+$/.exec(String(name || '')); return m ? m[0].toLowerCase() : ''; }

  /** Does an <input accept="..."> take this file? Empty accept takes everything; tokens are ".ext", "type/sub" or "type/*". */
  function acceptMatches(accept, name, type) {
    var toks = String(accept || '').split(',').map(function (t) { return t.trim().toLowerCase(); }).filter(Boolean);
    if (!toks.length) return true;
    var ext = extOf(name), mime = String(type || '').toLowerCase();
    return toks.some(function (t) {
      if (t.charAt(0) === '.') return ext === t;
      if (t.slice(-2) === '/*') return mime.indexOf(t.slice(0, -1)) === 0;
      return mime === t;
    });
  }

  var GENERIC = /^(image|blob|untitled)(\s*\(\d+\))?\.\w+$/i;
  /** A screenshot arrives as "image.png": give it a name that says what and when; real file names are kept. */
  function pastedName(name, type, when, index, count) {
    if (name && !GENERIC.test(name)) return name;
    var t = String(type || ''), kind = t.indexOf('video/') === 0 ? 'video' : t.indexOf('audio/') === 0 ? 'audio' : 'picture';
    var ext = (t.split('/')[1] || 'png').split(';')[0].split('+')[0].replace('jpeg', 'jpg') || 'png';
    var stamp = when.toISOString().slice(0, 16).replace('T', ' ').replace(':', '-');
    return 'Pasted ' + kind + ' ' + stamp + (count > 1 ? ' ' + (index + 1) : '') + '.' + ext;
  }

  /** "/uploads/audio/x.mp3" (bare, or with this site's origin in front, ?query ignored) -> {path, kind}; anything else -> null. */
  function addressOf(text, origin) {
    var t = String(text || '').trim();
    if (!t || /\s/.test(t)) return null;
    if (origin && t.indexOf(origin) === 0) t = t.slice(origin.length);
    var m = /^(\/uploads\/[^?#]+)(?:[?#].*)?$/.exec(t);
    if (!m) return null;
    var path = m[1], ext = extOf(path);
    var kind = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif'].indexOf(ext) >= 0 ? 'image'
      : ['.mp4', '.m4v', '.webm', '.ogv', '.mov', '.mkv', '.avi', '.mpg', '.mpeg'].indexOf(ext) >= 0 && /^\/uploads\/video\//.test(path) ? 'video'
      : ['.mp3', '.ogg', '.oga', '.wav', '.m4a', '.flac', '.opus', '.aac', '.webm'].indexOf(ext) >= 0 && /^\/uploads\/audio\//.test(path) ? 'audio' : '';
    if (!kind) return null;
    try { path = decodeURIComponent(path); } catch (e) { /* keep as is */ }
    return { path: path, kind: kind };
  }

  var api = { acceptMatches: acceptMatches, pastedName: pastedName, addressOf: addressOf };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }
  if (typeof document === 'undefined') return;
  root.ndClipboard = api;

  // ── browser part ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
  var toastEl = null, toastTimer = 0, lastHover = null;

  function toast(text) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.setAttribute('role', 'status'); toastEl.setAttribute('aria-live', 'polite');
      toastEl.style.cssText = 'position:fixed;left:50%;bottom:calc(70px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:2147483000;max-width:min(92vw,520px);' +
        'background:#10131c;color:#e6e9ef;border:1px solid #27e0c3;border-radius:8px;padding:.55rem .85rem;font:13px/1.35 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.5);pointer-events:none;';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text; toastEl.style.display = 'block';
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toastEl.style.display = 'none'; }, 4200);
  }

  function isEditable(el) {
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && !(el.tagName === 'INPUT' && /^(file|checkbox|radio|button|submit|range)$/.test(el.type)));
  }
  function rendered(el) { return !!el && !!(el.getClientRects && el.getClientRects().length); }
  function labelOf(input) {
    var l = (input.labels && input.labels[0] && input.labels[0].textContent) || input.getAttribute('aria-label') || input.title || '';
    l = String(l).replace(/\s+/g, ' ').trim();
    if (!l) {                                                                // else the heading of the box the field sits in ("Upload Clips")
      for (var el = input.parentElement, up = 0; el && up < 4 && !l; el = el.parentElement, up++) {
        var h = Array.prototype.find.call(el.children, function (c) { return /^(H[1-4]|LEGEND)$/.test(c.tagName); });
        if (h) l = h.textContent.replace(/[^\w\s&'’-]/g, ' ').replace(/\s+/g, ' ').trim();
      }
    }
    return l ? l.slice(0, 40) : (input.name ? input.name : 'the upload field');
  }
  /** The button that starts the upload next to a field ("Upload" / a submit button), so a paste can leave focus on it. */
  function uploadButtonFor(input) {
    var box = input.closest('form') || input.parentElement;
    return box && (box.querySelector('button[type=submit], input[type=submit]') || box.querySelector('button[id*="upload" i], button.btn-save'));
  }
  // ---- paste ----
  function candidates(files) {
    return Array.prototype.filter.call(document.querySelectorAll('input[type=file]'), function (inp) {
      if (inp.disabled || inp.webkitdirectory || inp.getAttribute('data-nd-paste') === 'off') return false;
      if (!(rendered(inp) || rendered(inp.parentElement) || rendered(inp.closest('label,form,div')))) return false;
      return files.some(function (f) { return acceptMatches(inp.accept, f.name, f.type); });
    });
  }
  function nearest(cands, from) {
    for (var el = from; el && el !== document.body; el = el.parentElement) {
      var inside = cands.filter(function (c) { return el.contains(c); });
      if (inside.length === 1) return inside[0];
      if (inside.length > 1) return null;
    }
    return null;
  }
  function deliver(files) {
    var cands = candidates(files);
    if (!cands.length) {
      if (document.querySelector('input[type=file]')) toast('Nothing on this page takes that kind of file.');
      return;
    }
    var input = cands.length === 1 ? cands[0] : (nearest(cands, document.activeElement) || nearest(cands, lastHover));
    if (!input) {
      toast('This page has several upload fields (' + cands.slice(0, 3).map(labelOf).join(', ') + '). Click next to the one you want, then paste again.');
      return;
    }
    var take = files.filter(function (f) { return acceptMatches(input.accept, f.name, f.type); });
    if (!input.multiple) take = take.slice(0, 1);
    var now = new Date(), dt = new DataTransfer();
    take.forEach(function (f, i) { dt.items.add(new File([f], pastedName(f.name, f.type, now, i, take.length), { type: f.type })); });
    input.files = dt.files;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    try { input.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { /* old browser */ }
    var box = input.closest('[data-dropzone],label,form') || input, old = box.style.outline;
    box.style.outline = '2px solid #27e0c3'; setTimeout(function () { box.style.outline = old; }, 1200);
    var go = uploadButtonFor(input);
    if (go) { try { go.focus({ preventScroll: true }); } catch (e) { /* not focusable */ } }
    toast('Pasted ' + take.length + ' file' + (take.length === 1 ? '' : 's') + ' into “' + labelOf(input) + '”' + (go ? ' — press Enter (or the highlighted button) to upload.' : '.'));
  }
  document.addEventListener('paste', function (e) {
    var cd = e.clipboardData;
    if (!cd || isEditable(e.target)) return;
    var files = Array.prototype.slice.call(cd.files || []);                 // read now: the clipboard is closed once this handler returns
    if (!files.length) return;
    setTimeout(function () { if (!e.defaultPrevented) deliver(files); }, 0); // after any page-specific handler has had its say
  });

  // ---- copy ----
  document.addEventListener('mouseover', function (e) { lastHover = e.target; }, true);
  function subjectAt(el) {
    for (var n = el; n && n !== document.body; n = n.parentElement) {
      var u = n.getAttribute && n.getAttribute('data-nd-copy-url');
      if (u) return { kind: addressOf(u, location.origin) ? addressOf(u, location.origin).kind : 'image', url: u, name: n.getAttribute('data-nd-copy-name') || '' };
      if (n.tagName === 'IMG' && (n.currentSrc || n.src) && n.naturalWidth > 48) return { kind: 'image', url: n.getAttribute('data-full') || n.currentSrc || n.src, name: n.alt || '' };
      if (n.tagName === 'VIDEO' || n.tagName === 'AUDIO') {
        var src = n.currentSrc || n.src || (n.querySelector('source') && n.querySelector('source').src);
        if (src) return { kind: n.tagName.toLowerCase(), url: src, name: '' };
      }
      if (n.tagName === 'A' && /^\/uploads\//.test(n.getAttribute('href') || '')) return { kind: (addressOf(n.getAttribute('href'), '') || {}).kind || 'file', url: n.getAttribute('href'), name: (n.textContent || '').trim().slice(0, 40) };
    }
    return null;
  }
  function absolute(url) { try { return new URL(url, location.href).href; } catch (e) { return url; } }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea'); ta.value = text; ta.style.cssText = 'position:fixed;opacity:0;left:-999px';
      document.body.appendChild(ta); ta.select();
      var ok = false; try { ok = document.execCommand('copy'); } catch (e) { /* refused */ }
      document.body.removeChild(ta); ok ? resolve() : reject(new Error('copy refused'));
    });
  }
  function toPng(blob) {
    if (blob.type === 'image/png') return Promise.resolve(blob);
    return createImageBitmap(blob).then(function (bmp) {
      var c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
      c.getContext('2d').drawImage(bmp, 0, 0);
      return new Promise(function (res, rej) { c.toBlob(function (b) { b ? res(b) : rej(new Error('no png')); }, 'image/png'); });
    });
  }
  function copyPicture(url) {
    var abs = absolute(url);
    var png = fetch(abs, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); }).then(toPng);
    // Both flavours in one clipboard item: a picture for other programs and upload fields, and the address for this app's "paste a copied
    // picture" (which then attaches the existing file instead of uploading a second copy).
    return navigator.clipboard.write([new ClipboardItem({ 'image/png': png, 'text/plain': new Blob([abs], { type: 'text/plain' }) })]);
  }
  function copyNow(subject) {
    var what = subject.kind === 'image' ? 'picture' : subject.kind === 'file' ? 'link' : subject.kind + ' link';
    var done = function () { toast('Copied the ' + what + (subject.name ? ' “' + subject.name.slice(0, 30) + '”' : '') + (subject.kind === 'image' ? '.' : ' — press Ctrl+V on a session page to attach it.')); };
    var linkOnly = function () { copyText(absolute(subject.url)).then(function () { toast('Copied the picture’s address (this browser cannot copy the picture itself).'); }, function () { toast('Could not copy.'); }); };
    if (subject.kind === 'image' && window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
      copyPicture(subject.url).then(done, linkOnly);
    } else if (subject.kind === 'image') {
      linkOnly();
    } else {
      copyText(absolute(subject.url)).then(done, function () { toast('Could not copy.'); });
    }
  }
  document.addEventListener('keydown', function (e) {
    if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey || (e.key || '').toLowerCase() !== 'c' || e.defaultPrevented) return;
    if (isEditable(e.target) || isEditable(document.activeElement)) return;
    var sel = window.getSelection && String(window.getSelection());
    if (sel && sel.trim()) return;                                           // a text selection copies as usual
    var subject = subjectAt(lastHover) || subjectAt(document.activeElement);
    if (!subject) return;
    e.preventDefault();
    copyNow(subject);
  });
})(typeof window !== 'undefined' ? window : this);
