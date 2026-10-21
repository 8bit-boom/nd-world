"use strict";
// The lightbox's zoom (base.html's openLightbox / closeLightbox call in here).
//
// A large upload opens as its display copy (2,560 px, ?display=1 - app/imaging.py) so opening a picture stays fast. The ORIGINAL - a 4K
// upload, say - is only downloaded when it is asked for: the "Full size" button, a click / zoom past what the copy can show, or the
// "Always open at full size" box (remembered in this browser only, so it changes nothing for anyone else). It replaces the copy in place,
// same position and zoom, just sharper.
//
//   click the picture   fit <-> actual size (1 picture pixel = 1 screen pixel), at the point clicked
//   wheel / pinch       zoom around the pointer, from "fit" up to 4x actual size
//   drag                move around a zoomed picture
//   keys                F full size, 0 fit, 1 actual size, + / - zoom, Esc close
//
// The pure bit (fit / actual scales, keeping the picture in view) is exported for tests/test_lightbox_zoom.py.
(function (root) {
  const MAX_OVER_ACTUAL = 4;

  // view: {W, H (stage), w, h (the picture's laid-out size), natW (pixels of the loaded file), dpr}
  function fitScale(v) { return Math.min((v.W * 0.96) / v.w, (v.H * 0.92) / v.h, 1); }
  function actualScale(v) { return (v.natW / v.w) / (v.dpr || 1); }
  function maxScale(v) { return Math.max(fitScale(v), actualScale(v)) * MAX_OVER_ACTUAL; }
  // a translation that keeps the picture centred where it is smaller than the stage, and covering it where it is larger
  function clampPan(v, s, tx, ty) {
    const pw = v.w * s, ph = v.h * s;
    tx = pw <= v.W ? (v.W - pw) / 2 : Math.min(0, Math.max(v.W - pw, tx));
    ty = ph <= v.H ? (v.H - ph) / 2 : Math.min(0, Math.max(v.H - ph, ty));
    return [tx, ty];
  }
  // zoom to scale s2 keeping the picture point under (px, py) where it is
  function zoomAt(v, s1, tx, ty, s2, px, py) {
    const k = s2 / s1;
    return clampPan(v, s2, px - (px - tx) * k, py - (py - ty) * k);
  }
  const core = { fitScale, actualScale, maxScale, clampPan, zoomAt };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.ndLightboxCore = core;
  if (typeof document === "undefined") return;

  const PREF = "nd_lightbox_full";
  const pref = () => { try { return localStorage.getItem(PREF) === "1"; } catch (e) { return false; } };
  const $ = (id) => document.getElementById(id);

  let st = null;            // {orig, big, full, loading, s, tx, ty, w, h}
  let drag = null, pinch = null;
  const pointers = new Map();

  function view() {
    const stage = $("lightbox-stage"), img = $("lightbox-img");
    return { W: stage.clientWidth, H: stage.clientHeight, w: st.w, h: st.h, natW: img.naturalWidth || st.w, dpr: root.devicePixelRatio || 1 };
  }
  function paint() {
    const img = $("lightbox-img");
    img.style.transform = "translate(" + st.tx + "px," + st.ty + "px) scale(" + st.s + ")";
    const v = view();
    $("lightbox-img").classList.toggle("lb-zoomed", st.s > fitScale(v) * 1.01);
    const btn = $("lightbox-full");
    if (btn) {
      btn.hidden = !st.big;
      btn.textContent = st.loading ? "Loading full size…" : st.full ? "Full size ✓" : "⤢ Full size";
      btn.disabled = st.loading || st.full;
    }
  }
  function setScale(s2, px, py) {
    const v = view();
    s2 = Math.min(maxScale(v), Math.max(fitScale(v), s2));
    if (px == null) { px = v.W / 2; py = v.H / 2; }
    [st.tx, st.ty] = zoomAt(v, st.s, st.tx, st.ty, s2, px, py);
    st.s = s2;
    // zoomed past what the display copy can show sharply: fetch the original
    if (st.big && !st.full && s2 > actualScale(v) * 1.05) loadFull();
    paint();
  }
  function fit() {
    const v = view();
    st.s = fitScale(v);
    [st.tx, st.ty] = clampPan(v, st.s, 0, 0);
    paint();
  }
  function loadFull(then) {
    if (!st || !st.big || st.full) { if (then) then(); return; }
    if (st.loading) return;
    st.loading = true; paint();
    const want = st.orig, pic = new Image();
    pic.onload = () => {
      if (!st || st.orig !== want) return;            // closed or another picture opened meanwhile
      st.loading = false; st.full = true;
      $("lightbox-img").src = want;                   // same laid-out size (st.w x st.h): only sharper
      paint();
      if (then) then();
    };
    pic.onerror = () => { if (st && st.orig === want) { st.loading = false; paint(); } };
    pic.src = want;
  }
  function toggleAt(px, py) {
    const v = view();
    if (st.s > fitScale(v) * 1.01) { fit(); return; }
    const go = () => setScale(actualScale(view()), px, py);
    if (st.big && !st.full) loadFull(go); else go();
  }

  function open(src, big) {
    const img = $("lightbox-img");
    const full = big && pref();
    st = { orig: src, big, full, loading: false, s: 1, tx: 0, ty: 0, w: 0, h: 0 };
    img.style.transform = "";
    img.style.width = img.style.height = "";
    img.classList.remove("lb-ready", "lb-zoomed");
    img.onload = () => {
      if (!st || st.w) return;                        // the first file decides the laid-out size; the original keeps it
      st.w = img.naturalWidth; st.h = img.naturalHeight;
      // a display copy is smaller than the original: lay the picture out at the copy's size, the original then fills the same box
      img.style.width = st.w + "px"; img.style.height = st.h + "px";
      img.classList.add("lb-ready");
      fit();
    };
    img.src = big && !full ? src + "?display=1" : src;
    const box = $("lightbox-always");
    if (box) { box.checked = pref(); box.parentNode.hidden = !big; }
    const orig = $("lightbox-orig");
    if (orig) { orig.hidden = !big; orig.href = src; }
    paint();
  }
  function close() { st = null; drag = null; pinch = null; pointers.clear(); }

  function wire() {
    const stage = $("lightbox-stage"), img = $("lightbox-img");
    if (!stage || stage.dataset.wired) return;
    stage.dataset.wired = "1";
    stage.addEventListener("wheel", (e) => {
      if (!st || !st.w) return;
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      setScale(st.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    img.addEventListener("pointerdown", (e) => {
      if (!st || !st.w) return;
      e.preventDefault();
      img.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), s: st.s };
        drag = null;
      } else {
        drag = { x: e.clientX, y: e.clientY, tx: st.tx, ty: st.ty, moved: false };
      }
    });
    img.addEventListener("pointermove", (e) => {
      if (!st || !pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const r = stage.getBoundingClientRect();
        setScale(pinch.s * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
      } else if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
        if (drag.moved) { [st.tx, st.ty] = clampPan(view(), st.s, drag.tx + dx, drag.ty + dy); paint(); }
      }
    });
    const up = (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (drag && pointers.size === 0) {
        if (!drag.moved && st) { const r = stage.getBoundingClientRect(); toggleAt(e.clientX - r.left, e.clientY - r.top); }
        drag = null;
      }
    };
    img.addEventListener("pointerup", up);
    img.addEventListener("pointercancel", up);
    img.addEventListener("click", (e) => e.stopPropagation());       // a click on the picture zooms; on the dark around it, closes
    const btn = $("lightbox-full");
    if (btn) btn.addEventListener("click", (e) => { e.stopPropagation(); loadFull(); });
    const box = $("lightbox-always");
    if (box) {
      box.parentNode.addEventListener("click", (e) => e.stopPropagation());
      box.addEventListener("change", () => {
        try { localStorage.setItem(PREF, box.checked ? "1" : "0"); } catch (e) { /* this visit only */ }
        if (box.checked && st) loadFull();
      });
    }
    root.addEventListener("resize", () => { if (st && st.w) fit(); });
    document.addEventListener("keydown", (e) => {
      if (!st || !st.w || !$("lightbox-overlay").classList.contains("open") || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "f" || e.key === "F") loadFull();
      else if (e.key === "0") fit();
      else if (e.key === "1") toggleAt(view().W / 2, view().H / 2);
      else if (e.key === "+" || e.key === "=") setScale(st.s * 1.25);
      else if (e.key === "-") setScale(st.s / 1.25);
      else return;
      e.preventDefault();
    });
  }

  root.ndLightbox = { open, close, wire };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
})(typeof window !== "undefined" ? window : globalThis);
