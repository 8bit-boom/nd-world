"use strict";
// The world's home page layouts (templates/index.html; section "home" in app/view_prefs.py):
//   Classic       - the page as it always was.
//   Session night - what the table needs when play starts: the next game night, the party's vitals, the active quests, a link to the last recap,
//                   and a d20 / d6 roller - read from the same JSON the cockpit uses, so it shows exactly what this viewer may see.
//   Focus         - the quick links and what changed recently, nothing else.
//   My widgets    - every block can be moved up / down or hidden; the order and the hidden blocks are kept on the account.
// The page's blocks carry data-hb="..." and are only ever shown, hidden or re-ordered (CSS `order`), never rebuilt. Pure helpers are exported for
// tests/test_home_layouts.py.

(function (root) {
  const MODES = ["classic", "night", "focus", "widgets"];
  const FOCUS = ["links", "recent"];
  // the order the blocks are laid out in: the saved order first (only blocks that exist), then the rest in the page's own order
  function orderBlocks(present, saved) {
    const out = (saved || []).filter((id, i, a) => present.indexOf(id) >= 0 && a.indexOf(id) === i);
    present.forEach((id) => { if (out.indexOf(id) < 0) out.push(id); });
    return out;
  }
  function moveBlock(order, id, dir) {
    const i = order.indexOf(id), j = i + dir;
    if (i < 0 || j < 0 || j >= order.length) return order.slice();
    const out = order.slice(); out[i] = order[j]; out[j] = id;
    return out;
  }
  function toggleHidden(hidden, id) { return hidden.indexOf(id) >= 0 ? hidden.filter((x) => x !== id) : hidden.concat([id]); }
  const showsBlock = (mode, id, hidden) => mode === "focus" ? FOCUS.indexOf(id) >= 0 : (mode === "night" ? id === "links" : (mode === "widgets" ? (hidden || []).indexOf(id) < 0 : true));
  // the next confirmed game night still ahead (from /api/schedule/state), else an open poll
  function nextNight(plans, nowIso) {
    const ahead = (plans || []).filter((p) => p.status === "confirmed" && !p.past && p.confirmed_slot_id)
      .map((p) => ({ plan: p, at: (p.slots.find((s) => s.id === p.confirmed_slot_id) || {}).starts_at || "" })).filter((x) => x.at && x.at >= (nowIso || ""))
      .sort((a, b) => (a.at < b.at ? -1 : 1));
    if (ahead.length) return { kind: "confirmed", title: ahead[0].plan.title, at: ahead[0].at, location: ahead[0].plan.location || "" };
    const open = (plans || []).filter((p) => p.status === "open");
    return open.length ? { kind: "poll", title: open[0].title, at: "", location: "" } : null;
  }
  const core = { MODES, FOCUS, orderBlocks, moveBlock, toggleHidden, showsBlock, nextNight };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.homeLayoutsCore = core;
  if (typeof document === "undefined") return;

  const stack = document.getElementById("home-stack");
  const bar = document.getElementById("home-layoutbar");
  const night = document.getElementById("home-night");
  if (!stack || !bar) return;
  const LS = "nd_view:home";
  const blocks = Array.from(stack.querySelectorAll("[data-hb]"));
  const present = blocks.map((b) => b.dataset.hb).filter((id, i, a) => a.indexOf(id) === i);
  const idsOf = (v) => String(v || "").split(",").filter(Boolean);
  let mode = "classic", order = idsOf(stack.dataset.epOrder), hidden = idsOf(stack.dataset.epHidden), editing = false;

  const store = () => { try { return JSON.parse(localStorage.getItem(LS) || "null") || {}; } catch (e) { return {}; } };
  function save() {
    try { localStorage.setItem(LS, JSON.stringify({ mode, order, hidden })); } catch (e) { /* not remembered */ }
    fetch("/api/view-prefs/home", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode, order, hidden }) }).catch(() => { /* the browser copy holds */ });
  }
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  function paintBlocks() {
    const ord = orderBlocks(present, order);
    blocks.forEach((b) => {
      const id = b.dataset.hb;
      b.style.order = String(ord.indexOf(id) * 10);
      const gone = !showsBlock(mode, id, hidden);
      b.classList.toggle("hb-off", gone && !(mode === "widgets" && editing));
      b.classList.toggle("hb-dim", mode === "widgets" && editing && hidden.indexOf(id) >= 0);
    });
    stack.querySelectorAll(".hb-tools").forEach((t) => t.remove());
    if (mode === "widgets" && editing) {
      const done = new Set();
      blocks.forEach((b) => {
        const id = b.dataset.hb;
        if (done.has(id)) return; done.add(id);
        const t = el("div", "hb-tools");
        const mk = (label, title, fn) => { const x = el("button", "hb-btn", label); x.type = "button"; x.title = title; x.addEventListener("click", fn); t.append(x); };
        mk("↑", "Move up", () => { order = moveBlock(orderBlocks(present, order), id, -1); paintBlocks(); save(); });
        mk("↓", "Move down", () => { order = moveBlock(orderBlocks(present, order), id, 1); paintBlocks(); save(); });
        mk(hidden.indexOf(id) >= 0 ? "Show" : "Hide", "Show or hide this block", () => { hidden = toggleHidden(hidden, id); paintBlocks(); save(); });
        t.style.order = String(orderBlocks(present, order).indexOf(id) * 10 - 1);
        stack.append(t);
      });
    }
  }

  // ── Session night ────────────────────────────────────────────────────────────────────────────────────────
  let nightBuilt = false;
  async function getJson(url) { try { const r = await fetch(url, { credentials: "same-origin" }); return r.ok ? await r.json() : null; } catch (e) { return null; } }
  async function buildNight() {
    if (nightBuilt || !night) return;
    nightBuilt = true;
    const isGm = document.documentElement.dataset.gm === "1";
    const tile = (title, ...kids) => { const t = el("div", "hn-tile"); t.append(el("h3", "", title), ...kids); night.append(t); return t; };
    night.textContent = "";
    const [sched, board, gmQuests, combats] = await Promise.all([getJson("/api/schedule/state"), getJson("/api/cockpit/player-board"),
      isGm ? getJson("/api/quests/board") : Promise.resolve(null), isGm ? getJson("/api/combat/recent") : Promise.resolve(null)]);
    const nn = sched ? nextNight(sched.plans, sched.now) : null;
    tile("\u{1F4C5} Next game night", nn ? el("p", "hn-big", nn.kind === "confirmed" ? new Date(nn.at).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "A poll is open")
      : el("p", "hn-dim", "Nothing planned yet."), el("p", "hn-dim", nn ? nn.title + (nn.location ? " · " + nn.location : "") : ""), (() => { const a = el("a", "hn-link", "Open the schedule →"); a.href = "/schedule"; return a; })());
    const parties = (board && board.parties) || [];
    const pv = tile("❤ The party");
    if (!parties.length) pv.append(el("p", "hn-dim", isGm ? "Party vitals are in the GM Cockpit." : "No party yet."));
    parties.forEach((p) => {
      pv.append(el("p", "hn-dim", p.name));
      (p.members || []).forEach((m) => {
        const row = el("div", "hn-member");
        row.append(el("span", "", m.name), el("b", "", (m.hp != null ? m.hp : "?") + (m.max_hp != null ? "/" + m.max_hp : "")));
        pv.append(row);
      });
    });
    const quests = (isGm ? gmQuests : board) ? ((isGm ? gmQuests : board).quests || []) : [];
    const qv = tile("\u{1F4DC} Active quests");
    if (!quests.length) qv.append(el("p", "hn-dim", "No active quests."));
    quests.slice(0, 6).forEach((q) => qv.append(el("p", "hn-quest", q.title)));
    const lv = tile("\u{1F4D3} Last time");
    const a1 = el("a", "hn-link", isGm ? "Sessions →" : "Open the Session Log →"); a1.href = isGm ? "/sessions" : "/session-log"; lv.append(a1);
    const running = ((combats && combats.combats) || []).filter((c) => c.running);
    if (running.length) { const a2 = el("a", "hn-link hn-fight", "⚔ Fight running: " + running[0].name + " →"); a2.href = "/combat/" + running[0].id; lv.append(a2); }
    const dv = tile("\u{1F3B2} Dice");
    const out = el("p", "hn-big", "—");
    const row = el("div", "hn-dice");
    ["d20", "d6", "2d6", "d100"].forEach((n) => {
      const b = el("button", "hn-die", n); b.type = "button";
      b.addEventListener("click", async () => {
        try {
          const r = await fetch("/api/dice/roll", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ notation: n }) });
          const d = await r.json();
          out.textContent = r.ok ? n + " = " + (d.total != null ? d.total : (d.roll && d.roll.total)) : (d.detail || "Could not roll");
        } catch (e) { out.textContent = "Could not roll"; }
      });
      row.append(b);
    });
    dv.append(out, row);
  }

  function apply() {
    stack.dataset.view = mode;
    document.body.classList.toggle("home-night-on", mode === "night");
    bar.querySelectorAll("button[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    if (night) { night.hidden = mode !== "night"; if (mode === "night") buildNight(); }
    const edit = bar.querySelector("#hb-edit");
    if (edit) { edit.hidden = mode !== "widgets"; edit.setAttribute("aria-pressed", String(editing)); edit.textContent = editing ? "✓ Done" : "✎ Arrange"; }
    paintBlocks();
  }
  const NAMES = { classic: ["☰", "Classic"], night: ["\u{1F319}", "Session night"], focus: ["◎", "Focus"], widgets: ["\u{1F9E9}", "My widgets"] };
  MODES.forEach((m) => {
    const b = el("button", "ep-mode", NAMES[m][0] + " " + NAMES[m][1]); b.type = "button"; b.dataset.mode = m; b.title = NAMES[m][1];
    b.addEventListener("click", () => { mode = m; editing = m === "widgets" ? editing : false; apply(); save(); });
    bar.append(b);
  });
  const edit = el("button", "ep-mode", "✎ Arrange"); edit.type = "button"; edit.id = "hb-edit"; edit.hidden = true;
  edit.addEventListener("click", () => { editing = !editing; apply(); });
  bar.append(edit);

  const q = new URLSearchParams(location.search).get("layout");
  const saved = store();
  const pick = MODES.indexOf(q) >= 0 ? q : (MODES.indexOf(stack.dataset.epView) >= 0 ? stack.dataset.epView : (MODES.indexOf(saved.mode) >= 0 ? saved.mode : "classic"));
  if (!stack.dataset.epView && saved.order) { order = saved.order; hidden = saved.hidden || []; }
  mode = pick;
  apply();
})(typeof window !== "undefined" ? window : globalThis);
