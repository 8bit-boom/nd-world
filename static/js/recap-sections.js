"use strict";
// A session recap as SECTIONS (or CARDS: the same folds, all open, laid out as a grid with each part's pictures first) (templates/sessions/player_detail.html, section "recap"): the recap's headings (# / ## / ###) become folds - the
// first open, the others closed - so a long recap is read one part at a time on a phone. The text is the page's own (moved into the folds and
// moved back for "Full text", never rewritten), and a recap that is generated after the page loads is handled when it arrives.
// Pure helper exported for tests/test_recap_sections.py.

(function (root) {
  // tags of the recap's top-level nodes -> [{start, end (exclusive), heading: index or -1}]; a new group starts at each H1-H3, text before the first is its own
  function groupIndexes(tags) {
    const out = [];
    tags.forEach((t, i) => {
      const isHead = /^H[1-3]$/.test(String(t || "").toUpperCase());
      if (isHead || !out.length) out.push({ start: i, end: i + 1, heading: isHead ? i : -1 });
      else out[out.length - 1].end = i + 1;
    });
    return out;
  }
  const core = { groupIndexes };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.recapSectionsCore = core;
  if (typeof document === "undefined") return;

  const box = document.getElementById("recap-box");
  if (!box || !box.hasAttribute("data-pl")) return;
  let busy = false;
  const targets = () => Array.from(box.querySelectorAll(".rendered-md"));
  function fold(el) {
    if (el._orig && el.querySelector(":scope > .rs-fold")) return;      // already folded
    el._orig = null;                                                  // (a recap written since then replaced the folds)
    if (!el.childNodes.length) return;
    const nodes = Array.from(el.childNodes).filter((n) => !(n.nodeType === 3 && !n.textContent.trim()));
    const groups = groupIndexes(nodes.map((n) => n.nodeName));
    if (groups.length < 2) return;                                      // nothing to fold: a recap without headings stays as it is
    el._orig = Array.from(el.childNodes);
    const folds = groups.map((g, k) => {
      const d = document.createElement("details"); d.className = "rs-fold"; d.open = box.dataset.view === "cards" || k === 0;
      const sum = document.createElement("summary");
      const head = g.heading >= 0 ? nodes[g.heading] : null;
      sum.textContent = head ? head.textContent.trim() : "Opening";
      d.append(sum);
      nodes.slice(g.start + (head ? 1 : 0), g.end).forEach((n) => d.append(n));
      return d;
    });
    el.replaceChildren(...folds);
  }
  function unfold(el) { if (el._orig) { el.replaceChildren(...el._orig); el._orig = null; } }
  function sync() {
    if (busy) return;
    busy = true;
    try { targets().forEach((el) => (box.dataset.view === "sections" || box.dataset.view === "cards" ? fold(el) : unfold(el))); } finally { busy = false; }
  }
  box.addEventListener("nd-pl-ready", sync);
  box.addEventListener("nd-pl-change", sync);
  // a recap written after the page loaded replaces the text: fold the new text too
  if (window.MutationObserver) new MutationObserver(() => { if (!busy && (box.dataset.view === "sections" || box.dataset.view === "cards")) setTimeout(sync, 0); }).observe(box, { childList: true, subtree: true });
})(typeof window !== "undefined" ? window : globalThis);
