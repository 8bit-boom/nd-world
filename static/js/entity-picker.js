"use strict";
// Shared entity picker: a folder-tree of entities (grouped by Entity.folder,
// an "A/B/C" string split into a nested tree) with live client-side text
// search — no page reload, so it's safe to embed inside a form that has
// other unsaved state (e.g. a Session's Summary field). Originally built
// for the Session "NPCs Featured" picker (multi-select checkboxes into an
// existing form); factored out here so any other picker that needs "find
// an entity by browsing folders or searching" can reuse the same tree +
// search logic instead of re-implementing it.
//
// opts:
//   entities   — [{id, name, folder}, ...] (folder may be "" or "A/B/C")
//   mode       — "multi" (checkboxes, default) or "single" (click a row)
//   selected   — mode:"multi" only — an iterable of already-checked ids
//   fieldName  — mode:"multi" only — the checkbox `name` attribute (default "entity_ids")
//   onPick(entity) — mode:"single" only — called when a row is clicked
//   emptyText  — message shown when `entities` is empty
//
// Returns { render, filter } — render() is called once up front; filter(q)
// is wired to `searchInputEl`'s input event automatically if given, but is
// also exposed directly in case the caller drives search some other way.
function ndEntityPicker(containerEl, searchInputEl, opts) {
  const mode = opts.mode || "multi";
  const selected = new Set(opts.selected || []);
  const fieldName = opts.fieldName || "entity_ids";
  const emptyText = opts.emptyText || "No entities available.";

  function buildTree(entities) {
    const root = { children: {}, entities: [] };
    for (const e of entities) {
      if (!e.folder) { root.entities.push(e); continue; }
      const parts = e.folder.split('/');
      let node = root;
      for (const part of parts) {
        if (!node.children[part]) node.children[part] = { name: part, children: {}, entities: [] };
        node = node.children[part];
      }
      node.entities.push(e);
    }
    return root;
  }

  function entityRow(e) {
    const row = document.createElement(mode === "single" ? "div" : "label");
    row.className = "entity-picker-row";
    row.dataset.name = e.name.toLowerCase();
    row.style.cssText = "display:flex;align-items:center;gap:.5rem;font-size:.82rem;padding:.15rem 0";
    if (mode === "single") {
      row.style.cursor = "pointer";
      row.appendChild(document.createTextNode(e.name));
      row.onclick = () => opts.onPick(e);
    } else {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.name = fieldName;
      cb.value = e.id;
      cb.style.width = "auto";
      cb.checked = selected.has(e.id);
      row.append(cb, document.createTextNode(" " + e.name));
    }
    return row;
  }

  function renderNode(node, container, depth) {
    Object.keys(node.children).sort().forEach((key) => {
      const child = node.children[key];
      const details = document.createElement("details");
      details.open = true;
      details.className = "entity-picker-folder";
      details.style.marginLeft = (depth * 12) + "px";
      const summary = document.createElement("summary");
      summary.textContent = "📁 " + child.name;
      summary.style.cssText = "cursor:pointer;font-size:.8rem;color:var(--text-dim);padding:.2rem 0";
      const body = document.createElement("div");
      details.append(summary, body);
      // renderNode(child, ...) already appends child's own entities (as
      // well as its sub-folders) into `body` — appending them again here
      // would double them up.
      renderNode(child, body, depth + 1);
      container.appendChild(details);
    });
    node.entities.sort((a, b) => a.name.localeCompare(b.name)).forEach((e) => container.appendChild(entityRow(e)));
  }

  function render() {
    containerEl.innerHTML = "";
    if (!opts.entities.length) {
      const p = document.createElement("p");
      p.style.cssText = "color:var(--text-dim);font-size:.8rem;font-style:italic;margin:0";
      p.textContent = emptyText;
      containerEl.appendChild(p);
      return;
    }
    renderNode(buildTree(opts.entities), containerEl, 0);
  }

  function filter(query) {
    const q = (query || "").trim().toLowerCase();
    const rows = containerEl.querySelectorAll(".entity-picker-row");
    const folders = containerEl.querySelectorAll(".entity-picker-folder");
    if (!q) {
      rows.forEach((r) => r.style.display = "flex");
      folders.forEach((f) => { f.style.display = ""; f.open = true; });
      return;
    }
    folders.forEach((f) => f.style.display = "none");
    rows.forEach((row) => {
      const match = row.dataset.name.includes(q);
      row.style.display = match ? "flex" : "none";
      if (match) {
        let el = row.closest(".entity-picker-folder");
        while (el) { el.style.display = ""; el.open = true; el = el.parentElement.closest(".entity-picker-folder"); }
      }
    });
  }

  if (searchInputEl) searchInputEl.addEventListener("input", () => filter(searchInputEl.value));
  render();

  return { render, filter };
}
