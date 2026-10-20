"use strict";
// View layouts for a collection of items (pictures today; audio, video, notes, entities next - docs/VIEW_LAYOUTS_PLAN.md).
//
// A page marks a container `.nd-collection` whose direct children are `.nd-item`s carrying data-name / data-uses / data-date / data-size /
// data-source, and puts a `.nd-viewbar[data-for=<container id>][data-spec=<json from app/view_prefs.py>]` where the controls go. This file
// builds the controls, applies the chosen layout / size / sort / grouping / filters, remembers the choice per browser (localStorage
// "nd_view:<section>") and accepts it from the URL (?view= &size= &sort= &group= &filter=). Layouts are CSS (static/style.css, "View layouts"),
// so switching is instant and never reloads the page.
//
// The decisions (cleanState, plan, compareItems) are pure and run under Node (tests/test_nd_views.py); the DOM layer below needs a browser.
// A page that adds items later (a "Load more") calls `container.ndViews.refresh()`.

(function (root) {
  const SIZES = [1, 2, 3];
  const SIZE_NAMES = { 1: "Small", 2: "Medium", 3: "Large" };

  const ids = (list) => (Array.isArray(list) ? list.map((x) => x.id) : []);

  // A stored / URL-given choice as a usable one, field by field (mirrors app/view_prefs.py `clean`).
  function cleanState(raw, spec) {
    raw = raw && typeof raw === "object" ? raw : {};
    const modes = ids(spec.modes), sorts = ids(spec.sorts), groups = ids(spec.groups), filters = ids(spec.filters);
    const mode = modes.includes(raw.mode) ? raw.mode : spec.default;
    let size = parseInt(raw.size, 10);
    if (!SIZES.includes(size)) size = spec.default_size || 2;
    const sortRaw = String(raw.sort || "");
    const desc = sortRaw.startsWith("-");
    const key = desc ? sortRaw.slice(1) : sortRaw;
    const sort = sorts.includes(key) ? (desc ? "-" : "") + key : spec.default_sort;
    const group = groups.includes(raw.group) ? raw.group : (raw.group === "" ? "" : (spec.default_group || ""));     // (a section may open grouped; "" is a person choosing none)
    const chosen = Array.isArray(raw.filters) ? raw.filters.filter((f) => filters.includes(f)) : [];
    return { mode, size, sort, group, filters: chosen };
  }

  const num = (v) => (Number.isFinite(+v) ? +v : 0);
  const textOf = (v) => String(v == null ? "" : v).toLowerCase();

  // Order two items' metadata by a sort key ("name", "-size" ...). Ties fall back to the name so the order is always stable.
  function compareItems(a, b, sortKey) {
    const desc = sortKey.startsWith("-");
    const key = desc ? sortKey.slice(1) : sortKey;
    let c = 0;
    if (key === "name") c = textOf(a.name).localeCompare(textOf(b.name), undefined, { numeric: true });
    else c = num(a[key]) - num(b[key]);
    if (desc) c = -c;
    return c || textOf(a.name).localeCompare(textOf(b.name), undefined, { numeric: true }) || a.index - b.index;
  }

  const VISIBLE = "Visible to players", HIDDEN = "Hidden from players", NONE = "None";

  function groupLabel(meta, group) {
    if (group === "visibility") return meta.vis === "hidden" ? HIDDEN : VISIBLE;
    if (group === "usage") return num(meta.uses) > 0 ? "Used" : "Unused";
    if (group === "source") return meta.source ? meta.source : "Unused";
    // any other group key reads the item's data-g-<key> (a subtype, the first tag ...); an item without one goes under "None"
    return (meta.g && meta.g[group]) || NONE;
  }

  // What to show: which items are hidden by the filters, and the entries (group headings + items, in order) to lay out.
  // metas: [{index, name, uses, date, size, source}]  ->  { hidden: [index], entries: [{head: label, count} | {item: index}] }
  function plan(metas, state) {
    const hidden = [];
    let shown = metas.map((m, i) => ({ ...m, index: m.index == null ? i : m.index }));
    if (state.filters.includes("unused")) {
      shown = shown.filter((m) => {
        const keep = num(m.uses) === 0;
        if (!keep) hidden.push(m.index);
        return keep;
      });
    }
    if (state.filters.includes("hidden")) {
      shown = shown.filter((m) => {
        const keep = m.vis === "hidden";
        if (!keep) hidden.push(m.index);
        return keep;
      });
    }
    shown.sort((a, b) => compareItems(a, b, state.sort));
    if (!state.group) return { hidden, entries: shown.map((m) => ({ item: m.index })) };
    const buckets = new Map();
    shown.forEach((m) => {
      const label = groupLabel(m, state.group);
      if (!buckets.has(label)) buckets.set(label, []);
      buckets.get(label).push(m);
    });
    const order = [...buckets.keys()].sort((x, y) => {
      if (state.group === "usage") return (x === "Used" ? 0 : 1) - (y === "Used" ? 0 : 1);
      if (state.group === "visibility") return (x === VISIBLE ? 0 : 1) - (y === VISIBLE ? 0 : 1);
      if (x === "Unused" || x === NONE) return 1;           // "Unused" / "None" last
      if (y === "Unused" || y === NONE) return -1;
      // months follow the sort's direction (newest month first when the notes are newest-first); other groups are alphabetical
      if (state.group === "month") return state.sort.startsWith("-") ? y.localeCompare(x) : x.localeCompare(y);
      return x.localeCompare(y, undefined, { numeric: true });
    });
    const entries = [];
    order.forEach((label) => {
      entries.push({ head: label, count: buckets.get(label).length });
      buckets.get(label).forEach((m) => entries.push({ item: m.index }));
    });
    return { hidden, entries };
  }

  const core = { cleanState, compareItems, plan, SIZES };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.ndViewsCore = core;
  if (typeof document === "undefined") return;

  // ── the browser layer ──────────────────────────────────────────────────────────────────────────────

  const KEY = (section) => "nd_view:" + section;

  function readStored(section) {
    try { return JSON.parse(localStorage.getItem(KEY(section)) || "null"); } catch (e) { return null; }
  }
  function writeStored(section, state) {
    try { localStorage.setItem(KEY(section), JSON.stringify(state)); } catch (e) { /* private window / blocked storage: just not remembered */ }
  }
  function fromUrl() {
    const q = new URLSearchParams(location.search);
    const out = {};
    if (q.get("view")) out.mode = q.get("view");
    if (q.get("size")) out.size = q.get("size");
    if (q.get("sort")) out.sort = q.get("sort");
    if (q.get("group") != null) out.group = q.get("group");
    if (q.get("filter")) out.filters = q.get("filter").split(",");
    return out;
  }
  function writeUrl(state, spec) {
    try {
      const q = new URLSearchParams(location.search);
      ["view", "size", "sort", "group", "filter"].forEach((k) => q.delete(k));
      if (state.mode !== spec.default) q.set("view", state.mode);
      if (state.size !== (spec.default_size || 2)) q.set("size", state.size);
      if (state.sort !== spec.default_sort) q.set("sort", state.sort);
      if (state.group) q.set("group", state.group);
      if (state.filters.length) q.set("filter", state.filters.join(","));
      const s = q.toString();
      history.replaceState(null, "", location.pathname + (s ? "?" + s : "") + location.hash);
    } catch (e) { /* not worth failing for */ }
  }

  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "value") e.value = v;
      else e.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((kid) => { if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid))); });
    return e;
  }

  function metaOf(item, index) {
    const d = item.dataset;
    const g = {};
    Object.keys(d).forEach((k) => { if (k.startsWith("g") && k.length > 1 && k[1] === k[1].toUpperCase()) g[k[1].toLowerCase() + k.slice(2)] = d[k]; });   // data-g-subtype -> g.subtype
    return { index, name: d.name || "", uses: num(d.uses), date: num(d.date), size: num(d.size), num: num(d.num), source: d.source || "", vis: d.vis || "", g };
  }

  function attach(bar) {
    const collection = document.getElementById(bar.dataset.for);
    if (!collection || collection.ndViews) return;
    let spec;
    try { spec = JSON.parse(bar.dataset.spec); } catch (e) { return; }
    if (!spec || !spec.section) return;
    let saved = {};
    try { saved = JSON.parse(bar.dataset.saved || "{}") || {}; } catch (e) { saved = {}; }
    const account = bar.dataset.account === "1";
    // the account's choice wins over this browser's (it follows the person from device to device); the URL wins over both
    const base = account && Object.keys(saved).length ? saved : (readStored(spec.section) || {});
    let state = cleanState({ ...base, ...fromUrl() }, spec);
    let saveTimer = null;
    function sendToAccount() {
      saveTimer = null;
      // keepalive: the request still completes if the person navigates away straight after choosing
      fetch("/api/view-prefs/" + encodeURIComponent(spec.section), { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify(state) })
        .catch(() => { /* the browser copy still has it */ });
    }
    function saveToAccount() {
      if (!account) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(sendToAccount, 250);     // a slider drag fires many changes: only the last one is sent
    }
    // leaving the page with a choice still waiting: send it now rather than lose it
    window.addEventListener("pagehide", () => { if (saveTimer) { clearTimeout(saveTimer); sendToAccount(); } });
    let current = null;                              // the picture shown in the filmstrip's big preview
    let film = null;

    const items = () => Array.from(collection.querySelectorAll(":scope > .nd-item"));

    // ── controls ────────────────────────────────────────────────────────────────────────────────────
    const modeBtns = {};
    const modeGroup = el("div", { class: "nd-vb-modes", role: "radiogroup", "aria-label": "Layout" },
      spec.modes.map((m) => (modeBtns[m.id] = el("button", { type: "button", class: "nd-vb-mode", role: "radio", title: m.label, "aria-label": m.label,
        onclick: () => change({ mode: m.id }) }, el("span", { "aria-hidden": "true", text: m.icon }), el("span", { class: "nd-vb-mode-label", text: m.label })))));
    const slider = el("input", { type: "range", min: "1", max: "3", step: "1", "aria-label": "Size",
      oninput: (e) => change({ size: parseInt(e.target.value, 10) }) });
    const sizeWrap = el("label", { class: "nd-vb-size", title: "Size" }, el("span", { "aria-hidden": "true", text: "▫" }), slider, el("span", { "aria-hidden": "true", text: "▣" }));
    const sortSel = el("select", { "aria-label": "Sort by", onchange: (e) => change({ sort: (state.sort.startsWith("-") ? "-" : "") + e.target.value }) },
      spec.sorts.map((s) => el("option", { value: s.id, text: s.label })));
    const dirBtn = el("button", { type: "button", class: "nd-vb-dir", "aria-label": "Reverse the order",
      onclick: () => change({ sort: state.sort.startsWith("-") ? state.sort.slice(1) : "-" + state.sort }) });
    const groupSel = spec.groups.length ? el("select", { "aria-label": "Group by", onchange: (e) => change({ group: e.target.value }) },
      el("option", { value: "", text: "No grouping" }), spec.groups.map((g) => el("option", { value: g.id, text: g.label }))) : null;
    const filterBoxes = {};
    const filterWrap = spec.filters.map((f) => el("label", { class: "nd-vb-filter" },
      (filterBoxes[f.id] = el("input", { type: "checkbox", onchange: () => change({ filters: spec.filters.map((x) => x.id).filter((id) => filterBoxes[id].checked) }) })),
      " " + f.label));
    const count = el("span", { class: "nd-vb-count", role: "status", "aria-live": "polite" });
    bar.classList.add("nd-viewbar-ready");
    bar.replaceChildren(...[modeGroup, sizeWrap, el("span", { class: "nd-vb-sort" }, sortSel, dirBtn), groupSel, ...filterWrap, count].filter(Boolean));

    function change(patch) {
      state = cleanState({ ...state, ...patch }, spec);
      writeStored(spec.section, state);
      saveToAccount();
      writeUrl(state, spec);
      apply();
    }

    // ── the filmstrip's big preview ────────────────────────────────────────────────────────────────
    function ensureFilm() {
      if (film) return film;
      const img = el("img", { class: "nd-film-img", alt: "" });
      const cap = el("div", { class: "nd-film-cap" });
      film = {
        root: el("div", { class: "nd-film", hidden: true },
          el("button", { type: "button", class: "nd-film-nav", "aria-label": "Previous", text: "‹", onclick: () => step(-1) }),
          el("div", { class: "nd-film-stage" }, img, cap),
          el("button", { type: "button", class: "nd-film-nav", "aria-label": "Next", text: "›", onclick: () => step(1) }),
          el("div", { class: "nd-film-tools" },
            window.ndStage ? el("button", { type: "button", class: "nd-film-full", title: "Show on the second screen", text: "📺", onclick: () => { if (current) window.ndStage.showImage(srcOf(current), (current.querySelector("img") || {}).alt || ""); } }) : null,
            el("button", { type: "button", class: "nd-film-full", text: "⤢ Full size", onclick: () => { if (current && window.openLightbox) openLightbox(srcOf(current), current.dataset.name || ""); } }))),
        img, cap,
      };
      collection.parentNode.insertBefore(film.root, collection);
      return film;
    }
    const srcOf = (item) => { const i = item.querySelector("img"); return i ? (i.dataset.full || i.currentSrc || i.src) : ""; };
    // the big preview shows an upload at display size (?display=1, 1,600 px - the lightbox's rule); "Full size" and the second screen keep the original
    const displayOf = (src) => (/^\/uploads\/(?!maps\/)[^?#]+\.(jpe?g|png|webp|avif)$/i.test(src) && !/_(thumb|display)\.webp$/i.test(src)) ? src + "?display=1" : src;
    const visibleItems = () => items().filter((i) => !i.hidden);
    function show(item) {
      if (!item) return;
      current = item;
      items().forEach((i) => i.classList.toggle("nd-current", i === item));
      const f = ensureFilm();
      f.img.src = displayOf(srcOf(item));
      f.img.alt = (item.querySelector("img") || {}).alt || item.dataset.name || "";
      const own = item.querySelector("img");
      f.cap.textContent = (own && own.alt) || item.dataset.name || "";
      item.scrollIntoView({ block: "nearest", inline: "center" });
    }
    function step(delta) {
      const list = visibleItems();
      if (!list.length) return;
      const at = Math.max(0, list.indexOf(current));
      show(list[(at + delta + list.length) % list.length]);
    }

    // ── applying the choice ─────────────────────────────────────────────────────────────────────────
    function apply() {
      // a page that moved the items into containers of its own (the entity board's columns) puts them back first
      collection.dispatchEvent(new CustomEvent("nd-view-before"));
      const list = items();
      const result = plan(list.map(metaOf), state);
      const hide = new Set(result.hidden);
      collection.querySelectorAll(":scope > .nd-group-head").forEach((h) => h.remove());
      result.entries.forEach((entry) => {
        if (entry.head != null) {
          collection.appendChild(el("div", { class: "nd-group-head", role: "heading", "aria-level": "3" }, entry.head + " ", el("span", { class: "nd-group-count", text: "(" + entry.count + ")" })));
        } else {
          collection.appendChild(list[entry.item]);
        }
      });
      list.forEach((item, i) => {
        item.hidden = hide.has(i);
        // a hidden picture must never stay selected: "Delete selected" would act on something the person cannot see
        if (item.hidden) item.querySelectorAll('input[type="checkbox"]').forEach((c) => { c.checked = false; c.dispatchEvent(new Event("change", { bubbles: true })); });
      });
      collection.dataset.view = state.mode;
      collection.dataset.size = String(state.size);
      Object.entries(modeBtns).forEach(([id, b]) => b.setAttribute("aria-checked", id === state.mode ? "true" : "false"));
      slider.value = String(state.size);
      slider.title = SIZE_NAMES[state.size];
      sizeWrap.hidden = !(spec.size_modes || spec.modes.map((m) => m.id)).includes(state.mode);
      const desc = state.sort.startsWith("-");
      sortSel.value = desc ? state.sort.slice(1) : state.sort;
      dirBtn.textContent = desc ? "↓" : "↑";
      dirBtn.title = desc ? "Descending - click for ascending" : "Ascending - click for descending";
      if (groupSel) groupSel.value = state.group;
      Object.entries(filterBoxes).forEach(([id, box]) => { box.checked = state.filters.includes(id); });
      const shown = list.filter((i) => !i.hidden).length;
      count.textContent = shown === list.length ? list.length + " items" : shown + " of " + list.length;
      if (state.mode === "filmstrip") {
        ensureFilm().root.hidden = false;
        const vis = visibleItems();
        show(vis.includes(current) ? current : vis[0]);
      } else if (film) {
        film.root.hidden = true;
      }
      // pages with layout-specific behaviour (the soundboard's pads, the playlist's shared player) listen for this
      collection.dispatchEvent(new CustomEvent("nd-view-change", { bubbles: true, detail: { ...state } }));
    }

    // In the filmstrip a click on a picture selects it instead of doing what the picture does elsewhere (open the lightbox ...). Capture phase,
    // so the picture's own handler never runs; the tile's buttons and checkbox are not touched.
    collection.addEventListener("click", (e) => {
      if (state.mode !== "filmstrip" || !e.target.closest) return;
      const img = e.target.closest("img");
      const item = img && img.closest(".nd-item");
      if (!item || item.parentNode !== collection) return;
      e.preventDefault();
      e.stopPropagation();
      show(item);
    }, true);
    // The strip of thumbnails can be dragged sideways with the mouse and scrolled with the wheel (a touch screen scrolls it natively). A drag
    // that moved is not a click: the click that ends it is swallowed so it does not select a picture by accident.
    let drag = null;
    collection.addEventListener("pointerdown", (e) => {
      if (state.mode !== "filmstrip" || e.pointerType === "touch" || e.button !== 0) return;
      if (e.target.closest && e.target.closest("button, input, a, label.nd-keep")) return;
      drag = { x: e.clientX, left: collection.scrollLeft, moved: false };
    });
    window.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x;
      if (!drag.moved && Math.abs(dx) < 5) return;
      if (!drag.moved) { drag.moved = true; collection.classList.add("nd-dragging"); }
      collection.scrollLeft = drag.left - dx;
    });
    window.addEventListener("pointerup", () => {
      if (!drag) return;
      const moved = drag.moved;
      drag = null;
      collection.classList.remove("nd-dragging");
      if (moved) {
        const swallow = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
        collection.addEventListener("click", swallow, { capture: true, once: true });
        setTimeout(() => collection.removeEventListener("click", swallow, true), 0);
      }
    });
    collection.addEventListener("wheel", (e) => {
      if (state.mode !== "filmstrip" || collection.scrollWidth <= collection.clientWidth || Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;
      e.preventDefault();                           // the wheel moves the strip sideways, not the page
      collection.scrollLeft += e.deltaY;
    }, { passive: false });

    document.addEventListener("keydown", (e) => {
      if (state.mode !== "filmstrip" || e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
      if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    });

    collection.ndViews = { refresh: apply, state: () => ({ ...state }) };
    apply();
  }

  function init() { document.querySelectorAll(".nd-viewbar").forEach(attach); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})(typeof window !== "undefined" ? window : globalThis);
