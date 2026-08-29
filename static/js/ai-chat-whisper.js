// ── Whisper model download ──────────────────────────────────────────────────

function wpRenderModelList(models) {
  const list = document.getElementById('wp-model-list');
  if (!list) return;
  list.innerHTML = (models || []).map(m => {
    const status = m.active ? '🟢 active' : (m.downloaded ? '✓ downloaded' : '○ not downloaded');
    const btnLabel = m.downloaded ? '↺ Re-download' : '⬇ Download';
    const activateBtn = (m.downloaded && !m.active)
      ? `<button class="ig-btn wp-model-act-btn" data-filename="${m.filename}" onclick="wpActivate(this.dataset.filename)"
                 style="padding:.25rem .6rem;font-size:.72rem;white-space:nowrap;border-color:var(--neon);color:var(--neon)">★ Make active</button>`
      : '';
    return `<div style="display:flex;align-items:center;justify-content:space-between;gap:.6rem;padding:.35rem .55rem;background:var(--bg3);border-radius:4px;font-size:.8rem">
      <div><strong>${m.label}</strong><span style="color:var(--text-dim);margin-left:.5rem">${m.size}</span></div>
      <div style="display:flex;align-items:center;gap:.6rem">
        <span style="color:var(--text-dim);white-space:nowrap;font-size:.75rem">${status}</span>
        ${activateBtn}
        <button class="ig-btn wp-model-dl-btn" data-filename="${m.filename}" onclick="wpDownload(this.dataset.filename)"
                style="padding:.25rem .6rem;font-size:.72rem;white-space:nowrap">${btnLabel}</button>
      </div>
    </div>`;
  }).join('');
}

async function wpLoadStatus() {
  const el = document.getElementById('wp-status');
  if (!el) return;
  try {
    const r = await fetch('/api/ai/whisper/model-status');
    const d = await r.json();
    const sourceHint = d.active_source === 'env' ? ' — no model explicitly activated yet, this is just the fallback' : '';
    el.textContent = d.downloaded
      ? `Active: ${d.filename} (${(d.bytes / 1e9).toFixed(2)} GB)${sourceHint}`
      : `Active model file not downloaded yet: ${d.filename}${sourceHint}`;
    wpRenderModelList(d.models);
  } catch (e) {
    el.textContent = '✗ Could not check status';
  }
}

let _wpActivating = false;
async function wpActivate(filename) {
  if (_wpActivating) return;
  _wpActivating = true;
  document.querySelectorAll('.wp-model-act-btn, .wp-model-dl-btn').forEach(b => b.disabled = true);
  const lbl = document.getElementById('wp-progress-label');
  const prog = document.getElementById('wp-progress');
  if (prog) prog.style.display = 'block';
  if (lbl) lbl.textContent = `Activating ${filename}…`;
  try {
    const r = await fetch('/api/ai/whisper/activate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, hot_swap: true }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    if (lbl) {
      lbl.textContent = d.hot_swapped
        ? `✓ ${d.detail}`
        : `⚠ ${d.detail}`;
    }
  } catch (e) {
    if (lbl) lbl.textContent = '✗ Could not activate: ' + e.message;
  } finally {
    document.querySelectorAll('.wp-model-act-btn, .wp-model-dl-btn').forEach(b => b.disabled = false);
    _wpActivating = false;
    wpLoadStatus();
  }
}

let _wpDownloading = false;
async function wpDownload(filename) {
  if (_wpDownloading) return;
  _wpDownloading = true;
  document.querySelectorAll('.wp-model-dl-btn').forEach(b => b.disabled = true);
  const prog = document.getElementById('wp-progress');
  const bar = document.getElementById('wp-progress-bar');
  const lbl = document.getElementById('wp-progress-label');
  const urlInput = document.getElementById('wp-custom-url');
  const customUrl = filename ? '' : (urlInput?.value || '').trim();
  prog.style.display = 'block';
  bar.style.width = '0%';
  lbl.textContent = 'Starting download…';
  try {
    const res = await fetch('/api/ai/whisper/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: customUrl, filename: filename || '' }),
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
            lbl.textContent = `Downloading… ${pct}% (${(obj.completed / 1e9).toFixed(2)} / ${(obj.total / 1e9).toFixed(2)} GB)`;
          } else if (obj.status === 'done') {
            bar.style.width = '100%';
            lbl.textContent = `✓ Downloaded ${obj.filename} — click ★ Make active below to switch to it`;
          }
        } catch (pe) {
          if (pe.message && !pe.message.includes('JSON')) throw pe;
        }
      }
    }
  } catch (e) {
    lbl.textContent = '✗ Failed: ' + e.message;
  } finally {
    document.querySelectorAll('.wp-model-dl-btn').forEach(b => b.disabled = false);
    _wpDownloading = false;
    wpLoadStatus();
  }
}

// ── Whisper test tab ─────────────────────────────────────────────────────────

async function wtLoadStatus() {
  const el = document.getElementById('wt-status');
  if (!el) return;
  el.textContent = '⏳ Checking…';
  try {
    const [debugRes, modelRes] = await Promise.all([
      fetch('/api/ai/debug').then(r => r.json()),
      fetch('/api/ai/whisper/model-status').then(r => r.json()),
    ]);
    const w = debugRes.whisper || {};
    const lines = [
      w.url ? `Server: ${w.url}` : 'Server: not configured — set WHISPER_URL or the Whisper URL field in Settings',
      w.url ? `Reachable: ${w.ok ? '✓ yes' : '✗ no' + (w.reason ? ' — ' + w.reason : '')}` : null,
      modelRes.downloaded
        ? `Model: ✓ ${modelRes.filename} (${(modelRes.bytes / 1e9).toFixed(2)} GB)`
        : `Model: ○ not downloaded yet (${modelRes.filename}) — see the 🤖 Models tab`,
    ].filter(l => l !== null);
    el.textContent = lines.join('\n');
  } catch (e) {
    el.textContent = '✗ Could not check status: ' + e.message;
  }
}

async function wlLoadLanguage() {
  const sel = document.getElementById('wl-select');
  if (!sel) return;
  try {
    const d = await fetch('/api/ai/whisper/language').then(r => r.json());
    sel.value = d.language || '';
  } catch (e) { /* leave at Auto-detect — save will still work */ }
}

async function wlSaveLanguage() {
  const btn = document.getElementById('wl-save-btn');
  const status = document.getElementById('wl-status');
  const sel = document.getElementById('wl-select');
  btn.disabled = true;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/whisper/language', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: sel.value }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = '✓ Saved';
    setTimeout(() => { if (status.textContent === '✓ Saved') status.textContent = ''; }, 2000);
  } catch (e) {
    status.textContent = '✗ Could not save: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function wdLoadDenoise() {
  const cb = document.getElementById('wd-checkbox');
  if (!cb) return;
  try {
    const d = await fetch('/api/ai/whisper/denoise').then(r => r.json());
    cb.checked = !!d.enabled;
    const unavailable = document.getElementById('wd-unavailable');
    const label = document.getElementById('wd-label');
    if (!d.available) {
      if (unavailable) unavailable.style.display = '';
      cb.disabled = true;
      if (label) label.style.opacity = '0.5';
    } else {
      if (unavailable) unavailable.style.display = 'none';
      cb.disabled = false;
      if (label) label.style.opacity = '1';
    }
  } catch (e) { /* leave unchecked — save will still work (and 400 if unavailable) */ }
}

async function wdSaveDenoise() {
  const cb = document.getElementById('wd-checkbox');
  const status = document.getElementById('wd-status');
  cb.disabled = true;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/whisper/denoise', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: cb.checked }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }
    status.textContent = '✓ Saved';
    setTimeout(() => { if (status.textContent === '✓ Saved') status.textContent = ''; }, 2000);
  } catch (e) {
    cb.checked = !cb.checked;
    status.textContent = '✗ Could not save: ' + e.message;
  } finally {
    cb.disabled = false;
  }
}

async function wgLoadGlossary() {
  const ta = document.getElementById('wg-textarea');
  if (!ta) return;
  try {
    const d = await fetch('/api/ai/whisper/glossary').then(r => r.json());
    ta.value = d.glossary || '';
    const countEl = document.getElementById('wg-entity-count');
    if (countEl) {
      countEl.textContent = d.entity_terms_count
        ? `+ ${d.entity_terms_count} entity name${d.entity_terms_count === 1 ? '' : 's'} from this World`
        : '';
    }
  } catch (e) { /* leave blank — save will still work */ }
}

async function wgSaveGlossary() {
  const btn = document.getElementById('wg-save-btn');
  const status = document.getElementById('wg-status');
  const ta = document.getElementById('wg-textarea');
  btn.disabled = true;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/whisper/glossary', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ glossary: ta.value }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = '✓ Saved';
    setTimeout(() => { if (status.textContent === '✓ Saved') status.textContent = ''; }, 2000);
  } catch (e) {
    status.textContent = '✗ Could not save: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function riLoadInstructions() {
  const ta = document.getElementById('ri-textarea');
  if (!ta) return;
  try {
    const d = await fetch('/api/ai/recap-instructions').then(r => r.json());
    ta.value = d.instructions || '';
  } catch (e) { /* leave blank — save will still work */ }
}

async function riSaveInstructions() {
  const btn = document.getElementById('ri-save-btn');
  const status = document.getElementById('ri-status');
  const ta = document.getElementById('ri-textarea');
  btn.disabled = true;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/recap-instructions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions: ta.value }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = '✓ Saved';
    setTimeout(() => { if (status.textContent === '✓ Saved') status.textContent = ''; }, 2000);
  } catch (e) {
    status.textContent = '✗ Could not save: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function wtTranscribeFile(file) {
  const btn = document.getElementById('wt-transcribe-btn');
  const micBtn = document.getElementById('wt-mic-btn');
  const resultBox = document.getElementById('wt-result');
  const resultText = document.getElementById('wt-result-text');
  const bar = ndProgressBar(document.getElementById('wt-progress'));

  btn.disabled = true;
  micBtn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = '⏳ Transcribing…';
  resultBox.style.display = 'block';
  resultText.textContent = '';
  bar.setPercent(0, 'Uploading… 0%');
  try {
    const data = await ndChunkedUpload(file, {
      directUrl: '/api/ai/attachments/upload',
      chunkUrl: '/api/ai/attachments/upload/chunk',
      completeUrl: '/api/ai/attachments/upload/complete',
      onProgress: (e) => ndProgressFromUpload(bar, e, 'Transcribing via Whisper…'),
    });
    if (data.kind !== 'audio') throw new Error(`Not recognized as audio (got kind=${data.kind})`);
    resultText.textContent = data.text
      ? data.text
      : '(empty transcript — Whisper may be unreachable, still loading the model, or the clip has no detected speech; check Status above)';
  } catch (e) {
    resultText.textContent = '✗ ' + e.message;
  } finally {
    bar.clear();
    btn.disabled = false;
    micBtn.disabled = false;
    btn.textContent = origLabel;
  }
}

function wtTranscribe() {
  const file = document.getElementById('wt-file').files[0];
  if (!file) { alert('Pick an audio file first'); return; }
  wtTranscribeFile(file);
}

let _wtMicRecorder = null;
function wtToggleMic() {
  const micBtn = document.getElementById('wt-mic-btn');
  if (_wtMicRecorder && _wtMicRecorder.isRecording()) { _wtMicRecorder.stop(); return; }
  _wtMicRecorder = ndMicRecorder(
    (blob, mimeType) => {
      micBtn.textContent = '🎤 Record';
      wtTranscribeFile(new File([blob], ndMicFilename(mimeType), { type: mimeType }));
    },
    (err) => {
      micBtn.textContent = '🎤 Record';
      document.getElementById('wt-result').style.display = 'block';
      document.getElementById('wt-result-text').textContent = '✗ Microphone error: ' + err.message;
    },
  );
  _wtMicRecorder.start().then(started => { if (started) micBtn.textContent = '⏹ Stop'; });
}

const wtJobs = ndAudioJobs(document.getElementById('wt-jobs-panel'), {
  createUrl: '/api/ai/attachments/audio-jobs',
  chunkUrl: '/api/ai/attachments/audio-jobs/chunk',
  completeUrl: '/api/ai/attachments/audio-jobs/complete',
  listUrl: '/api/ai/attachments/audio-jobs',
  onUse: (job) => {
    document.getElementById('wt-result').style.display = 'block';
    document.getElementById('wt-result-text').textContent = job.transcript
      || '(empty transcript — Whisper may be unreachable, still loading the model, or the clip has no detected speech; check Status above)';
  },
});

async function wtStartBackgroundJob() {
  const file = document.getElementById('wt-file').files[0];
  if (!file) { alert('Pick an audio file first'); return; }
  const btn = document.getElementById('wt-bgjob-btn');
  btn.disabled = true;
  const bar = ndProgressBar(document.getElementById('wt-progress'));
  bar.setPercent(0, 'Uploading… 0%');
  try {
    await wtJobs.startJob(file, {}, (e) => ndProgressFromUpload(bar, e, 'Job started — processing in background…'));
  } catch (e) {
    alert('Failed to start background job: ' + e.message);
  } finally {
    bar.clear();
    btn.disabled = false;
  }
}

