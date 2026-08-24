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
//
// jobsPanelEl (optional): if given, also wires up a durable background-job
// option (audio-jobs.js) for a voice memo long enough that waiting on one
// blocking upload isn't practical — see startBackgroundJob below. A
// finished job is folded into the same `pending` list as a normal
// already-uploaded attachment, so it flows into the next message exactly
// like one added via addFiles.
function ndAiAttachments(pendingListEl, jobsPanelEl, onChange) {
  let pending = [];
  const ICONS = { image: "🖼", document: "📄", audio: "🎵" };

  const jobs = jobsPanelEl ? ndAudioJobs(jobsPanelEl, {
    createUrl: "/api/ai/attachments/audio-jobs",
    chunkUrl: "/api/ai/attachments/audio-jobs/chunk",
    completeUrl: "/api/ai/attachments/audio-jobs/complete",
    listUrl: "/api/ai/attachments/audio-jobs",
    onUse: (job) => {
      pending.push({
        name: job.filename, kind: "audio", uploading: false, error: "",
        url: job.attachment_url, text: job.transcript,
      });
      render();
    },
  }) : null;

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
      const progressSuffix = att.uploading && att.progress
        ? (att.progress.phase === "upload" ? ` ${att.progress.percent}%` : "… processing")
        : "…";
      const label = document.createElement("span");
      label.textContent = att.uploading
        ? `${icon} ${att.name}${progressSuffix}`
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
      const entry = { name: file.name, kind: "", uploading: true, error: "", progress: null };
      pending.push(entry);
      render();
      try {
        const data = await ndChunkedUpload(file, {
          directUrl: "/api/ai/attachments/upload",
          chunkUrl: "/api/ai/attachments/upload/chunk",
          completeUrl: "/api/ai/attachments/upload/complete",
          onProgress: (e) => { entry.progress = e; render(); },
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

  // Starts a durable background transcription job for `file` instead of
  // uploading it inline — the job keeps running server-side even if this
  // tab closes; a "Use this" button in jobsPanelEl folds the result into
  // `pending` (via the onUse handler above) once it's done.
  async function startBackgroundJob(file, onProgress) {
    if (!jobs) throw new Error("Background jobs not configured for this attachment picker");
    return jobs.startJob(file, {}, onProgress);
  }

  return {
    addFiles,
    startBackgroundJob,
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

// Thin wrapper shared by every "Process in Background" button that targets
// an ndAiAttachments instance — just surfaces a failure to start the job
// (e.g. an unsupported extension) since there's no chip yet for an error
// state to live in at this point.
async function ndStartAttachmentBackgroundJob(attachmentsCtrl, file) {
  if (!file) return;
  try {
    await attachmentsCtrl.startBackgroundJob(file);
  } catch (err) {
    alert("Failed to start background job: " + ((err && err.message) || "unknown error"));
  }
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
