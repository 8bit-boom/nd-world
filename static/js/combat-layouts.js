"use strict";
// The combat tracker's layouts (templates/combat/detail.html; section "combat" in app/view_prefs.py). Only how the page is arranged changes -
// the rows, the turn bar and every control are the tracker's own, so nothing about saving, undo or the players' view differs.
//   Standard   - today's rows.
//   Compact    - the tracker's own compact setting (every row but the one acting folds to a line).
//   Cards      - the combatants as tiles in a grid.
//   Turn view  - who acts now and who is next, with big buttons; the setup, add and log panels are out of the way (a toggle shows everyone).
//   Big screen - large type for a screen the whole table looks at; the controls that edit are hidden.

(function (root) {
  const MODES = ["rows", "compact", "cards", "turn", "big"];
  const effectiveMode = (m) => (MODES.indexOf(m) >= 0 ? m : "rows");
  // the Turn view shows the one acting and the next in the list; with nobody acting yet, the first two
  function turnWindow(count, activeIndex) {
    if (!count) return [];
    const at = activeIndex >= 0 && activeIndex < count ? activeIndex : 0;
    return count > 1 ? [at, (at + 1) % count] : [at];
  }
  const core = { MODES, effectiveMode, turnWindow };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.combatLayoutsCore = core;
  if (typeof document === "undefined") return;

  const wrap = document.querySelector(".ct-wrap[data-ep-view]");
  const bar = document.getElementById("ct-layoutbar");
  const list = document.getElementById("combatants-list");
  if (!wrap || !bar || !list) return;
  const LS = "nd_view:combat";
  const store = () => { try { return localStorage.getItem(LS) || ""; } catch (e) { return ""; } };
  let mode = "rows", showAll = false;

  // the Turn view marks its two rows itself (the order the tracker lists them in is the turn order)
  function markTurn() {
    list.querySelectorAll(".ct-next").forEach((r) => r.classList.remove("ct-next"));
    if (mode !== "turn") return;
    const rows = Array.from(list.querySelectorAll(":scope > .combatant-row"));
    const idx = rows.findIndex((r) => r.classList.contains("active-turn"));
    turnWindow(rows.length, idx).forEach((i, k) => { if (rows[i] && !rows[i].classList.contains("active-turn")) rows[i].classList.add("ct-next"); });
  }
  function apply(prev) {
    wrap.dataset.layout = mode;
    wrap.classList.toggle("show-all", showAll);
    bar.querySelectorAll("button[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    const all = document.getElementById("ct-all");
    if (all) { all.hidden = mode !== "turn"; all.checked = showAll; all.parentElement.hidden = mode !== "turn"; }
    // Compact IS the tracker's compact setting
    const box = document.getElementById("compact-toggle");
    if (box && ((mode === "compact" && !box.checked) || (prev === "compact" && mode !== "compact" && box.checked))) { box.checked = mode === "compact"; box.dispatchEvent(new Event("change", { bubbles: true })); }
    markTurn();
  }
  function save() {
    try { localStorage.setItem(LS, JSON.stringify({ mode })); } catch (e) { /* not remembered */ }
    fetch("/api/view-prefs/combat", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) }).catch(() => { /* the browser copy holds */ });
  }
  const NAMES = { rows: ["☰", "Standard"], compact: ["≡", "Compact"], cards: ["⊞", "Cards"], turn: ["\u{1F3AF}", "Turn view"], big: ["\u{1F5A5}", "Big screen"] };
  MODES.forEach((m) => {
    const b = document.createElement("button"); b.type = "button"; b.className = "ep-mode"; b.dataset.mode = m; b.title = NAMES[m][1]; b.textContent = NAMES[m][0] + " " + NAMES[m][1];
    b.addEventListener("click", () => { const prev = mode; mode = m; apply(prev); save(); });
    bar.append(b);
  });
  const label = document.createElement("label"); label.className = "ct-check"; label.hidden = true;
  const all = document.createElement("input"); all.type = "checkbox"; all.id = "ct-all";
  all.addEventListener("change", () => { showAll = all.checked; apply(mode); });
  label.append(all, " show everyone"); bar.append(label);

  const q = new URLSearchParams(location.search).get("layout");
  mode = effectiveMode(MODES.indexOf(q) >= 0 ? q : (MODES.indexOf(wrap.dataset.epView) >= 0 ? wrap.dataset.epView : (() => { try { return (JSON.parse(store() || "null") || {}).mode; } catch (e) { return ""; } })()));
  apply("rows");
  // the tracker redraws the rows after every change: mark the turn rows again each time
  if (window.MutationObserver) new MutationObserver(markTurn).observe(list, { childList: true });
})(typeof window !== "undefined" ? window : globalThis);
