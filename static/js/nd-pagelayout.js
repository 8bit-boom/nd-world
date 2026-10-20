"use strict";
// Layouts that are a plain choice of one mode for a page (rules, search results, facts, maps, parties ...): `data-pl="<section>"` on the element that
// gets `data-view="<mode>"`, `data-pl-modes` (the spec's modes), `data-pl-default`, `data-pl-saved` (the account's mode, rendered by the server) and a
// `[data-pl-bar="<section>"]` for the buttons (templates/_views.html: `pl_attrs` / `pl_bar`). The CSS does the arranging; a page whose layout also
// changes behaviour listens for the "nd-pl-change" event (detail {section, mode}). Choice: ?layout=<mode> (or ?layout_<section>=) > the account >
// this browser > the default; every change is saved to the account (POST /api/view-prefs/<section>). Pure bits exported for tests/test_pagelayout.py.

(function (root) {
  function pickMode(ids, def, url, saved, local) {
    const ok = (m) => ids.indexOf(m) >= 0;
    return [url, saved, local].find(ok) || (ok(def) ? def : ids[0] || "");
  }
  const core = { pickMode };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.ndPageLayoutCore = core;
  if (typeof document === "undefined") return;

  const params = new URLSearchParams(location.search);
  const targets = Array.from(document.querySelectorAll("[data-pl]"));
  targets.forEach((el) => {
    const section = el.dataset.pl;
    let modes = [];
    try { modes = JSON.parse(el.dataset.plModes || "[]"); } catch (e) { modes = []; }
    const ids = modes.map((m) => m.id);
    const key = "nd_view:" + section;
    let local = "";
    try { local = (JSON.parse(localStorage.getItem(key) || "null") || {}).mode || ""; } catch (e) { local = ""; }
    let mode = pickMode(ids, el.dataset.plDefault, params.get("layout_" + section) || (targets.length === 1 ? params.get("layout") : "") || "", el.dataset.plSaved || "", local);
    const bar = document.querySelector('[data-pl-bar="' + section + '"]');
    function apply(notify) {
      el.dataset.view = mode;
      if (bar) bar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
      if (notify) el.dispatchEvent(new CustomEvent("nd-pl-change", { bubbles: true, detail: { section, mode } }));
    }
    if (bar && modes.length > 1) modes.forEach((m) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "ep-mode"; b.dataset.mode = m.id; b.title = m.label; b.textContent = m.icon + " " + m.label;
      b.addEventListener("click", () => {
        mode = m.id; apply(true);
        try { localStorage.setItem(key, JSON.stringify({ mode })); } catch (e) { /* not remembered */ }
        fetch("/api/view-prefs/" + encodeURIComponent(section), { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) }).catch(() => { /* the browser copy holds */ });
      });
      bar.append(b);
    });
    apply(false);
    el.dispatchEvent(new CustomEvent("nd-pl-ready", { bubbles: true, detail: { section, mode } }));
  });
})(typeof window !== "undefined" ? window : globalThis);
