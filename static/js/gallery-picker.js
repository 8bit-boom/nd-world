"use strict";
// Shared "Choose from Gallery" picker — originally built inline in
// entities/form.html (see git history), pulled out here so any page can
// reuse the same modal instead of re-declaring it per template. A page
// using this must:
//   1. Define `window.NDGalleryImages = {{ gallery_images | tojson }};`
//      (an array of {url, name}, from app/gallery.py's all_world_image_urls)
//      before this script loads — used for the "All Images" leaf below.
//   2. Include the modal markup once — see
//      app/templates/_gallery_picker_modal.html.
//   3. Call ndGalleryPickerOpen(onPick) from a trigger button's onclick,
//      where onPick(entry) receives the picked {url, name}.
// One shared overlay serves any number of trigger buttons on a page
// (e.g. maps.html has one per map/schematic card) — only one picker is
// ever open at a time, so a single callback/navigation-state slot is enough.
//
// Root view: every top-level album (folder) plus a synthetic "All Images"
// folder for the flat NDGalleryImages list. Opening a real album fetches
// its sub-albums/images from GET /api/gallery/browse (app/routers/gallery.py)
// lazily, so a host page never has to fetch/pass the whole album tree just
// in case the picker gets opened.

const ALL_IMAGES_ID = "__all__";

let _ndGalleryPickerCallback = null;
let _ndGalleryPickerBreadcrumb = []; // [{id, name}], id === ALL_IMAGES_ID for the synthetic leaf
let _ndGalleryPickerRootAlbums = null; // cached after first fetch — root rarely changes mid-session
let _ndGalleryPickerToken = 0; // guards against a slow fetch resolving after a newer navigation started

// app.imaging.make_thumbnail writes a small WebP preview alongside every
// upload under a predictable name — this picker can render potentially every
// image in the world (all_world_image_urls has no size cap), so loading full
// resolution here is the single worst offender for "everywhere" being slow.
// onerror falls back to the full image for anything uploaded before this
// feature shipped, since plain JS has no way to check the file exists first
// the way the server-side thumb_url() Jinja filter can.
function _ndThumbSrc(url) {
  const i = url.lastIndexOf(".");
  return i === -1 ? url : url.slice(0, i) + "_thumb.webp";
}
function _ndThumbFallback(img) {
  img.onerror = null;
  img.src = img.dataset.full;
}

function _ndGalleryPickerSetLoading() {
  const grid = document.getElementById("gallery-picker-grid");
  grid.innerHTML = '<div class="gallery-picker-empty">Loading…</div>';
}

function _ndGalleryPickerRenderBreadcrumb() {
  const nav = document.getElementById("gallery-picker-breadcrumb");
  const crumbs = ['<a data-root>🖼 All</a>'];
  _ndGalleryPickerBreadcrumb.forEach((b) => {
    crumbs.push('<span class="gallery-picker-bc-sep">›</span>');
    crumbs.push(`<a data-id="${b.id}">${_ndGalleryPickerEsc(b.name)}</a>`);
  });
  nav.innerHTML = crumbs.join(" ");
  nav.querySelector("[data-root]").onclick = () => ndGalleryPickerGoRoot();
  nav.querySelectorAll("a[data-id]").forEach((a, i) => {
    a.onclick = () => {
      const target = _ndGalleryPickerBreadcrumb[i];
      if (target.id === ALL_IMAGES_ID) ndGalleryPickerGoAllImages();
      else ndGalleryPickerGoAlbum(target.id, target.name);
    };
  });
}

function _ndGalleryPickerEsc(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

function _ndGalleryPickerAlbumCard(album) {
  const cell = document.createElement("div");
  cell.className = "gallery-picker-cell gallery-picker-folder";
  const thumb = document.createElement("div");
  thumb.className = "gallery-picker-folder-thumb";
  if (album.cover_url) {
    const img = document.createElement("img");
    img.src = _ndThumbSrc(album.cover_url);
    img.dataset.full = album.cover_url;
    img.loading = "lazy";
    img.onerror = () => _ndThumbFallback(img);
    thumb.appendChild(img);
  } else {
    thumb.classList.add("gallery-picker-folder-empty");
    thumb.textContent = "📁";
  }
  cell.appendChild(thumb);
  const name = document.createElement("div");
  name.className = "gallery-picker-cell-name";
  name.title = album.name;
  const countBits = [];
  if (album.image_count) countBits.push(`${album.image_count} image${album.image_count === 1 ? "" : "s"}`);
  if (album.sub_album_count) countBits.push(`${album.sub_album_count} sub-album${album.sub_album_count === 1 ? "" : "s"}`);
  name.innerHTML = `📁 ${_ndGalleryPickerEsc(album.name)}` + (countBits.length ? `<span class="gallery-picker-folder-count">${countBits.join(" · ")}</span>` : "");
  cell.appendChild(name);
  cell.onclick = () => ndGalleryPickerGoAlbum(album.id, album.name);
  return cell;
}

function _ndGalleryPickerImageCard(entry) {
  const cell = document.createElement("div");
  cell.className = "gallery-picker-cell";
  const img = document.createElement("img");
  img.src = _ndThumbSrc(entry.url);
  img.dataset.full = entry.url;
  img.alt = entry.name;
  img.loading = "lazy";
  img.onerror = () => _ndThumbFallback(img);
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
  return cell;
}

function _ndGalleryPickerRenderGrid(albums, images) {
  const grid = document.getElementById("gallery-picker-grid");
  grid.innerHTML = "";
  if ((!albums || !albums.length) && (!images || !images.length)) {
    grid.innerHTML = '<div class="gallery-picker-empty">Nothing here yet — upload one above, or add some from the <a href="/images" style="color:var(--neon)">Images gallery</a> first.</div>';
    return;
  }
  (albums || []).forEach((a) => grid.appendChild(_ndGalleryPickerAlbumCard(a)));
  (images || []).forEach((e) => grid.appendChild(_ndGalleryPickerImageCard(e)));
}

async function ndGalleryPickerGoRoot() {
  const token = ++_ndGalleryPickerToken;
  _ndGalleryPickerBreadcrumb = [];
  _ndGalleryPickerRenderBreadcrumb();
  _ndGalleryPickerSetLoading();
  try {
    if (!_ndGalleryPickerRootAlbums) {
      const res = await fetch("/api/gallery/browse");
      if (!res.ok) throw new Error("Failed to load albums");
      const data = await res.json();
      _ndGalleryPickerRootAlbums = data.albums || [];
    }
  } catch (e) {
    if (token !== _ndGalleryPickerToken) return;
    document.getElementById("gallery-picker-grid").innerHTML =
      `<div class="gallery-picker-empty">Couldn't load albums: ${_ndGalleryPickerEsc(e.message)}</div>`;
    return;
  }
  if (token !== _ndGalleryPickerToken) return;
  const images = window.NDGalleryImages || [];
  const allImagesFolder = {
    id: ALL_IMAGES_ID, name: "All Images", cover_url: images[0] ? images[0].url : null,
    image_count: images.length, sub_album_count: 0,
  };
  _ndGalleryPickerRenderGrid([allImagesFolder, ..._ndGalleryPickerRootAlbums], null);
}

function ndGalleryPickerGoAllImages() {
  _ndGalleryPickerBreadcrumb = [{ id: ALL_IMAGES_ID, name: "All Images" }];
  _ndGalleryPickerRenderBreadcrumb();
  _ndGalleryPickerRenderGrid(null, window.NDGalleryImages || []);
}

async function ndGalleryPickerGoAlbum(id, name) {
  const token = ++_ndGalleryPickerToken;
  _ndGalleryPickerSetLoading();
  try {
    const res = await fetch(`/api/gallery/browse?album_id=${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error("Failed to load album");
    const data = await res.json();
    if (token !== _ndGalleryPickerToken) return;
    _ndGalleryPickerBreadcrumb = data.breadcrumb && data.breadcrumb.length ? data.breadcrumb : [{ id, name }];
    _ndGalleryPickerRenderBreadcrumb();
    _ndGalleryPickerRenderGrid(data.albums, data.images);
  } catch (e) {
    if (token !== _ndGalleryPickerToken) return;
    document.getElementById("gallery-picker-grid").innerHTML =
      `<div class="gallery-picker-empty">Couldn't load album: ${_ndGalleryPickerEsc(e.message)}</div>`;
  }
}

function ndGalleryPickerOpen(onPick) {
  _ndGalleryPickerCallback = onPick;
  document.getElementById("gallery-picker-overlay").classList.add("open");
  ndGalleryPickerGoRoot();
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
