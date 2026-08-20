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
