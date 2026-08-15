"use strict";
// Shared formatting toolbar for markdown-authored textareas (entity/rules
// bodies, character notes/backstory, private notes) and the plain-text board
// note body. Any <textarea data-fmt> on the page gets a toolbar inserted
// before it; clicking a button wraps the current selection in the matching
// syntax. Rendering of that syntax into HTML happens server-side in
// app/rendering.py (render_md/_apply_inline_styles) for markdown fields —
// ndFmtRenderInline() below is a client-side mirror of just the [color=]/
// [mark]/[u] + bold/italic/strike subset, used only where there's no server
// round trip to render against (the investigation board's node body).
//
// Keep the color allowlist and tag regexes here in sync with
// app/rendering.py's _COLOR_NAMES/_HEX_COLOR_RE/_COLOR_TAG_RE/_MARK_TAG_RE/
// _U_TAG_RE — they must accept exactly the same syntax.

const NDFMT_COLORS = [
  { name: "Red", value: "#ff5555" },
  { name: "Orange", value: "#ff9944" },
  { name: "Yellow", value: "#ffdd33" },
  { name: "Green", value: "#55ff88" },
  { name: "Cyan", value: "#33e6ff" },
  { name: "Blue", value: "#5599ff" },
  { name: "Purple", value: "#bb66ff" },
  { name: "Pink", value: "#ff66bb" },
];

const NDFMT_COLOR_NAMES = new Set([
  "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink",
  "white", "black", "gray", "grey", "magenta", "lime", "teal", "gold",
  "silver", "brown", "crimson", "violet", "indigo", "salmon", "coral",
]);
const NDFMT_HEX_RE = /^#(?:[0-9a-fA-F]{3}){1,2}$/;

function ndFmtSafeColor(raw) {
  const v = (raw || "").trim();
  if (NDFMT_HEX_RE.test(v)) return v;
  if (NDFMT_COLOR_NAMES.has(v.toLowerCase())) return v.toLowerCase();
  return null;
}

function ndFmtEscapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function ndFmtEscapeAttr(s) {
  return ndFmtEscapeHtml(s).replace(/"/g, "&quot;");
}

// Only a same-origin upload path or a plain http(s) URL — never javascript:
// or any other scheme — can end up in a src="..." attribute here.
function ndFmtSafeImageUrl(raw) {
  const v = (raw || "").trim();
  if (v.startsWith("/uploads/") || /^https?:\/\//i.test(v)) return v;
  return null;
}

// Client-side render of the inline-formatting subset only (no headings,
// lists, tables — those are markdown2's job server-side). Used for the board
// note card body, which has no server render pass. Images are included (but
// not full link syntax) since the toolbar's image button writes ![]() here
// same as everywhere else data-fmt appears.
function ndFmtRenderInline(text) {
  let html = ndFmtEscapeHtml(text || "");
  html = html.replace(/!\[([^\]]{0,300})\]\(([^)\s]{1,2000})\)/g, (_, alt, url) => {
    const safeUrl = ndFmtSafeImageUrl(url);
    if (!safeUrl) return "";
    return `<img src="${ndFmtEscapeAttr(safeUrl)}" alt="${ndFmtEscapeAttr(alt)}" style="max-width:100%;border-radius:4px;margin:.3em 0;display:block">`;
  });
  html = html.replace(/\*\*(.+?)\*\*/gs, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/gs, "<em>$1</em>");
  html = html.replace(/~~(.+?)~~/gs, "<del>$1</del>");
  html = html.replace(/\[u\](.*?)\[\/u\]/gs, "<u>$1</u>");
  html = html.replace(/\[color=([^\]]{1,20})\](.*?)\[\/color\]/gs, (_, color, inner) => {
    const c = ndFmtSafeColor(color);
    return c ? `<span style="color:${c}">${inner}</span>` : inner;
  });
  html = html.replace(/\[mark(?:=([^\]]{1,20}))?\](.*?)\[\/mark\]/gs, (_, color, inner) => {
    const c = color ? ndFmtSafeColor(color) : null;
    return c ? `<mark style="background-color:${c}">${inner}</mark>` : `<mark>${inner}</mark>`;
  });
  return html.replace(/\n/g, "<br>");
}

function ndFmtWrapSelection(ta, before, after) {
  const start = ta.selectionStart, end = ta.selectionEnd;
  const val = ta.value;
  const selected = val.slice(start, end) || "text";
  ta.value = val.slice(0, start) + before + selected + after + val.slice(end);
  ta.focus();
  ta.selectionStart = start + before.length;
  ta.selectionEnd = start + before.length + selected.length;
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

// Uploads through the same /api/upload-image endpoint the entity portrait
// field already posts to (app/main.py's save_upload — converts to the
// world's configured format, rejects disallowed extensions). Markdown image
// syntax is already rendered server-side (app/rendering.py's render_md has
// no special-casing to disable it) and already special-cased for stripping
// in card summaries (strip_md), so this button is purely a convenience for
// getting a file onto disk and its URL into the textarea — nothing new to
// teach the renderer.
//
// Shared by both the toolbar button (one file, alt text pulled from the
// current selection) and drag-and-drop (one or more files, dropped in
// sequence at `pos` — see ndFmtSetupDragDrop below). Returns the cursor
// position immediately after the inserted markdown, so a caller inserting
// several files in a row knows where to place the next one.
async function ndFmtUploadOneImage(ta, file, start, end, alt) {
  const placeholder = `![Uploading ${file.name}…]()`;
  ta.value = ta.value.slice(0, start) + placeholder + ta.value.slice(end);
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  const fd = new FormData();
  fd.append("file", file);
  // Which endpoint to POST to is per-textarea (data-fmt-upload) — entity/
  // rules/board/private-note bodies are GM-only pages so the default
  // GM-only /api/upload-image is fine, but the character backstory/notes
  // fields are player-writable and need the player-safe
  // /api/characters/upload-image instead (see characters.py).
  const endpoint = ta.dataset.fmtUpload || "/api/upload-image";
  let endPos = start + placeholder.length;
  try {
    const res = await fetch(endpoint, { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
    const data = await res.json();
    const at = ta.value.indexOf(placeholder);
    const markdown = `![${alt}](${data.url})`;
    if (at !== -1) {
      ta.value = ta.value.slice(0, at) + markdown + ta.value.slice(at + placeholder.length);
      endPos = at + markdown.length;
      ta.selectionStart = ta.selectionEnd = endPos;
    }
  } catch (e) {
    const at = ta.value.indexOf(placeholder);
    if (at !== -1) {
      ta.value = ta.value.slice(0, at) + ta.value.slice(at + placeholder.length);
      endPos = at;
    }
    alert("Image upload failed: " + e.message);
  } finally {
    ta.focus();
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }
  return endPos;
}

function ndFmtInsertImage(ta, btn) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.style.display = "none";
  document.body.appendChild(input);
  input.addEventListener("change", async () => {
    const file = input.files[0];
    input.remove();
    if (!file) return;
    const start = ta.selectionStart, end = ta.selectionEnd;
    const alt = ta.value.slice(start, end).trim();
    btn.disabled = true;
    try {
      await ndFmtUploadOneImage(ta, file, start, end, alt);
    } finally {
      btn.disabled = false;
    }
  });
  input.click();
}

// Loads a local .md file's text straight into the textarea — read entirely
// client-side via FileReader, never uploaded anywhere (unlike the image
// button above, there's no server round trip: markdown text just becomes
// the field's value). Replacing rather than inserting-at-cursor matches
// what "import a file" means for a notes field — this IS the note, not a
// snippet to weave into existing text — so a non-empty textarea gets a
// confirm() first to guard against silently discarding a draft in
// progress, same instinct as this app's other destructive-action confirms
// (album delete, image-remove, etc.).
function ndFmtImportMdFile(ta, btn) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".md,.markdown,text/markdown,text/plain";
  input.style.display = "none";
  document.body.appendChild(input);
  input.addEventListener("change", () => {
    const file = input.files[0];
    input.remove();
    if (!file) return;
    if (ta.value.trim() && !confirm(`Replace the current text with the contents of "${file.name}"?`)) return;
    btn.disabled = true;
    const reader = new FileReader();
    reader.onload = () => {
      ta.value = String(reader.result || "");
      ta.focus();
      ta.selectionStart = ta.selectionEnd = ta.value.length;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      btn.disabled = false;
    };
    reader.onerror = () => {
      alert("Couldn't read that file.");
      btn.disabled = false;
    };
    reader.readAsText(file);
  });
  input.click();
}

// Drag a file (or several) from the desktop straight onto the textarea.
// Native <textarea> content has no DOM text nodes for the drop event's
// coordinates to resolve against, so — same as every plain-textarea
// drag-drop implementation — this inserts at the current cursor position
// rather than trying to land exactly under the pointer; multiple files
// drop in sequence, one upload at a time, each starting where the last one
// left off. Only intercepts drags that actually carry files, so it never
// interferes with this app's other drag-and-drop (e.g. dragging a nav tab
// onto the home page's Quick Links, which carries plain-text data instead).
function ndFmtHasFiles(dt) {
  return !!dt && Array.from(dt.types || []).includes("Files");
}

async function ndFmtHandleDroppedFiles(ta, fileList) {
  const files = Array.from(fileList || []).filter((f) => f.type && f.type.startsWith("image/"));
  if (!files.length) return;
  let pos = ta.selectionStart;
  for (const file of files) {
    pos = await ndFmtUploadOneImage(ta, file, pos, pos, "");
    if (files.length > 1) {
      ta.value = ta.value.slice(0, pos) + "\n" + ta.value.slice(pos);
      pos += 1;
      ta.selectionStart = ta.selectionEnd = pos;
    }
  }
}

function ndFmtSetupDragDrop(ta) {
  ta.addEventListener("dragover", (e) => {
    if (!ndFmtHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    ta.classList.add("fmt-drag-over");
  });
  ta.addEventListener("dragleave", () => ta.classList.remove("fmt-drag-over"));
  ta.addEventListener("drop", (e) => {
    if (!ndFmtHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    ta.classList.remove("fmt-drag-over");
    ndFmtHandleDroppedFiles(ta, e.dataTransfer.files);
  });
}

// A drop that misses the (usually short) textarea and lands elsewhere on
// the page would otherwise make the browser navigate away and open the
// image file directly, discarding whatever the GM was mid-editing. Only
// suppressed for actual file drags — the app's own text/plain drag payloads
// (nav-tab-onto-Quick-Links, etc.) are untouched.
document.addEventListener("dragover", (e) => { if (ndFmtHasFiles(e.dataTransfer)) e.preventDefault(); });
document.addEventListener("drop", (e) => { if (ndFmtHasFiles(e.dataTransfer)) e.preventDefault(); });

function ndFmtButton(label, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "fmt-btn";
  b.textContent = label;
  b.title = title;
  b.onclick = onClick;
  return b;
}

function ndFmtBuildToolbar(ta) {
  const bar = document.createElement("div");
  bar.className = "fmt-toolbar";

  bar.appendChild(ndFmtButton("B", "Bold", () => ndFmtWrapSelection(ta, "**", "**")));
  bar.appendChild(ndFmtButton("I", "Italic", () => ndFmtWrapSelection(ta, "*", "*")));
  bar.appendChild(ndFmtButton("U", "Underline", () => ndFmtWrapSelection(ta, "[u]", "[/u]")));
  bar.appendChild(ndFmtButton("S", "Strikethrough", () => ndFmtWrapSelection(ta, "~~", "~~")));
  bar.appendChild(ndFmtButton("⬛", "Highlight", () => ndFmtWrapSelection(ta, "[mark]", "[/mark]")));

  const imgBtn = ndFmtButton("🖼", "Insert image", () => ndFmtInsertImage(ta, imgBtn));
  bar.appendChild(imgBtn);

  const importBtn = ndFmtButton("📄 Import .md", "Import a .md file into this field", () => ndFmtImportMdFile(ta, importBtn));
  importBtn.classList.add("fmt-btn-labeled");
  bar.appendChild(importBtn);

  const sep = document.createElement("div");
  sep.className = "fmt-sep";
  bar.appendChild(sep);

  const colorGroup = document.createElement("div");
  colorGroup.className = "fmt-color-group";
  NDFMT_COLORS.forEach((c) => {
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "fmt-swatch";
    sw.style.background = c.value;
    sw.title = `Color: ${c.name}`;
    sw.onclick = () => ndFmtWrapSelection(ta, `[color=${c.value}]`, "[/color]");
    colorGroup.appendChild(sw);
  });
  const custom = document.createElement("input");
  custom.type = "color";
  custom.className = "fmt-custom-color";
  custom.title = "Custom color";
  custom.value = "#ffffff";
  custom.onchange = () => ndFmtWrapSelection(ta, `[color=${custom.value}]`, "[/color]");
  colorGroup.appendChild(custom);
  bar.appendChild(colorGroup);

  ta.parentNode.insertBefore(bar, ta);
}

function ndFmtInit() {
  document.querySelectorAll("textarea[data-fmt]").forEach((ta) => {
    if (ta.dataset.fmtReady) return;
    ta.dataset.fmtReady = "1";
    ndFmtBuildToolbar(ta);
    ndFmtSetupDragDrop(ta);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", ndFmtInit);
} else {
  ndFmtInit();
}
