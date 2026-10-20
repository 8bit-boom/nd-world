"use strict";
// The Audio library's two playing layouts (templates/audio_library.html, layouts from static/js/nd-views.js):
//
//   Soundboard ("pads"): each clip is a big pad - one tap plays, a second tap stops. Digits 1-9 play the first nine pads that are showing.
//     A pad has its own volume (kept in this browser), a fade-out, and the clip's own Loop / "Play for players" buttons. "One at a time" stops the
//     others when a pad starts; "Stop all" and "Fade all" are for the end of a scene.
//   Playlist ("playlist"): clips play one after another through ONE shared bar (previous / play / next, shuffle, repeat all) - the rows only
//     carry a play button.
//   Table ("table"): one line per clip with columns - name, when it was added, file size, who can hear it - and a play button; a click on a
//     column heading sorts by it (a second click reverses), through the same sort the layout bar uses.
//
// Every clip keeps using its own <audio id="audio-el-ID"> (the same element Loop, Pop out and subtitles already use), so nothing about those
// changes. The pure bits (the order a playlist plays in, the hotkey -> pad mapping) are exported for tests/test_audio_views.py.

(function (root) {
  // The next index to play: sequential, wrapping when `repeat`, or a random other one when `shuffle`. -1 = the list is over.
  function nextIndex(current, count, opts, rand) {
    if (!count) return -1;
    opts = opts || {};
    if (opts.shuffle) {
      if (count === 1) return opts.repeat ? 0 : -1;
      let pick = Math.floor((rand ? rand() : Math.random()) * (count - 1));
      if (pick >= current) pick += 1;                       // never the same clip twice in a row
      return pick;
    }
    const n = current + 1;
    if (n < count) return n;
    return opts.repeat ? 0 : -1;
  }
  function prevIndex(current, count) {
    if (!count) return -1;
    return current <= 0 ? count - 1 : current - 1;
  }
  // Digit keys 1..9 -> the pad's position (0-based) among the pads showing, or -1.
  function padForKey(key, count) {
    if (!/^[1-9]$/.test(key)) return -1;
    const i = parseInt(key, 10) - 1;
    return i < count ? i : -1;
  }
  // A file size for the table: "512 B", "342 KB", "3.5 MB".
  function fmtSize(bytes) {
    const b = Number(bytes) || 0;
    if (b < 1024) return b + " B";
    if (b < 1024 * 1024) return Math.round(b / 1024) + " KB";
    const mb = b / (1024 * 1024);
    return (mb < 10 ? mb.toFixed(1) : String(Math.round(mb))) + " MB";
  }
  // The sort a click on a column heading asks for: that column ascending (dates newest first), or the reverse when it already sorts by it.
  function headingSort(current, col) {
    const desc = String(current || "").startsWith("-");
    const key = desc ? current.slice(1) : current;
    if (key === col) return desc ? col : "-" + col;
    return col === "date" ? "-date" : col;
  }
  const core = { nextIndex, prevIndex, padForKey, fmtSize, headingSort };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.audioViewsCore = core;
  if (typeof document === "undefined") return;

  const list = document.getElementById("audio-clips");
  if (!list) return;
  let mode = "rows";
  const rows = () => Array.from(list.querySelectorAll(":scope > .audio-clip-row")).filter((r) => !r.hidden);
  const audioOf = (row) => document.getElementById("audio-el-" + row.dataset.clipId);
  const volKey = (id) => "nd_pad_vol:" + id;
  const getVol = (id) => { try { const v = parseFloat(localStorage.getItem(volKey(id))); return v >= 0 && v <= 1 ? v : 1; } catch (e) { return 1; } };
  const setVol = (id, v) => { try { localStorage.setItem(volKey(id), String(v)); } catch (e) { /* not remembered */ } };

  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((kid) => { if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid))); });
    return e;
  }

  // ── what every row gets: a big play button, a progress line, a volume slider, a fade-out ────────────
  function decorate(row) {
    if (row.querySelector(".av-controls")) return;
    const id = row.dataset.clipId, audio = audioOf(row);
    if (!audio) return;
    const play = el("button", { type: "button", class: "av-play", "aria-label": "Play", text: "▶" });
    const fill = el("div", { class: "av-fill" });
    const vol = el("input", { type: "range", min: "0", max: "1", step: "0.05", class: "av-vol", "aria-label": "Volume" });
    vol.value = String(getVol(id));
    audio.volume = parseFloat(vol.value);
    const fade = el("button", { type: "button", class: "av-fade", title: "Fade out", "aria-label": "Fade out", text: "⤵" });
    const key = el("span", { class: "av-key", "aria-hidden": "true" });
    const box = el("div", { class: "av-controls" }, key, play, el("div", { class: "av-bar" }, fill), vol, fade);
    row.appendChild(box);
    // the table's cells (hidden in the other layouts)
    const added = parseInt(row.dataset.date, 10);
    row.insertBefore(el("div", { class: "av-cells" },
      el("span", { class: "av-cell av-c-date", text: added ? new Date(added * 1000).toLocaleDateString() : "—" }),
      el("span", { class: "av-cell av-c-size", text: fmtSize(row.dataset.size) }),
      el("span", { class: "av-cell av-c-vis", text: row.dataset.vis === "hidden" ? "GM only" : "Players" })), box);

    play.addEventListener("click", () => toggle(row));
    vol.addEventListener("input", () => { audio.volume = parseFloat(vol.value); setVol(id, vol.value); });
    fade.addEventListener("click", () => fadeOut(row));
    audio.addEventListener("play", () => sync(row));
    audio.addEventListener("pause", () => sync(row));
    audio.addEventListener("ended", () => { sync(row); if (mode === "playlist") advance(row); });
    audio.addEventListener("timeupdate", () => {
      fill.style.width = audio.duration ? Math.min(100, (audio.currentTime / audio.duration) * 100) + "%" : "0%";
      if (nowRow === row) updateNow();
    });
  }

  function sync(row) {
    const audio = audioOf(row);
    const playing = !!audio && !audio.paused;
    row.classList.toggle("av-playing", playing);
    const b = row.querySelector(".av-play");
    if (b) { b.textContent = playing ? "■" : "▶"; b.setAttribute("aria-label", playing ? "Stop" : "Play"); }
    if (nowRow === row) updateNow();
  }

  function startRow(row) {
    const audio = audioOf(row);
    if (!audio) return;
    if (oneAtATime() || mode === "playlist") rows().forEach((r) => { if (r !== row) stopRow(r); });
    audio.currentTime = 0;
    audio.volume = getVol(row.dataset.clipId);
    audio.play().catch(() => { /* the browser blocked it until a tap: the next tap works */ });
    if (mode === "playlist") { nowRow = row; updateNow(); }
  }
  function stopRow(row) {
    const audio = audioOf(row);
    if (!audio) return;
    audio.pause();
    audio.currentTime = 0;
    sync(row);
  }
  function toggle(row) {
    const audio = audioOf(row);
    if (audio && !audio.paused) stopRow(row); else startRow(row);
  }
  function fadeOut(row, ms) {
    const audio = audioOf(row);
    if (!audio || audio.paused) return;
    const start = audio.volume, t0 = performance.now(), total = ms || 2000;
    (function tick(now) {
      const f = Math.min(1, (now - t0) / total);
      audio.volume = Math.max(0, start * (1 - f));
      if (f < 1) requestAnimationFrame(tick);
      else { stopRow(row); audio.volume = getVol(row.dataset.clipId); }
    })(t0);
  }

  // ── soundboard bar ──────────────────────────────────────────────────────────────────────────────
  let padBar = null, oneBox = null;
  const oneAtATime = () => !!(oneBox && oneBox.checked);
  function ensurePadBar() {
    if (padBar) return padBar;
    oneBox = el("input", { type: "checkbox", id: "av-one" });
    padBar = el("div", { class: "av-padbar", hidden: true },
      el("button", { type: "button", class: "btn-cancel", text: "■ Stop all", onclick: () => rows().forEach(stopRow) }),
      el("button", { type: "button", class: "btn-cancel", text: "⤵ Fade all", onclick: () => rows().forEach((r) => fadeOut(r)) }),
      el("label", { class: "av-one" }, oneBox, " One at a time"),
      el("span", { class: "av-hint", text: "Keys 1–9 play the first nine pads" }));
    list.parentNode.insertBefore(padBar, list);
    return padBar;
  }

  // ── playlist bar ────────────────────────────────────────────────────────────────────────────────
  let nowBar = null, nowRow = null, shuffleOn = false, repeatOn = false, nowTitle = null, nowFill = null, nowPlay = null;
  function ensureNow() {
    if (nowBar) return nowBar;
    nowTitle = el("div", { class: "av-now-title", text: "Nothing playing" });
    nowFill = el("div", { class: "av-fill" });
    nowPlay = el("button", { type: "button", class: "av-now-btn", "aria-label": "Play / stop", text: "▶", onclick: () => { if (nowRow) toggle(nowRow); else start(0); } });
    const shuf = el("button", { type: "button", class: "av-now-btn", "aria-pressed": "false", "aria-label": "Shuffle", title: "Shuffle", text: "🔀",
      onclick: () => { shuffleOn = !shuffleOn; shuf.setAttribute("aria-pressed", String(shuffleOn)); } });
    const rep = el("button", { type: "button", class: "av-now-btn", "aria-pressed": "false", "aria-label": "Repeat all", title: "Repeat all", text: "🔁",
      onclick: () => { repeatOn = !repeatOn; rep.setAttribute("aria-pressed", String(repeatOn)); } });
    nowBar = el("div", { class: "av-now", hidden: true, role: "region", "aria-label": "Playlist player" },
      el("button", { type: "button", class: "av-now-btn", "aria-label": "Previous", text: "⏮", onclick: () => step(-1) }), nowPlay,
      el("button", { type: "button", class: "av-now-btn", "aria-label": "Next", text: "⏭", onclick: () => step(1) }),
      el("div", { class: "av-now-main" }, nowTitle, el("div", { class: "av-bar" }, nowFill)), shuf, rep);
    document.body.appendChild(nowBar);
    return nowBar;
  }
  function updateNow() {
    if (!nowBar) return;
    const audio = nowRow && audioOf(nowRow);
    nowTitle.textContent = nowRow ? (nowRow.dataset.name || "").replace(/^./, (c) => c.toUpperCase()) : "Nothing playing";
    const nameEl = nowRow && nowRow.querySelector(".audio-clip-name span");
    if (nameEl) nowTitle.textContent = nameEl.textContent;
    nowFill.style.width = audio && audio.duration ? Math.min(100, (audio.currentTime / audio.duration) * 100) + "%" : "0%";
    nowPlay.textContent = audio && !audio.paused ? "■" : "▶";
  }
  function start(i) { const r = rows(); if (r[i]) startRow(r[i]); }
  function step(delta) {
    const r = rows();
    const at = Math.max(0, r.indexOf(nowRow));
    const to = delta < 0 ? prevIndex(at, r.length) : nextIndex(at, r.length, { shuffle: shuffleOn, repeat: true });
    if (to >= 0) start(to);
  }
  function advance(row) {
    const r = rows();
    const to = nextIndex(r.indexOf(row), r.length, { shuffle: shuffleOn, repeat: repeatOn });
    if (to >= 0) start(to);
  }

  // ── layout switching ────────────────────────────────────────────────────────────────────────────
  function numberPads() {
    rows().forEach((r, i) => { const k = r.querySelector(".av-key"); if (k) k.textContent = i < 9 ? String(i + 1) : ""; });
  }
  // ── the table's column headings ─────────────────────────────────────────────────────────────────
  let head = null;
  const COLS = [["name", "Name"], ["date", "Added"], ["size", "Size"], ["", "Who hears it"], ["", ""]];
  function ensureHead() {
    if (head) return head;
    head = el("div", { class: "av-thead", role: "row", hidden: true }, COLS.map(([col, label]) => col
      ? el("button", { type: "button", class: "av-th", "data-col": col, role: "columnheader", onclick: () => { if (list.ndViews) list.ndViews.set({ sort: headingSort(list.ndViews.state().sort, col) }); } }, label)
      : el("span", { class: "av-th", role: "columnheader", text: label })));
    list.parentNode.insertBefore(head, list);
    return head;
  }
  function markHead(sort) {
    const desc = String(sort || "").startsWith("-"), key = desc ? sort.slice(1) : sort;
    ensureHead().querySelectorAll("button.av-th").forEach((b) => {
      const on = b.dataset.col === key;
      b.setAttribute("aria-sort", on ? (desc ? "descending" : "ascending") : "none");
      b.dataset.dir = on ? (desc ? "↓" : "↑") : "";
    });
  }

  function onMode(m, sort) {
    mode = m;
    list.querySelectorAll(":scope > .audio-clip-row").forEach(decorate);
    ensureHead().hidden = m !== "table";
    if (m === "table") markHead(sort || (list.ndViews ? list.ndViews.state().sort : ""));
    ensurePadBar().hidden = m !== "pads";
    ensureNow().hidden = m !== "playlist";
    document.body.classList.toggle("av-has-now", m === "playlist");
    if (m === "pads") numberPads();
  }
  list.addEventListener("nd-view-change", (e) => { onMode(e.detail.mode, e.detail.sort); });
  document.addEventListener("keydown", (e) => {
    if (mode !== "pads" || e.ctrlKey || e.metaKey || e.altKey) return;
    // typing somewhere must not trigger a pad - but a checkbox, slider or button the person just used must not eat the hotkeys
    if (e.target.closest && e.target.closest("textarea, select, [contenteditable], input:not([type=checkbox]):not([type=range]):not([type=button])")) return;
    const i = padForKey(e.key, rows().length);
    if (i >= 0) { e.preventDefault(); toggle(rows()[i]); }
  });
  // the engine may already have applied the saved layout before this script ran
  if (list.dataset.view) onMode(list.dataset.view);
})(typeof window !== "undefined" ? window : globalThis);
