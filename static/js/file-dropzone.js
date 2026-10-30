"use strict";
// Generic drag-and-drop wrapper for a plain <input type=file>. Wrap any
// input in a container marked data-dropzone (reusing the .form-dropzone/
// .drag-over CSS already defined in static/style.css for map_form.html's
// original hand-rolled dropzone) and this wires it up automatically.
//
// A file dropped anywhere in the zone populates the input's .files exactly
// as a manual pick would, then fires a native "change" event — so whatever
// the page already does on change (a live preview, an auto-submit) just
// runs unmodified. No upload/preview logic is duplicated here; pages with
// no onchange handler (form-submit- or button-triggered uploads) still get
// .files populated, which is all a later submit/click needs — dispatching
// change there is a harmless no-op.
//
// Deliberately does not filter by the input's accept attribute: native
// drag-and-drop doesn't honor accept the way the file-picker dialog does,
// so a mismatched drop reaches the same server-side validation a mismatched
// pick already would.
function ndDropzoneHasFiles(dt) {
  return !!dt && Array.from(dt.types || []).includes("Files");
}

function ndDropzoneSetup(zone) {
  const input = zone.querySelector('input[type=file]');
  if (!input) return;
  if (ndInputTakesImages(input)) {
    ndRegisterPasteZone(zone, (files) => {
      const dt = new DataTransfer();
      (input.multiple ? files : files.slice(0, 1)).forEach((f) => dt.items.add(f));
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  zone.addEventListener("dragenter", (e) => {
    if (!ndDropzoneHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragover", (e) => {
    if (!ndDropzoneHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    if (!ndDropzoneHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    zone.classList.remove("drag-over");
    input.files = e.dataTransfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-dropzone]").forEach(ndDropzoneSetup);
});

// ── paste (Ctrl+V) as well as drop ─────────────────────────────────────────────────────────────────────────
// A picture on the clipboard (a screenshot, "Copy image" in a browser) pasted anywhere on the page goes to the drop zone it belongs to, as if it had been
// dropped there. Which zone: the one holding the focus or the cursor; failing that, the page's only one. Pasting TEXT is never touched - a paste into a
// text field that has text on the clipboard stays an ordinary paste. A zone registers itself with the function that takes the pasted files, so a page with
// its own hand-rolled zone (the gallery album, the map upload, the schematic background) joins with one line: ndRegisterPasteZone(zone, files => ...).
const ndPasteZones = [];
let ndPasteHover = null;
const ndImageExts = /\.(png|jpe?g|gif|webp|avif|bmp|svg)$/i;

function ndInputTakesImages(input) {
  const accept = (input.getAttribute("accept") || "").trim();
  if (!accept) return true;
  return accept.split(",").some((a) => {
    a = a.trim().toLowerCase();
    return a === "image/*" || a.startsWith("image/") || ndImageExts.test(a);
  });
}

function ndRegisterPasteZone(zone, handler) {
  if (!zone || ndPasteZones.some((z) => z.zone === zone)) return;
  ndPasteZones.push({ zone, handler });
  const hint = zone.querySelector(".form-dropzone-hint");
  if (hint && !/Ctrl\+V/.test(hint.textContent)) hint.textContent = hint.textContent.replace(/\s*$/, "") + " \u2014 or paste (Ctrl+V)";
  zone.addEventListener("mouseenter", () => { ndPasteHover = zone; });
  zone.addEventListener("mouseleave", () => { if (ndPasteHover === zone) ndPasteHover = null; });
}

function ndPasteEditable(el) {
  if (!el || el.nodeType !== 1) return false;
  if (el.isContentEditable || el.tagName === "TEXTAREA") return true;
  if (el.tagName !== "INPUT") return false;
  return !["file", "checkbox", "radio", "button", "submit", "reset", "range", "color", "image"].includes((el.type || "text").toLowerCase());
}

function ndPasteTarget(e) {
  const inside = ndPasteZones.find((z) => e.target && z.zone.contains(e.target));
  if (inside) return inside;
  const hovered = ndPasteZones.find((z) => z.zone === ndPasteHover);
  if (hovered) return hovered;
  const visible = ndPasteZones.filter((z) => z.zone.isConnected && z.zone.getClientRects().length > 0);
  return visible.length === 1 ? visible[0] : null;
}

// a pasted screenshot is called "image.png" by every browser: give it a name that tells one paste from another
function ndPastedName(file, i) {
  if (file.name && file.name !== "image.png" && file.name !== "image") return file.name;
  const ext = { "image/jpeg": "jpg", "image/svg+xml": "svg" }[file.type] || (file.type.split("/")[1] || "png");
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `pasted-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}${i ? "-" + (i + 1) : ""}.${ext}`;
}

document.addEventListener("paste", (e) => {
  const cd = e.clipboardData;
  if (!cd || !cd.files || !cd.files.length || !ndPasteZones.length) return;
  const images = Array.from(cd.files).filter((f) => (f.type || "").startsWith("image/"));
  if (!images.length) return;
  if (ndPasteEditable(e.target) && Array.from(cd.types || []).includes("text/plain")) return; // text into a text field: an ordinary paste
  const target = ndPasteTarget(e);
  if (!target) return;
  e.preventDefault();
  const files = images.map((f, i) => new File([f], ndPastedName(f, i), { type: f.type, lastModified: Date.now() }));
  target.zone.classList.add("drag-over");
  setTimeout(() => target.zone.classList.remove("drag-over"), 700);
  target.handler(files);
});
