"use strict";
// The entity list's Board layout, too: a column per subtype (or per first tag); a card can be dragged to another column - which changes its
// subtype through POST /api/entity/ID/field - or moved with the little menu on the card (a touch screen cannot drag). Every change shows a toast
// with Undo. The cards stay the very same elements (checkboxes and bulk actions keep working); the engine puts them back before it re-sorts.
//
// The entity list's Split layout (templates/entities/list.html; the other layouts are CSS from static/js/nd-views.js): the cards become a
// compact list on the left and the entity you pick is shown on the right without leaving the page - name, picture, summary, tags and its text.
//
// The preview comes from GET /api/entity/ID/preview, the same route the hover popup uses, so what it shows is exactly what that viewer may see
// ([gmonly] / per-player secrets are already stripped there). A click on a card selects it instead of opening the page (a click with Ctrl / Shift
// or the middle button still opens it normally); j / k or the arrow keys move through the list.

(function (root) {
  // Which index to select after a key: j / ArrowDown = next, k / ArrowUp = previous, clamped to the list. -1 = not a navigation key.
  function stepKey(key, current, count) {
    if (!count) return -1;
    if (key === "j" || key === "ArrowDown") return Math.min(count - 1, current + 1);
    if (key === "k" || key === "ArrowUp") return Math.max(0, current - 1);
    return -1;
  }
  const core = { stepKey };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.entityViewsCore = core;
  if (typeof document === "undefined") return;

  const list = document.getElementById("entity-cards");
  const pane = document.getElementById("ev-preview");
  if (!list || !pane) return;
  let mode = "grid", current = null, token = 0;

  const narrow = () => !!(window.matchMedia && window.matchMedia("(max-width: 720px)").matches);   // no room for two columns: the list opens pages as usual
  const items = () => Array.from(list.querySelectorAll(":scope > .nd-item")).filter((i) => !i.hidden);

  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k === "href") e.setAttribute("href", v);
      else e.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((kid) => { if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid))); });
    return e;
  }

  async function show(item) {
    if (!item) return;
    current = item;
    items().forEach((i) => i.classList.toggle("nd-current", i === item));
    item.scrollIntoView({ block: "nearest" });
    const id = item.dataset.entityId, mine = ++token;
    pane.replaceChildren(el("p", { class: "ev-wait", text: "Loading…" }));
    try {
      const res = await fetch("/api/entity/" + encodeURIComponent(id) + "/preview", { credentials: "same-origin" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      if (mine !== token) return;                                   // a newer pick is already loading
      const body = el("div", { class: "ev-body prose" });
      body.innerHTML = d.body_html || "";                          // already rendered and stripped for this viewer by the server
      pane.replaceChildren(...[
        d.image_url ? el("img", { class: "ev-img", src: d.thumb_url || d.image_url, alt: d.name }) : null,
        el("div", { class: "ev-kind", text: (d.kind_icon || "") + " " + d.kind + (d.subtype ? " · " + d.subtype : "") }),
        el("h2", { class: "ev-title", text: d.name }),
        d.summary ? el("p", { class: "ev-summary", text: d.summary }) : null,
        d.tags && d.tags.length ? el("div", { class: "tags" }, d.tags.map((t) => el("span", { class: "tag", text: t }))) : null,
        body,
        el("a", { class: "ev-open", href: item.querySelector("a.entity-card").getAttribute("href"), text: "Open the full page →" })].filter(Boolean));
    } catch (e) {
      if (mine === token) pane.replaceChildren(el("p", { class: "ev-wait", text: "Could not load the preview." }));
    }
  }

  // ── board ───────────────────────────────────────────────────────────────────────────────────────
  const canEdit = list.dataset.canEdit === "1";
  let boardEl = null, toast = null, toastTimer = null;
  const cap = (k) => k.charAt(0).toUpperCase() + k.slice(1);
  const boardKey = () => { const st = list.ndViews && list.ndViews.state(); return (st && st.group) || "subtype"; };
  const valueOf = (item, key) => item.dataset["g" + cap(key)] || "";

  function say(text, undo) {
    if (!toast) { toast = el("div", { class: "ev-toast", role: "status" }); document.body.appendChild(toast); }
    toast.replaceChildren(text + " ");
    if (undo) toast.append(el("button", { type: "button", class: "ev-toast-undo", text: "Undo" }));
    if (undo) toast.lastChild.addEventListener("click", () => { toast.hidden = true; undo(); });
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 9000);
  }

  async function setSubtype(item, value, quiet) {
    const old = item.dataset.gSubtype || "";
    if (old === value) return;
    try {
      const res = await fetch("/api/entity/" + encodeURIComponent(item.dataset.entityId) + "/field", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field: "subtype", value }) });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { say(d.error || "Could not move it (" + res.status + ")."); return; }
      item.dataset.gSubtype = d.value || "";
      placeInColumn(item);
      if (!quiet) say("Moved " + (item.dataset.name || "it") + " to " + (d.value || "None") + ".", () => setSubtype(item, old, true));
    } catch (e) { say("Could not reach the server."); }
  }

  function column(value) {
    let col = boardEl.querySelector('.ev-col[data-value="' + CSS.escape(value) + '"]');
    if (col) return col;
    col = el("div", { class: "ev-col", "data-value": value },
      el("div", { class: "ev-col-head" }, el("span", { class: "ev-col-name", text: value || "None" }), el("span", { class: "ev-col-count" })),
      el("div", { class: "ev-col-body" }));
    if (canEdit && boardKey() === "subtype") {
      const body = col.querySelector(".ev-col-body");
      body.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("ev-over"); });
      body.addEventListener("dragleave", () => col.classList.remove("ev-over"));
      body.addEventListener("drop", (e) => {
        e.preventDefault(); col.classList.remove("ev-over");
        const id = e.dataTransfer.getData("text/plain");
        const item = boardEl.querySelector('.nd-item[data-entity-id="' + CSS.escape(id) + '"]');
        if (item) setSubtype(item, value);
      });
    }
    boardEl.appendChild(col);
    return col;
  }
  function recount() { boardEl.querySelectorAll(".ev-col").forEach((c) => { c.querySelector(".ev-col-count").textContent = String(c.querySelectorAll(".nd-item").length); }); }
  function placeInColumn(item) { column(valueOf(item, boardKey())).querySelector(".ev-col-body").appendChild(item); if (boardEl) recount(); }

  function buildBoard() {
    flattenBoard();
    const key = boardKey(), its = items();
    boardEl = el("div", { class: "ev-board" });
    list.insertBefore(boardEl, list.firstChild);
    if (key === "subtype") { try { JSON.parse(list.dataset.subtypes || "[]").forEach((v) => column(v)); } catch (e) { /* no suggested columns */ } }
    its.forEach(placeInColumn);
    // columns with cards first (by name), then the empty ones a card could be dropped into
    const cols = Array.from(boardEl.querySelectorAll(".ev-col"));
    cols.sort((x, y) => (x.querySelector(".nd-item") ? 0 : 1) - (y.querySelector(".nd-item") ? 0 : 1) || x.dataset.value.localeCompare(y.dataset.value))
      .forEach((c) => boardEl.appendChild(c));
    if (canEdit && key === "subtype") {
      const values = cols.map((c) => c.dataset.value);
      its.forEach((item) => {
        item.draggable = true;
        item.addEventListener("dragstart", item._evDrag = (e) => { e.dataTransfer.setData("text/plain", item.dataset.entityId); e.dataTransfer.effectAllowed = "move"; });
        const sel = el("select", { class: "ev-move", "aria-label": "Move to a column" }, el("option", { value: "", text: "Move to…" }),
          values.map((v) => el("option", { value: v, text: v || "None" })));
        sel.addEventListener("change", () => { if (sel.selectedIndex > 0) setSubtype(item, sel.value); sel.selectedIndex = 0; });
        item.appendChild(sel);
      });
    } else {
      cols.forEach((c) => { if (!c.querySelector(".nd-item")) c.remove(); });
    }
    recount();
  }
  function flattenBoard() {
    if (!boardEl) return;
    boardEl.querySelectorAll(".nd-item").forEach((item) => {
      list.appendChild(item);
      item.draggable = false;
      if (item._evDrag) { item.removeEventListener("dragstart", item._evDrag); item._evDrag = null; }
      const sel = item.querySelector(":scope > .ev-move");
      if (sel) sel.remove();
    });
    boardEl.remove();
    boardEl = null;
  }
  list.addEventListener("nd-view-before", flattenBoard);

  function onMode(m) {
    mode = m;
    if (m === "board") buildBoard();
    pane.hidden = m !== "split";
    if (m !== "split" || narrow()) { token++; return; }
    const vis = items();
    show(vis.includes(current) ? current : vis[0]);
  }

  // In the split layout a plain click picks the entity; modified clicks and the middle button keep opening the page.
  list.addEventListener("click", (e) => {
    if (mode !== "split" || narrow() || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    if (e.target.closest && e.target.closest(".card-cb, input")) return;
    const card = e.target.closest && e.target.closest("a.entity-card");
    const item = card && card.closest(".nd-item");
    if (!item || item.parentNode !== list) return;
    e.preventDefault();
    show(item);
  });
  document.addEventListener("keydown", (e) => {
    if (mode !== "split" || narrow() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    const vis = items();
    const at = stepKey(e.key, vis.indexOf(current), vis.length);
    if (at >= 0) { e.preventDefault(); show(vis[at]); }
  });
  list.addEventListener("nd-view-change", (e) => onMode(e.detail.mode));
  if (list.dataset.view && list.dataset.view !== "grid") onMode(list.dataset.view);
  window.addEventListener("pagehide", flattenBoard);
})(typeof window !== "undefined" ? window : globalThis);
