"use strict";
// The SITE shell's layout: navigation (top bar / side rail / icon-only bar / phone bottom bar), content width, density, theme, scale, motion - one
// choice per person, saved to the account (POST /api/view-prefs/site, section "site") and mirrored in this browser. The server renders the
// account's choice into <html data-nav data-width data-density data-motion data-acct-theme data-acct-scale data-pins> so the first paint is
// already right; this file applies later changes, saves them, and builds the phone's bottom bar. The CSS that does the arranging is in
// static/style.css ("Site shell"). Pure decisions are exported for tests/test_nd_site.py.

(function (root) {
  const KEYS = ["nav", "width", "density", "theme", "scale", "motion"];
  const MAX_PINS = 6;
  const BOTTOM_SLOTS = 4;
  const DEFAULT_PINS = { gm: ["cockpit", "characters", "combat", "quests"], player: ["characters", "session_log", "dice", "rules"] };

  // The destinations to keep in reach: the person's pins that exist on this page, else the role's defaults that exist, else the first links.
  function pinsFor(chosen, available, isGm, max) {
    max = max || BOTTOM_SLOTS;
    const have = (id) => available.indexOf(id) >= 0;
    let out = (Array.isArray(chosen) ? chosen : []).filter((id, i, a) => have(id) && a.indexOf(id) === i);
    if (!out.length) out = (isGm ? DEFAULT_PINS.gm : DEFAULT_PINS.player).filter(have);
    available.forEach((id) => { if (out.length < max && out.indexOf(id) < 0) out.push(id); });
    return out.slice(0, max);
  }
  // Which navigation actually shows at this window width: the rail needs room, the bottom bar is for a phone; otherwise the top bar.
  function effectiveNav(nav, width) {
    if (nav === "rail" && width >= 900) return "rail";
    if (nav === "bottom" && width <= 768) return "bottom";
    if (nav === "compact") return "compact";
    return "top";
  }
  const core = { KEYS, DEFAULT_PINS, pinsFor, effectiveNav };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.ndSiteCore = core;
  if (typeof document === "undefined") return;

  const html = document.documentElement;
  const STORE = "nd_site";
  const signed = html.dataset.signed === "1";
  const pinsOf = (s) => String(s || "").split(",").filter(Boolean);

  function readHtml() {
    return {
      options: { nav: html.dataset.nav || "top", width: html.dataset.width || "centered", density: html.dataset.density || "comfortable",
                 theme: html.dataset.acctTheme || "", scale: html.dataset.acctScale || "", motion: html.dataset.motion || "" },
      pins: pinsOf(html.dataset.pins),
    };
  }
  function paint(st) {
    html.dataset.nav = st.options.nav; html.dataset.width = st.options.width; html.dataset.density = st.options.density;
    html.dataset.motion = st.options.motion; html.dataset.acctTheme = st.options.theme; html.dataset.acctScale = st.options.scale;
    html.dataset.pins = st.pins.join(",");
    if (st.options.theme) html.dataset.theme = st.options.theme;
    if (st.options.scale) html.dataset.scale = st.options.scale;
  }
  // signed out (the login page): this browser's own copy decides
  if (!signed) {
    try { const b = JSON.parse(localStorage.getItem(STORE) || "null"); if (b && b.options) paint({ options: Object.assign(readHtml().options, b.options), pins: Array.isArray(b.pins) ? b.pins : [] }); } catch (e) { /* defaults */ }
  }

  let timer = null;
  function save(st) {
    try { localStorage.setItem(STORE, JSON.stringify(st)); } catch (e) { /* private window */ }
    if (!signed) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      fetch("/api/view-prefs/site", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ options: st.options, pins: st.pins }) })
        .catch(() => { /* the browser copy still holds */ });
    }, 300);
  }
  function set(patch, pins) {
    const st = readHtml();
    Object.keys(patch || {}).forEach((k) => { if (KEYS.indexOf(k) >= 0) st.options[k] = String(patch[k]); });
    if (Array.isArray(pins)) st.pins = pins.slice(0, MAX_PINS);
    paint(st);
    if (patch && patch.theme) { try { localStorage.setItem("nd_theme", patch.theme); } catch (e) { /* ok */ } }
    if (patch && patch.scale) { try { localStorage.setItem("nd_ui_scale", patch.scale); } catch (e) { /* ok */ } }
    save(st);
    buildBottomNav();
    window.dispatchEvent(new CustomEvent("nd-site-change", { detail: st }));
    return st;
  }

  // every destination the top bar offers (also what the Account page lets you pin)
  function navItems() {
    const seen = {}, out = [];
    document.querySelectorAll("#nav-kinds [data-nav-id]").forEach((a) => {
      const id = a.dataset.navId;
      if (seen[id] || id === "settings" || id === "account") return;
      seen[id] = 1;
      const ic = a.querySelector(".nav-ic"), lb = a.querySelector(".nav-lb");
      out.push({ id, href: a.getAttribute("href"), icon: ic ? ic.textContent.trim() : "", label: lb ? lb.textContent.trim() : a.title });
    });
    return out;
  }

  // the phone's bottom bar: the pinned destinations and a "More" that opens the usual menu
  function buildBottomNav() {
    const old = document.getElementById("nd-bottom-nav");
    if (old) old.remove();
    document.body.classList.remove("nd-has-bn");
    if (html.dataset.nav !== "bottom") return;
    // the character page and the cockpit bring their own bottom bars, and an embedded window has no site chrome at all
    if (document.body.classList.contains("pch-hub-page") || document.body.classList.contains("nd-embed") || document.getElementById("ck-wrap")) return;
    const items = navItems();
    const ids = pinsFor(pinsOf(html.dataset.pins), items.map((i) => i.id), html.dataset.gm === "1", BOTTOM_SLOTS);
    const bar = document.createElement("nav");
    bar.id = "nd-bottom-nav";
    bar.setAttribute("aria-label", "Quick destinations");
    ids.forEach((id) => {
      const it = items.find((x) => x.id === id);
      if (!it) return;
      const a = document.createElement("a");
      a.href = it.href; a.className = "nd-bn";
      if (location.pathname.indexOf(it.href.split("?")[0]) === 0 && it.href !== "/") a.classList.add("active");
      const i = document.createElement("span"); i.className = "nd-bn-ic"; i.textContent = it.icon;
      const t = document.createElement("small"); t.textContent = it.label.length > 11 ? it.label.slice(0, 10) + "…" : it.label;
      a.append(i, t);
      bar.append(a);
    });
    const more = document.createElement("button");
    more.type = "button"; more.className = "nd-bn"; more.setAttribute("aria-label", "More");
    more.innerHTML = '<span class="nd-bn-ic">☰</span><small>More</small>';
    more.addEventListener("click", () => { const t = document.getElementById("nav-toggle"); if (t) t.click(); });
    bar.append(more);
    document.body.append(bar);
    document.body.classList.add("nd-has-bn");
  }
  // the top bar's height only counts when it is on top: a side rail takes width, not height
  window.ndNavOffset = function () {
    const tb = document.querySelector(".topbar");
    if (!tb || !tb.offsetHeight) return 0;
    if (html.dataset.nav === "rail" && window.innerWidth >= 900) return 0;
    return tb.offsetHeight;
  };
  window.ndSite = { get: readHtml, set, navItems, pinsFor, effectiveNav };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", buildBottomNav); else buildBottomNav();
})(typeof window !== "undefined" ? window : globalThis);
