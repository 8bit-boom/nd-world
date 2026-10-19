"use strict";
// A BOARD layout for any collection (the entity list's subtype board, the quest board): a column per value of one field, the cards stacked in
// them, a card dragged (or moved with its small menu - a touch screen cannot drag) to another column, and a toast with Undo after every move.
//
//   const board = ndBoard.attach(collection, {
//     mode: "board",                          // the layout name this board is (default "board")
//     key: () => "subtype",                   // which data-g-<key> the columns come from (read when the board is built)
//     columns: () => ["active", "complete"],  // columns to show even when empty (suggested values), optional
//     label: (value) => value || "None",      // a column's title, optional
//     editable: () => true,                   // may cards be moved? (permission AND a key that can change)
//     idOf: (item) => item.dataset.id,        // what a dropped card is called in the drag data
//     move: async (item, value) => stored | null   // do the change on the server; the stored value, or null when it failed (say() was used)
//     noun: (item) => "Quest name",           // for the toast, optional
//   });
//
// The cards stay the very same elements (checkboxes, bulk actions and links keep working). The engine (nd-views.js) tells the board to put the
// cards back (`nd-view-before`) before it re-sorts, and `nd-view-change` rebuilds it. Pure helpers are exported for tests/test_nd_board.py.

(function (root) {
  // Columns in display order: those holding cards first (by name), then the empty ones a card could be dropped into.
  function orderColumns(columns) {
    return columns.slice().sort((a, b) => (a.count ? 0 : 1) - (b.count ? 0 : 1) || String(a.value).localeCompare(String(b.value)));
  }
  // The values to make columns for: suggested ones, then any other value the cards have.
  function columnValues(suggested, cardValues) {
    const seen = new Set(), out = [];
    (suggested || []).concat(cardValues || []).forEach((v) => { v = v == null ? "" : String(v); if (!seen.has(v)) { seen.add(v); out.push(v); } });
    return out;
  }
  const core = { orderColumns, columnValues };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.ndBoardCore = core;
  if (typeof document === "undefined") return;

  const cap = (k) => k.charAt(0).toUpperCase() + k.slice(1);
  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else e.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((kid) => { if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid))); });
    return e;
  }

  let toast = null, toastTimer = null;
  function say(text, undo) {
    if (!toast) { toast = el("div", { class: "nb-toast", role: "status" }); document.body.appendChild(toast); }
    toast.replaceChildren(text + " ");
    if (undo) {
      const b = el("button", { type: "button", class: "nb-toast-undo", text: "Undo" });
      b.addEventListener("click", () => { toast.hidden = true; undo(); });
      toast.append(b);
    }
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 9000);
  }

  function attach(collection, opts) {
    const mode = opts.mode || "board";
    const label = opts.label || ((v) => v || "None");
    const itemsFlat = () => Array.from(collection.querySelectorAll(":scope > .nd-item")).filter((i) => !i.hidden);
    let boardEl = null, key = "";
    const valueOf = (item) => item.dataset["g" + cap(key)] || "";
    const canMove = () => !!(opts.editable && opts.editable());

    function column(value) {
      let col = Array.from(boardEl.children).find((c) => c.dataset.value === value);
      if (col) return col;
      col = el("div", { class: "nb-col", "data-value": value },
        el("div", { class: "nb-col-head" }, el("span", { class: "nb-col-name", text: label(value) }), el("span", { class: "nb-col-count" })),
        el("div", { class: "nb-col-body" }));
      if (canMove()) {
        const body = col.querySelector(".nb-col-body");
        body.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("nb-over"); });
        body.addEventListener("dragleave", () => col.classList.remove("nb-over"));
        body.addEventListener("drop", (e) => {
          e.preventDefault();
          col.classList.remove("nb-over");
          const id = e.dataTransfer.getData("text/plain");
          const item = Array.from(boardEl.querySelectorAll(".nd-item")).find((i) => String(opts.idOf(i)) === id);
          if (item) moveTo(item, value);
        });
      }
      boardEl.appendChild(col);
      return col;
    }
    function recount() { boardEl.querySelectorAll(".nb-col").forEach((c) => { c.querySelector(".nb-col-count").textContent = String(c.querySelectorAll(".nd-item").length); }); }
    function place(item) { column(valueOf(item)).querySelector(".nb-col-body").appendChild(item); recount(); }

    async function moveTo(item, value, quiet) {
      const old = valueOf(item);
      if (old === value) return;
      const stored = await opts.move(item, value);
      if (stored == null) return;
      item.dataset["g" + cap(key)] = stored;
      place(item);
      if (!quiet) say("Moved " + (item.dataset.name || "it") + " to " + label(stored) + ".", () => moveTo(item, old, true));
    }

    function build() {
      flatten();
      key = opts.key();
      const cards = itemsFlat();
      boardEl = el("div", { class: "nb-board" });
      collection.insertBefore(boardEl, collection.firstChild);
      columnValues(opts.columns ? opts.columns() : [], cards.map(valueOf)).forEach(column);
      cards.forEach(place);
      const cols = Array.from(boardEl.querySelectorAll(".nb-col"));
      orderColumns(cols.map((c) => ({ value: c.dataset.value, count: c.querySelectorAll(".nd-item").length, el: c })))
        .forEach((c) => boardEl.appendChild(c.el));
      if (canMove()) {
        const values = Array.from(boardEl.querySelectorAll(".nb-col")).map((c) => c.dataset.value);
        cards.forEach((item) => {
          item.draggable = true;
          item.addEventListener("dragstart", item._nbDrag = (e) => { e.dataTransfer.setData("text/plain", String(opts.idOf(item))); e.dataTransfer.effectAllowed = "move"; });
          const sel = el("select", { class: "nb-move", "aria-label": "Move to a column" }, el("option", { value: "", text: "Move to…" }),
            values.map((v) => el("option", { value: v, text: label(v) })));
          sel.addEventListener("change", () => { if (sel.selectedIndex > 0) moveTo(item, sel.value); sel.selectedIndex = 0; });
          item.appendChild(sel);
        });
      } else {
        // nobody can move a card: columns with nothing in them would only be noise
        Array.from(boardEl.querySelectorAll(".nb-col")).forEach((c) => { if (!c.querySelector(".nd-item")) c.remove(); });
      }
      recount();
    }
    function flatten() {
      if (!boardEl) return;
      boardEl.querySelectorAll(".nd-item").forEach((item) => {
        collection.appendChild(item);
        item.draggable = false;
        if (item._nbDrag) { item.removeEventListener("dragstart", item._nbDrag); item._nbDrag = null; }
        const sel = item.querySelector(":scope > .nb-move");
        if (sel) sel.remove();
      });
      boardEl.remove();
      boardEl = null;
    }

    collection.addEventListener("nd-view-before", flatten);
    collection.addEventListener("nd-view-change", (e) => { if (e.detail.mode === mode) build(); });
    window.addEventListener("pagehide", flatten);
    if (collection.dataset.view === mode && collection.ndViews) build();
    return { build, flatten, say };
  }

  root.ndBoard = { attach, say };
})(typeof window !== "undefined" ? window : globalThis);
