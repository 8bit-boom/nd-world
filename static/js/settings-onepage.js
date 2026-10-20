"use strict";
// Settings as ONE PAGE (templates/settings.html, section "settings_page"): every tab's pane shows, one after another, under its own heading;
// the tab bar becomes a sticky contents row (a click scrolls to that part) and a search box hides the settings that don't match. The panes
// are the page's own - nothing is rebuilt, a hidden setting is only hidden. Pure helper exported for tests/test_settings_presets.py.

(function (root) {
  // every word of the query is somewhere in the text (case-insensitive); an empty query matches everything
  function matches(text, q) {
    const hay = String(text || "").toLowerCase();
    return String(q || "").toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
  }
  const core = { matches };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.settingsOnePageCore = core;
  if (typeof document === "undefined") return;

  const page = document.querySelector(".form-page[data-pl='settings_page']");
  if (!page) return;
  const panes = [...page.querySelectorAll(":scope > [id^='pane-']")];
  const q = document.getElementById("settings-q");
  panes.forEach((p) => {
    const tab = document.getElementById("tab-" + p.id.slice(5));
    const h = document.createElement("h2");
    h.className = "sp-head"; h.textContent = tab ? tab.textContent : p.id.slice(5);
    p.insertBefore(h, p.firstChild);
  });
  const one = () => page.dataset.view === "onepage";
  function filter() {
    const needle = one() && q ? q.value : "";
    let shown = 0;
    panes.forEach((p) => {
      let any = false;
      p.querySelectorAll(".form-group").forEach((g) => { const ok = matches(g.textContent, needle); g.classList.toggle("sp-miss", !ok); if (ok) any = true; });
      const whole = matches(p.textContent, needle);
      p.classList.toggle("sp-miss", !!needle && !any && !whole);
      if (!p.classList.contains("sp-miss")) shown++;
    });
    const none = document.getElementById("settings-q-none");
    if (none) none.hidden = !needle || shown > 0;
  }
  if (q) q.addEventListener("input", filter);
  page.querySelector(".settings-tab-bar").addEventListener("click", (e) => {
    const b = e.target.closest(".settings-tab");
    if (!b || !one()) return;
    const p = document.getElementById("pane-" + b.id.slice(4));
    if (p) p.scrollIntoView({ block: "start", behavior: "smooth" });
  });
  page.addEventListener("nd-pl-ready", filter);
  page.addEventListener("nd-pl-change", filter);
})(typeof window !== "undefined" ? window : globalThis);
