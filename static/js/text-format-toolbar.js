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

// Client-side render of the inline-formatting subset only (no headings,
// lists, tables, links — those are markdown2's job server-side). Used for
// the board note card body, which has no server render pass.
function ndFmtRenderInline(text) {
  let html = ndFmtEscapeHtml(text || "");
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
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", ndFmtInit);
} else {
  ndFmtInit();
}
