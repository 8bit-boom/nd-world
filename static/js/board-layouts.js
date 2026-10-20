"use strict";
// The board's reading layouts (templates/board.html, section "board_view"): Canvas (today), Outline (the nodes as a tree following their
// connections) and Timeline (event nodes in date order). Both read the page's own `nodes` / `edges` - nothing is copied or saved - and a click on
// an entry opens the node exactly like a click on its card. Pure helpers exported for tests/test_board_layouts.py.

(function (root) {
  // nodes [{id}], edges [{from,to}] -> [{id, depth, via}]: depth-first from the best-connected node, each node once, the rest as further roots
  function outlineTree(nodes, edges) {
    const ids = nodes.map((n) => n.id), known = new Set(ids), adj = new Map(ids.map((i) => [i, []]));
    edges.forEach((e) => { if (known.has(e.from) && known.has(e.to) && e.from !== e.to) { adj.get(e.from).push(e.to); adj.get(e.to).push(e.from); } });
    const degree = (i) => adj.get(i).length;
    const seen = new Set(), out = [];
    const order = ids.slice().sort((a, b) => degree(b) - degree(a));          // stable: ties keep page order
    const visit = (id, depth, via) => {
      seen.add(id); out.push({ id, depth, via });
      adj.get(id).slice().sort((a, b) => degree(b) - degree(a)).forEach((nx) => { if (!seen.has(nx)) visit(nx, depth + 1, id); });
    };
    order.forEach((id) => { if (!seen.has(id)) visit(id, 0, null); });
    return out;
  }
  // a sortable key from the text: an ISO date (2026-10-20), else a year (1888), else null
  function dateKey(text) {
    const s = String(text || "");
    let m = s.match(/\b(\d{1,4})-(\d{2})-(\d{2})\b/);
    if (m) return +m[1] * 10000 + +m[2] * 100 + +m[3];
    m = s.match(/\b(1\d{3}|2\d{3})\b/);
    return m ? +m[1] * 10000 : null;
  }
  // event nodes, dated ones first (by date), then undated ones left to right as laid out on the canvas
  function timelineOrder(nodes) {
    const ev = nodes.filter((n) => n.type === "event").map((n, i) => ({ n, i, k: dateKey(n.title) ?? dateKey(n.body) }));
    ev.sort((a, b) => (a.k === null) - (b.k === null) || (a.k === null ? (a.n.x || 0) - (b.n.x || 0) || a.i - b.i : a.k - b.k || a.i - b.i));
    return ev.map((e) => ({ id: e.n.id, key: e.k }));
  }
  const core = { outlineTree, dateKey, timelineOrder };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.boardLayoutsCore = core;
  if (typeof document === "undefined") return;

  const wrap = document.getElementById("brd-wrap");
  if (!wrap || !wrap.dataset.pl) return;
  const view = document.createElement("div");
  view.id = "brd-reading"; view.setAttribute("aria-live", "polite");
  wrap.appendChild(view);
  const byId = () => new Map(nodes.map((n) => [n.id, n]));
  const ICON = { person: "👤", location: "📍", event: "⚡", clue: "🔍", faction: "🏛", note: "📝" };
  function entry(n, extra) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "br-entry";
    const t = document.createElement("b"); t.textContent = (ICON[n.type] || "📝") + " " + (n.title || "Untitled");
    b.append(t);
    if (extra) { const s = document.createElement("small"); s.textContent = extra; b.append(s); }
    const body = (n.body || "").trim();
    if (body) { const p = document.createElement("span"); p.className = "br-body"; p.textContent = body.length > 240 ? body.slice(0, 240) + "…" : body; b.append(p); }
    b.addEventListener("click", () => { if (typeof openNodeModal === "function") openNodeModal(n.id); });
    return b;
  }
  function render() {
    const mode = wrap.dataset.view;
    view.replaceChildren();
    if (mode === "outline") {
      const m = byId();
      outlineTree(nodes, edges).forEach((o) => {
        const n = m.get(o.id), b = entry(n, o.via ? "linked from " + ((m.get(o.via) || {}).title || "?") : "");
        b.style.marginLeft = Math.min(o.depth, 8) * 1.4 + "rem";
        view.append(b);
      });
    } else if (mode === "timeline") {
      const m = byId(), list = timelineOrder(nodes);
      list.forEach((o) => view.append(entry(m.get(o.id), o.key === null ? "undated - in canvas order" : "")));
      if (!list.length) { const p = document.createElement("p"); p.className = "br-empty"; p.textContent = "No ⚡ Event nodes on this board yet. Dates are read from an event's title or text (2026-10-20, or a year)."; view.append(p); }
    }
    if (!view.children.length && mode === "outline") { const p = document.createElement("p"); p.className = "br-empty"; p.textContent = "This board has no nodes yet."; view.append(p); }
  }
  wrap.addEventListener("nd-pl-ready", render);
  wrap.addEventListener("nd-pl-change", render);
})(typeof window !== "undefined" ? window : globalThis);
