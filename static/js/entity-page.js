"use strict";
// An entity page's own layouts (templates/entities/detail.html; the choice is the person's, section "entity_page" in app/view_prefs.py):
//
//   Long page (classic) - everything under each other, as it always was.
//   Tabs               - Overview / Details / Notes / Connections / Media / Ask AI, one at a time; the last tab is remembered per kind.
//   Two columns        - the text and notes on the left, a sticky side (details, connections, media) on the right (a wide screen).
//   Reader             - the text and notes in a narrow reading column; the rest tucked into one "Details, connections and media" fold.
//
// The page's blocks carry data-ep="overview|details|notes|links|media|ai". They are never rebuilt - only shown, hidden or MOVED (back to their
// first order when the layout changes), so forms, listeners and what is typed in them survive. Pure decisions are exported for tests/test_entity_page.py.

(function (root) {
  const ORDER = ["overview", "details", "notes", "links", "media", "ai"];
  const LABEL = { overview: "Overview", details: "Details", notes: "Notes", links: "Connections", media: "Media", ai: "Ask AI" };
  const MODES = ["classic", "tabs", "cols", "reader"];
  const SIDE = ["details", "links", "media"];                 // what goes beside the text in two columns / behind the fold in the reader

  const tabsFor = (present) => ORDER.filter((g) => present.indexOf(g) >= 0);
  // the tab to show: the one asked for (address), else the remembered one, else the first
  function pickTab(want, saved, tabs) { return tabs.indexOf(want) >= 0 ? want : (tabs.indexOf(saved) >= 0 ? saved : (tabs[0] || "")); }
  function groupOfHash(hash) { const m = /^#tab-([a-z]+)$/.exec(hash || ""); return m ? m[1] : ""; }
  // where each group goes in two columns / the reader
  function columnsOf(present) { return { main: present.filter((g) => SIDE.indexOf(g) < 0), side: present.filter((g) => SIDE.indexOf(g) >= 0) }; }
  // two columns need room; on a phone they are the long page
  function effectiveMode(mode, width) { return MODES.indexOf(mode) < 0 ? "classic" : (mode === "cols" && width < 900 ? "classic" : mode); }

  const core = { ORDER, LABEL, MODES, tabsFor, pickTab, groupOfHash, columnsOf, effectiveMode };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.entityPageCore = core;
  if (typeof document === "undefined") return;

  const article = document.querySelector("article.detail[data-ep-kind]");
  const bar = document.getElementById("ep-layoutbar");
  if (!article || !bar) return;
  const KIND = article.dataset.epKind, LS = "nd_view:entity_page", LS_TAB = "nd_ep_tab:" + KIND;
  const original = Array.from(article.children);
  const blocks = (g) => Array.from(article.querySelectorAll('[data-ep="' + g + '"]'));
  const present = ORDER.filter((g) => blocks(g).length);
  let mode = "classic", injected = [];

  const store = (k) => { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } };
  const put = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* not remembered */ } };
  function startMode() {
    const q = new URLSearchParams(location.search).get("layout");
    if (MODES.indexOf(q) >= 0) return q;
    if (MODES.indexOf(article.dataset.epSaved) >= 0) return article.dataset.epSaved;           // the account's
    try { const b = JSON.parse(store(LS) || "null"); if (b && MODES.indexOf(b.mode) >= 0) return b.mode; } catch (e) { /* none */ }
    return "classic";
  }
  function save() {
    put(LS, JSON.stringify({ mode }));
    fetch("/api/view-prefs/entity_page", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) }).catch(() => { /* the browser copy holds */ });
  }

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function count(g) {
    if (g === "notes") return article.querySelectorAll(".en-note").length;
    if (g === "links") return article.querySelectorAll("#conn-items > .nd-item").length;
    if (g === "media") return article.querySelectorAll('[data-ep="media"] audio, [data-ep="media"] video').length;
    return 0;
  }

  // back to the first order, nothing hidden
  function reset() {
    injected.forEach((e) => e.remove());
    injected = [];
    original.forEach((e) => article.appendChild(e));
    original.forEach((e) => e.classList.remove("ep-off"));
    article.classList.remove("ep-reader");
    article.removeAttribute("data-ep-mode");
  }
  function showTab(tab, remember) {
    blocks("overview").concat(blocks("details"), blocks("notes"), blocks("links"), blocks("media"), blocks("ai")).forEach((e) => e.classList.add("ep-off"));
    blocks(tab).forEach((e) => e.classList.remove("ep-off"));
    article.querySelectorAll(".ep-tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    if (remember) { put(LS_TAB, tab); try { history.replaceState(null, "", "#tab-" + tab); } catch (e) { /* ok */ } }
    window.dispatchEvent(new Event("resize"));                  // things measured while hidden (the notes' picture handles) measure again
  }
  function apply() {
    reset();
    const m = effectiveMode(mode, window.innerWidth);
    article.dataset.epMode = m;
    const anchor = article.querySelector("#ep-layoutbar");
    if (m === "tabs") {
      const tabsEl = el("div", "ep-tabs"); tabsEl.setAttribute("role", "tablist");
      const list = tabsFor(present);
      list.forEach((g) => {
        const b = el("button", "ep-tab", LABEL[g] + (count(g) ? " (" + count(g) + ")" : ""));
        b.type = "button"; b.dataset.tab = g; b.setAttribute("role", "tab");
        b.addEventListener("click", () => showTab(g, true));
        tabsEl.append(b);
      });
      anchor.after(tabsEl); injected.push(tabsEl);
      showTab(pickTab(groupOfHash(location.hash), store(LS_TAB), list), false);
    } else if (m === "cols" || m === "reader") {
      const cols = columnsOf(present), wrap = el("div", m === "cols" ? "ep-cols" : "ep-readcol");
      const main = el("div", "ep-main"), side = el(m === "cols" ? "aside" : "details", "ep-side");
      if (m === "reader") { article.classList.add("ep-reader"); side.append(el("summary", "", "Details, connections and media")); }
      // the blocks keep their order inside each column
      const inOrder = (groups) => original.filter((e) => groups.indexOf(e.dataset && e.dataset.ep) >= 0);
      inOrder(cols.main).forEach((e) => main.append(e));
      inOrder(cols.side).forEach((e) => side.append(e));
      wrap.append(main);
      if (cols.side.length) wrap.append(side);
      anchor.after(wrap); injected.push(wrap);
    }
    paintBar();
  }
  function paintBar() {
    bar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  }
  const NAMES = { classic: ["☰", "Long page"], tabs: ["\u{1F5C2}", "Tabs"], cols: ["◧", "Two columns"], reader: ["\u{1F4D6}", "Reader"] };
  MODES.forEach((m) => {
    const b = el("button", "ep-mode", NAMES[m][0] + " " + NAMES[m][1]);
    b.type = "button"; b.dataset.mode = m; b.title = NAMES[m][1];
    b.addEventListener("click", () => { mode = m; apply(); save(); });
    bar.append(b);
  });
  mode = startMode();
  apply();
  let rt = null;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (article.dataset.epMode !== effectiveMode(mode, window.innerWidth)) apply(); }, 200); });
})(typeof window !== "undefined" ? window : globalThis);
