"use strict";
// The encounter list "By status" (templates/combat/list.html, section "combat_list"): running fights first, then finished ones, each under a
// heading. The cards are the page's own - the headings are added and removed, the cards only move within their grid.
(function () {
  const wrap = document.querySelector(".combat-list-wrap[data-pl]");
  const grid = document.getElementById("enc-grid");
  if (!wrap || !grid) return;
  const cards = Array.from(grid.querySelectorAll("[data-status]"));
  const order = cards.slice();                                       // the page's own order
  const heads = { running: head("⚔ Running"), finished: head("🏁 Finished") };
  function head(text) { const h = document.createElement("div"); h.className = "cl-head"; h.textContent = text; return h; }
  function apply() {
    const by = wrap.dataset.view === "grouped";
    Object.values(heads).forEach((h) => h.remove());
    const list = by ? ["running", "finished"].flatMap((s) => order.filter((c) => c.dataset.status === s)) : order;
    if (by) ["running", "finished"].forEach((s) => { const first = list.find((c) => c.dataset.status === s); if (first) first.before(heads[s]); });
    list.forEach((c) => grid.append(c));
    if (by) ["running", "finished"].forEach((s) => { const first = list.find((c) => c.dataset.status === s); if (first) grid.insertBefore(heads[s], first); });
  }
  wrap.addEventListener("nd-pl-ready", apply);
  wrap.addEventListener("nd-pl-change", apply);
})();
