"use strict";
// The Rules page's Book layout (templates/rules.html, data-view="book"): one section at a time with Previous / Next, a jump list, ← / → keys and
// a swipe. The sections are the page's own (never rebuilt) - Book only decides which one is showing. The contents list, the search box and the
// tabs of a tabbed rule set keep working: a contents link jumps to its section, a search narrows the book to the matches.
// Pure helpers are exported for tests/test_rules_book.py.

(function (root) {
  const clamp = (i, n) => Math.max(0, Math.min(n - 1, i));
  // after a key: ArrowRight / ArrowLeft move one section, Home / End go to the ends; -1 = not a book key
  function stepKey(key, at, n) {
    if (!n) return -1;
    if (key === "ArrowRight") return clamp(at + 1, n);
    if (key === "ArrowLeft") return clamp(at - 1, n);
    if (key === "Home") return 0;
    if (key === "End") return n - 1;
    return -1;
  }
  // a short, mostly horizontal, quick drag turns the page
  function swipeStep(dx, dy, dt) { return Math.abs(dx) >= 70 && Math.abs(dx) >= 2 * Math.abs(dy) && dt <= 800 ? (dx < 0 ? 1 : -1) : 0; }
  const label = (text, i) => (String(text || "").replace(/\s+/g, " ").trim().slice(0, 60)) || "Section " + (i + 1);
  const core = { clamp, stepKey, swipeStep, label };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.rulesBookCore = core;
  if (typeof document === "undefined") return;

  const layout = document.querySelector(".rules-layout[data-pl]");
  const content = document.querySelector(".rules-content");
  if (!layout || !content) return;
  let at = 0, navTop = null, navBottom = null;

  // the sections that can show: not filtered out by the search, and in the open tab of a tabbed rule set
  function sections() {
    return Array.from(content.querySelectorAll(".rules-section")).filter((s) => s.style.display !== "none" && (!s.closest(".rules-tab-pane") || s.closest(".rules-tab-pane").classList.contains("active")));
  }
  function build() {
    const mk = () => {
      const nav = document.createElement("div"); nav.className = "rb-nav"; nav.setAttribute("role", "navigation"); nav.setAttribute("aria-label", "Pages of the rules");
      const prev = document.createElement("button"); prev.type = "button"; prev.textContent = "◀"; prev.setAttribute("aria-label", "Previous section");
      const sel = document.createElement("select"); sel.setAttribute("aria-label", "Jump to a section");
      const next = document.createElement("button"); next.type = "button"; next.textContent = "▶"; next.setAttribute("aria-label", "Next section");
      const count = document.createElement("span"); count.className = "rb-count";
      prev.addEventListener("click", () => show(at - 1, true)); next.addEventListener("click", () => show(at + 1, true));
      sel.addEventListener("change", () => show(parseInt(sel.value, 10), true));
      nav.append(prev, sel, next, count);
      return nav;
    };
    navTop = mk(); navBottom = mk();
    const body = content.querySelector(".rules-body");
    body.before(navTop); body.after(navBottom);
  }
  function fill() {
    const list = sections();
    [navTop, navBottom].forEach((nav) => {
      const sel = nav.querySelector("select");
      sel.replaceChildren(...list.map((s, i) => { const o = document.createElement("option"); o.value = String(i); o.textContent = (i + 1) + ". " + label((s.querySelector("h1, h2, h3, h4") || s).textContent, i); return o; }));
    });
    return list;
  }
  function show(i, scroll) {
    const list = fill();
    at = clamp(i, list.length);
    content.querySelectorAll(".rules-section.rb-on").forEach((s) => s.classList.remove("rb-on"));
    if (list[at]) list[at].classList.add("rb-on");
    [navTop, navBottom].forEach((nav) => {
      nav.querySelector("select").value = String(at);
      const [prev, , next, count] = Array.from(nav.children);
      prev.disabled = at <= 0; next.disabled = at >= list.length - 1;
      count.textContent = list.length ? (at + 1) + " / " + list.length : "nothing matches";
    });
    if (scroll && navTop.getBoundingClientRect().top < 0) navTop.scrollIntoView({ block: "start" });
  }
  const isBook = () => layout.dataset.view === "book";
  build();
  layout.addEventListener("nd-pl-ready", () => { if (isBook()) show(at); });
  layout.addEventListener("nd-pl-change", () => { if (isBook()) show(at); });
  const search = document.getElementById("rules-search");
  if (search) search.addEventListener("input", () => { if (isBook()) setTimeout(() => show(0), 0); });
  content.addEventListener("click", (e) => { if (e.target.closest && e.target.closest(".rules-tab") && isBook()) setTimeout(() => show(0), 0); });
  // a contents link names a heading: show the section that holds it
  const toc = document.getElementById("rules-toc-wrap");
  if (toc) toc.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a.toc-entry");
    if (!a || !isBook()) return;
    const target = document.getElementById((a.getAttribute("href") || "").slice(1));
    const sec = target && target.closest(".rules-section");
    const list = sections(), i = list.indexOf(sec);
    if (i >= 0) { e.preventDefault(); e.stopPropagation(); show(i, true); }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (!isBook() || e.ctrlKey || e.metaKey || e.altKey || (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]"))) return;
    const to = stepKey(e.key, at, sections().length);
    if (to >= 0) { e.preventDefault(); show(to, true); }
  });
  let t0 = null;
  content.addEventListener("touchstart", (e) => { t0 = e.touches.length === 1 && isBook() && !(e.target.closest && e.target.closest("table, pre, input, textarea, select")) ? { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() } : null; }, { passive: true });
  content.addEventListener("touchend", (e) => {
    if (!t0) return;
    const c = e.changedTouches[0], d = swipeStep(c.clientX - t0.x, c.clientY - t0.y, Date.now() - t0.t);
    t0 = null;
    if (d) show(at + d, true);
  }, { passive: true });
})(typeof window !== "undefined" ? window : globalThis);
