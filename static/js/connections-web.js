"use strict";
// An entity page's CONNECTIONS as a WEB (templates/entities/detail.html, layouts from static/js/nd-views.js): this entity in the middle, everything it
// links to (solid line, arrow) or that links to it (dashed line) around it, each one a link to its page. The nodes are made from the cards the page
// already has (so what a viewer may see is exactly what the cards show, and a filter / grouping choice never disagrees with the web).
// A phone has no room for it: there the web stays hidden and the cards are shown. The pure placement is exported for tests/test_connections_web.py.

(function (root) {
  // Positions for `count` nodes around a centre: one ring up to `perRing`, more rings (each a little wider) after that. Angles start at the top
  // and run clockwise; the rings are offset by half a step so the nodes of two rings do not line up. -> [{x, y, ring}]
  function ringLayout(count, w, h, perRing) {
    perRing = perRing || 10;
    const cx = w / 2, cy = h / 2, out = [];
    if (!count) return out;
    const rings = Math.ceil(count / perRing);
    const base = Math.min(w, h) / 2 - 60;                       // the outer ring leaves room for the labels
    for (let i = 0; i < count; i++) {
      const ring = Math.floor(i / perRing), inRing = Math.min(perRing, count - ring * perRing);
      const at = i - ring * perRing;
      const r = rings === 1 ? base * (count <= 4 ? 0.7 : 0.9) : base * (0.66 + 0.34 * (ring / (rings - 1)));
      const a = -Math.PI / 2 + (at / inRing) * 2 * Math.PI + (ring % 2 ? Math.PI / inRing : 0);
      out.push({ x: Math.round((cx + r * Math.cos(a)) * 10) / 10, y: Math.round((cy + r * Math.sin(a)) * 10) / 10, ring });
    }
    return out;
  }
  function clip(text, max) { text = String(text || ""); return text.length > max ? text.slice(0, max - 1) + "…" : text; }
  const core = { ringLayout, clip };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.connectionsWebCore = core;
  if (typeof document === "undefined") return;

  const list = document.getElementById("conn-items");
  if (!list) return;
  const NS = "http://www.w3.org/2000/svg", W = 760, H = 520, MAX = 36;
  let web = null;
  const svg = (tag, attrs, ...kids) => {
    const e = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v != null) e.setAttribute(k, v); });
    kids.forEach((k) => e.append(k));
    return e;
  };
  const items = () => Array.from(list.querySelectorAll(":scope > .nd-item")).filter((i) => !i.hidden);

  function build() {
    if (web) web.remove();
    const all = items(), shown = all.slice(0, MAX), pos = ringLayout(shown.length, W, H, 10);
    const g = svg("g");
    shown.forEach((item, i) => {
      const p = pos[i], out = item.dataset.dir === "out";
      g.append(svg("line", { x1: W / 2, y1: H / 2, x2: p.x, y2: p.y, class: "cw-edge" + (out ? "" : " cw-in") }));
    });
    shown.forEach((item, i) => {
      const p = pos[i], a = item.querySelector("a.rel-link");
      const node = svg("a", { href: a ? a.getAttribute("href") : "#", class: "cw-node" + (item.dataset.dir === "out" ? "" : " cw-in") },
        svg("title", null, document.createTextNode(item.dataset.label + (item.dataset.dir === "out" ? " (linked from here)" : " (links here)"))),
        svg("circle", { cx: p.x, cy: p.y, r: 20 }),
        svg("text", { x: p.x, y: p.y + 5, "text-anchor": "middle", class: "cw-icon" }, document.createTextNode(item.dataset.icon || "•")),
        svg("text", { x: p.x, y: p.y + 38, "text-anchor": "middle", class: "cw-label" }, document.createTextNode(clip(item.dataset.label, 22))));
      g.append(node);
    });
    const centre = svg("g", { class: "cw-centre" },
      svg("circle", { cx: W / 2, cy: H / 2, r: 30 }),
      svg("text", { x: W / 2, y: H / 2 + 6, "text-anchor": "middle", class: "cw-icon" }, document.createTextNode(list.dataset.centerIcon || "★")),
      svg("text", { x: W / 2, y: H / 2 + 52, "text-anchor": "middle", class: "cw-label cw-centre-label" }, document.createTextNode(clip(list.dataset.centerName, 28))));
    const frame = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Connections of " + (list.dataset.centerName || "this entity"), class: "cw-svg" }, g, centre);
    web = document.createElement("div");
    web.className = "cw-web";
    web.append(frame);
    const legend = document.createElement("p");
    legend.className = "cw-legend";
    legend.textContent = "Solid line: this page links to it · dashed line: it links here" + (all.length > MAX ? " · showing " + MAX + " of " + all.length + " (use the cards for the rest)" : "");
    web.append(legend);
    list.insertBefore(web, list.firstChild);
  }
  function drop() { if (web) { web.remove(); web = null; } }

  list.addEventListener("nd-view-before", drop);
  list.addEventListener("nd-view-change", (e) => { drop(); if (e.detail.mode === "web") build(); });
  if (list.dataset.view === "web") build();
})(typeof window !== "undefined" ? window : globalThis);
