"use strict";
// The private-notes page's two layouts that behave (templates/private_notes.html; the others are CSS from static/js/nd-views.js):
//
//   Outline: every note is one line (title + date); a click opens it in place, a second click closes it. Enter / Space on the line do the same
//     and the line says whether it is open (aria-expanded).
//   Reader ("split"): the notes become a list on the left and the one you pick is read on the right. The note's text is copied from the page
//     itself (the server already rendered it for this viewer), so nothing is fetched. j / k or the arrow keys move through the list.

(function (root) {
  // The note to read after a key: j / ArrowDown = next, k / ArrowUp = previous, clamped. -1 = not a navigation key.
  function stepKey(key, current, count) {
    if (!count) return -1;
    if (key === "j" || key === "ArrowDown") return Math.min(count - 1, current + 1);
    if (key === "k" || key === "ArrowUp") return Math.max(0, current - 1);
    return -1;
  }
  const core = { stepKey };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.notesViewsCore = core;
  if (typeof document === "undefined") return;

  const list = document.getElementById("pn-notes");
  const reader = document.getElementById("nv-reader");
  if (!list || !reader) return;
  let mode = "timeline", current = null;
  const notes = () => Array.from(list.querySelectorAll(":scope > .pn-note")).filter((n) => !n.hidden && n.style.display !== "none");
  const narrow = () => !!(window.matchMedia && window.matchMedia("(max-width: 720px)").matches);

  function show(note) {
    if (!note) return;
    current = note;
    notes().forEach((n) => n.classList.toggle("nd-current", n === note));
    note.scrollIntoView({ block: "nearest" });
    const body = note.querySelector(".pn-note-content");
    const head = document.createElement("h2");
    head.className = "nv-title";
    head.textContent = note.dataset.title || "";
    const date = document.createElement("div");
    date.className = "pn-note-date";
    date.textContent = (note.querySelector(".pn-note-date") || {}).textContent || "";
    const text = document.createElement("div");
    text.className = "pn-note-content rendered-md";
    text.innerHTML = body ? body.innerHTML : "";               // the server's own rendering for this viewer, copied as it is
    reader.replaceChildren(head, date, text);
  }

  // outline: toggle a note open / closed from its title line
  function setOpen(note, open) {
    note.classList.toggle("nv-open", open);
    const head = note.querySelector(".nv-line");
    if (head) head.setAttribute("aria-expanded", String(open));
  }
  function decorate() {
    list.querySelectorAll(":scope > .pn-note").forEach((note) => {
      if (note.querySelector(".nv-line")) return;
      const line = document.createElement("div");
      line.className = "nv-line";
      line.tabIndex = 0;
      line.setAttribute("role", "button");
      line.setAttribute("aria-expanded", "false");
      const t = document.createElement("span");
      t.className = "nv-line-title";
      t.textContent = note.dataset.title || "(note)";
      const d = document.createElement("span");
      d.className = "nv-line-date";
      d.textContent = (note.querySelector(".pn-note-date") || {}).textContent || "";
      line.append(t, d);
      note.insertBefore(line, note.firstChild);
      const toggle = () => {
        if (mode === "outline" || (mode === "split" && narrow())) setOpen(note, !note.classList.contains("nv-open"));   // a phone has no second column
        else if (mode === "split") show(note);
      };
      line.addEventListener("click", toggle);
      line.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
  }

  function onMode(m) {
    mode = m;
    decorate();
    reader.hidden = !(m === "split" && !narrow());
    if (m === "split" && !narrow()) { const v = notes(); show(v.includes(current) ? current : v[0]); }
  }
  document.addEventListener("keydown", (e) => {
    if (mode !== "split" || narrow() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    const v = notes();
    const at = stepKey(e.key, v.indexOf(current), v.length);
    if (at >= 0) { e.preventDefault(); show(v[at]); }
  });
  list.addEventListener("nd-view-change", (e) => onMode(e.detail.mode));
  decorate();
  if (list.dataset.view && list.dataset.view !== "timeline") onMode(list.dataset.view);
})(typeof window !== "undefined" ? window : globalThis);
