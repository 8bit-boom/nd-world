"use strict";
// Shared "Choose from Gallery" picker — originally built inline in
// entities/form.html (see git history), pulled out here so any page can
// reuse the same modal instead of re-declaring it per template. A page
// using this must:
//   1. Define `window.NDGalleryImages = {{ gallery_images | tojson }};`
//      (an array of {url, name}, from app/gallery.py's all_world_image_urls)
//      before this script loads.
//   2. Include the modal markup once — see
//      app/templates/_gallery_picker_modal.html.
//   3. Call ndGalleryPickerOpen(onPick) from a trigger button's onclick,
//      where onPick(entry) receives the picked {url, name}.
// One shared overlay serves any number of trigger buttons on a page
// (e.g. maps.html has one per map/schematic card) — only one picker is
// ever open at a time, so a single callback slot is enough.

let _ndGalleryPickerRendered = false;
let _ndGalleryPickerCallback = null;

function ndGalleryPickerRender() {
  const grid = document.getElementById("gallery-picker-grid");
  const images = window.NDGalleryImages || [];
  if (!images.length) {
    grid.innerHTML = '<div class="gallery-picker-empty">No images yet — upload one above, or add some from the <a href="/images" style="color:var(--neon)">Images gallery</a> first.</div>';
    return;
  }
  grid.innerHTML = "";
  images.forEach((entry) => {
    const cell = document.createElement("div");
    cell.className = "gallery-picker-cell";
    const img = document.createElement("img");
    img.src = entry.url;
    img.alt = entry.name;
    img.loading = "lazy";
    cell.appendChild(img);
    const name = document.createElement("div");
    name.className = "gallery-picker-cell-name";
    name.title = entry.name;
    name.textContent = entry.name;
    cell.appendChild(name);
    cell.onclick = function () {
      const cb = _ndGalleryPickerCallback;
      ndGalleryPickerClose();
      if (cb) cb(entry);
    };
    grid.appendChild(cell);
  });
}

function ndGalleryPickerOpen(onPick) {
  _ndGalleryPickerCallback = onPick;
  if (!_ndGalleryPickerRendered) {
    ndGalleryPickerRender();
    _ndGalleryPickerRendered = true;
  }
  document.getElementById("gallery-picker-overlay").classList.add("open");
}

function ndGalleryPickerClose() {
  document.getElementById("gallery-picker-overlay").classList.remove("open");
}

document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") ndGalleryPickerClose();
});

// Turns an already-hosted /uploads/... image URL back into a File object
// so a gallery pick can be dropped straight into an existing <input
// type=file> and ride the page's normal (already-tested) upload form
// submission — no new backend endpoint needed just to accept "a URL
// instead of a file". Same-origin fetch, so no CORS concerns.
async function ndGalleryUrlToFile(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Couldn't load the picked image");
  const blob = await res.blob();
  const filename = url.split("/").pop() || "image";
  return new File([blob], filename, { type: blob.type });
}
