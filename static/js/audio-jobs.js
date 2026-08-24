"use strict";
// Shared "durable background audio job" panel — starts a transcription
// (+ optional summarization, server-side) job that keeps running in the
// server process even if the tab that started it closes (see
// app/audio_jobs.py), and polls/renders its status. A finished job's
// result isn't applied anywhere automatically: the panel shows a "Use
// this" action per done job, and the caller decides what "use" means
// (fill a recap draft, show a transcript, attach to a chat message) via
// opts.onUse. Recent jobs (including ones started from a since-closed tab,
// or a different browser) are loaded from opts.listUrl on mount.
//
// opts:
//   createUrl/chunkUrl/completeUrl — same trio ndChunkedUpload expects
//   listUrl    — GET the recent-jobs list for the active world
//   onUse(job) — called when the user clicks "Use this" on a finished job
//   pollMs     — how often to re-poll while any job is in progress (default 3000)
function ndAudioJobs(panelEl, opts) {
  const pollMs = opts.pollMs || 3000;
  let jobs = [];
  let pollTimer = null;

  const STATUS_LABEL = {
    pending: "Queued…", transcribing: "Transcribing…",
    summarizing: "Summarizing…", done: "✓ Done", error: "✗ Failed",
  };
  const IN_PROGRESS = new Set(["pending", "transcribing", "summarizing"]);

  function render() {
    panelEl.innerHTML = "";
    if (!jobs.length) { panelEl.style.display = "none"; return; }
    panelEl.style.display = "block";
    const title = document.createElement("div");
    title.className = "nd-jobs-title";
    title.textContent = "Background jobs";
    panelEl.appendChild(title);
    jobs.forEach((job) => {
      const row = document.createElement("div");
      row.className = "nd-job-row" + (job.status === "error" ? " nd-job-row--error" : "");
      const label = document.createElement("span");
      label.className = "nd-job-label";
      label.textContent = `${job.filename || "audio"} — ${STATUS_LABEL[job.status] || job.status}`;
      row.appendChild(label);
      if (job.status === "error" && job.error) {
        const err = document.createElement("span");
        err.className = "nd-job-error";
        err.textContent = job.error;
        row.appendChild(err);
      }
      if (job.status === "done") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "nd-job-use-btn";
        btn.textContent = "Use this";
        btn.onclick = () => opts.onUse(job);
        row.appendChild(btn);
      }
      panelEl.appendChild(row);
    });
  }

  async function refreshList() {
    try {
      const res = await fetch(opts.listUrl);
      if (!res.ok) return;
      jobs = await res.json();
    } catch (e) {
      return; // transient — the next scheduled poll will retry
    }
    render();
    schedulePollIfNeeded();
  }

  function schedulePollIfNeeded() {
    if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
    if (jobs.some((j) => IN_PROGRESS.has(j.status))) {
      pollTimer = setTimeout(refreshList, pollMs);
    }
  }

  // Uploads `file` (chunked automatically if it's large — see
  // chunked-upload.js) and starts a background job for it; onProgress, if
  // given, only covers the upload itself (the job's own progress shows up
  // in the panel once created). Refreshes the list immediately afterward
  // so the new job appears right away instead of waiting for the next poll.
  async function startJob(file, extraFields, onProgress) {
    const data = await ndChunkedUpload(file, {
      directUrl: opts.createUrl, chunkUrl: opts.chunkUrl, completeUrl: opts.completeUrl,
      extraFields: extraFields || {}, onProgress,
    });
    await refreshList();
    return data.job_id;
  }

  refreshList();

  return { startJob, refreshList };
}
