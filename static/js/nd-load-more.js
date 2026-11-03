"use strict";
// "Load more" under an entity list's card grid (templates/entities/_load_more.html): fetches the same page with a bigger ?limit=, takes the cards it has that this page
// does not, adds them to the grid and swaps the button - no reload, no scroll jump. The links work without JavaScript (a full page with the bigger limit).
(function () {
  function cardId(card) {
    const cb = card.querySelector(".row-cb");
    return cb ? cb.getAttribute("data-id") : "";
  }
  function gridOf(root) {
    return root.querySelector("#entity-cards") || root.querySelector(".folder-section-label + .card-grid");
  }
  async function load(href, button) {
    const wrap = document.getElementById("load-more-wrap");
    const grid = gridOf(document);
    if (!wrap || !grid) { location.href = href; return; }
    const was = button.textContent;
    button.textContent = "Loading…";
    button.setAttribute("aria-busy", "true");
    try {
      const res = await fetch(href, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const doc = new DOMParser().parseFromString(await res.text(), "text/html");
      const next = gridOf(doc);
      if (!next) throw new Error("no list in the answer");
      const have = new Set(Array.from(grid.querySelectorAll(".entity-card-wrap")).map(cardId));
      const anchor = grid.querySelector("#ev-preview");
      let added = 0;
      next.querySelectorAll(".entity-card-wrap").forEach((card) => {
        if (have.has(cardId(card))) return;
        grid.insertBefore(document.importNode(card, true), anchor);
        added++;
      });
      const fresh = doc.getElementById("load-more-wrap");
      if (fresh) wrap.replaceWith(document.importNode(fresh, true)); else wrap.remove();
      try { history.replaceState(null, "", href); } catch (e) { /* not kept in the address bar */ }
      if (grid.ndViews && grid.ndViews.refresh) grid.ndViews.refresh();
      if (typeof window.updateBulkBar === "function") window.updateBulkBar();
      grid.dispatchEvent(new CustomEvent("nd-items-added", { bubbles: true, detail: { added } }));
    } catch (e) {
      button.textContent = was;
      button.removeAttribute("aria-busy");
      location.href = href; // anything unexpected: the plain page with the bigger limit
    }
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("#load-more-btn, #load-more-all");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
    e.preventDefault();
    load(a.getAttribute("href"), a);
  });
})();
