"use strict";
// The folder navigation layouts of an entity list (/kind/<kind>): Tree (today's sidebar), Chips, Tiles, Columns and Drawer. The page builds the tree
// sidebar itself; this file builds the other four from the same folder list and counts, and the CSS (static/style.css "Folder layouts") shows the one the
// person chose (section "folders", applied by nd-pagelayout.js as data-view on .list-layout). The model is pure and tested under Node (tests/test_folders.py).
(function (root) {
  // folders: ["A", "A/B", ...] (a path may exist only as the parent of another); counts: {"A/B": 3} direct counts per path
  function model(folders, counts) {
    counts = counts || {};
    const paths = new Set();
    (folders || []).forEach((f) => {
      let cur = "";
      String(f).split("/").forEach((part) => { cur = cur ? cur + "/" + part : part; paths.add(cur); });
    });
    function count(path) {
      const prefix = path + "/";
      let n = counts[path] || 0;
      Object.keys(counts).forEach((k) => { if (k.indexOf(prefix) === 0) n += counts[k] || 0; });
      return n;
    }
    // the direct children of a folder ("" = the top level), by name
    function children(parent) {
      const prefix = parent ? parent + "/" : "";
      const out = [];
      paths.forEach((p) => {
        if (p.length > prefix.length && p.indexOf(prefix) === 0 && p.slice(prefix.length).indexOf("/") < 0) out.push(p);
      });
      out.sort((a, b) => a.localeCompare(b));
      return out.map((p) => ({ name: p.slice(prefix.length), path: p, count: count(p) }));
    }
    function parentOf(path) { const i = path.lastIndexOf("/"); return i < 0 ? "" : path.slice(0, i); }
    // the folders from the top down to `path`, each as a path
    function trail(path) {
      const out = [];
      let cur = "";
      (path ? path.split("/") : []).forEach((part) => { cur = cur ? cur + "/" + part : part; out.push(cur); });
      return out;
    }
    return { count, children, parentOf, trail, has: (p) => paths.has(p) };
  }

  const core = { model };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.ndFoldersCore = core;
  if (typeof document === "undefined") return;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function link(href, cls) { const a = el("a", cls); a.href = href; return a; }
  function badge(n) { return el("span", "fn-count", String(n)); }

  // cfg: {folders, counts, active (null = All, "" = Unfiled, else a path), total, unfiled, kind, url(path), allUrl, unfiledUrl}
  function build(cfg) {
    const nav = document.getElementById("folder-nav");
    if (!nav) return;
    const m = model(cfg.folders, cfg.counts);
    const active = cfg.active;
    const here = active ? active : "";
    const base = active ? m.parentOf(active) : "";          // the level the active folder sits in
    const named = (path) => path.slice(path.lastIndexOf("/") + 1);

    // ── Chips: the siblings of where you are, and what is inside it
    const chips = document.getElementById("fn-chips");
    if (chips) {
      chips.textContent = "";
      const row1 = el("div", "fn-row");
      const all = link(cfg.allUrl, "fn-chip" + (active === null ? " active" : ""));
      all.append("All ", badge(cfg.total));
      row1.append(all);
      if (cfg.unfiled) {
        const u = link(cfg.unfiledUrl, "fn-chip" + (active === "" ? " active" : ""));
        u.append("Unfiled ", badge(cfg.unfiled));
        row1.append(u);
      }
      if (active) {
        const up = link(base ? cfg.url(base) : cfg.allUrl, "fn-chip fn-up");
        up.textContent = "⬆ " + (base ? named(base) : "Top");
        row1.append(up);
      }
      m.children(base).forEach((c) => {
        const a = link(cfg.url(c.path), "fn-chip" + (c.path === active ? " active" : ""));
        a.append("📁 " + c.name + " ", badge(c.count));
        row1.append(a);
      });
      chips.append(row1);
      const inside = active ? m.children(active) : [];
      if (inside.length) {
        const row2 = el("div", "fn-row fn-inside");
        row2.append(el("span", "fn-label", "Inside"));
        inside.forEach((c) => { const a = link(cfg.url(c.path), "fn-chip"); a.append("📁 " + c.name + " ", badge(c.count)); row2.append(a); });
        chips.append(row2);
      }
    }

    // ── Tiles: big cards for the folders at this level (the ones inside the open folder, else the top level)
    const tiles = document.getElementById("fn-tiles");
    if (tiles) {
      tiles.textContent = "";
      const level = active ? m.children(active) : m.children("");
      const add = (href, icon, name, count, hint, cls) => {
        const a = link(href, "fn-tile" + (cls ? " " + cls : ""));
        a.append(el("span", "fn-tile-icon", icon), el("span", "fn-tile-name", name));
        if (count != null) a.append(el("span", "fn-tile-count", count + (count === 1 ? " item" : " items")));
        if (hint) a.append(el("span", "fn-tile-hint", hint));
        tiles.append(a);
      };
      if (active) add(base ? cfg.url(base) : cfg.allUrl, "⬆", "Up", null, base ? named(base) : "All " + cfg.kind + "s", "fn-tile-up");
      if (active === null && cfg.unfiled) add(cfg.unfiledUrl, "📄", "Unfiled", cfg.unfiled, "", "");
      level.forEach((c) => {
        const kids = m.children(c.path);
        add(cfg.url(c.path), kids.length ? "🗂" : "📁", c.name, c.count, kids.length ? kids.slice(0, 3).map((k) => k.name).join(" · ") + (kids.length > 3 ? " …" : "") : "", "");
      });
    }

    // ── Columns: one column per level down to the open folder, then the folders inside it
    const cols = document.getElementById("fn-columns");
    if (cols) {
      cols.textContent = "";
      const path = m.trail(here);
      const levels = [""].concat(path);                       // the parents whose children get a column: the top, then every folder down to the open one
      levels.forEach((parent, i) => {
        const kids = m.children(parent);
        if (!kids.length && i > 0) return;
        const col = el("div", "fn-col");
        if (i === 0) {
          const a = link(cfg.allUrl, "fn-item" + (active === null ? " active" : ""));
          a.append(el("span", "fn-name", "All"), badge(cfg.total));
          col.append(a);
          if (cfg.unfiled) {
            const u = link(cfg.unfiledUrl, "fn-item" + (active === "" ? " active" : ""));
            u.append(el("span", "fn-name", "Unfiled"), badge(cfg.unfiled));
            col.append(u);
          }
        }
        const open = path[i] || null;                        // the folder of this column that leads on to the next column
        kids.forEach((c) => {
          const has = m.children(c.path).length > 0;
          const a = link(cfg.url(c.path), "fn-item" + (c.path === active ? " active" : "") + (c.path === open && c.path !== active ? " open" : ""));
          a.append(el("span", "fn-name", "📁 " + c.name), badge(c.count));
          if (has) a.append(el("span", "fn-more", "▸"));
          col.append(a);
        });
        cols.append(col);
      });
    }

    // ── Drawer: the tree sidebar slides in over the page
    const layout = document.querySelector(".list-layout");
    const btn = document.getElementById("fn-drawer-btn");
    const back = document.getElementById("fn-backdrop");
    function drawer(open) { if (layout) layout.classList.toggle("fn-open", open); }
    if (btn) btn.addEventListener("click", () => drawer(!(layout && layout.classList.contains("fn-open"))));
    if (back) back.addEventListener("click", () => drawer(false));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") drawer(false); });
    if (layout) layout.addEventListener("nd-pl-change", () => { drawer(false); scrollColumns(); });
    function scrollColumns() { if (cols) cols.scrollLeft = cols.scrollWidth; }
    scrollColumns();
    if (layout) layout.addEventListener("nd-pl-ready", scrollColumns);
  }
  root.ndFolders = { build };
})(typeof window !== "undefined" ? window : globalThis);
