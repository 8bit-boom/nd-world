"use strict";
// The dice page's layouts (templates/dice.html; section "dice" in app/view_prefs.py):
//   3D tray (today's) - the tray, the formula box, the shared log.
//   Quick buttons     - no animation: one tap on a die rolls it (count and modifier set above), saved formulas (this browser) roll in one tap too,
//                       and the latest roll at the table is shown big.
//   History first     - the shared roll log on top, the formula box under it, the tray out of the way.
// Every roll still goes through the page's own form (so the log, the other players and the second screen see it exactly as before).
// The pure helpers are exported for tests/test_dice_layouts.py.

(function (root) {
  const MODES = ["tray", "quick", "log"];
  const MAX_SAVED = 12;

  // "2d6+3" from a die, how many, and a modifier (0 = none)
  function notation(sides, count, mod) {
    count = Math.max(1, Math.min(50, parseInt(count, 10) || 1));
    mod = parseInt(mod, 10) || 0;
    return count + "d" + sides + (mod > 0 ? "+" + mod : mod < 0 ? String(mod) : "");
  }
  // a formula is worth keeping if it looks like dice maths: letters d / numbers / + - and spaces, short
  function cleanFormula(text) {
    const t = String(text || "").replace(/\s+/g, "").toLowerCase();
    return /^[0-9d+\-*()kh!l<>=]{1,40}$/.test(t) && /\d/.test(t) ? t : "";
  }
  function addSaved(list, text) {
    const t = cleanFormula(text);
    if (!t) return list.slice();
    return [t].concat(list.filter((x) => x !== t)).slice(0, MAX_SAVED);
  }
  function removeSaved(list, text) { return list.filter((x) => x !== text); }
  function effectiveMode(m) { return MODES.indexOf(m) >= 0 ? m : "tray"; }
  const core = { MODES, MAX_SAVED, notation, cleanFormula, addSaved, removeSaved, effectiveMode };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.diceLayoutsCore = core;
  if (typeof document === "undefined") return;

  const page = document.querySelector(".dice-page[data-ep-view]");
  const form = document.getElementById("dice-form");
  if (!page || !form) return;
  const LS = "nd_view:dice", SAVED = "nd_dice_saved";
  const store = (k) => { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } };
  const put = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* not remembered */ } };
  const input = form.querySelector('input[name="notation"]');
  let mode = "tray", count = 1, mod = 0, saved = [];
  try { saved = JSON.parse(store(SAVED) || "[]").filter((x) => cleanFormula(x)); } catch (e) { saved = []; }

  function roll(text) {
    if (!text || !input) return;
    input.value = text;
    if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event("submit", { cancelable: true }));
  }
  const pad = document.getElementById("dq");
  function paintPad() {
    if (!pad) return;
    pad.querySelector("#dq-count").textContent = count + "×";
    pad.querySelector("#dq-mod").textContent = (mod > 0 ? "+" : "") + mod;
    const host = pad.querySelector("#dq-saved");
    host.textContent = "";
    saved.forEach((f) => {
      const wrap = document.createElement("span"); wrap.className = "dq-chip";
      const b = document.createElement("button"); b.type = "button"; b.className = "dq-roll"; b.textContent = f; b.title = "Roll " + f;
      b.addEventListener("click", () => roll(f));
      const x = document.createElement("button"); x.type = "button"; x.className = "dq-x"; x.textContent = "×"; x.setAttribute("aria-label", "Forget " + f);
      x.addEventListener("click", () => { saved = removeSaved(saved, f); put(SAVED, JSON.stringify(saved)); paintPad(); });
      wrap.append(b, x); host.append(wrap);
    });
  }
  if (pad) {
    pad.querySelectorAll("button[data-sides]").forEach((b) => b.addEventListener("click", () => roll(notation(b.dataset.sides, count, mod))));
    const step = (id, fn) => pad.querySelector(id).addEventListener("click", () => { fn(); paintPad(); });
    step("#dq-count-up", () => { count = Math.min(10, count + 1); });
    step("#dq-count-down", () => { count = Math.max(1, count - 1); });
    step("#dq-mod-up", () => { mod = Math.min(20, mod + 1); });
    step("#dq-mod-down", () => { mod = Math.max(-20, mod - 1); });
    step("#dq-reset", () => { count = 1; mod = 0; });
    pad.querySelector("#dq-save").addEventListener("click", () => { saved = addSaved(saved, input.value || notation(20, count, mod)); put(SAVED, JSON.stringify(saved)); paintPad(); });
    // the latest roll at the table, big - read from the log the page already keeps up to date
    const log = document.getElementById("dice-log"), last = document.getElementById("dq-last");
    const showLast = () => {
      const row = log && log.querySelector(".dice-row");
      if (!row || !last) return;
      const spans = row.querySelectorAll("span");
      last.textContent = "";
      const who = document.createElement("small"); who.textContent = spans[0].textContent + " · " + (row.querySelector("span[style*=monospace]") || spans[1]).textContent;
      const total = document.createElement("strong"); total.textContent = spans[spans.length - 1].textContent.replace("=", "").trim();
      last.append(who, total);
    };
    if (log && window.MutationObserver) new MutationObserver(showLast).observe(log, { childList: true });
    showLast();
  }

  const bar = document.getElementById("dice-layoutbar");
  const NAMES = { tray: ["\u{1F3B2}", "3D tray"], quick: ["⚡", "Quick buttons"], log: ["\u{1F4DC}", "History first"] };
  function apply() {
    page.dataset.view = mode;
    if (bar) bar.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    if (pad) pad.hidden = mode !== "quick";
    if (mode !== "quick" && window.ndLoadDiceTray) window.ndLoadDiceTray();      // the 3-D tray's heavy scripts, only when it is shown
  }
  function save() {
    put(LS, JSON.stringify({ mode }));
    fetch("/api/view-prefs/dice", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) }).catch(() => { /* the browser copy holds */ });
  }
  if (bar) MODES.forEach((m) => {
    const b = document.createElement("button"); b.type = "button"; b.className = "ep-mode"; b.dataset.mode = m; b.title = NAMES[m][1]; b.textContent = NAMES[m][0] + " " + NAMES[m][1];
    b.addEventListener("click", () => { mode = m; apply(); save(); });
    bar.append(b);
  });
  const q = new URLSearchParams(location.search).get("layout");
  mode = effectiveMode(MODES.indexOf(q) >= 0 ? q : (MODES.indexOf(page.dataset.epView) >= 0 ? page.dataset.epView : (() => { try { return (JSON.parse(store(LS) || "null") || {}).mode; } catch (e) { return ""; } })()));
  apply();
})(typeof window !== "undefined" ? window : globalThis);
