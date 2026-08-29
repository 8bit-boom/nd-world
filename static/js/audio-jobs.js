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
    pending: "Queued…", transcribing: "Transcribing…", summarizing: "Summarizing…",
    done: "✓ Done", error: "✗ Failed", cancelled: "○ Cancelled",
    interrupted: "⏸ Interrupted",
  };
  const IN_PROGRESS = new Set(["pending", "transcribing", "summarizing"]);
  // "interrupted" is deliberately in neither set — it's not still running
  // (IN_PROGRESS) but it's also not a dead end (FINISHED normally means
  // "safe to just delete"): the whole point of an interrupted job is that
  // it can be resumed. It gets its own Resume button below instead.
  const FINISHED = new Set(["done", "error", "cancelled", "interrupted"]);

  function statusLabel(job) {
    if (!IN_PROGRESS.has(job.status)) {
      const base = STATUS_LABEL[job.status] || job.status;
      const dur = ndDurationLabel(job.run_started_at || job.created_at, job.finished_at);
      return dur ? `${base} (took ${dur})` : base;
    }
    // Both phases can report real chunk progress now — audio chunking
    // gives transcribing one too, same as map-reduce already did for
    // summarizing. A short clip/transcript skips chunking for either.
    const base = job.chunk_total && (job.status === "summarizing" || job.status === "transcribing")
      ? `${job.status === "summarizing" ? "Summarizing" : "Transcribing"}… part ${job.chunk_current}/${job.chunk_total}`
      : (STATUS_LABEL[job.status] || job.status);
    return ndElapsedLabel(base, job.run_started_at || job.created_at);
  }

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
      label.textContent = `${job.filename || "audio"} — ${statusLabel(job)}`;
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
      if (job.status === "interrupted") {
        // Previously this status had no button and no further polling at
        // all here — a job interrupted (e.g. by a server restart) just
        // sat frozen on the panel forever, even though the exact same job
        // is resumable from the Background Jobs page.
        const resumeBtn = document.createElement("button");
        resumeBtn.type = "button";
        resumeBtn.className = "nd-job-use-btn";
        resumeBtn.textContent = "▶ Resume";
        resumeBtn.onclick = () => resumeJob(job.id, resumeBtn);
        row.appendChild(resumeBtn);
      }
      if (FINISHED.has(job.status)) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "nd-job-use-btn";
        delBtn.textContent = "🗑";
        delBtn.title = "Delete this job";
        delBtn.onclick = () => deleteJob(job.id, delBtn);
        row.appendChild(delBtn);
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

  // Same unified resume route the Background Jobs page's own "▶ Resume"
  // button posts to (app/routers/audio_jobs.py) — starts the job running
  // again server-side, then refreshList() picks the now-"transcribing"/
  // "summarizing" status back up and (since that's back in IN_PROGRESS)
  // resumes polling on its own.
  async function resumeJob(jobId, btn) {
    btn.disabled = true;
    btn.textContent = "Resuming…";
    try {
      const res = await fetch(`/api/audio-jobs/${jobId}/resume`, { method: "POST" });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
    } catch (e) {
      alert("Failed to resume: " + e.message);
      btn.disabled = false;
      btn.textContent = "▶ Resume";
      return;
    }
    await refreshList();
  }

  // Every AudioJob (whichever purpose-scoped panel started it) shares the
  // same unified status/cancel/delete routes (app/routers/audio_jobs.py) —
  // no opts.deleteUrl needed, this is the one path regardless of listUrl.
  async function deleteJob(jobId, btn) {
    btn.disabled = true;
    try {
      const res = await fetch(`/api/audio-jobs/${jobId}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
    } catch (e) {
      alert("Could not delete job: " + e.message);
      btn.disabled = false;
      return;
    }
    await refreshList();
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
