// ── Image gen state ───────────────────────────────────────────────────────────

let _igBatchSize = 1;
let _igUpscaleFactor = 2.0;
let _igInitImageB64 = '';
let _igCNImageB64 = '';
let _igLastUrls = [];
let _igStarred = new Set();
let _igHiresFix = false;
let _igProgressTimer = null;
let _igIsComfyUI = false; // imagegen/progress always reports 0/0 on ComfyUI — see igGenerate
let _igLoras = [{name: '', weight: '0.8'}];
let _igLoraList = [];
let _igIPAImageB64 = '';

function igSetSize(w, h, btn) {
  document.getElementById('ig-width').value = w;
  document.getElementById('ig-height').value = h;
  document.querySelectorAll('.ig-preset-btn').forEach(b => b.classList.remove('ig-preset-active'));
  if (btn) btn.classList.add('ig-preset-active');
}

function igSetBatch(btn) {
  _igBatchSize = parseInt(btn.dataset.val);
  document.querySelectorAll('.ig-batch-btn[data-val]').forEach(b => b.classList.remove('ig-batch-active'));
  btn.classList.add('ig-batch-active');
}

function igSetUpscale(btn) {
  _igUpscaleFactor = parseFloat(btn.dataset.scale);
  document.querySelectorAll('.ig-batch-btn[data-scale]').forEach(b => b.classList.remove('ig-batch-active'));
  btn.classList.add('ig-batch-active');
}

function igPreviewInit(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    _igInitImageB64 = e.target.result; // full data URI
    document.getElementById('ig-init-img').src = _igInitImageB64;
    document.getElementById('ig-i2i-preview').style.display = 'block';
  };
  reader.readAsDataURL(file);
}

function igClearInit() {
  _igInitImageB64 = '';
  document.getElementById('ig-init-file').value = '';
  document.getElementById('ig-i2i-preview').style.display = 'none';
}

function igPreviewCN(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    _igCNImageB64 = e.target.result;
    document.getElementById('ig-cn-img').src = _igCNImageB64;
    document.getElementById('ig-cn-preview').style.display = 'block';
  };
  reader.readAsDataURL(file);
}

function igClearCN() {
  _igCNImageB64 = '';
  document.getElementById('ig-cn-file').value = '';
  document.getElementById('ig-cn-preview').style.display = 'none';
}

// ── Image gen init ────────────────────────────────────────────────────────────

(async function igInit() {
  try {
    const st = await fetch('/api/ai/imagegen/status').then(r => r.json());
    const dot = document.getElementById('ig-dot');
    if (st.ok) {
      dot.style.background = '#4f4';
      dot.title = 'Image gen ready — ' + st.type;
    } else {
      dot.style.background = '#888';
      dot.title = 'Image gen unavailable' + (st.reason ? ': ' + st.reason : '');
    }
    _igIsComfyUI = st.type === 'comfyui';
    const note = document.getElementById('ig-comfyui-note');
    if (note) note.style.display = _igIsComfyUI ? 'block' : 'none';
    await igReloadModels();
    igRenderLoras();
    igRenderPresets();
    igLoadSamplersSchedulers();
    igLoadLoras();
    igLoadUpscalers();
    igLoadRefiners();
    igLoadIPAModels();
    igLoadHistory();
    igLoadSourcesPanel();
    igLoadStarred();
    igLoadJobs();
    // Init auto-resize on prompt textareas
    const promptEl = document.getElementById('ig-prompt');
    const negEl = document.getElementById('ig-negative');
    if (promptEl) autoResize(promptEl);
    if (negEl) autoResize(negEl);
  } catch (e) {
    console.warn('igInit error', e);
  }
})();

// ── Build prompt from world lore ──────────────────────────────────────────────

async function igBuildPrompt() {
  const concept = document.getElementById('ig-concept').value.trim();
  if (!concept) { alert('Enter a concept/subject first.'); return; }
  const btn = document.getElementById('ig-build-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Building…';
  const ta = document.getElementById('ig-prompt');
  ta.value = '';
  try {
    const ctxLimit = document.getElementById('ctx-limit') ? parseInt(document.getElementById('ctx-limit').value) : 10;
    const notesLimit = document.getElementById('notes-limit') ? parseInt(document.getElementById('notes-limit').value) : 0;
    const ctxR = await fetch('/api/ai/world-context-smart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: concept, limit: ctxLimit, notes_limit: notesLimit }),
    });
    const ctxD = await ctxR.json();
    const lore = (ctxD.context || '').trim();
    const system = 'You are an image prompt writer for a cyberpunk-fantasy TTRPG. Given world lore and a subject, write ONE detailed image generation prompt (comma-separated tags and descriptive phrases, ~50-80 words). Output only the prompt — no explanation, no preamble, no quotes.';
    const userMsg = `Subject: ${concept}\n\n${lore ? 'World lore context:\n' + lore : '(No lore context — use general cyberpunk-fantasy style)'}`;
    const resp = await fetch('/api/ai/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: userMsg }], system, model: activeModel, surface: 'chat' }),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') break;
        try { const d = JSON.parse(raw); if (d.token) ta.value += d.token; } catch {}
      }
    }
  } catch (e) {
    const msg = e.message || String(e);
    ta.value = '';
    alert('Build from World Lore failed: ' + (msg.toLowerCase().includes('networkerror') || msg.toLowerCase().includes('fetch')
      ? 'Cannot reach AI service. Is Ollama running?'
      : msg));
  } finally {
    btn.disabled = false;
    btn.textContent = '✨ Build from World Lore';
  }
}

// ── Generate image ────────────────────────────────────────────────────────────

// Every field on the ImagegenBody form — shared by the direct (blocking)
// generate button and "Generate in Background" so the two request bodies
// can never drift apart.
function _igBuildBody(prompt) {
    const activeLoras = _igLoras.filter(l => (l.name || '').trim());
    return {
      prompt,
      negative:    document.getElementById('ig-negative').value,
      model:       document.getElementById('ig-model').value,
      width:       parseInt(document.getElementById('ig-width').value),
      height:      parseInt(document.getElementById('ig-height').value),
      steps:       parseInt(document.getElementById('ig-steps').value),
      cfg:         parseFloat(document.getElementById('ig-cfg').value),
      seed:        parseInt(document.getElementById('ig-seed').value),
      sampler:     document.getElementById('ig-sampler').value,
      scheduler:   document.getElementById('ig-scheduler').value,
      batch_size:  _igBatchSize,
      loras:       activeLoras.map(l => l.name).join(','),
      lora_weights: activeLoras.map(l => l.weight || '0.8').join(','),
      vae:         (document.getElementById('ig-vae')?.value || '').trim(),
      clip_skip:   parseInt(document.getElementById('ig-clipskip')?.value || '-1'),
      init_image:  _igInitImageB64,
      init_strength: parseFloat(document.getElementById('ig-strength')?.value || '0.65'),
      upscale_model: (document.getElementById('ig-upscaler')?.value || '').trim(),
      upscale_factor: _igUpscaleFactor,
      controlnet_image: _igCNImageB64,
      controlnet_strength: parseFloat(document.getElementById('ig-cn-strength')?.value || '0.8'),
      controlnet_preprocessor: (document.getElementById('ig-cn-preprocessor')?.value || ''),
      controlnet_model: (document.getElementById('ig-cn-model')?.value || '').trim(),
      hiresfix: _igHiresFix,
      hireswidth: _igHiresFix ? parseInt(document.getElementById('ig-hires-w')?.value || 0) : 0,
      hiresheight: _igHiresFix ? parseInt(document.getElementById('ig-hires-h')?.value || 0) : 0,
      hiresdenoisestrength: _igHiresFix ? parseFloat(document.getElementById('ig-hires-denoise')?.value || 0.5) : 0.5,
      hiressteps: _igHiresFix ? parseInt(document.getElementById('ig-hires-steps')?.value || 0) : 0,
      refiner_model: (document.getElementById('ig-refiner')?.value || '').trim(),
      refiner_control: parseFloat(document.getElementById('ig-refiner-ctrl')?.value || 0.8),
      seamless_x: document.getElementById('ig-seamless-x')?.checked || false,
      seamless_y: document.getElementById('ig-seamless-y')?.checked || false,
      variation_seed: parseInt(document.getElementById('ig-variation-seed')?.value || '-1'),
      variation_strength: parseFloat(document.getElementById('ig-variation-strength')?.value || '0'),
      freeu_enabled: document.getElementById('ig-freeu-enable')?.checked || false,
      freeu_b1: parseFloat(document.getElementById('ig-freeu-b1')?.value || 1.3),
      freeu_b2: parseFloat(document.getElementById('ig-freeu-b2')?.value || 1.4),
      freeu_s1: parseFloat(document.getElementById('ig-freeu-s1')?.value || 0.9),
      freeu_s2: parseFloat(document.getElementById('ig-freeu-s2')?.value || 0.2),
      dynthresh_enabled: document.getElementById('ig-dynthresh-enable')?.checked || false,
      dynthresh_mimic_scale: parseFloat(document.getElementById('ig-dynthresh-scale')?.value || 7),
      dynthresh_percentile: parseFloat(document.getElementById('ig-dynthresh-pct')?.value || 0.999),
      cfg_rescale: parseFloat(document.getElementById('ig-cfgrescale')?.value || 0),
      ipadapter_image: _igIPAImageB64,
      ipadapter_strength: parseFloat(document.getElementById('ig-ipa-strength')?.value || 0.6),
      ipadapter_model: (document.getElementById('ig-ipa-model')?.value || '').trim(),
    };
}

// Renders one result card per url into `grid` — shared by the direct
// generate flow and a finished background job, so both look identical.
function _igRenderResultCards(urls, body, grid) {
  urls.forEach((url, i) => {
    const card = document.createElement('div');
    card.className = 'ig-result-card';
    const img = document.createElement('img');
    img.src = url;
    img.alt = `Generated image ${i + 1}`;
    const actions = document.createElement('div');
    actions.className = 'ig-result-card-actions';
    const dlLink = document.createElement('a');
    dlLink.href = url;
    dlLink.download = `nd-image-${i + 1}.png`;
    dlLink.textContent = '⬇ Save';
    const attachBtn = document.createElement('button');
    attachBtn.textContent = '📎 Attach…';
    attachBtn.onclick = () => igAttachToEntity(url);
    const starBtn = document.createElement('button');
    starBtn.className = 'star-btn';
    starBtn.title = 'Star this image';
    starBtn.textContent = '☆';
    starBtn.onclick = () => igToggleStar(url, body, starBtn);
    const i2iBtn = document.createElement('button');
    i2iBtn.textContent = '→ Img2Img';
    i2iBtn.title = 'Use as img2img source';
    i2iBtn.onclick = () => igSendToImg2Img(url);
    actions.appendChild(dlLink);
    actions.appendChild(i2iBtn);
    actions.appendChild(attachBtn);
    actions.appendChild(starBtn);
    // Only when this generation came from "🎭 Pick entity…" above — bound
    // to that entity at generation time, not whatever's picked now, so a
    // later pick doesn't retarget an older result card's button.
    if (_igPortraitEntityId) {
      const portraitEntityId = _igPortraitEntityId;
      const portraitEntityName = _igPortraitEntityName;
      const portraitBtn = document.createElement('button');
      portraitBtn.textContent = '🖼 Set as ' + portraitEntityName + '\'s portrait';
      portraitBtn.onclick = () => igSetAsPortrait(url, portraitEntityId, portraitEntityName, portraitBtn);
      actions.appendChild(portraitBtn);
    }
    card.appendChild(img);
    card.appendChild(actions);
    grid.appendChild(card);
  });
}

async function igGenerate() {
  const prompt = document.getElementById('ig-prompt').value.trim();
  if (!prompt) { alert('Enter a prompt first — or use "Build from World Lore".'); return; }

  const btn    = document.getElementById('ig-generate-btn');
  const status = document.getElementById('ig-status');
  const err    = document.getElementById('ig-error');
  const results = document.getElementById('ig-results');
  const grid   = document.getElementById('ig-results-grid');

  btn.disabled = true;
  status.style.display = 'inline';
  err.style.display = 'none';
  results.style.display = 'none';
  grid.innerHTML = '';

  const progressWrap = document.getElementById('ig-progress-wrap');
  const track = document.getElementById('ig-progress-bar-track');
  if (progressWrap) {
    progressWrap.style.display = 'block';
    const pb = document.getElementById('ig-progress-bar');
    if (pb) pb.style.width = '0%';
    const thumb = document.getElementById('ig-preview-thumb');
    if (thumb) { thumb.style.display = 'none'; thumb.src = ''; }
    // ComfyUI's /imagegen/progress always reports 0/0 (unlike SwarmUI) — a
    // bar frozen at 0% reads as broken, so show an indeterminate "working"
    // state instead of a real percentage nothing will ever move.
    if (track) track.style.display = _igIsComfyUI ? 'none' : 'block';
    document.getElementById('ig-progress-text').textContent = _igIsComfyUI ? 'Generating… (ComfyUI reports no progress)' : '';
  }
  if (!_igIsComfyUI) _igProgressTimer = setInterval(igPollProgress, 1200);

  try {
    const body = _igBuildBody(prompt);
    const r = await fetch('/api/ai/imagegen/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (e) { /* non-JSON error body */ }
      throw new Error(detail || `Server error ${r.status}`);
    }
    const data = await r.json();
    if (data.error) throw new Error(data.error);

    const urls = data.urls && data.urls.length ? data.urls : (data.url ? [data.url] : []);
    if (!urls.length) throw new Error('No images returned');
    _igLastUrls = urls;
    igSaveToHistory(urls, body);
    _igRenderResultCards(urls, body, grid);
    results.style.display = 'block';
  } catch (e) {
    const msg = e.message || String(e);
    err.textContent = '❌ ' + (msg.toLowerCase().includes('networkerror') || msg.toLowerCase().includes('fetch')
      ? 'Cannot reach image generation service. Is SwarmUI/ComfyUI running and configured?'
      : msg);
    err.style.display = 'block';
  } finally {
    clearInterval(_igProgressTimer);
    _igProgressTimer = null;
    if (progressWrap) progressWrap.style.display = 'none';
    btn.disabled = false;
    status.style.display = 'none';
  }
}

// ── Background image generation jobs ────────────────────────────────────────

let _igJobs = [];
let _igJobsPollTimer = null;
const _IG_JOB_IN_PROGRESS = new Set(['pending', 'generating']);
const _IG_JOB_STATUS_LABEL = { pending: 'Queued…', generating: 'Generating…', done: '✓ Done', error: '✗ Failed', cancelled: 'Cancelled' };

async function igStartBackgroundJob() {
  const prompt = document.getElementById('ig-prompt').value.trim();
  if (!prompt) { alert('Enter a prompt first — or use "Build from World Lore".'); return; }
  const btn = document.getElementById('ig-bgjob-btn');
  btn.disabled = true;
  try {
    const body = _igBuildBody(prompt);
    const r = await fetch('/api/ai/imagegen/jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (e) {}
      throw new Error(detail || `Server error ${r.status}`);
    }
    await igLoadJobs();
  } catch (e) {
    alert('Could not start background job: ' + (e.message || e));
  } finally {
    btn.disabled = false;
  }
}

async function igLoadJobs() {
  const panel = document.getElementById('ig-jobs-panel');
  if (!panel) return;
  try {
    const jobs = await fetch('/api/ai/imagegen/jobs').then(r => r.json());
    _igJobs = jobs || [];
  } catch (e) { return; }

  // /imagegen/progress is a single global "what's SwarmUI doing right now"
  // status, not scoped to a job id — but since generation only ever runs
  // one at a time against the backend GPU in practice, it's a real,
  // measurable percentage for whichever job is actively "generating".
  // Always 0/0 on ComfyUI (see _igIsComfyUI's other use above), so skip the
  // fetch there and fall back to elapsed time like everything else with no
  // progress signal of its own.
  let progress = null;
  if (!_igIsComfyUI && _igJobs.some(j => j.status === 'generating')) {
    try {
      const d = await fetch('/api/ai/imagegen/progress').then(r => r.json());
      if (d.total > 0) progress = d;
    } catch (e) { /* fall back to elapsed time below */ }
  }
  _igRenderJobs(progress);

  if (_igJobsPollTimer !== null) { clearTimeout(_igJobsPollTimer); _igJobsPollTimer = null; }
  if (_igJobs.some(j => _IG_JOB_IN_PROGRESS.has(j.status))) {
    _igJobsPollTimer = setTimeout(igLoadJobs, 3000);
  }
}

function _igRenderJobs(progress) {
  const panel = document.getElementById('ig-jobs-panel');
  if (!panel) return;
  panel.innerHTML = '';
  if (!_igJobs.length) { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  const title = document.createElement('div');
  title.className = 'nd-jobs-title';
  title.textContent = 'Background jobs';
  panel.appendChild(title);
  _igJobs.forEach((job) => {
    const row = document.createElement('div');
    row.className = 'nd-job-row' + (job.status === 'error' ? ' nd-job-row--error' : '');
    const label = document.createElement('span');
    label.className = 'nd-job-label';
    const shortPrompt = (job.prompt || '').slice(0, 60) + ((job.prompt || '').length > 60 ? '…' : '');
    let igStatusText = _IG_JOB_STATUS_LABEL[job.status] || job.status;
    if (job.status === 'generating') {
      igStatusText = progress
        ? `Generating… step ${progress.step}/${progress.total} (${Math.round(progress.step / progress.total * 100)}%)`
        : ndElapsedLabel(igStatusText, job.created_at);
    }
    label.textContent = `${shortPrompt || 'image'} — ${igStatusText}`;
    row.appendChild(label);
    if (job.status === 'error' && job.error) {
      const err = document.createElement('span');
      err.className = 'nd-job-error';
      err.textContent = job.error;
      row.appendChild(err);
    }
    if (job.status === 'done' && job.urls && job.urls.length) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'nd-job-use-btn';
      btn.textContent = 'Show results';
      btn.onclick = () => {
        const results = document.getElementById('ig-results');
        const grid = document.getElementById('ig-results-grid');
        _igLastUrls = job.urls;
        _igRenderResultCards(job.urls, { prompt: job.prompt }, grid);
        results.style.display = 'block';
      };
      row.appendChild(btn);
    }
    if (_IG_JOB_IN_PROGRESS.has(job.status)) {
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'nd-job-use-btn';
      cancelBtn.textContent = 'Cancel';
      cancelBtn.onclick = async () => {
        cancelBtn.disabled = true;
        try {
          await fetch('/api/ai/imagegen/jobs/' + job.id + '/cancel', { method: 'POST' });
        } finally {
          await igLoadJobs();
        }
      };
      row.appendChild(cancelBtn);
    } else {
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'nd-job-use-btn';
      delBtn.textContent = '🗑';
      delBtn.title = 'Delete this job';
      delBtn.onclick = async () => {
        delBtn.disabled = true;
        try {
          const res = await fetch('/api/ai/imagegen/jobs/' + job.id, { method: 'DELETE' });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || `HTTP ${res.status}`);
          }
        } catch (e) {
          alert('Could not delete job: ' + e.message);
          delBtn.disabled = false;
          return;
        }
        await igLoadJobs();
      };
      row.appendChild(delBtn);
    }
    panel.appendChild(row);
  });
}

// ── SwarmUI model downloads ─────────────────────────────────────────────────

let _dlmDownloading = false;

async function igReloadModels() {
  const sel = document.getElementById('ig-model');
  if (!sel) return;
  const prev = sel.value;
  try {
    const mr = await fetch('/api/ai/imagegen/models').then(r => r.json());
    if (mr.models && mr.models.length) {
      sel.innerHTML = mr.models.map(m => `<option value="${m}">${m}</option>`).join('');
      if (prev && mr.models.includes(prev)) {
        sel.value = prev;
      } else {
        const dr = await fetch('/api/ai/defaults').then(r => r.json()).catch(() => null);
        const imageDefault = dr && dr.image;
        if (imageDefault && mr.models.includes(imageDefault)) sel.value = imageDefault;
      }
    } else {
      sel.innerHTML = '<option value="">— no models found —</option>';
    }
  } catch (e) {
    // Leave whatever was already in the dropdown — this is a refresh, not
    // the initial load, so a transient fetch failure shouldn't blank it.
  }
}

// Restarts the SwarmUI process via its own Admin API (app.ai.swarmui_restart,
// /API/UpdateAndRestart) — the reliable fallback for the case
// swarmui_refresh_after_local_change's automatic rescan doesn't cover, which
// is exactly why a download can finish with "restart SwarmUI if it doesn't
// show up in the pickers below" (see dlmStartDownload's own status text
// below). Not a docker-restart — no Docker access needed, SwarmUI's own API
// already exposes this.
let _igRestartingSwarmui = false;
async function igRestartSwarmUI() {
  if (_igRestartingSwarmui) return;
  if (!confirm('Restart SwarmUI now? Any in-progress image generation will be interrupted.')) return;
  _igRestartingSwarmui = true;
  const btn = document.getElementById('ig-restart-swarmui-btn');
  const status = document.getElementById('ig-restart-swarmui-status');
  btn.disabled = true;
  status.textContent = 'Restarting…';
  try {
    const res = await fetch('/api/ai/imagegen/restart', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      status.textContent = '✗ Could not restart — is SwarmUI configured and reachable?';
    } else {
      status.textContent = '✓ Restart requested — give it a moment, then reload this tab.';
    }
  } catch (e) {
    status.textContent = '✗ Failed: ' + (e.message || e);
  } finally {
    btn.disabled = false;
    _igRestartingSwarmui = false;
  }
}

// "Check for Updates" / "Update & Restart" — SwarmUI has its own built-in
// self-updater (git-pull + restart) reachable via /API/CheckForUpdates and
// /API/UpdateAndRestart, independent of nd-world's own Watchtower-driven
// Docker image updates. Read-only check first (never changes anything on
// its own); the Update & Restart button only appears once a server update
// is actually reported, so it's never offered to pull nothing.
let _igCheckingSwarmuiUpdates = false;
async function igCheckSwarmUIUpdates() {
  if (_igCheckingSwarmuiUpdates) return;
  _igCheckingSwarmuiUpdates = true;
  const btn = document.getElementById('ig-check-updates-btn');
  const status = document.getElementById('ig-check-updates-status');
  const updateBtn = document.getElementById('ig-update-restart-btn');
  btn.disabled = true;
  updateBtn.style.display = 'none';
  status.textContent = 'Checking…';
  try {
    const res = await fetch('/api/ai/imagegen/updates');
    const data = await res.json().catch(() => ({}));
    const u = data.updates || {};
    if (!res.ok || !u.server) {
      status.textContent = '✗ Could not check — is SwarmUI configured and reachable?';
    } else {
      const serverCount = u.server.count || 0;
      const extCount = Object.values(u.extensions || {}).reduce((n, e) => n + (e.count || 0), 0);
      const backendCount = Object.values(u.backends || {}).reduce((n, e) => n + (e.count || 0), 0);
      if (!serverCount && !extCount && !backendCount) {
        status.textContent = '✓ Up to date.';
      } else {
        status.textContent = `${serverCount} server, ${extCount} extension, ${backendCount} backend update(s) available.`;
        if (serverCount) updateBtn.style.display = 'inline-block';
      }
    }
  } catch (e) {
    status.textContent = '✗ Failed: ' + (e.message || e);
  } finally {
    btn.disabled = false;
    _igCheckingSwarmuiUpdates = false;
  }
}

async function igUpdateAndRestartSwarmUI() {
  if (_igRestartingSwarmui) return;
  if (!confirm('Update and restart SwarmUI now? Any in-progress image generation will be interrupted.')) return;
  _igRestartingSwarmui = true;
  const btn = document.getElementById('ig-update-restart-btn');
  const status = document.getElementById('ig-check-updates-status');
  btn.disabled = true;
  status.textContent = 'Updating…';
  try {
    const res = await fetch('/api/ai/imagegen/update', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      status.textContent = '✗ Update failed — ' + (data.result || 'is SwarmUI configured and reachable?');
    } else {
      status.textContent = '✓ ' + (data.result || 'Update applied — restarting.') + ' Give it a moment, then reload this tab.';
      btn.style.display = 'none';
    }
  } catch (e) {
    status.textContent = '✗ Failed: ' + (e.message || e);
  } finally {
    btn.disabled = false;
    _igRestartingSwarmui = false;
  }
}

async function dlmLoadDownloaded() {
  const list = document.getElementById('dlm-list');
  const suggestions = document.getElementById('dlm-subfolder-suggestions');
  if (!list) return;
  try {
    const d = await fetch('/api/ai/imagegen/models/downloaded').then(r => r.json());
    if (suggestions && suggestions.children.length === 0) {
      suggestions.innerHTML = (d.folder_suggestions || [])
        .filter(s => s)
        .map(s => `<option value="${s}">`).join('');
    }
    const models = d.models || [];
    if (!models.length) {
      list.textContent = 'Nothing downloaded yet.';
      return;
    }
    list.innerHTML = '';
    models.forEach((m) => {
      const row = document.createElement('div');
      row.className = 'nd-job-row';
      const label = document.createElement('span');
      label.className = 'nd-job-label';
      const path = m.subfolder ? `${m.subfolder}/${m.filename}` : m.filename;
      label.textContent = `${path} (${(m.bytes / 1e9).toFixed(2)} GB)`;
      row.appendChild(label);
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'nd-job-use-btn';
      delBtn.textContent = '🗑';
      delBtn.title = 'Delete this file';
      delBtn.onclick = async () => {
        delBtn.disabled = true;
        let refreshed = false;
        try {
          const params = new URLSearchParams({ subfolder: m.subfolder || '', filename: m.filename });
          const res = await fetch('/api/ai/imagegen/models/downloaded?' + params, { method: 'DELETE' });
          if (!res.ok) throw await ndApiErrorFrom(res);
          const data = await res.json().catch(() => ({}));
          refreshed = !!data.model_list_refreshed;
        } catch (e) {
          alert('Could not delete: ' + e.message);
          delBtn.disabled = false;
          return;
        }
        await dlmLoadDownloaded();
        igReloadModels();
        igLoadLoras();
        igLoadUpscalers();
        igLoadRefiners();
        igLoadIPAModels();
        if (!refreshed) {
          const status = document.getElementById('dlm-status');
          if (status) status.textContent = 'Deleted — restart SwarmUI if it still shows up in the pickers below';
        }
      };
      row.appendChild(delBtn);
      list.appendChild(row);
    });
  } catch (e) {
    list.textContent = 'Could not load — is nd-world reachable?';
  }
}

async function dlmStartDownload() {
  if (_dlmDownloading) return;
  const urlInput = document.getElementById('dlm-url');
  const url = urlInput.value.trim();
  if (!url) { alert('Paste a download URL first.'); return; }
  _dlmDownloading = true;
  const btn = document.getElementById('dlm-download-btn');
  const progWrap = document.getElementById('dlm-progress');
  const bar = document.getElementById('dlm-progress-bar');
  const status = document.getElementById('dlm-status');
  btn.disabled = true;
  progWrap.style.display = 'block';
  bar.style.width = '0%';
  status.textContent = 'Starting download…';
  try {
    const res = await fetch('/api/ai/imagegen/models/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        subfolder: document.getElementById('dlm-subfolder').value.trim(),
        filename: document.getElementById('dlm-filename').value.trim(),
      }),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const raw = p.slice(6);
        if (raw === '[DONE]') break;
        try {
          const obj = JSON.parse(raw);
          if (obj.error) throw new Error(obj.error);
          if (obj.total && obj.completed) {
            const pct = obj.total ? Math.round(obj.completed / obj.total * 100) : 0;
            bar.style.width = pct + '%';
            status.textContent = `Downloading… ${pct}% (${(obj.completed / 1e9).toFixed(2)} / ${(obj.total / 1e9).toFixed(2)} GB)`;
          } else if (obj.status === 'done') {
            bar.style.width = '100%';
            const savedAs = `${obj.subfolder ? obj.subfolder + '/' : ''}${obj.filename}`;
            status.textContent = obj.model_list_refreshed
              ? `✓ Downloaded ${savedAs} — now available in the pickers below`
              : `✓ Downloaded ${savedAs} — restart SwarmUI if it doesn't show up in the pickers below`;
            urlInput.value = '';
            document.getElementById('dlm-filename').value = '';
            igReloadModels();
            igLoadLoras();
            igLoadUpscalers();
            igLoadRefiners();
            igLoadIPAModels();
          }
        } catch (pe) {
          if (pe.message && !pe.message.includes('JSON')) throw pe;
        }
      }
    }
  } catch (e) {
    status.textContent = '✗ Failed: ' + e.message;
  } finally {
    btn.disabled = false;
    _dlmDownloading = false;
    await dlmLoadDownloaded();
  }
}

// ── Samplers / Schedulers dynamic loading ─────────────────────────────────────

async function igLoadSamplersSchedulers() {
  try {
    const d = await fetch('/api/ai/imagegen/samplers-schedulers').then(r => r.json());
    const samSel = document.getElementById('ig-sampler');
    const schSel = document.getElementById('ig-scheduler');
    if (d.samplers && d.samplers.length) {
      const cur = samSel.value || 'euler';
      samSel.innerHTML = d.samplers.map(s =>
        `<option value="${s}"${s === cur ? ' selected' : ''}>${s}</option>`
      ).join('');
    }
    if (d.schedulers && d.schedulers.length) {
      const cur = schSel.value || 'normal';
      schSel.innerHTML = d.schedulers.map(s =>
        `<option value="${s}"${s === cur ? ' selected' : ''}>${s}</option>`
      ).join('');
    }
  } catch(e) { console.warn('igLoadSamplersSchedulers', e); }
}

// ── LoRA multi-stack ──────────────────────────────────────────────────────────

function igRenderLoras() {
  const container = document.getElementById('ig-lora-list');
  if (!container) return;
  container.innerHTML = '';
  _igLoras.forEach((lora, idx) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:.22rem;align-items:center;margin-bottom:.18rem';

    const sel = document.createElement('select');
    sel.className = 'ig-select';
    sel.style.cssText = 'flex:1;font-size:.74rem';
    sel.innerHTML = '<option value="">— none —</option>' +
      _igLoraList.map(l => {
        const name = l.length > 34 ? '…' + l.slice(-31) : l;
        return `<option value="${l}"${l === lora.name ? ' selected' : ''} title="${l}">${name}</option>`;
      }).join('');
    if (lora.name && !_igLoraList.includes(lora.name)) {
      sel.innerHTML += `<option value="${lora.name}" selected title="${lora.name}">${lora.name.slice(-28)}</option>`;
    }
    sel.value = lora.name;
    sel.onchange = () => { _igLoras[idx].name = sel.value; };

    const wt = document.createElement('input');
    wt.type = 'number'; wt.className = 'ig-input-num';
    wt.style.cssText = 'width:54px;flex-shrink:0';
    wt.value = lora.weight; wt.step = '0.05'; wt.min = '-2'; wt.max = '2';
    wt.oninput = () => { _igLoras[idx].weight = wt.value; };

    const rm = document.createElement('button');
    rm.className = 'ig-btn';
    rm.style.cssText = 'padding:.12rem .28rem;font-size:.72rem;flex-shrink:0;color:#888;border-color:#444';
    rm.textContent = '✕'; rm.title = 'Remove';
    rm.onclick = () => igRemoveLora(idx);

    row.appendChild(sel); row.appendChild(wt); row.appendChild(rm);
    container.appendChild(row);
  });
  const addBtn = document.getElementById('ig-add-lora-btn');
  if (addBtn) addBtn.style.display = _igLoras.length >= 4 ? 'none' : '';
}

function igAddLora() {
  if (_igLoras.length >= 4) return;
  _igLoras.push({name: '', weight: '0.8'});
  igRenderLoras();
}

function igRemoveLora(idx) {
  _igLoras.splice(idx, 1);
  if (!_igLoras.length) _igLoras = [{name: '', weight: '0.8'}];
  igRenderLoras();
}

async function igLoadLoras() {
  try {
    const d = await fetch('/api/ai/imagegen/loras').then(r => r.json());
    _igLoraList = d.loras || [];
    igRenderLoras();
  } catch(e) {
    _igLoraList = [];
    igRenderLoras();
  }
}

// ── Refiner loading ───────────────────────────────────────────────────────────

async function igLoadRefiners() {
  const sel = document.getElementById('ig-refiner');
  if (!sel) return;
  sel.innerHTML = '<option value="">⏳ Loading…</option>';
  try {
    const d = await fetch('/api/ai/imagegen/refiners').then(r => r.json());
    sel.innerHTML = '<option value="">— none —</option>';
    if (d.refiners && d.refiners.length) {
      for (const r of d.refiners) {
        const name = r.length > 44 ? '…' + r.slice(-41) : r;
        sel.innerHTML += `<option value="${r}" title="${r}">${name}</option>`;
      }
    }
  } catch(e) {
    sel.innerHTML = '<option value="">— unavailable —</option>';
  }
}

// ── Highres Fix helpers ───────────────────────────────────────────────────────

function igUpdateHiresState() {
  const body = document.getElementById('ig-hires-body');
  if (body) {
    body.style.opacity = _igHiresFix ? '1' : '.4';
    body.style.pointerEvents = _igHiresFix ? '' : 'none';
  }
}

function igSetHiresScale(btn) {
  const scale = parseFloat(btn.dataset.hiresScale);
  document.querySelectorAll('[data-hires-scale]').forEach(b => b.classList.remove('ig-batch-active'));
  btn.classList.add('ig-batch-active');
  const w = parseInt(document.getElementById('ig-width')?.value || 512);
  const h = parseInt(document.getElementById('ig-height')?.value || 512);
  _setClosestOpt(document.getElementById('ig-hires-w'), Math.round(w * scale / 64) * 64);
  _setClosestOpt(document.getElementById('ig-hires-h'), Math.round(h * scale / 64) * 64);
}

function _setClosestOpt(sel, val) {
  if (!sel) return;
  let best = null, bestDiff = Infinity;
  for (const opt of sel.options) {
    const diff = Math.abs(parseInt(opt.value) - val);
    if (diff < bestDiff) { bestDiff = diff; best = opt.value; }
  }
  if (best !== null) sel.value = best;
}

// ── Variation seed helper ─────────────────────────────────────────────────────

function igUpdateVariationStrength() {
  const v = parseInt(document.getElementById('ig-variation-seed')?.value || '-1');
  const row = document.getElementById('ig-variation-strength-row');
  if (row) row.style.display = v >= 0 ? '' : 'none';
}

// ── Progress polling ──────────────────────────────────────────────────────────

async function igPollProgress() {
  try {
    const d = await fetch('/api/ai/imagegen/progress').then(r => r.json());
    const step = d.step || 0, total = d.total || 0;
    const bar = document.getElementById('ig-progress-bar');
    const txt = document.getElementById('ig-progress-text');
    if (total > 0) {
      if (bar) bar.style.width = Math.round(step / total * 100) + '%';
      if (txt) txt.textContent = `Step ${step} / ${total}`;
    }
    if (d.preview) {
      const thumb = document.getElementById('ig-preview-thumb');
      if (thumb) { thumb.src = 'data:image/png;base64,' + d.preview; thumb.style.display = 'block'; }
    }
  } catch(e) {}
}

// ── FreeU helpers ─────────────────────────────────────────────────────────────

function igUpdateFreeUState() {
  const body = document.getElementById('ig-freeu-body');
  if (body) {
    const on = document.getElementById('ig-freeu-enable')?.checked;
    body.style.opacity = on ? '1' : '.4';
    body.style.pointerEvents = on ? '' : 'none';
  }
}

function igSetFreeUPreset(b1, b2, s1, s2) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('ig-freeu-b1', b1); set('ig-freeu-b2', b2);
  set('ig-freeu-s1', s1); set('ig-freeu-s2', s2);
}

// ── DynThresh helpers ─────────────────────────────────────────────────────────

function igUpdateDynThreshState() {
  const body = document.getElementById('ig-dynthresh-body');
  if (body) {
    const on = document.getElementById('ig-dynthresh-enable')?.checked;
    body.style.opacity = on ? '1' : '.4';
    body.style.pointerEvents = on ? '' : 'none';
  }
}

// ── IP-Adapter helpers ────────────────────────────────────────────────────────

function igPreviewIPA(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    _igIPAImageB64 = e.target.result;
    document.getElementById('ig-ipa-img').src = _igIPAImageB64;
    document.getElementById('ig-ipa-preview').style.display = 'block';
  };
  reader.readAsDataURL(file);
}

function igClearIPA() {
  _igIPAImageB64 = '';
  document.getElementById('ig-ipa-file').value = '';
  document.getElementById('ig-ipa-preview').style.display = 'none';
}

async function igLoadIPAModels() {
  const sel = document.getElementById('ig-ipa-model');
  if (!sel) return;
  sel.innerHTML = '<option value="">⏳ Loading…</option>';
  try {
    const d = await fetch('/api/ai/imagegen/ipadapter-models').then(r => r.json());
    sel.innerHTML = '<option value="">— auto —</option>';
    if (d.models && d.models.length) {
      for (const m of d.models) {
        const name = m.length > 44 ? '…' + m.slice(-41) : m;
        sel.innerHTML += `<option value="${m}" title="${m}">${name}</option>`;
      }
    }
  } catch(e) {
    sel.innerHTML = '<option value="">— unavailable —</option>';
  }
}

// ── Send to Img2Img ────────────────────────────────────────────────────────────

async function igSendToImg2Img(url) {
  try {
    const blob = await fetch(url).then(r => r.blob());
    const dataUrl = await new Promise(resolve => {
      const reader = new FileReader();
      reader.onload = e => resolve(e.target.result);
      reader.readAsDataURL(blob);
    });
    _igInitImageB64 = dataUrl;
    document.getElementById('ig-init-img').src = dataUrl;
    document.getElementById('ig-i2i-preview').style.display = 'block';
    const section = document.getElementById('ig-i2i-section');
    if (section) { section.open = true; section.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }
  } catch(e) {
    alert('Could not load image for Img2Img: ' + e.message);
  }
}

// ── Prompt Presets ─────────────────────────────────────────────────────────────
// GM-editable, per-world, server-side (see /api/ai/prompt-presets?scope=image)
// — previously localStorage-only, so presets vanished on a different browser
// while every other saved thing in this app (starred images, audio jobs,
// model config) lives server-side; also shares its backing table with
// Chat's Quick Prompts (scope="chat").

let _igPresets = [];

async function igRenderPresets() {
  const sel = document.getElementById('ig-preset-sel');
  if (!sel) return;
  try {
    const d = await fetch('/api/ai/prompt-presets?scope=image').then(r => r.json());
    _igPresets = d.presets || [];
  } catch (e) {
    _igPresets = [];
  }
  const cur = sel.value;
  sel.innerHTML = '<option value="">— Saved presets —</option>' +
    _igPresets.map((p) => `<option value="${p.id}">${p.label}</option>`).join('');
  if (cur !== '' && sel.querySelector(`option[value="${cur}"]`)) sel.value = cur;
}

async function igSavePreset() {
  const prompt = document.getElementById('ig-prompt')?.value.trim();
  if (!prompt) { alert('Enter a prompt first.'); return; }
  const name = window.prompt('Preset name:', '');
  if (!name || !name.trim()) return;
  try {
    const r = await fetch('/api/ai/prompt-presets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope: 'image', label: name.trim(), text: prompt,
        negative: document.getElementById('ig-negative')?.value || '',
      }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const saved = await r.json();
    await igRenderPresets();
    const sel = document.getElementById('ig-preset-sel');
    if (sel) sel.value = saved.id;
  } catch (e) {
    alert('Could not save preset: ' + e.message);
  }
}

function igLoadPreset() {
  const sel = document.getElementById('ig-preset-sel');
  const id = parseInt(sel?.value);
  if (isNaN(id)) return;
  const p = _igPresets.find((x) => x.id === id);
  if (!p) return;
  const promptEl = document.getElementById('ig-prompt');
  const negEl = document.getElementById('ig-negative');
  if (promptEl) { promptEl.value = p.text; autoResize(promptEl); }
  if (negEl && p.negative) { negEl.value = p.negative; autoResize(negEl); }
}

async function igDeletePreset() {
  const sel = document.getElementById('ig-preset-sel');
  const id = parseInt(sel?.value);
  if (isNaN(id)) return;
  await fetch('/api/ai/prompt-presets/' + id, { method: 'DELETE' });
  await igRenderPresets();
}

// ── Upscaler dynamic loading ───────────────────────────────────────────────────

async function igLoadUpscalers() {
  const sel = document.getElementById('ig-upscaler');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">⏳ Loading…</option>';
  try {
    const d = await fetch('/api/ai/imagegen/upscalers').then(r => r.json());
    sel.innerHTML = '<option value="">— none (output at gen size) —</option>';
    if (d.upscalers && d.upscalers.length) {
      for (const u of d.upscalers) {
        const name = u.length > 44 ? '…' + u.slice(-41) : u;
        sel.innerHTML += `<option value="${u}"${u === cur ? ' selected' : ''} title="${u}">${name}</option>`;
      }
    }
  } catch(e) {
    sel.innerHTML = '<option value="">— unavailable —</option>';
  }
}

// ── Image generation history (localStorage) ────────────────────────────────────

const _HIST_KEY = 'nd_imagegen_history';
const _HIST_MAX = 50;

// A generated image is saved full-resolution (often several MB — SwarmUI/
// ComfyUI output) but app.imaging.make_thumbnail also writes a small WebP
// preview alongside it at generation time (see app/ai.py's imagegen_generate)
// under a predictable name. Grids here load that instead — an <img>'s own
// onerror fallback to the full image covers the one case a thumbnail might
// not exist: an entry saved to localStorage/starred before this feature
// shipped, since there's no per-request way to check the file exists from
// plain JS the way the server-side thumb_url() Jinja filter can.
function ndThumbSrc(url) {
  const i = url.lastIndexOf('.');
  return i === -1 ? url : url.slice(0, i) + '_thumb.webp';
}
function ndThumbFallback(img) {
  img.onerror = null;
  img.src = img.dataset.full;
}

function igSaveToHistory(urls, params) {
  if (!urls || !urls.length) return;
  try {
    let hist = JSON.parse(localStorage.getItem(_HIST_KEY) || '[]');
    hist.unshift({urls, params, ts: Date.now()});
    if (hist.length > _HIST_MAX) hist = hist.slice(0, _HIST_MAX);
    localStorage.setItem(_HIST_KEY, JSON.stringify(hist));
    igLoadHistory();
  } catch(e) {}
}

function igLoadHistory() {
  try {
    const hist = JSON.parse(localStorage.getItem(_HIST_KEY) || '[]');
    const section = document.getElementById('ig-history-section');
    const grid    = document.getElementById('ig-history-grid');
    if (!section || !grid) return;
    if (!hist.length) { section.style.display = 'none'; return; }
    section.style.display = 'block';
    grid.innerHTML = hist.map((entry, i) => {
      const url  = entry.urls[0];
      const p    = entry.params || {};
      const date = new Date(entry.ts).toLocaleDateString();
      const time = new Date(entry.ts).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
      const more = entry.urls.length > 1 ? ` +${entry.urls.length - 1}` : '';
      const shortPrompt = (p.prompt || '').slice(0, 45);
      return `
        <div class="ig-result-card">
          <img src="${ndThumbSrc(url)}" data-full="${url}" alt="Generated" loading="lazy" style="cursor:pointer"
               onclick="igHistViewAll(${i})" onerror="ndThumbFallback(this)">
          <div class="ig-result-card-actions" style="flex-direction:column;gap:.22rem;padding:.3rem .4rem">
            <div style="font-size:.63rem;color:var(--text-dim)">${date} ${time}${more}</div>
            <div style="font-size:.63rem;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                 title="${(p.prompt||'').replace(/"/g,'&quot;')}">${shortPrompt}${shortPrompt.length < (p.prompt||'').length ? '…' : ''}</div>
            <div style="display:flex;gap:.25rem;flex-wrap:wrap">
              <button onclick="igReuseParams(${i})"
                      style="font-size:.68rem;background:transparent;border:1px solid var(--neon);color:var(--neon);border-radius:3px;padding:.12rem .38rem;cursor:pointer;font-family:inherit">↩ Reuse</button>
              <a href="${url}" download
                 style="font-size:.68rem;background:transparent;border:1px solid var(--border);color:var(--text-dim);border-radius:3px;padding:.12rem .38rem;text-decoration:none">⬇</a>
              <button onclick="igDelHistEntry(${i})"
                      style="font-size:.68rem;background:transparent;border:none;color:#555;cursor:pointer;font-family:inherit;padding:.12rem .2rem">✕</button>
            </div>
          </div>
        </div>`;
    }).join('');
  } catch(e) {}
}

function igHistViewAll(idx) {
  try {
    const hist  = JSON.parse(localStorage.getItem(_HIST_KEY) || '[]');
    const entry = hist[idx];
    if (!entry) return;
    const results = document.getElementById('ig-results');
    const grid    = document.getElementById('ig-results-grid');
    grid.innerHTML = '';
    results.style.display = 'block';
    entry.urls.forEach((url, i) => {
      const card = document.createElement('div');
      card.className = 'ig-result-card';
      const img = document.createElement('img');
      img.src = ndThumbSrc(url); img.dataset.full = url; img.alt = `Generated image ${i + 1}`;
      img.onerror = () => ndThumbFallback(img);
      const actions = document.createElement('div');
      actions.className = 'ig-result-card-actions';
      const dlLink = document.createElement('a');
      dlLink.href = url; dlLink.download = `nd-image-${i + 1}.png`; dlLink.textContent = '⬇ Save';
      const attachBtn = document.createElement('button');
      attachBtn.textContent = '📎 Attach…';
      attachBtn.onclick = () => igAttachToEntity(url);
      actions.appendChild(dlLink);
      actions.appendChild(attachBtn);
      card.appendChild(img); card.appendChild(actions);
      grid.appendChild(card);
    });
    results.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch(e) {}
}

function igReuseParams(idx) {
  try {
    const hist  = JSON.parse(localStorage.getItem(_HIST_KEY) || '[]');
    const entry = hist[idx];
    if (!entry || !entry.params) return;
    const p = entry.params;
    const set = (id, val) => { const el = document.getElementById(id); if (el && val !== undefined && val !== null) el.value = val; };
    set('ig-prompt',     p.prompt);
    set('ig-negative',   p.negative);
    set('ig-model',      p.model);
    set('ig-width',      p.width);
    set('ig-height',     p.height);
    set('ig-steps',      p.steps);
    set('ig-cfg',        p.cfg);
    set('ig-seed',       p.seed);
    set('ig-sampler',    p.sampler);
    set('ig-scheduler',  p.scheduler);
    if (p.loras) {
      const names = p.loras.split(',').map(s => s.trim()).filter(Boolean);
      const wts   = (p.lora_weights || '').split(',').map(s => s.trim());
      _igLoras = names.map((n, i) => ({name: n, weight: wts[i] || '0.8'}));
    } else {
      _igLoras = [{name: '', weight: '0.8'}];
    }
    igRenderLoras();
    set('ig-vae',        p.vae || '');
    set('ig-clipskip',   p.clip_skip || '-1');
    set('ig-upscaler',   p.upscale_model || '');
    if (p.upscale_factor) {
      _igUpscaleFactor = p.upscale_factor;
      document.querySelectorAll('.ig-batch-btn[data-scale]').forEach(b => {
        b.classList.toggle('ig-batch-active', parseFloat(b.dataset.scale) === p.upscale_factor);
      });
    }
    const stepsVal = document.getElementById('ig-steps-val');
    if (stepsVal && p.steps) stepsVal.textContent = p.steps;
    const cfgVal = document.getElementById('ig-cfg-val');
    if (cfgVal && p.cfg) cfgVal.textContent = p.cfg;
    document.querySelector('.ig-main')?.scrollTo({top: 0, behavior: 'smooth'});
  } catch(e) { alert('Could not load parameters: ' + e.message); }
}

function igDelHistEntry(idx) {
  try {
    let hist = JSON.parse(localStorage.getItem(_HIST_KEY) || '[]');
    hist.splice(idx, 1);
    localStorage.setItem(_HIST_KEY, JSON.stringify(hist));
    igLoadHistory();
  } catch(e) {}
}

function igClearHistory() {
  if (!confirm('Clear all generated image history?')) return;
  localStorage.removeItem(_HIST_KEY);
  igLoadHistory();
}

// ── Tag sources manager ───────────────────────────────────────────────────────

async function igLoadSourcesPanel() {
  const list = document.getElementById('tag-sources-list');
  const summary = document.getElementById('tag-ac-summary');
  try {
    const d = await fetch('/api/ai/imagegen/tags/sources').then(r => r.json());
    const sources = d.sources || [];
    const activeItem = sources.find(s => s.active);
    summary.textContent = activeItem
      ? `${activeItem.label} · ${activeItem.count.toLocaleString()} tags`
      : 'no source active';
    list.innerHTML = '';
    for (const s of sources) {
      const row = document.createElement('div');
      row.className = 'tag-source-row' + (s.active ? ' active-source' : '');
      const info = document.createElement('div');
      info.className = 'tag-source-info';
      info.innerHTML = `<div class="tag-source-label">${s.label}</div><div class="tag-source-desc">${s.description}</div>`;
      const badge = document.createElement('span');
      badge.className = 'tag-source-badge ' + (s.active ? 'active' : s.downloaded ? 'dl' : 'missing');
      badge.textContent = s.active ? '● Active' : s.downloaded ? `✓ ${(s.count/1000).toFixed(0)}k tags` : 'Not downloaded';
      const acts = document.createElement('div');
      acts.style.cssText = 'display:flex;gap:.25rem;flex-shrink:0';
      if (!s.downloaded) {
        const dlBtn = document.createElement('button');
        dlBtn.className = 'ig-btn';
        dlBtn.style.cssText = 'border-color:var(--neon);color:var(--neon);font-size:.7rem;padding:.15rem .4rem';
        dlBtn.textContent = '⬇';
        dlBtn.title = 'Download';
        dlBtn.onclick = () => igDownloadSource(s.id, dlBtn);
        acts.appendChild(dlBtn);
      } else {
        if (!s.active) {
          const actBtn = document.createElement('button');
          actBtn.className = 'ig-btn';
          actBtn.style.cssText = 'font-size:.7rem;padding:.15rem .4rem';
          actBtn.textContent = '✓ Use';
          actBtn.title = 'Set as active';
          actBtn.onclick = () => igActivateSource(s.id);
          acts.appendChild(actBtn);
        }
        const delBtn = document.createElement('button');
        delBtn.className = 'ig-btn';
        delBtn.style.cssText = 'font-size:.7rem;padding:.15rem .35rem;color:#888;border-color:#444';
        delBtn.textContent = '✕';
        delBtn.title = 'Delete downloaded file';
        delBtn.onclick = () => igDeleteSource(s.id);
        acts.appendChild(delBtn);
      }
      row.appendChild(info);
      row.appendChild(badge);
      row.appendChild(acts);
      list.appendChild(row);
    }
  } catch(e) {
    if (summary) summary.textContent = 'unavailable';
    if (list) list.innerHTML = '<div style="font-size:.75rem;color:#c44">Could not load tag sources</div>';
  }
}

async function igDownloadSource(sourceId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    const r = await fetch('/api/ai/imagegen/tags/fetch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId}),
    }).then(r => r.json());
    if (!r.ok) throw new Error(r.error || 'Download failed');
    await igLoadSourcesPanel();
  } catch(e) {
    alert('Download failed: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = '⬇'; }
  }
}

async function igActivateSource(sourceId) {
  try {
    await fetch('/api/ai/imagegen/tags/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId}),
    });
    await igLoadSourcesPanel();
  } catch(e) { alert('Failed to activate: ' + e.message); }
}

async function igDeleteSource(sourceId) {
  if (!confirm(`Delete the downloaded "${sourceId}" tag file?`)) return;
  try {
    await fetch('/api/ai/imagegen/tags/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId}),
    });
    await igLoadSourcesPanel();
  } catch(e) { alert('Failed to delete: ' + e.message); }
}

async function igAddCustomSource() {
  const id  = document.getElementById('tag-custom-id').value.trim();
  const url = document.getElementById('tag-custom-url').value.trim();
  if (!id || !url) { alert('Enter both an ID and URL.'); return; }
  const btn = document.querySelector('#ig-tag-sources-section .ig-btn:last-child');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Downloading…'; }
  try {
    const r = await fetch('/api/ai/imagegen/tags/fetch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: id, url, label: id}),
    }).then(r => r.json());
    if (!r.ok) throw new Error(r.error || 'Download failed');
    document.getElementById('tag-custom-id').value = '';
    document.getElementById('tag-custom-url').value = '';
    await igLoadSourcesPanel();
  } catch(e) {
    alert('Download failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⬇ Download'; }
  }
}

// Per-textarea autocomplete state
const _acState = {};

function _acInit(taId, dropId) {
  const ta   = document.getElementById(taId);
  const drop = document.getElementById(dropId);
  let _debounce = null;
  let _selIdx   = -1;
  let _items    = [];

  function _currentWord() {
    const val = ta.value;
    const pos = ta.selectionStart;
    // Find the start of the current tag (after last comma/newline)
    let start = pos - 1;
    while (start >= 0 && val[start] !== ',' && val[start] !== '\n') start--;
    start++;
    return val.slice(start, pos).trim().replace(/\s+/g, '_');
  }

  function _insertTag(tag) {
    const val = ta.value;
    const pos = ta.selectionStart;
    let start = pos - 1;
    while (start >= 0 && val[start] !== ',' && val[start] !== '\n') start--;
    start++;
    // Strip leading whitespace in the current token
    while (start < pos && (val[start] === ' ' || val[start] === '\t')) start++;
    const before = val.slice(0, start);
    const after  = val.slice(pos);
    const sep    = (after.trimStart().startsWith(',') || after === '') ? '' : ', ';
    ta.value = before + tag + sep + after;
    const newPos = (before + tag + sep).length;
    ta.setSelectionRange(newPos, newPos);
    _hideDrop();
    ta.focus();
  }

  function _renderItems(tags) {
    _items = tags;
    _selIdx = -1;
    if (!tags.length) { _hideDrop(); return; }
    const q = _currentWord();
    drop.innerHTML = tags.map((t, i) =>
      `<div class="tag-ac-item" data-i="${i}" onmousedown="event.preventDefault()" onclick="_acClick('${taId}',${i})">
        <span class="tag-ac-cat" style="background:${t.color}"></span>
        <span class="tag-ac-name">${_acHighlight(t.tag, q)}</span>
        <span class="tag-ac-count">${_fmtCount(t.count)}</span>
      </div>`
    ).join('');
    drop.style.display = '';
    _acState[taId] = { items: tags, selIdx: -1, insert: _insertTag };
  }

  function _hideDrop() {
    drop.style.display = 'none';
    drop.innerHTML = '';
    _items = [];
    _selIdx = -1;
    if (_acState[taId]) _acState[taId].selIdx = -1;
  }

  ta.addEventListener('input', function() {
    clearTimeout(_debounce);
    const word = _currentWord();
    if (word.length < 2) { _hideDrop(); return; }
    _debounce = setTimeout(async () => {
      try {
        const r = await fetch(`/api/ai/imagegen/tags?q=${encodeURIComponent(word)}&limit=20`).then(r => r.json());
        _renderItems(r.tags || []);
      } catch(e) { _hideDrop(); }
    }, 80);
  });

  ta.addEventListener('keydown', function(e) {
    if (drop.style.display === 'none') return;
    const items = _acState[taId]?.items || [];
    if (!items.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _selIdx = Math.min(_selIdx + 1, items.length - 1);
      _updateSel(drop, _selIdx);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _selIdx = Math.max(_selIdx - 1, -1);
      _updateSel(drop, _selIdx);
    } else if ((e.key === 'Enter' || e.key === 'Tab') && _selIdx >= 0) {
      e.preventDefault();
      _insertTag(items[_selIdx].tag);
    } else if (e.key === 'Escape') {
      _hideDrop();
    }
    if (_acState[taId]) _acState[taId].selIdx = _selIdx;
  });

  ta.addEventListener('blur', function() {
    setTimeout(_hideDrop, 150);
  });

  _acState[taId] = { items: [], selIdx: -1, insert: _insertTag };
}

function _acClick(taId, idx) {
  const state = _acState[taId];
  if (state && state.items[idx]) state.insert(state.items[idx].tag);
}

function _updateSel(drop, idx) {
  drop.querySelectorAll('.tag-ac-item').forEach((el, i) => {
    el.classList.toggle('tag-ac-sel', i === idx);
    if (i === idx) el.scrollIntoView({block:'nearest'});
  });
}

function _acHighlight(tag, q) {
  if (!q) return tag;
  const i = tag.indexOf(q);
  if (i < 0) return tag;
  return tag.slice(0, i) + '<em>' + tag.slice(i, i + q.length) + '</em>' + tag.slice(i + q.length);
}

function _fmtCount(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000)    return (n / 1000).toFixed(0) + 'k';
  return String(n);
}

// Initialise autocomplete on both prompt areas
_acInit('ig-prompt',   'tag-ac-pos');
_acInit('ig-negative', 'tag-ac-neg');

// ── Star / unstar images ──────────────────────────────────────────────────────

async function igToggleStar(url, params, btn) {
  if (_igStarred.has(url)) {
    // Unstar
    try {
      await fetch('/api/ai/imagegen/unstar', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url}),
      });
      _igStarred.delete(url);
      btn.textContent = '☆'; btn.classList.remove('starred'); btn.title = 'Star this image';
    } catch(e) { alert('Failed to unstar: ' + e.message); }
  } else {
    // Star
    try {
      await fetch('/api/ai/imagegen/star', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url,
          prompt: params.prompt || '',
          negative: params.negative || '',
          model: params.model || '',
          seed: params.seed || -1,
          params: {width: params.width, height: params.height, steps: params.steps,
                   cfg: params.cfg, sampler: params.sampler},
        }),
      });
      _igStarred.add(url);
      btn.textContent = '⭐'; btn.classList.add('starred'); btn.title = 'Unstar';
    } catch(e) { alert('Failed to star: ' + e.message); }
  }
}

async function igLoadStarred() {
  const grid = document.getElementById('starred-grid');
  if (!grid) return;
  try {
    const d = await fetch('/api/ai/imagegen/starred').then(r => r.json());
    _igStarred = new Set((d.images || []).map(i => i.url));
    if (!d.images || !d.images.length) {
      grid.innerHTML = '<div style="color:var(--text-dim);font-size:.88rem;grid-column:1/-1">No starred images yet. Star images from the 🎨 Image Gen tab.</div>';
      return;
    }
    grid.innerHTML = '';
    for (const img of d.images) {
      const card = document.createElement('div');
      card.className = 'starred-card';
      const imgEl = document.createElement('img');
      imgEl.src = ndThumbSrc(img.url); imgEl.dataset.full = img.url; imgEl.alt = 'Starred image';
      imgEl.loading = 'lazy';
      imgEl.onerror = () => ndThumbFallback(imgEl);
      imgEl.onclick = () => window.open(img.url, '_blank');
      const info = document.createElement('div');
      info.className = 'starred-card-info';
      const shortPrompt = (img.prompt || '').slice(0, 80);
      info.innerHTML = `<div class="prompt" title="${(img.prompt||'').replace(/"/g,'&quot;')}">${shortPrompt || '(no prompt)'}</div>
        <div>${img.model ? img.model.split('/').pop() : ''} · seed ${img.seed >= 0 ? img.seed : 'random'}</div>`;
      const acts = document.createElement('div');
      acts.className = 'starred-card-actions';
      const dl = document.createElement('a');
      dl.href = img.url; dl.download = 'starred.png'; dl.textContent = '⬇ Save';
      const reuseBtn = document.createElement('button');
      reuseBtn.textContent = '↩ Reuse'; reuseBtn.title = 'Load params into Image Gen';
      reuseBtn.onclick = () => {
        switchTab('image');
        const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
        set('ig-prompt', img.prompt); set('ig-negative', img.negative);
        set('ig-model', img.model); set('ig-seed', img.seed);
        if (img.params) { set('ig-width', img.params.width); set('ig-height', img.params.height);
          set('ig-steps', img.params.steps); set('ig-cfg', img.params.cfg);
          set('ig-sampler', img.params.sampler); }
      };
      const unstarBtn = document.createElement('button');
      unstarBtn.textContent = '✕ Unstar'; unstarBtn.style.color = '#c44'; unstarBtn.style.borderColor = '#c44';
      unstarBtn.onclick = async () => {
        await fetch('/api/ai/imagegen/unstar', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url: img.url})});
        card.remove();
        _igStarred.delete(img.url);
        if (!grid.children.length) igLoadStarred();
      };
      acts.appendChild(dl); acts.appendChild(reuseBtn); acts.appendChild(unstarBtn);
      card.appendChild(imgEl); card.appendChild(info); card.appendChild(acts);
      grid.appendChild(card);
    }
  } catch(e) {
    if (grid) grid.innerHTML = '<div style="color:#c44">Failed to load starred images</div>';
  }
}

async function igClearAllStarred() {
  const d = await fetch('/api/ai/imagegen/starred').then(r => r.json()).catch(() => ({images:[]}));
  if (!d.images || !d.images.length) return;
  if (!confirm(`Unstar all ${d.images.length} images?`)) return;
  await Promise.all(d.images.map(i =>
    fetch('/api/ai/imagegen/unstar', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url: i.url})})
  ));
  _igStarred.clear();
  igLoadStarred();
}

// ── Attach generated image to an entity ──────────────────────────────────────

// Shared entity-picker overlay — a small modal (search + folder tree via
// static/js/entity-picker.js) over GET /api/entities/picker, used by both
// "Attach…" below and "🎭 Pick entity…" above. Simpler than a prompt()-based
// name search (the previous implementation here, which also called a
// backend route — /api/entities/search — that never actually existed) and
// consistent with the Session NPC picker's tree+search UX.
function igOpenEntityPicker(onPick, title) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:flex;align-items:center;justify-content:center;padding:1rem';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:1rem;max-width:420px;width:100%;max-height:80vh;display:flex;flex-direction:column;gap:.5rem';

  const heading = document.createElement('div');
  heading.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:.5rem';
  const headingLabel = document.createElement('span');
  headingLabel.style.cssText = 'color:var(--neon);font-size:.9rem;font-weight:700';
  headingLabel.textContent = title || 'Pick an entity';
  const closeBtn = document.createElement('button');
  closeBtn.className = 'ig-btn';
  closeBtn.style.cssText = 'padding:.1rem .5rem;font-size:.8rem';
  closeBtn.textContent = '✕';
  closeBtn.onclick = () => overlay.remove();
  heading.append(headingLabel, closeBtn);

  const search = document.createElement('input');
  search.type = 'text';
  search.placeholder = 'Search entities…';
  search.style.cssText = 'width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:.4rem .6rem;font-family:var(--font);font-size:.82rem;border-radius:3px;box-sizing:border-box';

  const tree = document.createElement('div');
  tree.style.cssText = 'overflow-y:auto;flex:1;min-height:120px;background:var(--bg3);border:1px solid var(--border);border-radius:3px;padding:.5rem';
  tree.innerHTML = '<div style="color:var(--text-dim);font-size:.8rem">⏳ Loading…</div>';

  box.append(heading, search, tree);
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  fetch('/api/entities/picker').then(r => r.json()).then((d) => {
    ndEntityPicker(tree, search, {
      entities: d.entities || [], mode: 'single',
      emptyText: 'No entities in this world yet.',
      onPick: (entity) => { overlay.remove(); onPick(entity); },
    });
  }).catch(() => {
    tree.innerHTML = '<div style="color:#c44;font-size:.8rem">✗ Could not load entities</div>';
  });
}

async function igAttachToEntity(url) {
  const imgUrl = url || (_igLastUrls[0] || '');
  if (!imgUrl) return;
  igOpenEntityPicker(async (entity) => {
    try {
      const r = await fetch(`/api/entity/${entity.id}/image`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_url: imgUrl }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      alert(`✓ Image attached to "${entity.name}"`);
    } catch (e) {
      alert('Could not attach image: ' + e.message);
    }
  }, 'Attach image to entity');
}

// ── Illustrate an Entity ─────────────────────────────────────────────────────
// Picks an entity, asks the chat model to turn its own write-up (summary,
// body, custom fields — the structured "appearance" fields a template adds,
// like eyes/hair/height, nobody currently feeds an image model) into a
// prompt, then remembers which entity this generation is "for" so a result
// card can offer a direct "Set as portrait" action afterward.
let _igPortraitEntityId = null;
let _igPortraitEntityName = '';

function igPickEntityForPortrait() {
  igOpenEntityPicker(async (entity) => {
    const label = document.getElementById('ig-entity-picked-label');
    label.textContent = '✨ Building prompt for ' + entity.name + '…';
    try {
      const preview = await fetch('/api/entity/' + entity.id + '/preview').then(r => r.json());
      const fields = [];
      if (preview.subtype) fields.push('Subtype: ' + preview.subtype);
      if (preview.summary) fields.push('Summary: ' + preview.summary);
      if (preview.custom_fields_json) {
        try {
          const cf = JSON.parse(preview.custom_fields_json);
          for (const [k, v] of Object.entries(cf)) {
            if (v !== null && v !== undefined && String(v).trim()) fields.push(k + ': ' + v);
          }
        } catch (e) { /* malformed custom_fields_json — skip, not fatal */ }
      }
      if (preview.body) fields.push('Description:\n' + preview.body);
      const sourceText = `Name: ${preview.name}\nKind: ${preview.kind}\n` + fields.join('\n');

      const system = 'You are an image prompt writer for a cyberpunk-fantasy TTRPG. Given a world entity\'s write-up, write ONE detailed image generation prompt depicting it — comma-separated tags and descriptive phrases, about 40-70 words. Output only the prompt, no explanation, no preamble, no quotes.';
      const promptRes = await fetch('/api/ai/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: [{ role: 'user', content: sourceText.slice(0, 6000) }], system, model: activeModel, surface: 'image' }),
      });
      if (!promptRes.ok) throw new Error(`Server error ${promptRes.status}`);
      const promptData = await promptRes.json();
      const imgPrompt = (promptData.result || '').trim();
      if (!imgPrompt || imgPrompt.startsWith('[AI ')) throw new Error(imgPrompt || 'Could not write a prompt');

      document.getElementById('ig-prompt').value = imgPrompt;
      _igPortraitEntityId = entity.id;
      _igPortraitEntityName = entity.name;
      label.textContent = '🎭 Illustrating: ' + entity.name;
    } catch (e) {
      label.textContent = '✗ Failed: ' + (e.message || e);
    }
  }, 'Illustrate which entity?');
}

async function igSetAsPortrait(url, entityId, entityName, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '⏳';
  try {
    const r = await fetch(`/api/entity/${entityId}/image`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_url: url }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
    btn.textContent = '✓ Set as portrait';
  } catch (e) {
    alert('Could not set portrait: ' + e.message);
    btn.textContent = orig;
    btn.disabled = false;
  }
}
