"use strict";
// The Video library's theater layout (templates/video_library.html, layouts from static/js/nd-views.js): one big player on a stage above the
// list, the other clips as a strip of posters under it - a click on one puts it on the stage. The clip's own <video> element is MOVED onto the
// stage (so its subtitles track, position and controls come along) and put back in its row when another clip takes the stage or the layout
// changes. Arrow keys step through the strip. The pure bit (which clip a step lands on) is exported for tests/test_saved_views.py.

(function (root) {
  // the index `delta` steps from `current` among `count` clips, wrapping; -1 when there are none
  function stepIndex(current, count, delta) {
    if (!count) return -1;
    if (current < 0) return delta < 0 ? count - 1 : 0;
    return ((current + delta) % count + count) % count;
  }
  const core = { stepIndex };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.videoViewsCore = core;
  if (typeof document === "undefined") return;

  const list = document.getElementById("video-clips");
  if (!list) return;
  let mode = "rows", current = null, home = null, stage = null;
  const rows = () => Array.from(list.querySelectorAll(":scope > .video-clip-row")).filter((r) => !r.hidden);

  function ensureStage() {
    if (stage) return stage;
    stage = document.createElement("div");
    stage.className = "vv-stage";
    stage.hidden = true;
    stage.innerHTML = '<div class="vv-screen"></div><div class="vv-title" aria-live="polite"></div>';
    list.parentNode.insertBefore(stage, list);
    return stage;
  }
  function putBack() {
    if (!current || !home) return;
    const v = stage && stage.querySelector(".vv-screen > video");
    if (v) home.parent.insertBefore(v, home.next && home.next.parentNode === home.parent ? home.next : null);
    current.classList.remove("vv-current");
    current = null; home = null;
  }
  function show(row) {
    if (!row || row === current) return;
    putBack();
    const v = row.querySelector("video");
    if (!v) return;
    home = { parent: v.parentNode, next: v.nextSibling };
    current = row;
    row.classList.add("vv-current");
    ensureStage().querySelector(".vv-screen").appendChild(v);
    const name = row.querySelector(".video-clip-name span");
    stage.querySelector(".vv-title").textContent = name ? name.textContent : "";
    row.scrollIntoView({ block: "nearest", inline: "center" });
  }
  function onMode(m) {
    mode = m;
    if (m === "theater") {
      ensureStage().hidden = false;
      const vis = rows();
      show(vis.includes(current) ? current : vis[0]);
    } else {
      putBack();
      if (stage) stage.hidden = true;
    }
  }
  // in the strip a click on a clip puts it on the stage instead of playing the small copy (its buttons still work)
  list.addEventListener("click", (e) => {
    if (mode !== "theater" || !e.target.closest) return;
    if (e.target.closest("button, a, input, select, textarea, form, label")) return;
    const row = e.target.closest(".video-clip-row");
    if (!row || row.parentNode !== list) return;
    e.preventDefault();
    show(row);
  }, true);
  document.addEventListener("keydown", (e) => {
    if (mode !== "theater" || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable], video")) return;
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const vis = rows();
    const to = stepIndex(vis.indexOf(current), vis.length, e.key === "ArrowLeft" ? -1 : 1);
    if (to >= 0) { e.preventDefault(); show(vis[to]); }
  });
  // the engine re-orders and hides rows before it announces the change; a clip that got filtered out leaves the stage
  list.addEventListener("nd-view-change", (e) => {
    if (e.detail.mode === "theater" && current && current.hidden) putBack();
    onMode(e.detail.mode);
  });
  if (list.dataset.view) onMode(list.dataset.view);
})(typeof window !== "undefined" ? window : globalThis);
