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
// The media button/drag-drop/paste all insert plain ![alt](url) markdown
// for image, audio, AND video — a size change or an audio/video attachment
// is layered on by putting a marker in markdown's own optional title slot
// (![alt](url "TITLE")) rather than a non-standard syntax: "audio"/"video"
// for those two, "size:NN" (a percent, 10-300) for a resized image. See
// app/rendering.py's _transform_media_tags for the server-side match of
// this same logic.
//
// Keep the color allowlist and tag regexes here in sync with
// app/rendering.py's _COLOR_NAMES/_HEX_COLOR_RE/_COLOR_TAG_RE/_MARK_TAG_RE/
// _U_TAG_RE, and the size/audio/video title markers in sync with
// app/rendering.py's _SIZE_TITLE_RE/"audio"/"video" — they must accept
// exactly the same syntax.

const NDFMT_AV_TITLES = new Set(["audio", "video"]);
const NDFMT_SIZE_TITLE_RE = /^size:(\d{1,3})$/;
const NDFMT_MIN_SIZE_PCT = 10;
const NDFMT_MAX_SIZE_PCT = 300;
const NDFMT_RESIZE_PCTS = [50, 75, 100, 150, 200];

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
// or any other scheme — can end up in a src="..." attribute here. Used for
// every media type (image/audio/video), not just images.
function ndFmtSafeImageUrl(raw) {
  const v = (raw || "").trim();
  if (v.startsWith("/uploads/") || /^https?:\/\//i.test(v)) return v;
  return null;
}

// Client-side render of the inline-formatting subset only (no headings,
// lists, tables — those are markdown2's job server-side). Used for the board
// note card body, which has no server render pass. Images/audio/video are
// included (but not full link syntax) since the toolbar's media button
// writes ![]() here same as everywhere else data-fmt appears — see the
// module header comment for the title-slot marker convention this mirrors
// from app/rendering.py's _transform_media_tags.
function ndFmtRenderInline(text) {
  let html = ndFmtEscapeHtml(text || "");
  html = html.replace(/!\[([^\]]{0,300})\]\(([^)\s]{1,2000})(?:\s+"([^"]{0,20})")?\)/g, (_, alt, url, title) => {
    const safeUrl = ndFmtSafeImageUrl(url);
    if (!safeUrl) return "";
    const esc = ndFmtEscapeAttr(safeUrl);
    if (title === "audio") return `<audio controls preload="metadata" src="${esc}"></audio>`;
    if (title === "video") return `<video controls preload="metadata" src="${esc}" style="max-width:100%"></video>`;
    const sizeMatch = title && NDFMT_SIZE_TITLE_RE.exec(title);
    const style = sizeMatch
      ? `width:${Math.max(NDFMT_MIN_SIZE_PCT, Math.min(NDFMT_MAX_SIZE_PCT, parseInt(sizeMatch[1], 10)))}%;border-radius:4px;margin:.3em 0;display:block`
      : "max-width:100%;border-radius:4px;margin:.3em 0;display:block";
    return `<img src="${esc}" alt="${ndFmtEscapeAttr(alt)}" style="${style}">`;
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

// Uploads through /api/upload-media — image, audio, or video (app/main.py's
// save_upload_media: an image converts to the world's configured format and
// gets a thumbnail same as before this fn accepted anything else; audio/
// video are saved as-is, see uploads.save_inline_av). Markdown media syntax
// is already rendered server-side (app/rendering.py's render_md has no
// special-casing to disable it) and already special-cased for stripping in
// card summaries (strip_md), so this button is purely a convenience for
// getting a file onto disk and its URL into the textarea — nothing new to
// teach the renderer.
//
// Shared by the toolbar button (one file, alt text pulled from the current
// selection), drag-and-drop (one or more files, dropped in sequence at
// `pos` — see ndFmtSetupDragDrop below), and clipboard paste (ndFmtSetupPaste).
// Returns the cursor position immediately after the inserted markdown, so a
// caller inserting several files in a row knows where to place the next one.
async function ndFmtUploadOneImage(ta, file, start, end, alt) {
  const placeholder = `![Uploading ${file.name}…]()`;
  ta.value = ta.value.slice(0, start) + placeholder + ta.value.slice(end);
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  const fd = new FormData();
  fd.append("file", file);
  // Which endpoint to POST to is per-textarea (data-fmt-upload-media) —
  // entity/rules/board/private-note bodies are GM-only pages so the default
  // GM-only /api/upload-media is fine, but the character backstory/notes
  // fields are player-writable and need the player-safe
  // /api/characters/upload-media instead (see characters.py).
  const endpoint = ta.dataset.fmtUploadMedia || "/api/upload-media";
  let endPos = start + placeholder.length;
  try {
    const res = await fetch(endpoint, { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
    const data = await res.json();
    const at = ta.value.indexOf(placeholder);
    // Only audio/video get a title marker — a plain image reference stays
    // exactly the ![alt](url) shape it always was, so every note/entity
    // saved before this feature existed renders identically.
    const title = data.kind === "audio" || data.kind === "video" ? ` "${data.kind}"` : "";
    const markdown = `![${alt}](${data.url}${title})`;
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
    alert("Upload failed: " + e.message);
  } finally {
    ta.focus();
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }
  return endPos;
}

function ndFmtInsertImage(ta, btn) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*,audio/*,video/*";
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
// (album delete, image-remove, etc.). Shared by both the toolbar button
// (ndFmtImportMdFile below) and drag-and-drop (ndFmtHandleDroppedFiles).
function ndFmtLoadMdFileIntoTextarea(ta, file) {
  return new Promise((resolve) => {
    if (ta.value.trim() && !confirm(`Replace the current text with the contents of "${file.name}"?`)) {
      resolve(false);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      ta.value = String(reader.result || "");
      ta.focus();
      ta.selectionStart = ta.selectionEnd = ta.value.length;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      resolve(true);
    };
    reader.onerror = () => {
      alert("Couldn't read that file.");
      resolve(false);
    };
    reader.readAsText(file);
  });
}

function ndFmtImportMdFile(ta, btn) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".md,.markdown,text/markdown,text/plain";
  input.style.display = "none";
  document.body.appendChild(input);
  input.addEventListener("change", async () => {
    const file = input.files[0];
    input.remove();
    if (!file) return;
    btn.disabled = true;
    await ndFmtLoadMdFileIntoTextarea(ta, file);
    btn.disabled = false;
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

// File.type is unreliable for .md (many OSes report "" rather than
// text/markdown), so the extension is checked first and the MIME type only
// as a fallback — same two ways the button's <input accept> already matches.
function ndFmtIsMarkdownFile(file) {
  const name = (file.name || "").toLowerCase();
  if (name.endsWith(".md") || name.endsWith(".markdown") || name.endsWith(".txt")) return true;
  return file.type === "text/markdown" || file.type === "text/plain";
}

async function ndFmtHandleDroppedFiles(ta, fileList) {
  const files = Array.from(fileList || []);
  // A dropped .md/.txt file means "import", not "insert" — same one-file,
  // replace-with-confirm behavior as the toolbar button. Takes priority over
  // any images in the same drop rather than mixing both actions from one drop.
  const mdFile = files.find(ndFmtIsMarkdownFile);
  if (mdFile) {
    await ndFmtLoadMdFileIntoTextarea(ta, mdFile);
    return;
  }
  const media = files.filter((f) => f.type && /^(image|audio|video)\//.test(f.type));
  if (!media.length) return;
  let pos = ta.selectionStart;
  for (const file of media) {
    pos = await ndFmtUploadOneImage(ta, file, pos, pos, "");
    if (media.length > 1) {
      ta.value = ta.value.slice(0, pos) + "\n" + ta.value.slice(pos);
      pos += 1;
      ta.selectionStart = ta.selectionEnd = pos;
    }
  }
}

// Ctrl+V/Cmd+V of a screenshot (the common case), or any image/audio/video
// the OS clipboard exposes as a real File (some file managers put one on
// the clipboard for "Copy" on a file) — same upload-and-insert path drag-
// and-drop uses above. Only preventDefault() when a matching file was
// actually found, so an ordinary text paste (including one that happens to
// carry a URL/plain string alongside non-file clipboard data) is completely
// unaffected.
function ndFmtSetupPaste(ta) {
  ta.addEventListener("paste", (e) => {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const files = items
      .filter((it) => it.kind === "file")
      .map((it) => it.getAsFile())
      .filter((f) => f && /^(image|audio|video)\//.test(f.type));
    if (!files.length) return;
    e.preventDefault();
    (async () => {
      let pos = ta.selectionStart, end = ta.selectionEnd;
      for (const file of files) {
        pos = await ndFmtUploadOneImage(ta, file, pos, end, "");
        end = pos;
        if (files.length > 1) {
          ta.value = ta.value.slice(0, pos) + "\n" + ta.value.slice(pos);
          pos += 1;
          end = pos;
          ta.selectionStart = ta.selectionEnd = pos;
        }
      }
    })();
  });
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
//
// Excludes drops landing directly on a plain <input type=file> (e.g. the
// entity form's portrait upload, which has no drag-drop handling of its
// own) — the browser's native default action for that case is exactly
// "populate the input with the dropped file", not a page navigation, so
// preventDefault() here would silently break it instead of protecting
// anything.
function ndFmtIsFileInputTarget(target) {
  return !!(target && target.closest && target.closest('input[type="file"]'));
}
document.addEventListener("dragover", (e) => {
  if (ndFmtHasFiles(e.dataTransfer) && !ndFmtIsFileInputTarget(e.target)) e.preventDefault();
});
document.addEventListener("drop", (e) => {
  if (ndFmtHasFiles(e.dataTransfer) && !ndFmtIsFileInputTarget(e.target)) e.preventDefault();
});

// Matches every ![alt](url ["title"]) occurrence in the textarea — used
// both to find the one under the cursor (ndFmtFindImageRefAtCursor) and,
// via ndFmtParseImageRef, to pull it back apart once found.
const NDFMT_IMAGE_REF_RE = /!\[[^\]]{0,300}\]\([^)\s]{1,2000}(?:\s+"[^"]{0,20}")?\)/g;

// Resize has no selection UI of its own — the user either just inserted an
// image (cursor lands immediately after it, which this range check treats
// as "inside" via the <= end comparison) or clicked into an existing
// reference earlier in the text. Scanning for the occurrence whose range
// contains the cursor avoids requiring an exact manual selection first.
function ndFmtFindImageRefAtCursor(ta) {
  const val = ta.value;
  const pos = ta.selectionStart;
  NDFMT_IMAGE_REF_RE.lastIndex = 0;
  let m;
  while ((m = NDFMT_IMAGE_REF_RE.exec(val))) {
    const end = m.index + m[0].length;
    if (m.index <= pos && pos <= end) return { start: m.index, end, text: m[0] };
    if (m.index > pos) break;
  }
  return null;
}

function ndFmtParseImageRef(text) {
  const m = /^!\[([^\]]*)\]\(([^)\s]*)(?:\s+"([^"]*)")?\)$/.exec(text);
  if (!m) return null;
  return { alt: m[1], url: m[2], title: m[3] || "" };
}

// Rewrites the image reference at the cursor with a new size percentage
// (100% removes the title marker entirely, restoring the plain ![alt](url)
// shape a never-resized image already has) and re-selects the rewritten
// text so a second resize click can immediately target the same image.
function ndFmtSetImageSize(ta, pct) {
  const ref = ndFmtFindImageRefAtCursor(ta);
  if (!ref) {
    alert("Click inside an image reference in the text first, then choose a size.");
    return;
  }
  const parsed = ndFmtParseImageRef(ref.text);
  if (!parsed || NDFMT_AV_TITLES.has(parsed.title)) {
    alert("That isn't a resizable image (it may be an audio/video attachment).");
    return;
  }
  const title = pct === 100 ? "" : ` "size:${pct}"`;
  const rebuilt = `![${parsed.alt}](${parsed.url}${title})`;
  ta.value = ta.value.slice(0, ref.start) + rebuilt + ta.value.slice(ref.end);
  ta.focus();
  ta.selectionStart = ref.start;
  ta.selectionEnd = ref.start + rebuilt.length;
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

// ── Live thumbnail preview + drag-resize, below the textarea ───────────────
// A raw ![]() reference is unreadable text with no visual feedback, so a
// note/body with several images pasted in back-to-back (no separating text)
// is impossible to tell apart or resize by cursor position alone. This
// mirrors every image reference in the field as a small thumbnail with its
// own corner-drag handles, updating the SAME size:NN title-marker
// convention as ndFmtSetImageSize above but targeted at a specific match's
// [start,end) span rather than the cursor — see ndFmtImageMatches.
//
// Each thumbnail's own wrapping <span> is deliberately given NO explicit
// width (just display:inline-block + position:relative) and the <img>
// inside it is sized in PIXELS, never a percentage — a percentage would
// resolve against this auto-width wrapper's own box, which is exactly the
// circular shrink-to-fit sizing hazard described where the toolbar's own
// resize handles are built for the *rendered* page (see
// entities/detail.html's note-image resize script). Pixels sidestep that
// entirely: the wrapper just shrinks to hug whatever pixel width the image
// ends up at, so the corner handles (plain CSS, anchored to the wrapper's
// own corners) are always correctly placed with no JS position bookkeeping.
const NDFMT_PREVIEW_BASE_PX = 160; // maps size:100 to this many preview pixels
const NDFMT_PREVIEW_MIN_PX = 40;
const NDFMT_PREVIEW_MAX_PX = 260;

// Every ![]() occurrence in the field, in order, each with its exact
// [start,end) span in `text` — used both to render one preview thumbnail
// per image and, on that thumbnail's own resize, to rewrite precisely that
// occurrence (by span, not by URL — so two references to the same image
// resize independently, unlike the rendered-page version which can't
// address a specific occurrence since it only has the <img> it was clicked
// on, not the textarea's raw source).
function ndFmtImageMatches(text) {
  const out = [];
  NDFMT_IMAGE_REF_RE.lastIndex = 0;
  let m;
  while ((m = NDFMT_IMAGE_REF_RE.exec(text))) {
    const parsed = ndFmtParseImageRef(m[0]);
    if (parsed) out.push({ start: m.index, end: m.index + m[0].length, parsed });
  }
  return out;
}

function ndFmtRefreshPreview(ta, preview) {
  const matches = ndFmtImageMatches(ta.value).filter(
    (m) => !NDFMT_AV_TITLES.has(m.parsed.title) && ndFmtSafeImageUrl(m.parsed.url)
  );
  preview.innerHTML = "";
  preview.classList.toggle("fmt-preview-empty", matches.length === 0);
  matches.forEach((match) => {
    const item = document.createElement("span");
    item.className = "fmt-preview-item";
    const img = document.createElement("img");
    img.src = ndFmtSafeImageUrl(match.parsed.url);
    img.alt = match.parsed.alt || "";
    const sizeMatch = match.parsed.title && NDFMT_SIZE_TITLE_RE.exec(match.parsed.title);
    const pct = sizeMatch
      ? Math.max(NDFMT_MIN_SIZE_PCT, Math.min(NDFMT_MAX_SIZE_PCT, parseInt(sizeMatch[1], 10)))
      : 100;
    const startPx = Math.max(NDFMT_PREVIEW_MIN_PX, Math.min(NDFMT_PREVIEW_MAX_PX, NDFMT_PREVIEW_BASE_PX * pct / 100));
    img.style.width = startPx + "px";
    item.appendChild(img);

    ["nw", "ne", "sw", "se"].forEach((corner) => {
      const handle = document.createElement("span");
      handle.className = "fmt-preview-handle fmt-preview-handle-" + corner;
      handle.title = "Drag to resize";
      item.appendChild(handle);
      const growsRight = corner === "ne" || corner === "se";
      handle.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        handle.setPointerCapture(e.pointerId);
        const startX = e.clientX;
        const dragStartWidth = img.getBoundingClientRect().width;

        function onMove(ev) {
          const delta = growsRight ? ev.clientX - startX : startX - ev.clientX;
          const px = Math.max(NDFMT_PREVIEW_MIN_PX, Math.min(NDFMT_PREVIEW_MAX_PX, dragStartWidth + delta));
          img.style.width = px + "px";
        }
        function onUp() {
          handle.removeEventListener("pointermove", onMove);
          const finalPx = img.getBoundingClientRect().width;
          let newPct = Math.round((finalPx / NDFMT_PREVIEW_BASE_PX) * 100);
          newPct = Math.max(NDFMT_MIN_SIZE_PCT, Math.min(NDFMT_MAX_SIZE_PCT, newPct));
          const title = newPct === 100 ? "" : ` "size:${newPct}"`;
          const rebuilt = `![${match.parsed.alt}](${match.parsed.url}${title})`;
          ta.value = ta.value.slice(0, match.start) + rebuilt + ta.value.slice(match.end);
          ta.dispatchEvent(new Event("input", { bubbles: true }));
          ndFmtRefreshPreview(ta, preview);
        }
        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", onUp, { once: true });
      });
    });

    preview.appendChild(item);
  });
}

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

  const imgBtn = ndFmtButton("🖼", "Insert image, audio, or video (or paste/drag one in)", () => ndFmtInsertImage(ta, imgBtn));
  bar.appendChild(imgBtn);

  // Resize has no selection of its own to click — see ndFmtFindImageRefAtCursor's
  // own comment — so the popup is declared before the button that toggles it.
  const resizePopup = document.createElement("div");
  resizePopup.className = "fmt-popup";
  NDFMT_RESIZE_PCTS.forEach((pct) => {
    const sizeBtn = document.createElement("button");
    sizeBtn.type = "button";
    sizeBtn.textContent = pct + "%";
    sizeBtn.onclick = () => {
      ndFmtSetImageSize(ta, pct);
      resizePopup.classList.remove("fmt-popup-open");
    };
    resizePopup.appendChild(sizeBtn);
  });
  const resizeBtn = ndFmtButton("📐", "Resize the image at the cursor", () => {
    resizePopup.classList.toggle("fmt-popup-open");
  });
  bar.appendChild(resizeBtn);
  bar.appendChild(resizePopup);

  const importBtn = ndFmtButton("📄 Import .md", "Import a .md file into this field (or drag and drop one onto the text area)", () => ndFmtImportMdFile(ta, importBtn));
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

  const preview = document.createElement("div");
  preview.className = "fmt-preview";
  ta.parentNode.insertBefore(preview, ta.nextSibling);
  ndFmtRefreshPreview(ta, preview);
  let previewDebounce;
  ta.addEventListener("input", () => {
    clearTimeout(previewDebounce);
    previewDebounce = setTimeout(() => ndFmtRefreshPreview(ta, preview), 250);
  });
}

function ndFmtInit() {
  document.querySelectorAll("textarea[data-fmt]").forEach((ta) => {
    if (ta.dataset.fmtReady) return;
    ta.dataset.fmtReady = "1";
    ndFmtBuildToolbar(ta);
    ndFmtSetupDragDrop(ta);
    ndFmtSetupPaste(ta);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", ndFmtInit);
} else {
  ndFmtInit();
}
