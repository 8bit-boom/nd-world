// The GM session page's script, the part the New Session form uses too (AI recap, audio jobs, mic) - moved out of app/templates/sessions/detail.html (docs/LEFTOVERS_PLAN.md, step 3) so the browser caches it. The page's own values
// (session id, title, prep, loot, ...) come from the <script type="application/json" id="nd-session-data"> block as ND_SESSION.
const ND_SESSION = JSON.parse(document.getElementById('nd-session-data').textContent);
// ── AI recap assist — unconditional (works on the New Session form too,
// where gsession doesn't exist yet, unlike the prep/loot/XP panels below) ──
let _aiRecapDraft = '';
ndUpdateTokenCount('summary-field', 'summary-token-count');

function _aiRecapButtons() { return document.querySelectorAll('.ai-recap-btn'); }

// Null-safe getElementById wrappers — a background job's "Use this" can
// fire long after this page loaded (this panel keeps polling/re-rendering
// for as long as ANY job is in progress, see audio-jobs.js' render()), so
// an element genuinely missing (a stale page, a browser extension mutating
// the DOM, a future template change that drops one of these ids) must not
// throw and abort the whole draft-loading flow — every OTHER element here
// still needs to update even if one is unexpectedly absent. Previously each
// call site did a bare document.getElementById(...).property, which threw
// "Cannot read properties of null" the moment any single element was
// missing — the exact failure this app's own onUse try/catch (below)
// caught and reported in practice.
function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
function _setStyle(id, prop, value) {
  const el = document.getElementById(id);
  if (el) el.style[prop] = value;
}

function _setRecapPreviewText(text) {
  _setText('ai-recap-preview-text', text);
  _setText('ai-recap-preview-token-count', text ? `(${ndTokenLabel(text)})` : '');
}

function _setRecapTranscriptText(text) {
  _setText('ai-recap-transcript-text', text);
  _setText('ai-recap-transcript-token-count', text ? `(${ndTokenLabel(text)})` : '');
  _setStyle('ai-recap-transcript-wrap', 'display', 'block');
}

// Transcription succeeded but the summarize step itself failed (see
// recap_failed in the /summarize-from-audio(/complete) and
// /summarize-transcript responses) — the transcript above is still real
// and worth keeping, but `recap` is a failure sentinel, not an applyable
// draft. Swap the preview panel into a "retry" state instead of one that
// would otherwise offer the sentinel text as something to paste into the
// Summary field.
function _showRecapFailed(errorText) {
  _setText('ai-recap-preview-label', 'Summary failed');
  _setStyle('ai-recap-preview-label', 'color', '#f55');
  _setRecapPreviewText(errorText);
  _setStyle('ai-recap-preview', 'display', 'block');
  document.querySelectorAll('.ai-recap-apply-btn').forEach(b => b.style.display = 'none');
  _setStyle('ai-recap-retry-btn', 'display', '');
}

function _showRecapDraft(text) {
  _setText('ai-recap-preview-label', 'AI draft — review before applying');
  _setStyle('ai-recap-preview-label', 'color', 'var(--neon)');
  _setRecapPreviewText(text);
  _setStyle('ai-recap-preview', 'display', 'block');
  document.querySelectorAll('.ai-recap-apply-btn').forEach(b => b.style.display = '');
  _setStyle('ai-recap-retry-btn', 'display', 'none');
}

function _recapThinkEnabled() {
  const box = document.getElementById('recap-think-checkbox');
  return box ? box.checked : true;
}

// Shared by Condense (aiStartCondenseJob) and "Process in Background" —
// the only two AI actions on this page that actually run through
// app.audio_jobs' job engine and can retrieve World context (see
// app.audio_jobs._build_rag_context). Blank limit fields fall back to the
// module's own defaults server-side (see AudioJob.use_rag's docstring),
// so this only sends a number when the GM actually typed one.
function _recapRagOptions() {
  const box = document.getElementById('recap-rag-checkbox');
  const entityEl = document.getElementById('recap-rag-entity-limit');
  const notesEl = document.getElementById('recap-rag-notes-limit');
  return {
    use_rag: !!(box && box.checked),
    rag_entity_limit: (entityEl && entityEl.value.trim() !== '') ? parseInt(entityEl.value, 10) : null,
    rag_notes_limit: (notesEl && notesEl.value.trim() !== '') ? parseInt(notesEl.value, 10) : null,
  };
}

async function aiRunRecap(url, body) {
  const status = document.getElementById('ai-recap-status');
  status.textContent = 'Thinking…';
  _aiRecapButtons().forEach(b => b.disabled = true);
  try {
    const res = await ndAiFetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ think: _recapThinkEnabled(), ...body }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    const data = await res.json();
    _aiRecapDraft = data.recap || '';
    if (!_aiRecapDraft) {
      status.textContent = data.empty ? 'No facts logged for this session yet.' : 'No recap produced.';
      return;
    }
    // _showRecapDraft (not a bare _setRecapPreviewText+display) so the
    // Apply/Retry button visibility always resets here too — otherwise a
    // GM who previously hit a summarize-from-audio failure (which hides
    // Apply and shows Retry, see _showRecapFailed) and then ran a
    // DIFFERENT recap action through this shared function would see that
    // stale button state carry over into an actually-valid draft.
    _showRecapDraft(_aiRecapDraft);
    status.textContent = '';
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    _aiRecapButtons().forEach(b => b.disabled = false);
  }
}

function aiExpandNotes() {
  const notes = document.getElementById('summary-field').value.trim();
  if (!notes) { alert('Type some notes first.'); return; }
  aiRunRecap('/api/sessions/ai/expand-notes', { notes });
}

// Condense runs as a durable background job (like "Process in Background"
// below) rather than a blocking request — it uses whatever model is
// selected in the audio panel's model picker, and keeps running even if
// this tab is closed or you navigate away. Poll/result handling is the
// existing sessionAudioJobs panel (it already lists purpose="condense"
// jobs alongside session_recap ones, see api_audio_job_list's purpose
// filter) — "Use this" on a finished job loads it into the AI-draft
// preview exactly like every other recap source on this page.
function _condenseTokenBound(elId) {
  const el = document.getElementById(elId);
  if (!el || !el.value.trim()) return null;
  const n = parseInt(el.value, 10);
  return Number.isFinite(n) ? n : null;
}

async function aiStartCondenseJob(recap, fitContext) {
  const status = document.getElementById('ai-recap-status');
  const modelSel = document.getElementById('audio-bgjob-model');
  const minTokens = _condenseTokenBound('condense-min-tokens');
  const maxTokens = _condenseTokenBound('condense-max-tokens');
  if (minTokens && maxTokens && minTokens > maxTokens) {
    alert('Min tokens can\'t be greater than max tokens.');
    return;
  }
  status.textContent = 'Starting…';
  try {
    const res = await fetch('/api/sessions/ai/condense-job', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        recap, fit_context: !!fitContext, think: _recapThinkEnabled(),
        model: modelSel ? modelSel.value : '',
        min_tokens: minTokens, max_tokens: maxTokens,
        strictness: document.getElementById("condense-strictness").value || "guideline",
        extra_instructions: document.getElementById('condense-extra-instructions').value.trim(),
        game_session_id: ND_SESSION.id,
        ..._recapRagOptions(),
      }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    status.textContent = 'Condensing in the background — see Background Jobs below for the result.';
    await sessionAudioJobs.refreshList();
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  }
}

function aiCondenseRecap() {
  const recap = document.getElementById('summary-field').value.trim();
  if (!recap) { alert('Nothing to condense yet.'); return; }
  aiStartCondenseJob(recap, false);
}

function aiCondenseRecapFit() {
  const recap = document.getElementById('summary-field').value.trim();
  if (!recap) { alert('Nothing to condense yet.'); return; }
  aiStartCondenseJob(recap, true);
}

// ── Folk tale / song — a piece of in-world flavor lore, not a recap edit,
// so (unlike Expand/Condense/Summarize above) the result never touches the
// Summary field itself. Runs through the shared AI-assist engine (app.
// ai_assist's folk_tale op via POST /api/ai/assist) rather than a
// session-specific route — the Summary text is the only input it needs.
const SESSION_TITLE = ND_SESSION.title;

async function aiFolkTale() {
  const summary = document.getElementById('summary-field').value.trim();
  if (!summary) { alert('Write (or generate) a recap first.'); return; }
  const status = document.getElementById('ai-recap-status');
  const resultWrap = document.getElementById('tale-result-wrap');
  status.textContent = 'Weaving a tale…';
  resultWrap.style.display = 'none';
  _aiRecapButtons().forEach(b => b.disabled = true);
  try {
    const res = await ndAiFetch('/api/ai/assist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        op: 'folk_tale', surface: 'session-recap', body: summary,
        think: _recapThinkEnabled(), ..._recapRagOptions(),
      }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    const data = await res.json();
    document.getElementById('tale-result-text').textContent = data.text || '';
    document.getElementById('tale-name-field').value = SESSION_TITLE ? ('Tale: ' + SESSION_TITLE) : 'A Tale From the Table';
    document.getElementById('tale-save-status').textContent = '';
    resultWrap.style.display = 'block';
    resultWrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    status.textContent = '';
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    _aiRecapButtons().forEach(b => b.disabled = false);
  }
}

async function aiSaveTale() {
  const btn = document.getElementById('tale-save-btn');
  const status = document.getElementById('tale-save-status');
  const name = document.getElementById('tale-name-field').value.trim();
  const text = document.getElementById('tale-result-text').textContent;
  if (!text.trim()) return;
  btn.disabled = true;
  status.textContent = 'Saving…';
  try {
    const res = await fetch('/api/ai/save-note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: name || 'Untitled Tale', content: text, subtype: 'tale' }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    const data = await res.json();
    status.innerHTML = '✓ Saved — <a href="/entity/' + data.id + '" style="color:var(--neon)">view</a>';
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}


function aiApplyRecap(mode) {
  const ta = document.getElementById('summary-field');
  const current = ta.value.trim();
  ta.value = (mode === 'append' && current) ? current + '\n\n' + _aiRecapDraft : _aiRecapDraft;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  aiDiscardRecap();
}

function aiDiscardRecap() {
  document.getElementById('ai-recap-preview').style.display = 'none';
  document.getElementById('ai-recap-transcript-wrap').style.display = 'none';
  _aiRecapDraft = '';
}

// ── Audio → transcript → recap (file drop/picker or live mic recording) ────

async function aiSummarizeFromAudio(file) {
  // ALWAYS a durable background job now: transcribe + summarize on a long
  // recording comfortably outlives Cloudflare's ~100 s no-byte timeout
  // (the HTTP 524 the one-shot path kept hitting). The job survives
  // closing the page, reports progress in the jobs panel below, and its
  // "Use this" lands the recap/transcript here exactly like before.
  const status = document.getElementById('ai-recap-status');
  const micStatus = document.getElementById('mic-record-status');
  const bar = ndProgressBar(document.getElementById('audio-recap-progress'));
  status.textContent = '';
  micStatus.textContent = '';
  bar.setPercent(0, 'Uploading… 0%');
  try {
    const rag = _recapRagOptions();
    await sessionAudioJobs.startJob(
      file,
      {
        game_session_id: (ND_SESSION.id == null ? '' : String(ND_SESSION.id)),
        model: document.getElementById('audio-bgjob-model').value,
        extra_instructions: document.getElementById('audio-recap-instructions').value,
        think: _recapThinkEnabled(),
        use_rag: rag.use_rag,
        rag_entity_limit: rag.rag_entity_limit === null ? '' : rag.rag_entity_limit,
        rag_notes_limit: rag.rag_notes_limit === null ? '' : rag.rag_notes_limit,
      },
      (e) => ndProgressFromUpload(bar, e, 'Job started — transcribing & summarizing in background…'),
    );
    status.textContent = 'Background job started — results land in the jobs list below when ready (safe to close this page).';
  } catch (e) {
    status.textContent = 'Failed to start background job: ' + e.message;
  } finally {
    bar.clear();
  }
}

// Retries just the summarize step over the transcript already shown in the
// "Raw transcript" details block — no re-upload/re-transcription needed.
// Reachable only from the "🔁 Retry summary from this transcript" button,
// which _showRecapFailed reveals exactly when the prior attempt's
// transcribe step succeeded but summarize itself failed.
async function aiRetrySummaryFromTranscript() {
  const status = document.getElementById('ai-recap-status');
  const transcript = document.getElementById('ai-recap-transcript-text').textContent;
  if (!transcript) return;
  status.textContent = '';
  _aiRecapButtons().forEach(b => b.disabled = true);
  try {
    const res = await ndAiFetch('/api/sessions/ai/summarize-transcript', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        transcript,
        extra_instructions: document.getElementById('audio-recap-instructions').value,
        think: _recapThinkEnabled(),
      }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    const data = await res.json();
    if (data.recap_failed) {
      _aiRecapDraft = '';
      _showRecapFailed(data.recap || '(no error detail)');
    } else {
      _aiRecapDraft = data.recap || '';
      _showRecapDraft(_aiRecapDraft || '(empty recap)');
    }
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    _aiRecapButtons().forEach(b => b.disabled = false);
  }
}

const sessionAudioJobs = ndAudioJobs(document.getElementById('audio-jobs-panel'), {
  createUrl: '/api/sessions/ai/audio-jobs',
  chunkUrl: '/api/sessions/ai/audio-jobs/chunk',
  completeUrl: '/api/sessions/ai/audio-jobs/complete',
  createFromClipUrl: '/api/sessions/ai/audio-jobs/from-clip',
  // Scoped to THIS session's jobs when one exists (the server filters on
  // AudioJob.game_session_id, which every job-creating route on this page
  // persists) — a GM on session #12 shouldn't wade through every other
  // session's recap/condense jobs in the panel; the full world-wide list
  // (with purpose/status filters) stays one "All jobs →" click away.
  listUrl: '/api/sessions/ai/audio-jobs' + (ND_SESSION.id ? '?game_session_id=' + ND_SESSION.id : ''),
  allJobsUrl: '/background-jobs',
  onUse: (job) => {
    // A background job's own failure/retry story already lives on the
    // Background Jobs page (auto-retry + one-click "Retry without
    // Thinking", see app/templates/background_jobs.html) — this callback
    // only fires from "Use this" on a job the GM picked to bring over, so
    // _showRecapDraft (not _showRecapFailed) is always the right state
    // here; it still resets any stale Apply/Retry visibility left over
    // from a prior audio-upload failure on THIS page.
    //
    // Wrapped in try/catch so a genuinely unexpected failure (e.g. a huge
    // job.recap/job.transcript from a long map-reduce summarize tripping
    // something downstream) surfaces as visible text on the page instead
    // of a silent no-op — this handler previously had no error handling
    // at all, so any exception here just looked like "Use this" did
    // nothing, with no way to tell why short of opening devtools.
    try {
      _aiRecapDraft = job.recap || '';
      _showRecapDraft(_aiRecapDraft || '(empty recap)');
      if (job.transcript) _setRecapTranscriptText(job.transcript);
    } catch (e) {
      document.getElementById('ai-recap-status').textContent = 'Could not load this job: ' + e.message;
    }
  },
});

(async function loadBgJobModelOptions() {
  const sel = document.getElementById('audio-bgjob-model');
  try {
    const res = await fetch('/api/ai/models');
    if (!res.ok) return;
    const d = await res.json();
    (d.models || []).forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.label || m.id;
      sel.appendChild(opt);
    });
  } catch (e) { /* picker just stays on "(default model)" */ }
})();

document.getElementById('audio-file-input').addEventListener('change', function () {
  if (this.files[0]) aiSummarizeFromAudio(this.files[0]);
  this.value = '';
});

// "Choose from Audio Library" — transcribes a recording already saved in
// this world's Audio Library (app/routers/audio.py's /api/audio/clips)
// instead of uploading a new file. Clips are fetched once, on first open,
// same lazy-load pattern loadBgJobModelOptions above uses for the model
// dropdown.
let _audioLibraryClipsLoaded = false;
async function toggleAudioLibraryPicker() {
  const picker = document.getElementById('audio-library-picker');
  const showing = picker.style.display !== 'none';
  picker.style.display = showing ? 'none' : 'flex';
  if (!showing && !_audioLibraryClipsLoaded) await loadAudioLibraryClips();
}

async function loadAudioLibraryClips() {
  const sel = document.getElementById('audio-library-clip-select');
  try {
    const res = await fetch('/api/audio/clips');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const clips = await res.json();
    sel.innerHTML = '';
    if (!clips.length) {
      sel.innerHTML = '<option value="">No clips in the Audio Library yet</option>';
    } else {
      sel.innerHTML = '<option value="">Choose a clip…</option>';
      clips.forEach((c) => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = c.name;
        sel.appendChild(opt);
      });
    }
    _audioLibraryClipsLoaded = true;
  } catch (e) {
    sel.innerHTML = '<option value="">Failed to load clips</option>';
  }
}

async function aiStartAudioJobFromClip() {
  const clipId = document.getElementById('audio-library-clip-select').value;
  if (!clipId) { alert('Choose a clip first.'); return; }
  const status = document.getElementById('ai-recap-status');
  const model = document.getElementById('audio-bgjob-model').value;
  const instructions = document.getElementById('audio-recap-instructions').value;
  const rag = _recapRagOptions();
  status.textContent = 'Starting background job…';
  try {
    await sessionAudioJobs.startJobFromClip(clipId, {
      game_session_id: (ND_SESSION.id == null ? '' : String(ND_SESSION.id)), model: model,
      extra_instructions: instructions, think: _recapThinkEnabled(),
      use_rag: rag.use_rag,
      rag_entity_limit: rag.rag_entity_limit === null ? '' : rag.rag_entity_limit,
      rag_notes_limit: rag.rag_notes_limit === null ? '' : rag.rag_notes_limit,
    });
    status.textContent = 'Background job started — see the jobs list below.';
  } catch (e) {
    status.textContent = 'Failed to start background job: ' + e.message;
  }
}

const _audioDropzone = document.getElementById('audio-dropzone');
['dragover', 'dragenter'].forEach(ev => _audioDropzone.addEventListener(ev, e => {
  e.preventDefault();
  _audioDropzone.style.borderColor = 'var(--neon)';
}));
['dragleave', 'drop'].forEach(ev => _audioDropzone.addEventListener(ev, e => {
  e.preventDefault();
  _audioDropzone.style.borderColor = 'var(--border)';
}));
_audioDropzone.addEventListener('drop', e => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) aiSummarizeFromAudio(file);
});

let _micRecorder = null;
let _micStartedAt = 0;
let _micTimerId = null;

function toggleMicRecording() {
  if (_micRecorder && _micRecorder.isRecording()) {
    clearInterval(_micTimerId);
    _micRecorder.stop();
    return;
  }
  const micBtn = document.getElementById('mic-record-btn');
  const micStatus = document.getElementById('mic-record-status');
  _micRecorder = ndMicRecorder(
    (blob, mimeType) => {
      micBtn.textContent = '🎤 Record';
      micStatus.textContent = '';
      aiSummarizeFromAudio(new File([blob], ndMicFilename(mimeType), { type: mimeType }));
    },
    (err) => {
      micBtn.textContent = '🎤 Record';
      micStatus.textContent = 'Mic error: ' + err.message;
    },
  );
  _micRecorder.start().then(started => {
    if (!started) return;  // ndMicRecorder already invoked the onError above
    micBtn.textContent = '⏹ Stop';
    _micStartedAt = Date.now();
    _micTimerId = setInterval(() => {
      const secs = Math.floor((Date.now() - _micStartedAt) / 1000);
      micStatus.textContent = `Recording… ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
    }, 500);
  }).catch(err => {
    micBtn.textContent = '🎤 Record';
    micStatus.textContent = 'Mic error: ' + err.message;
  });
}

