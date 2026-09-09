"use strict";
// Shared "Recap → Facts" widget logic: a textarea + template chips, a
// background AI parse job with an idle/backoff polling ladder, a
// draft-review list, and Confirm & Save. Originally built inline in
// facts/list.html (the standalone Facts page); extracted here so the same
// widget can also be embedded directly on a session's own detail page
// (sessions/detail.html) without duplicating ~250 lines of JS, including
// the elaborate polling ladder — see pollFactsParseJob below for why that
// ladder exists (a long recap against a CPU-local model routinely runs
// past a reverse proxy's request timeout, so parsing is a durable
// background job, not a synchronous POST).
//
// Both markup instances use the SAME element ids (there is only ever one
// instance of this widget on a page), so this module needs to know only
// how to resolve "which session does a save belong to" and "what happens
// after a save" — everything else is wired against fixed ids.
//
// opts:
//   fixedSessionId  — number|string — when set, every parse/save this
//                      instance drives is scoped to exactly this session
//                      (the embedded per-session panel). Mutually
//                      exclusive with sessionSelectId.
//   sessionSelectId — string — id of a <select> the GM picks a session
//                      from (the standalone /facts page, which parses
//                      across the whole world). Mutually exclusive with
//                      fixedSessionId.
//   enableHandoff   — bool, default true — consume the Background Jobs
//                      page's "📋 Extract facts" sessionStorage hand-off
//                      (nd_facts_handoff) on load, pre-filling the recap
//                      textarea and auto-running a parse.
//   onSaved(data)   — called after a successful Confirm & Save POST
//                      ({created, skipped_duplicates}). Default: reload
//                      the page (matches the original /facts behavior).
//
// Returns { init } — call once after the widget's markup is in the DOM.
function ndFactsRecapParser(opts) {
  const fixedSessionId = opts.fixedSessionId != null ? String(opts.fixedSessionId) : null;
  const sessionSelectId = opts.sessionSelectId || null;
  const enableHandoff = opts.enableHandoff !== false;
  const onSaved = opts.onSaved || (() => window.location.reload());

  function getSessionId() {
    if (fixedSessionId != null) return fixedSessionId;
    const sel = sessionSelectId ? document.getElementById(sessionSelectId) : null;
    return sel ? sel.value : "";
  }

  function setSessionIdIfPossible(id) {
    // Only meaningful in standalone (picker) mode — the embedded panel's
    // session is fixed, so there's nothing to preselect.
    if (fixedSessionId != null || !sessionSelectId || !id) return;
    const sel = document.getElementById(sessionSelectId);
    if (sel && [...sel.options].some((o) => o.value === String(id))) {
      sel.value = String(id);
    }
  }

  let draftFacts = [];
  // When non-empty, the draft panel shows this explainer instead of review
  // rows — set when a parse succeeds but finds no in-character facts (an
  // empty result is a SUCCESS the GM needs explained, not an error to
  // swallow: out-of-character chatter produces exactly this).
  let draftEmptyNote = "";

  // Recap templates: skeletons shaped for parse_facts_from_recap — discrete
  // lines the splitter turns into one fact per line. Labels live in
  // RECAP_TEMPLATE_LABELS because they render in TWO places: the static
  // chips in the markup and the action buttons the empty-result note builds
  // at runtime (see renderDraft/loadParsedFacts).
  const RECAP_TEMPLATE_LABELS = {
    standard: "📄 Standard recap",
    timeline: "🕐 Timeline",
    combat: "⚔️ Combat & loot",
    investigation: "🔍 Investigation",
  };
  // ZERO-fill templates: each is a single English instruction line for the
  // model, NOT a form to fill in — the GM clicks a chip, pastes their raw
  // transcript under the instruction, and hits Parse.
  const RECAP_TEMPLATES = {
    standard:
      "Extract this session's key events as discrete facts — what happened, " +
      "who was there, what was learned, how it ended. Ignore out-of-character " +
      "table talk. Write each fact in English.\n\n" +
      "TRANSCRIPT:\n",
    timeline:
      "Extract this session's events as discrete facts in CHRONOLOGICAL order " +
      "(what happened first, what happened next, how it ended). Write each " +
      "fact in English. Ignore out-of-character table talk.\n\n" +
      "TRANSCRIPT:\n",
    combat:
      "Extract facts about EVERY combat in this session: enemies fought, " +
      "tactics, casualties, escapes, and every item of loot gained. Write " +
      "each fact in English. Ignore out-of-character table talk.\n\n" +
      "TRANSCRIPT:\n",
    investigation:
      "Extract facts about the investigation in this session: clues found, " +
      "persons of interest, conclusions drawn, and what remains unknown. " +
      "Write each fact in English.\n\n" +
      "TRANSCRIPT:\n",
  };

  // One template application, shared by the chips and the buttons the
  // empty-result note builds: replace the textarea's content with the
  // instruction line (confirm-guarded when something's already written),
  // then focus it. Returns whether it applied.
  function applyRecapTemplate(key) {
    const ta = document.getElementById("recap-input");
    const tpl = RECAP_TEMPLATES[key];
    if (!tpl) return false;
    if (ta.value.trim() && ta.value !== tpl && !confirm("Replace the current recap text with this template?")) return false;
    ta.value = tpl;
    ta.focus();
    return true;
  }

  function renderDraft() {
    const panel = document.getElementById("draft-panel");
    const list = document.getElementById("draft-list");
    const emptyNote = document.getElementById("draft-empty-note");
    panel.style.display = draftFacts.length || draftEmptyNote ? "block" : "none";
    emptyNote.style.display = draftEmptyNote ? "block" : "none";
    if (draftEmptyNote) {
      emptyNote.textContent = draftEmptyNote;
      const tplRow = document.createElement("div");
      tplRow.style.cssText = "display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.55rem";
      Object.keys(RECAP_TEMPLATES).forEach((key) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = RECAP_TEMPLATE_LABELS[key] || key;
        b.style.cssText = "background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:.25rem .6rem;font-size:.75rem;border-radius:3px;cursor:pointer";
        b.addEventListener("click", () => {
          if (applyRecapTemplate(key)) {
            draftFacts = [];
            draftEmptyNote = "";
            renderDraft();
            const ta = document.getElementById("recap-input");
            ta.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        });
        tplRow.appendChild(b);
      });
      emptyNote.appendChild(tplRow);
    }
    list.innerHTML = "";
    draftFacts.forEach((f, i) => {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;flex-direction:column;gap:.35rem;background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:.5rem .6rem";
      row.innerHTML = `
        <div style="display:flex;gap:.5rem;align-items:flex-start">
          <textarea rows="2" style="flex:1;background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:.35rem .5rem;font-family:var(--font);font-size:.83rem;border-radius:3px"></textarea>
          <label style="display:flex;align-items:center;gap:.3rem;font-size:.78rem;color:var(--text-dim);white-space:nowrap;margin-top:.3rem">
            <input type="checkbox" style="width:auto"> players know
          </label>
          <button type="button" style="background:none;border:none;color:#c44;cursor:pointer;font-size:1rem;margin-top:.2rem">✕</button>
        </div>
        <input class="draft-tags-input" placeholder="tags, comma, separated"
               style="background:var(--bg2);border:1px solid var(--border);color:var(--text-dim);padding:.3rem .5rem;font-family:var(--font);font-size:.78rem;border-radius:3px">
      `;
      const ta = row.querySelector("textarea");
      const cb = row.querySelector("input[type=checkbox]");
      const tagsInput = row.querySelector(".draft-tags-input");
      ta.value = f.content;
      cb.checked = !!f.visible_to_players;
      tagsInput.value = f.tags || "";
      ta.addEventListener("input", () => { draftFacts[i].content = ta.value; });
      cb.addEventListener("change", () => { draftFacts[i].visible_to_players = cb.checked; });
      tagsInput.addEventListener("input", () => { draftFacts[i].tags = tagsInput.value; });
      row.querySelector("button").addEventListener("click", () => { draftFacts.splice(i, 1); renderDraft(); });
      list.appendChild(row);
    });
  }

  // Loads a parsed draft array into the review panel — shared by the
  // background parse poll loop and "Restore last parse", so both render
  // identically (and an empty result from either shows the same
  // out-of-character explainer).
  function loadParsedFacts(facts) {
    draftFacts = Array.isArray(facts) ? facts.filter((f) => f && typeof f === "object") : [];
    draftEmptyNote = draftFacts.length ? "" :
      "No in-character facts found — the text looks like out-of-character discussion. " +
      "Rewrite what actually happened in play from a template:";
    renderDraft();
  }

  // The job id of the parse the CURRENT draft came from — set by the poll
  // loop (the job it started) and by restoreLastParse (the job whose draft
  // was restored). Sent with /api/facts/bulk so the server can flag that
  // job's draft as consumed.
  let lastParseJobId = null;

  // Polls a facts_parse job until it reaches a terminal status, then loads
  // its draft. See app/audio_jobs.py create_facts_parse_job: a long recap
  // against a CPU-local model can take minutes — far past the ~100s a
  // reverse proxy will hold one request open, which is exactly what lost
  // the GM's whole parse (HTTP 524, nothing kept) when this was a
  // synchronous POST.
  //
  // The loop NEVER reports "Failed" while the job is merely slow or the
  // connection merely flaky:
  // - SLOW JOB: every poll reads chunk_current/chunk_total progress; any
  //   CHANGE resets the give-up deadline, so a healthily progressing parse
  //   is polled indefinitely. Only NO progress for ~10 minutes (200 polls
  //   × 3s), or the ~2h absolute ceiling, ends the loop with a soft "it
  //   keeps running without this tab" note — never a failure.
  // - FLAKY CONNECTION: a poll that throws is NOT a parse failure. Poll
  //   failures back off exponentially (3s → 6s → 12s → … capped at 30s) up
  //   to 10 in a row before a soft give-up; one success resets the ladder.
  //   ONLY a job status of "error" produces a "Failed: <job.error>" here.
  async function pollFactsParseJob(jobId, status) {
    const POLL_MS = 3000;
    const MAX_IDLE_POLLS = 200;
    const HARD_CAP_MS = 2 * 60 * 60 * 1000;
    const MAX_CONSECUTIVE_FETCH_FAILURES = 10;
    const MAX_BACKOFF_MS = 30000;
    let lastChunkCurrent = null;
    let idlePolls = 0;
    let fetchFailures = 0;
    let nextDelayMs = POLL_MS;
    const startedAt = Date.now();
    for (let poll = 1; ; poll++) {
      status.textContent = `Parsing in background… (poll ${poll})`;
      await new Promise((r) => setTimeout(r, nextDelayMs));
      let job;
      try {
        const res = await fetch(`/api/audio-jobs/${jobId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        job = await res.json();
      } catch (e) {
        fetchFailures++;
        if (fetchFailures >= MAX_CONSECUTIVE_FETCH_FAILURES) {
          status.textContent = `Couldn't reach the server after ${fetchFailures} tries (${e.message}) — the parse itself keeps running in the background. Use Restore last parse once it finishes.`;
          return;
        }
        nextDelayMs = Math.min(MAX_BACKOFF_MS, POLL_MS * Math.pow(2, fetchFailures));
        status.textContent = `Poll failed (${e.message}) — retrying in ${Math.round(nextDelayMs / 1000)}s…`;
        continue;
      }
      fetchFailures = 0;
      nextDelayMs = POLL_MS;
      if (job.status === "done") {
        lastParseJobId = jobId;
        let facts = [];
        try { facts = JSON.parse(job.result_json || "[]"); } catch (e) { facts = []; }
        loadParsedFacts(facts);
        status.textContent = draftFacts.length
          ? `${draftFacts.length} fact(s) drafted — review below.`
          : "No in-character facts found — see the note below.";
        return;
      }
      if (job.status === "error") throw new Error(job.error || "The parse job failed.");
      if (job.chunk_current != null && job.chunk_current !== lastChunkCurrent) {
        lastChunkCurrent = job.chunk_current;
        idlePolls = 0;
      } else {
        idlePolls++;
      }
      const part = job.chunk_current && job.chunk_total ? ` — part ${job.chunk_current}/${job.chunk_total}` : "";
      status.textContent = `Parsing in background… (poll ${poll})${part}`;
      if (idlePolls >= MAX_IDLE_POLLS || Date.now() - startedAt >= HARD_CAP_MS) {
        status.textContent = "Still parsing in the background — check Background Jobs (it keeps running even with this tab closed). When it finishes, use Restore last parse to load the draft.";
        return;
      }
    }
  }

  // Same population mechanism as the Sessions page's background-job model
  // dropdown: server-offered models appended onto the "(default model)"
  // empty option. Any failure just leaves the picker on the default.
  async function loadParseModelOptions() {
    const sel = document.getElementById("parse-model");
    if (!sel) return;
    try {
      const res = await fetch("/api/ai/models");
      if (!res.ok) return;
      const d = await res.json();
      (d.models || []).forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label || m.id;
        sel.appendChild(opt);
      });
    } catch (e) { /* picker just stays on "(default model)" */ }
  }

  // The parse options the pickers hold — sent with every parse-job POST so
  // the background run reproduces exactly what the GM asked for. Blank
  // limit fields fall back to the module's own defaults server-side.
  function parseModelOptions() {
    const modelSel = document.getElementById("parse-model");
    const thinkBox = document.getElementById("parse-think");
    const ragBox = document.getElementById("parse-rag-checkbox");
    const entityEl = document.getElementById("parse-rag-entity-limit");
    const notesEl = document.getElementById("parse-rag-notes-limit");
    const instructionsEl = document.getElementById("parse-extra-instructions");
    return {
      model: modelSel ? modelSel.value : "",
      think: !!(thinkBox && thinkBox.checked),
      use_rag: !!(ragBox && ragBox.checked),
      rag_entity_limit: entityEl && entityEl.value.trim() !== "" ? parseInt(entityEl.value, 10) : null,
      rag_notes_limit: notesEl && notesEl.value.trim() !== "" ? parseInt(notesEl.value, 10) : null,
      extra_instructions: instructionsEl ? instructionsEl.value.trim() : "",
    };
  }

  // Everything that must go quiet while a parse poll loop owns the page
  // state: "Restore last parse" and the draft controls — restoring a draft
  // or adding rows mid-poll would race the poll's own loadParsedFacts call.
  // Re-enabled in parseDraftFromRecap's finally, which every exit path
  // (done, error, soft give-up, thrown poll failure) runs through.
  function _setParseBusyControls(disabled) {
    ["restore-parse-btn", "draft-add-btn", "draft-save-btn"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = disabled;
    });
  }

  async function parseDraftFromRecap() {
    const btn = document.getElementById("parse-btn");
    if (btn.disabled) return;
    const text = document.getElementById("recap-input").value.trim();
    if (!text) return;
    const status = document.getElementById("parse-status");
    const sessionId = getSessionId();
    btn.disabled = true;
    _setParseBusyControls(true);
    status.textContent = "Starting background parse…";
    try {
      const res = await fetch("/api/facts/parse-job", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, game_session_id: sessionId || null, ...parseModelOptions() }),
      });
      if (!res.ok) throw await ndApiErrorFrom(res);
      const data = await res.json();
      await pollFactsParseJob(data.job_id, status);
    } catch (e) {
      status.textContent = "Failed: " + e.message;
    } finally {
      btn.disabled = false;
      _setParseBusyControls(false);
    }
  }

  // The parsed draft is persisted on the job row server-side (AudioJob.
  // result_json), so a reload — or a parse that finished after the tab was
  // closed — doesn't throw the review work away.
  async function restoreLastParse(quiet) {
    const status = document.getElementById("parse-status");
    const btn = document.getElementById("restore-parse-btn");
    btn.disabled = true;
    try {
      const res = await fetch("/api/facts/last-parse");
      if (res.status === 404) {
        if (!quiet) status.textContent = "Nothing to restore yet — no finished parse saved.";
        return;
      }
      if (!res.ok) throw await ndApiErrorFrom(res);
      const data = await res.json();
      lastParseJobId = data.job_id || null;
      // Preselect the session the parse was run for (standalone mode only —
      // same guard the Background Jobs hand-off below uses against a
      // deleted session).
      setSessionIdIfPossible(data.game_session_id);
      loadParsedFacts(data.facts || []);
      const when = data.created_at ? ` (${new Date(data.created_at).toLocaleString()})` : "";
      status.textContent = `Restored ${draftFacts.length} drafted fact(s) from your last parse${when}.`;
    } catch (e) {
      if (!quiet) status.textContent = "Failed: " + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // If we arrived here via a Background Jobs "📋 Extract facts" button, the
  // transcript (and which Session it belongs to, if known) is waiting in
  // sessionStorage — pre-fill and auto-run the same parse flow instead of
  // making the GM copy-paste a possibly-hours-long transcript in by hand.
  function applyFactsHandoff() {
    const raw = sessionStorage.getItem("nd_facts_handoff");
    if (!raw) return;
    sessionStorage.removeItem("nd_facts_handoff");
    try {
      const handoff = JSON.parse(raw);
      if (handoff.text) document.getElementById("recap-input").value = handoff.text;
      setSessionIdIfPossible(handoff.game_session_id);
      if (handoff.text) parseDraftFromRecap();
    } catch (e) { /* malformed handoff — just leave the form empty */ }
  }

  async function saveDraft() {
    const clean = draftFacts.map((f) => ({
      content: (f.content || "").trim(), visible_to_players: !!f.visible_to_players,
      tags: (f.tags || "").trim(),
    })).filter((f) => f.content);
    if (!clean.length) return;
    const sessionId = getSessionId();
    const btn = document.getElementById("draft-save-btn");
    const status = document.getElementById("draft-status");
    btn.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch("/api/facts/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ facts: clean, game_session_id: sessionId || null, job_id: lastParseJobId }),
      });
      if (!res.ok) throw await ndApiErrorFrom(res);
      const data = await res.json();
      onSaved(data);
      if (data.skipped_duplicates) {
        // A caller whose onSaved reloads the page races this text — set it
        // anyway so a slow reload still shows why fewer rows landed than
        // were confirmed.
        status.textContent = `Saved ${data.created} fact(s); skipped ${data.skipped_duplicates} duplicate(s) of facts already logged.`;
      }
    } catch (e) {
      status.textContent = "Failed: " + e.message;
      btn.disabled = false;
    }
  }

  function init() {
    document.querySelectorAll(".tpl-btn").forEach((btn) => btn.addEventListener("click", () => {
      applyRecapTemplate(btn.dataset.tpl);
    }));
    document.getElementById("draft-add-btn").addEventListener("click", () => {
      draftFacts.push({ content: "", visible_to_players: true, tags: "" });
      draftEmptyNote = "";
      renderDraft();
    });
    document.getElementById("parse-btn").addEventListener("click", parseDraftFromRecap);
    document.getElementById("restore-parse-btn").addEventListener("click", () => restoreLastParse(false));
    document.getElementById("draft-save-btn").addEventListener("click", saveDraft);
    loadParseModelOptions();
    // Auto-restore on load — quiet=true keeps "nothing to restore" off an
    // untouched page.
    restoreLastParse(true);
    if (enableHandoff) applyFactsHandoff();
  }

  return { init };
}
