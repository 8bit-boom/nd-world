"use strict";
// Shared "attach a file to an AI chat message" helper for any page that
// talks to POST /api/ai/stream — currently ai_chat.html's World Chat and
// entities/detail.html's per-entity Ask AI panel. Handles uploading a
// picked/dropped file via ndChunkedUpload (chunked-upload.js) to
// /api/ai/attachments/upload (and its /chunk + /complete pair for anything
// over ndChunkedUpload's threshold), tracking the pending (not-yet-sent)
// attachment list, rendering removable chips for it, and handing back a
// plain array to fold into the next chat message's `attachments` field.
// Neither this file nor the caller ever reads file bytes itself — upload
// and text-extraction both happen server-side (app/routers/ai.py), this
// only tracks upload state and renders chips.
function ndAiAttachments(pendingListEl, onChange) {
  let pending = [];
  const ICONS = { image: "🖼", document: "📄", audio: "🎵" };

  function render() {
    pendingListEl.innerHTML = "";
    pendingListEl.style.display = pending.length ? "flex" : "none";
    pending.forEach((att, i) => {
      const chip = document.createElement("div");
      chip.className = "ai-attach-chip"
        + (att.uploading ? " ai-attach-chip--uploading" : "")
        + (att.error ? " ai-attach-chip--error" : "");
      const icon = ICONS[att.kind] || "📎";
      const transcribed = att.kind === "audio" && !att.uploading && !att.error && att.text ? " (transcribed)" : "";
      const label = document.createElement("span");
      label.textContent = att.uploading
        ? `${icon} ${att.name}…`
        : (att.error ? `⚠ ${att.name}: ${att.error}` : `${icon} ${att.name}${transcribed}`);
      chip.appendChild(label);
      const rm = document.createElement("button");
      rm.type = "button";
      rm.textContent = "✕";
      rm.title = "Remove";
      rm.onclick = () => { pending.splice(i, 1); render(); };
      chip.appendChild(rm);
      pendingListEl.appendChild(chip);
    });
    if (onChange) onChange();
  }

  async function addFiles(files) {
    for (const file of Array.from(files)) {
      const entry = { name: file.name, kind: "", uploading: true, error: "" };
      pending.push(entry);
      render();
      try {
        const data = await ndChunkedUpload(file, {
          directUrl: "/api/ai/attachments/upload",
          chunkUrl: "/api/ai/attachments/upload/chunk",
          completeUrl: "/api/ai/attachments/upload/complete",
        });
        entry.kind = data.kind;
        entry.url = data.url;
        entry.text = data.text || "";
        entry.uploading = false;
      } catch (err) {
        entry.error = (err && err.message) || "upload failed";
        entry.uploading = false;
      }
      render();
    }
  }

  return {
    addFiles,
    hasPending() { return pending.some((a) => a.uploading); },
    hasAny() { return pending.length > 0; },
    // Attachments ready to send — drops any still-uploading/errored ones —
    // and clears the pending list (the caller is expected to send now).
    take() {
      const ready = pending
        .filter((a) => !a.uploading && !a.error)
        .map((a) => ({ kind: a.kind, url: a.url, name: a.name, text: a.text }));
      pending = [];
      render();
      return ready;
    },
  };
}

// Renders an already-sent message's attachments (image thumbnail, or a
// filename chip linking to the file for audio/documents) — shared by
// ai_chat.html's addMessage() and entities/detail.html's epAdd() so a sent
// attachment looks identical on both surfaces.
function ndAiRenderAttachmentChips(attachments) {
  const list = document.createElement("div");
  list.className = "ai-msg-attachments";
  attachments.forEach((att) => {
    if (att.kind === "image") {
      const img = document.createElement("img");
      img.src = att.url; img.alt = att.name; img.className = "ai-msg-attach-img";
      list.appendChild(img);
    } else {
      const a = document.createElement("a");
      a.href = att.url; a.target = "_blank"; a.rel = "noopener";
      a.className = "ai-msg-attach-chip";
      a.textContent = (att.kind === "audio" ? "🎵 " : "📄 ") + att.name;
      list.appendChild(a);
    }
  });
  return list;
}
