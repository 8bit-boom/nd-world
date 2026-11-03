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
//   allJobsUrl — optional; renders an "All jobs →" link to the full
//                world-wide jobs page (set by panels whose listUrl filters
//                down to just their own page's jobs)
//   onUse(job) — called when the user clicks "Use this" on a finished job
//   pollMs     — how often to re-poll while any job is in progress (default 3000)
//   summaryModelSelect — optional <select> (or a function returning one) whose options fill the model picker of the
//                "choose the summary settings" form a transcribed job (status "transcribed", started with "review before
//                summarizing") shows; without it that form is not offered and the job is summarized from the Background Jobs page
function ndAudioJobs(panelEl, opts) {
  const pollMs = opts.pollMs || 3000;
  let jobs = [];
  let pollTimer = null;

  const STATUS_LABEL = {
    pending: "Queued…", transcribing: "Transcribing…", summarizing: "Summarizing…",
    transcribed: "✓ Transcribed — choose the summary settings below",
    done: "✓ Done", error: "✗ Failed", cancelled: "○ Cancelled",
    interrupted: "⏸ Interrupted",
  };
  const IN_PROGRESS = new Set(["pending", "transcribing", "summarizing"]);
  // "interrupted" is deliberately in neither set — it's not still running
  // (IN_PROGRESS) but it's also not a dead end (FINISHED normally means
  // "safe to just delete"): the whole point of an interrupted job is that
  // it can be resumed. It gets its own Resume button below instead.
  const FINISHED = new Set(["done", "error", "cancelled", "interrupted", "transcribed"]);


  // What a GM typed into a "summary settings" form survives the panel being rebuilt (every poll while another job is running): kept per job id.
  const drafts = new Map();

  function modelChoices() {
    const src = typeof opts.summaryModelSelect === "function" ? opts.summaryModelSelect() : opts.summaryModelSelect;
    return src && src.options ? Array.from(src.options).map((o) => ({ value: o.value, label: o.textContent })) : null;
  }

  function summaryForm(job) {
    const choices = modelChoices();
    if (!choices) return null;
    const d = drafts.get(job.id) || {
      model: job.model || "", min: job.min_tokens == null ? "" : String(job.min_tokens), max: job.max_tokens == null ? "" : String(job.max_tokens),
      strictness: job.strictness === "firm" ? "firm" : "guideline", think: job.think !== false, rag: !!job.use_rag,
      ragE: job.rag_entity_limit == null ? "" : String(job.rag_entity_limit), ragN: job.rag_notes_limit == null ? "" : String(job.rag_notes_limit),
      extra: job.extra_instructions || "",
    };
    drafts.set(job.id, d);
    const box = document.createElement("div");
    box.className = "nd-job-summary-form";
    box.dataset.jobId = job.id;
    const field = (label, el, title) => {
      const l = document.createElement("label");
      l.className = "nd-job-sf-field";
      if (title) l.title = title;
      l.append(label + " ", el);
      return l;
    };
    const sel = document.createElement("select");
    sel.dataset.k = "model";
    choices.forEach((c) => { const o = document.createElement("option"); o.value = c.value; o.textContent = c.label; sel.appendChild(o); });
    if (d.model && !choices.some((c) => c.value === d.model)) { const o = document.createElement("option"); o.value = d.model; o.textContent = d.model; sel.appendChild(o); }
    sel.value = d.model;
    const num = (k, ph) => { const i = document.createElement("input"); i.type = "number"; i.min = "1"; i.step = "50"; i.placeholder = ph; i.dataset.k = k; i.value = d[k === "min" ? "min" : "max"]; return i; };
    const strict = document.createElement("select");
    strict.dataset.k = "strictness";
    [["guideline", "Guideline (best effort)"], ["firm", "Firm requirement"]].forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; strict.appendChild(o); });
    strict.value = d.strictness;
    const check = (k, checked) => { const i = document.createElement("input"); i.type = "checkbox"; i.dataset.k = k; i.checked = checked; return i; };
    const limit = (k, ph) => { const i = document.createElement("input"); i.type = "number"; i.min = "0"; i.max = "100"; i.placeholder = ph; i.dataset.k = k; i.value = d[k]; i.style.width = "4.2rem"; return i; };
    const extra = document.createElement("input");
    extra.type = "text"; extra.dataset.k = "extra"; extra.value = d.extra; extra.placeholder = "Extra instructions for this summary (optional)…";
    extra.className = "nd-job-sf-extra";
    const go = document.createElement("button");
    go.type = "button"; go.className = "nd-job-use-btn"; go.textContent = "▶ Summarize"; go.dataset.action = "summarize"; go.dataset.jobId = job.id;
    const t = document.createElement("div");
    t.className = "nd-job-sf-title";
    t.textContent = "The recording is transcribed (" + ndTokenLabel(job.transcript || "") + "). Choose how to summarize it:";
    const row1 = document.createElement("div");
    row1.className = "nd-job-sf-row";
    row1.append(field("Model", sel, "The AI model that writes the recap (the speech-to-text model already did its part)"),
      field("Min tokens", num("min", "none"), "Aim for at least this long (prompt guidance — Ollama has no minimum-length option)"),
      field("Max tokens", num("max", "none"), "Stay at or below this; with Thinking off it is also the model's hard output cap"),
      field("Strictness", strict, "Guideline = the model is asked nicely. Firm = the length and your instructions become mandatory requirements"),
      field("Thinking", check("think", d.think), "Let a reasoning model think before answering (slower)"));
    const row2 = document.createElement("div");
    row2.className = "nd-job-sf-row";
    row2.append(field("World lore", check("rag", d.rag), "Look up this world's entities and notes so names are spelled right (GM only)"),
      field("Entities", limit("ragE", "15")), field("Notes", limit("ragN", "5")));
    box.append(t, row1, row2, extra, go);
    return box;
  }

  async function summarizeJob(jobId, btn) {
    const box = panelEl.querySelector('.nd-job-summary-form[data-job-id="' + jobId + '"]');
    const d = drafts.get(Number(jobId)) || drafts.get(jobId);
    if (!box || !d) return;
    const min = d.min.trim(), max = d.max.trim();
    if (min && max && parseInt(min, 10) > parseInt(max, 10)) { alert("Min tokens can't be greater than max tokens."); return; }
    const fd = new FormData();
    fd.append("model", d.model);
    fd.append("extra_instructions", d.extra);
    fd.append("think", d.think ? "true" : "false");
    fd.append("min_tokens", min);
    fd.append("max_tokens", max);
    fd.append("strictness", d.strictness);
    fd.append("use_rag", d.rag ? "true" : "false");
    fd.append("rag_entity_limit", d.ragE.trim());
    fd.append("rag_notes_limit", d.ragN.trim());
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const res = await fetch(`/api/audio-jobs/${jobId}/resummarize`, { method: "POST", body: fd });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
    } catch (e) {
      alert("Could not start the summary: " + e.message);
      btn.disabled = false;
      btn.textContent = "▶ Summarize";
      return;
    }
    drafts.delete(Number(jobId));
    await refreshList();
  }

  // typing in a form updates its draft (delegated: the inputs are recreated by every render)
  function onFormInput(e) {
    const el = e.target.closest("[data-k]");
    const box = el && el.closest(".nd-job-summary-form");
    if (!box) return;
    const d = drafts.get(Number(box.dataset.jobId));
    if (!d) return;
    d[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
  }
  panelEl.addEventListener("input", onFormInput);
  panelEl.addEventListener("change", onFormInput);

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
    if (opts.allJobsUrl) {
      // This panel intentionally shows only a SLICE of the world's jobs
      // (the session page passes a game_session_id filter in listUrl) —
      // the link is the advertised escape hatch to the full list, so a GM
      // who remembers "there was another job somewhere" has a one-click
      // path to it instead of having to know /background-jobs exists.
      // Text node last so the link floats right without wrapping it.
      const all = document.createElement("a");
      all.href = opts.allJobsUrl;
      all.textContent = "All jobs →";
      all.style.cssText = "float:right;color:var(--neon);text-decoration:none;text-transform:none;letter-spacing:0";
      title.appendChild(all);
      title.appendChild(document.createTextNode("Background jobs"));
    } else {
      title.textContent = "Background jobs";
    }
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
        // data-action/data-job-id + the single delegated listener below,
        // NOT btn.onclick here — this button is torn down and recreated by
        // panelEl.innerHTML="" on every poll (every pollMs while any job
        // is in progress), so a handler bound directly to it can lose a
        // tap that lands between touchstart and click on a slower/mobile
        // device: the element it started on gets removed from the DOM
        // before the click event ever fires, and the tap silently does
        // nothing — reported in practice from a session page with an
        // active Condense job keeping the poll loop running. Delegating
        // to panelEl (never itself replaced, only its children) means the
        // click is still caught regardless of how many re-renders happened
        // in between.
        btn.dataset.action = "use";
        btn.dataset.jobId = job.id;
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
        resumeBtn.dataset.action = "resume";
        resumeBtn.dataset.jobId = job.id;
        row.appendChild(resumeBtn);
      }
      if (FINISHED.has(job.status)) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "nd-job-use-btn";
        delBtn.textContent = "🗑";
        delBtn.title = "Delete this job";
        delBtn.dataset.action = "delete";
        delBtn.dataset.jobId = job.id;
        row.appendChild(delBtn);
      }
      panelEl.appendChild(row);
      if (job.status === "transcribed") {
        const form = summaryForm(job);
        if (form) panelEl.appendChild(form);
      }
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

  // Starts a background job sourced from an already-uploaded file (an
  // Audio Library clip, for the Session page's "choose from Audio Library"
  // picker — see app/routers/sessions.py's from-clip route) instead of a
  // fresh upload — no file to read/chunk, just clipId + extraFields.
  // Requires opts.createFromClipUrl; callers that never use this (the AI
  // Chat attachment panel, etc.) simply never call it.
  async function startJobFromClip(clipId, extraFields) {
    const fd = new FormData();
    fd.append("clip_id", clipId);
    for (const k in (extraFields || {})) fd.append(k, extraFields[k]);
    const res = await fetch(opts.createFromClipUrl, { method: "POST", body: fd });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    await refreshList();
    return data.job_id;
  }

  // ONE listener on panelEl itself (never replaced — render() only ever
  // clears/rebuilds its CHILDREN), so it survives every re-render instead
  // of being lost with whatever button element a tap happened to land on
  // — see the "Use this" button's own comment above for the race this
  // fixes. Registered once, here, not inside render().
  panelEl.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn || !panelEl.contains(btn)) return;
    const jobId = btn.dataset.jobId;
    const job = jobs.find((j) => String(j.id) === jobId);
    if (btn.dataset.action === "use") {
      if (job) opts.onUse(job);
    } else if (btn.dataset.action === "summarize") {
      summarizeJob(jobId, btn);
    } else if (btn.dataset.action === "resume") {
      resumeJob(jobId, btn);
    } else if (btn.dataset.action === "delete") {
      deleteJob(jobId, btn);
    }
  });

  refreshList();

  return { startJob, startJobFromClip, refreshList };
}
