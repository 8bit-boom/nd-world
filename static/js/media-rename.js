// Quick rename for images, videos and audio clips: a "Rename" button (data-nd-rename) opens a small dialog, saves through the same route the
// AI renamer uses (/api/media-rename/apply, so every rename is logged and undoable) and updates the name on the page without a reload.
//   <button data-nd-rename data-kind="audio|video|image" data-id="12" | data-url="/uploads/..." data-name="current name">
//   <span data-rename-label="audio:12">…</span>      what shows the name (text is replaced)
//   <img data-rename-img="/uploads/...">              its alt text follows the name
// A clip's name is the clip's own; an image's name is a label kept beside its URL (the file is never renamed, so portraits and embeds keep
// working) and an empty name puts back the automatic one.
(function () {
  function key(kind, ref) { return kind + ":" + ref; }

  function notice(msg, bad, action) {
    var n = document.createElement("div");
    n.className = "fmt-notice" + (bad ? " fmt-notice-bad" : "");
    n.setAttribute("role", "status");
    var t = document.createElement("span");
    t.textContent = msg;
    n.appendChild(t);
    if (action) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "fmt-notice-action"; b.textContent = action.label;
      b.onclick = function () { n.remove(); action.run(); };
      n.appendChild(b);
    }
    document.body.appendChild(n);
    setTimeout(function () { n.remove(); }, action ? 12000 : bad ? 7000 : 2500);
  }

  async function post(url, payload) {
    var res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.detail || data.error || ("That did not work (" + res.status + ")."));
    return data;
  }

  function show(kind, ref, name) {
    var k = key(kind, ref);
    document.querySelectorAll("[data-rename-label]").forEach(function (el) {
      if (el.getAttribute("data-rename-label") === k) { el.textContent = name; if (el.hasAttribute("title")) el.title = name; }
    });
    document.querySelectorAll("[data-rename-img]").forEach(function (el) {
      if (kind === "image" && el.getAttribute("data-rename-img") === ref) el.alt = name;
    });
    document.querySelectorAll("[data-nd-rename]").forEach(function (el) {
      var r = el.getAttribute("data-kind") === "image" ? el.getAttribute("data-url") : el.getAttribute("data-id");
      if (el.getAttribute("data-kind") === kind && r === ref) el.setAttribute("data-name", name);
    });
  }

  async function apply(kind, ref, name, undoTo) {
    var entry = { kind: kind, name: name };
    if (kind === "image") entry.url = ref; else entry.id = +ref;
    var out = await post("/api/media-rename/apply", { renames: [entry] });
    return out;
  }

  function dialog(kind, ref, current) {
    var back = document.createElement("div");
    back.className = "fmt-modal-back";
    back.innerHTML = '<div class="fmt-modal" role="dialog" aria-modal="true" aria-label="Rename" style="max-width:26rem">' +
      '<h3>✏ Rename</h3><div class="fmt-modal-custom"><input type="text" maxlength="256" aria-label="New name" style="font-size:16px"></div>' +
      '<p class="nd-rename-hint" style="margin:0 0 .6rem;font-size:.8rem;color:var(--text-dim)"></p>' +
      '<div class="fmt-modal-foot"><button type="button" class="nd-rename-reset" style="margin-right:auto;display:none">Use the automatic name</button>' +
      '<button type="button" class="fmt-modal-use">Save</button><button type="button" class="fmt-modal-cancel">Cancel</button></div></div>';
    var q = function (s) { return back.querySelector(s); };
    var input = q("input"), save = q(".fmt-modal-use"), reset = q(".nd-rename-reset"), hint = q(".nd-rename-hint");
    input.value = current || "";
    if (kind === "image") {
      hint.textContent = "Only the label changes - the file keeps its name, so portraits and embeds that use it are not affected.";
      reset.style.display = "";
    }
    var close = function () { back.remove(); document.removeEventListener("keydown", onKey, true); };
    var onKey = function (e) { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    document.addEventListener("keydown", onKey, true);
    var go = async function (name) {
      name = name.replace(/\s+/g, " ").trim();
      if (kind !== "image" && !name) { hint.textContent = "A name cannot be empty."; return; }
      if (name === (current || "")) { close(); return; }
      save.disabled = true;
      try {
        var out = await apply(kind, ref, name);
        close();
        if (!out.applied) { notice((out.skipped && out.skipped[0] && out.skipped[0].reason) || "Nothing changed.", true); return; }
        if (name) show(kind, ref, name);
        else location.reload();                          // the automatic name is worked out by the server
        var batch = out.batch_id;
        notice(name ? "Renamed." : "Back to the automatic name.", false, { label: "Undo", run: async function () {
          try { await post("/api/media-rename/undo", { batch_id: batch }); location.reload(); }
          catch (e) { notice(e.message, true); }
        } });
      } catch (e) { save.disabled = false; hint.textContent = e.message; hint.style.color = "var(--neon2)"; }
    };
    save.onclick = function () { go(input.value); };
    reset.onclick = function () { go(""); };
    q(".fmt-modal-cancel").onclick = close;
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(input.value); } });
    back.addEventListener("mousedown", function (e) { if (e.target === back) close(); });
    document.body.appendChild(back);
    input.focus(); input.select();
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-nd-rename]");
    if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    var kind = btn.getAttribute("data-kind");
    var ref = kind === "image" ? btn.getAttribute("data-url") : btn.getAttribute("data-id");
    if (!kind || !ref) return;
    dialog(kind, ref, btn.getAttribute("data-name") || "");
  }, true);

  window.ndRenameMedia = dialog;
})();
