// Right-click on an entity - for GMs and assistants, on any page:
//   * a link to an entity (/entity/12): open in a tab / floating window / second screen, copy the link, and ✏ Rename
//   * the title of an entity page: ✏ Rename this entity
// Shift + right-click still gives the browser's own menu. On an entity page's text the richer menu in text-format-toolbar.js takes over for
// links (it calls window.ndRenameEntity too), so this file stays out of its way.
(function () {
  function notice(msg, bad, action) {
    var n = document.createElement("div");
    n.className = "fmt-notice" + (bad ? " fmt-notice-bad" : "");
    n.setAttribute("role", "status");
    var t = document.createElement("span");
    t.textContent = msg;
    n.appendChild(t);
    if (action) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "fmt-notice-action"; b.textContent = action.label;
      b.onclick = function () { n.remove(); action.run(); };
      n.appendChild(b);
    }
    document.body.appendChild(n);
    setTimeout(function () { n.remove(); }, action ? 12000 : bad ? 7000 : 2500);
  }

  async function rename(id, name) {
    var res = await fetch("/api/entity/" + id + "/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name }) });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || data.detail || ("That did not work (" + res.status + ")."));
    return data;
  }

  // everything on the page that shows this entity's name follows the rename: the text inside links to it that is exactly the old name
  function showName(id, oldName, newName) {
    var path = "/entity/" + id;
    document.querySelectorAll("a[href]").forEach(function (a) {
      var h = a.getAttribute("href");
      if (!(h === path || h.indexOf(path + "?") === 0 || h.indexOf(path + "#") === 0)) return;
      var w = document.createTreeWalker(a, NodeFilter.SHOW_TEXT), n;
      while ((n = w.nextNode())) { if (n.textContent.trim() === oldName) n.textContent = n.textContent.replace(oldName, newName); }
    });
    var art = document.querySelector("article.detail[data-entity-id='" + id + "']");
    if (art) {
      var h1 = art.querySelector(".detail-meta > h1");
      if (h1 && h1.textContent.trim() === oldName) h1.textContent = newName;
      if (document.title.indexOf(oldName) >= 0) document.title = document.title.replace(oldName, newName);
    }
  }

  // the entity's real current name (a link's text may carry an icon or a kind), then the dialog
  async function start(id, hint) {
    var current = hint || "";
    try {
      var r = await fetch("/api/entity/" + id + "/preview");
      if (r.ok) current = (await r.json()).name || current;
    } catch (e) { /* the hint will do */ }
    dialog(id, current);
  }

  function dialog(id, current) {
    var back = document.createElement("div");
    back.className = "fmt-modal-back";
    back.innerHTML = '<div class="fmt-modal" role="dialog" aria-modal="true" aria-label="Rename entity" style="max-width:26rem">' +
      '<h3>✏ Rename</h3><div class="fmt-modal-custom"><input type="text" maxlength="256" aria-label="New name" style="font-size:16px"></div>' +
      '<p class="nd-rename-hint" style="margin:0 0 .6rem;font-size:.8rem;color:var(--text-dim)">Text elsewhere that mentions the old name stays as written; it just stops auto-linking.</p>' +
      '<div class="fmt-modal-foot"><button type="button" class="fmt-modal-use">Save</button><button type="button" class="fmt-modal-cancel">Cancel</button></div></div>';
    var q = function (s) { return back.querySelector(s); };
    var input = q("input"), save = q(".fmt-modal-use"), hint = q(".nd-rename-hint");
    input.value = current || "";
    var close = function () { back.remove(); document.removeEventListener("keydown", onKey, true); };
    var onKey = function (e) { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    var go = async function () {
      var name = input.value.replace(/\s+/g, " ").trim();
      if (!name) { hint.textContent = "A name cannot be empty."; return; }
      if (name === current) { close(); return; }
      save.disabled = true;
      try {
        var out = await rename(id, name);
        close();
        showName(id, out.old, out.name);
        notice("Renamed.", false, { label: "Undo", run: async function () {
          try { var back2 = await rename(id, out.old); showName(id, back2.old, back2.name); notice("Name put back."); }
          catch (e) { notice(e.message, true); }
        } });
      } catch (e) { save.disabled = false; hint.textContent = e.message; hint.style.color = "var(--neon2)"; }
    };
    save.onclick = go;
    q(".fmt-modal-cancel").onclick = close;
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } });
    back.addEventListener("mousedown", function (e) { if (e.target === back) close(); });
    document.body.appendChild(back);
    input.focus(); input.select();
  }
  window.ndRenameEntity = start;

  var menu = null;
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  document.addEventListener("mousedown", function (e) { if (menu && !menu.contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeMenu(); });
  window.addEventListener("scroll", closeMenu, true);
  window.addEventListener("resize", closeMenu);

  function open(x, y, items) {
    closeMenu();
    menu = document.createElement("div");
    menu.className = "fmt-ctx-menu";
    menu.setAttribute("role", "menu");
    menu.style.left = x + "px"; menu.style.top = y + "px";
    items.forEach(function (it) {
      var b = document.createElement("button");
      b.type = "button"; b.setAttribute("role", "menuitem"); b.textContent = it.label;
      if (it.title) b.title = it.title;
      b.onclick = function () { closeMenu(); it.run(); };
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    var r = menu.getBoundingClientRect();
    if (r.right > window.innerWidth) menu.style.left = Math.max(0, x - (r.right - window.innerWidth) - 8) + "px";
    if (r.bottom > window.innerHeight) menu.style.top = Math.max(0, y - (r.bottom - window.innerHeight) - 8) + "px";
    menu.querySelector("button").focus({ preventScroll: true });
  }

  function entityOf(a) {
    try {
      var u = new URL(a.getAttribute("href"), location.href);
      var m = u.origin === location.origin && /^\/entity\/(\d+)\/?$/.exec(u.pathname);
      return m ? { id: +m[1], url: u.pathname } : null;
    } catch (e) { return null; }
  }

  document.addEventListener("contextmenu", function (e) {
    if (e.shiftKey || e.defaultPrevented) return;
    var t = e.target;
    if (!t.closest || t.closest("textarea, input, select")) return;
    var link = t.closest("a[href]");
    var target = link && entityOf(link);
    if (target) {
      if (window.ndFmtOpenMenu && t.closest("[data-markup-entity]")) return;        // the richer menu on an entity page's text
      var name = link.textContent.trim();
      e.preventDefault();
      var items = [{ label: "↗ Open in a new tab", run: function () { window.open(target.url, "_blank"); } }];
      if (window.ndFloat) items.push({ label: "🪟 Open in a floating window", title: "A small window you can park on another monitor", run: function () { window.ndFloat(target.url, { title: name }); } });
      items.push({ label: "📺 Show on the second screen", title: "Sends this entity to the players' display window", run: function () {
        fetch("/api/display/show", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "entity", entity_id: target.id }) })
          .then(function (r) { notice(r.ok ? "Sent to the second screen." : "Could not send it.", !r.ok); });
      } });
      items.push({ label: "📋 Copy link", run: function () { navigator.clipboard && navigator.clipboard.writeText(location.origin + target.url).then(function () { notice("Copied."); }); } });
      items.push({ label: "✏ Rename…", title: "Change this entity's name", run: function () { start(target.id, name); } });
      open(e.pageX, e.pageY, items);
      return;
    }
    // the title of an entity page
    var h1 = t.closest(".detail-meta > h1");
    var art = h1 && h1.closest("article.detail[data-entity-id], article.detail[data-markup-entity]");
    if (art && !window.getSelection().toString().trim()) {
      var id = art.getAttribute("data-entity-id") || art.getAttribute("data-markup-entity");
      e.preventDefault();
      open(e.pageX, e.pageY, [{ label: "✏ Rename this entity…", run: function () { start(+id, h1.textContent.trim()); } }]);
    }
  });
})();
