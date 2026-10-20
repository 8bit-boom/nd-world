"use strict";
// The GM's session page layouts (templates/sessions/detail.html, section "session_gm"): Long page (today), Tabs (Recap / Transcript / Facts /
// Prep & rewards - one at a time) and Run mode (everything the night needs side by side: recording, facts, prep, loot, XP, combats - the recap
// form is out of the way). The blocks are the page's own (`data-sp`), only shown or hidden; the last tab is remembered in this browser.
// Pure helpers exported for tests/test_session_gm_layouts.py.

(function (root) {
  const GROUPS = [["recap", "Recap", ["recap"]], ["transcript", "Transcript", ["transcript"]], ["facts", "Facts", ["facts"]], ["prep", "Prep & rewards", ["prep", "loot", "xp", "combats"]]];
  // the tabs that have at least one block on this page, in order: [{id, label}]
  function tabsFor(present) {
    return GROUPS.filter((g) => g[2].some((b) => present.indexOf(b) >= 0)).map((g) => ({ id: g[0], label: g[1] }));
  }
  // the block names shown for a tab id (all of that group's blocks)
  function blocksOf(tabId) { const g = GROUPS.find((x) => x[0] === tabId); return g ? g[2].slice() : []; }
  // saved tab if it still exists, else the first
  function pickTab(tabs, saved) { return tabs.some((t) => t.id === saved) ? saved : (tabs[0] ? tabs[0].id : ""); }
  const core = { tabsFor, blocksOf, pickTab, RUN_BLOCKS: ["transcript", "facts", "prep", "loot", "xp", "combats"] };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.sessionGmCore = core;
  if (typeof document === "undefined") return;

  const page = document.querySelector(".session-gm[data-pl]");
  const bar = page && page.querySelector(".sg-tabs");
  if (!page || !bar) return;
  const blocks = Array.from(page.querySelectorAll("[data-sp]"));
  const present = blocks.map((b) => b.dataset.sp);
  const tabs = tabsFor(present);
  const KEY = "nd_session_gm_tab";
  let tab = "";
  try { tab = pickTab(tabs, localStorage.getItem(KEY)); } catch (e) { tab = pickTab(tabs, ""); }
  tabs.forEach((t) => {
    const b = document.createElement("button");
    b.type = "button"; b.role = "tab"; b.dataset.tab = t.id; b.textContent = t.label;
    b.addEventListener("click", () => { tab = t.id; try { localStorage.setItem(KEY, tab); } catch (e) { /* not remembered */ } apply(); });
    bar.append(b);
  });
  function apply() {
    const mode = page.dataset.view;
    const show = mode === "tabs" ? blocksOf(tab) : mode === "run" ? core.RUN_BLOCKS : null;
    blocks.forEach((b) => { b.hidden = !!show && show.indexOf(b.dataset.sp) < 0; });
    bar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  }
  page.addEventListener("nd-pl-ready", apply);
  page.addEventListener("nd-pl-change", apply);
})(typeof window !== "undefined" ? window : globalThis);
