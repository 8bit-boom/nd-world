// ── Models panel ─────────────────────────────────────────────────────────────

let _mpModels = [];
let _mpPulling = false;

async function mpLoad() {
  const list = document.getElementById('mp-model-list');
  const dot  = document.getElementById('mp-status-dot');
  const txt  = document.getElementById('mp-status-txt');
  list.innerHTML = '<div style="color:var(--text-dim);font-size:.82rem">⏳ Loading…</div>';
  try {
    // Check Ollama reachability
    const dbg = await fetch('/api/ai/debug').then(r => r.json());
    if (dbg.ollama_reachable) {
      dot.style.background = '#4f4';
      dot.title = 'Ollama reachable';
      txt.textContent = 'Connected · ' + dbg.ollama_url;
      txt.style.color = '#4f4';
    } else {
      dot.style.background = '#c44';
      dot.title = 'Ollama unreachable';
      txt.textContent = 'Ollama unreachable — is it running?';
      txt.style.color = '#c44';
    }

    const d = await fetch('/api/ai/models').then(r => r.json());
    _mpModels = d.models || [];
    mpRender(_mpModels, d.available || []);
    mpLoadDefaults(d.defaults || {});
  } catch(e) {
    dot.style.background = '#c44';
    txt.textContent = 'Error: ' + e.message;
    txt.style.color = '#c44';
    list.innerHTML = '<div style="color:#c44;font-size:.82rem">✗ Could not load models</div>';
  }
  mpLoadResident();
}

function _humanBytes(n) {
  if (n === null || n === undefined) return '?';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
}

async function mpLoadResident() {
  const wrap = document.getElementById('mp-resident-list');
  try {
    const d = await fetch('/api/ai/resident').then(r => r.json());
    const models = d.models || [];
    wrap.innerHTML = '';
    if (!models.length) {
      wrap.innerHTML = '<div style="color:var(--text-dim);font-size:.8rem;font-style:italic">Nothing resident — no model is currently loaded into memory.</div>';
      return;
    }
    models.forEach((m) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:.6rem;font-size:.8rem;flex-wrap:wrap';
      const label = document.createElement('span');
      label.style.cssText = 'flex:1;min-width:120px;overflow-wrap:anywhere';
      label.textContent = m.model;
      const size = document.createElement('span');
      size.style.cssText = 'color:var(--text-dim);white-space:nowrap';
      let sizeText = _humanBytes(m.size_vram_bytes) + ' VRAM';
      // A model doesn't have to fit in VRAM entirely — Ollama offloads
      // whatever doesn't fit to system RAM (slower, but still working).
      // Only worth a mention once it's a meaningful amount, not rounding noise.
      if (m.size_ram_bytes && m.size_ram_bytes > 1024 * 1024) {
        sizeText += ' + ' + _humanBytes(m.size_ram_bytes) + ' RAM';
      }
      size.textContent = sizeText;
      size.title = m.expires_at ? ('Unloads automatically at ' + new Date(m.expires_at).toLocaleTimeString()) : '';
      const unloadBtn = document.createElement('button');
      unloadBtn.className = 'ig-btn';
      unloadBtn.style.cssText = 'font-size:.72rem;padding:.15rem .5rem';
      unloadBtn.textContent = '⏏ Unload';
      unloadBtn.title = 'Free this model\'s VRAM and RAM — Ollama evicts it entirely, not just the part on the GPU';
      unloadBtn.onclick = () => mpUnloadModel(m.model, unloadBtn);
      row.append(label, size, unloadBtn);
      wrap.appendChild(row);
    });
  } catch(e) {
    wrap.innerHTML = '<div style="color:#c44;font-size:.8rem">✗ Could not check VRAM residency</div>';
  }
}

async function mpUnloadModel(modelId, btn) {
  btn.disabled = true;
  btn.textContent = '⏳';
  try {
    const r = await fetch('/api/ai/unload', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
    mpLoadResident();
  } catch(e) {
    alert('Unload failed: ' + e.message);
    btn.disabled = false;
    btn.textContent = '⏏ Unload';
  }
}

async function mpLoadDefaults(defaults) {
  const chatSel  = document.getElementById('mp-default-chat');
  const askSel   = document.getElementById('mp-default-ask_ai');
  const imgSel   = document.getElementById('mp-default-image');
  const recapSel = document.getElementById('mp-default-recap');
  const assistSel = document.getElementById('mp-default-assist');

  const llmOptions = _mpModels.map(m => `<option value="${m.id}">${m.label || m.id}</option>`).join('');
  chatSel.innerHTML  = '<option value="">— use system default —</option>' + llmOptions;
  askSel.innerHTML   = '<option value="">— use system default —</option>' + llmOptions;
  recapSel.innerHTML = '<option value="">— use system default —</option>' + llmOptions;
  assistSel.innerHTML = '<option value="">— use system default —</option>' + llmOptions;
  chatSel.value  = defaults.chat || '';
  askSel.value   = defaults.ask_ai || '';
  recapSel.value = defaults.recap || '';
  assistSel.value = defaults.assist || '';

  try {
    const mr = await fetch('/api/ai/imagegen/models').then(r => r.json());
    const imgModels = mr.models || [];
    imgSel.innerHTML = '<option value="">— none set —</option>' +
      imgModels.map(m => `<option value="${m}">${m}</option>`).join('');
    imgSel.value = defaults.image || '';
  } catch (_) {
    imgSel.innerHTML = '<option value="">— image models unavailable —</option>';
  }
}

async function mpSetDefault(surface, modelId) {
  const statusEl = document.getElementById('mp-defaults-status');
  statusEl.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/defaults', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ surface, model_id: modelId }),
    });
    if (!r.ok) throw new Error('Server error ' + r.status);
    statusEl.style.color = '#4f4';
    statusEl.textContent = modelId ? `✓ Default ${surface.replace('_', ' ')} model saved.` : `✓ Cleared — back to system default.`;
  } catch (e) {
    statusEl.style.color = '#c44';
    statusEl.textContent = '✗ Could not save: ' + e.message;
  }
  setTimeout(() => { statusEl.textContent = ''; }, 3000);
}

function mpRender(models, available) {
  const list = document.getElementById('mp-model-list');
  if (!models.length) {
    list.innerHTML = '<div style="color:var(--text-dim);font-size:.82rem">No models configured. Add one below.</div>';
    return;
  }
  list.innerHTML = '';
  for (const m of models) {
    const isActive = m.id === activeModel;
    const card = document.createElement('div');
    card.dataset.modelId = m.id;
    card.style.cssText = 'border:1px solid ' + (isActive ? 'var(--neon)' : 'var(--border)') + ';border-radius:6px;padding:.65rem .9rem;background:var(--bg2);display:flex;flex-direction:column;gap:.45rem';

    // Top row: dot + name
    const topRow = document.createElement('div');
    topRow.style.cssText = 'display:flex;align-items:flex-start;gap:.55rem';

    const sdot = document.createElement('span');
    sdot.id = 'mp-dot-' + btoa(m.id).replace(/[^a-z0-9]/gi,'');
    sdot.style.cssText = 'width:9px;height:9px;border-radius:50%;flex-shrink:0;margin-top:.35rem;background:' + (m.loaded ? '#4f4' : '#444');
    sdot.title = m.loaded ? 'Downloaded (see 🧠 Resident in Memory above for what\'s actually loaded)' : 'Not downloaded';

    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0';
    const nameEl = document.createElement('div');
    nameEl.style.cssText = 'font-size:.88rem;color:' + (isActive ? 'var(--neon)' : 'var(--text)') + ';overflow-wrap:anywhere;font-weight:' + (isActive ? '700' : '400');
    nameEl.textContent = m.label || m.id;
    const idEl = document.createElement('div');
    idEl.style.cssText = 'font-size:.68rem;color:var(--text-dim);overflow-wrap:anywhere;margin-top:.1rem';
    idEl.textContent = m.id;
    info.appendChild(nameEl);
    info.appendChild(idEl);

    topRow.appendChild(sdot);
    topRow.appendChild(info);

    // Actions row
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:.35rem;flex-wrap:wrap';

    if (!isActive) {
      const useBtn = document.createElement('button');
      useBtn.className = 'ig-btn';
      useBtn.textContent = '✓ Use';
      useBtn.title = 'Set as active chat model';
      useBtn.onclick = () => {
        activeModel = m.id;
        const shortId = m.id.split('/').pop().split(':')[0] || m.id;
        document.getElementById('active-model-label').textContent = shortId;
        mpLoad();
        loadModels();
      };
      actions.appendChild(useBtn);
    } else {
      const activeTag = document.createElement('span');
      activeTag.style.cssText = 'font-size:.72rem;color:var(--neon);padding:.22rem .5rem;border:1px solid var(--neon);border-radius:3px;white-space:nowrap';
      activeTag.textContent = '● Active';
      actions.appendChild(activeTag);
    }

    const pullKey = 'mp-pull-' + btoa(m.id).replace(/[^a-z0-9]/gi,'');
    if (!m.loaded) {
      const pullBtn = document.createElement('button');
      pullBtn.className = 'ig-btn';
      pullBtn.id = pullKey;
      pullBtn.style.cssText = 'border-color:var(--neon);color:var(--neon)';
      pullBtn.textContent = '⬇ Download';
      pullBtn.onclick = () => mpPullModel(m.id, card);
      actions.appendChild(pullBtn);
    } else {
      const repullBtn = document.createElement('button');
      repullBtn.className = 'ig-btn';
      repullBtn.id = pullKey;
      repullBtn.title = 'Re-download (fixes corrupted model)';
      repullBtn.textContent = '↺ Re-dl';
      repullBtn.onclick = () => mpPullModel(m.id, card, true);
      actions.appendChild(repullBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'ig-btn';
    delBtn.style.cssText = 'border-color:#555;color:#888';
    delBtn.textContent = '✕';
    delBtn.title = 'Remove model';
    delBtn.onclick = () => mpDeleteModel(m.id, m.loaded, card);
    actions.appendChild(delBtn);

    const benchKey = 'mp-bench-' + btoa(m.id).replace(/[^a-z0-9]/gi,'');
    if (m.loaded) {
      const benchBtn = document.createElement('button');
      benchBtn.className = 'ig-btn';
      benchBtn.id = benchKey;
      benchBtn.textContent = '⚡ Benchmark';
      benchBtn.title = 'Run a short fixed prompt and measure tokens/sec';
      benchBtn.onclick = () => mpBenchmark(m.id, benchKey);
      actions.appendChild(benchBtn);
    }

    // Progress bar (hidden until pulling)
    const progWrap = document.createElement('div');
    progWrap.id = 'mp-prog-' + btoa(m.id).replace(/[^a-z0-9]/gi,'');
    progWrap.style.cssText = 'display:none';
    progWrap.innerHTML = '<div style="font-size:.72rem;color:var(--neon);margin-bottom:.2rem" class="mp-prog-label">Pulling…</div><div style="height:4px;background:var(--bg3);border-radius:2px;overflow:hidden"><div class="mp-prog-bar" style="height:100%;width:0%;background:var(--neon);border-radius:2px;transition:width .2s"></div></div>';

    const benchResult = document.createElement('div');
    benchResult.id = benchKey + '-result';
    benchResult.style.cssText = 'font-size:.72rem;color:var(--text-dim);display:none';

    card.appendChild(topRow);
    card.appendChild(actions);
    card.appendChild(progWrap);
    card.appendChild(benchResult);
    list.appendChild(card);
  }
}

async function mpBenchmark(modelId, benchKey) {
  const btn = document.getElementById(benchKey);
  const resultEl = document.getElementById(benchKey + '-result');
  if (!btn || !resultEl) return;
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = '⏳ Running…';
  resultEl.style.display = 'block';
  resultEl.style.color = 'var(--text-dim)';
  resultEl.textContent = 'Benchmarking…';
  try {
    const r = await fetch('/api/ai/benchmark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelId }),
    });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (e) {}
      throw new Error(detail || `HTTP ${r.status}`);
    }
    const d = await r.json();
    resultEl.style.color = 'var(--neon)';
    resultEl.textContent = `⚡ ${d.tokens_per_sec} tok/s generation (${d.eval_count} tokens in ${(d.eval_duration_ms / 1000).toFixed(1)}s) · ${d.prompt_tokens_per_sec} tok/s prompt · ${(d.load_duration_ms / 1000).toFixed(2)}s load`;
  } catch (e) {
    resultEl.style.color = '#c44';
    resultEl.textContent = '✗ Benchmark failed: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
}

function mpFilter(q) {
  q = q.toLowerCase();
  const items = document.querySelectorAll('#mp-model-list > div[data-model-id]');
  items.forEach(el => {
    const id = (el.dataset.modelId || '').toLowerCase();
    el.style.display = (!q || id.includes(q)) ? '' : 'none';
  });
}

async function mpPullModel(modelId, cardEl, forceDelete = false) {
  if (_mpPulling) { alert('Another download is in progress. Please wait.'); return; }
  _mpPulling = true;

  if (forceDelete) {
    try {
      await fetch('/api/ai/models/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model_id: modelId, delete_from_ollama: true}),
      });
    } catch(e) {}
  }

  const key = btoa(modelId).replace(/[^a-z0-9]/gi,'');
  const progWrap = cardEl?.querySelector?.('[id^="mp-prog-"]') || document.getElementById('mp-prog-' + key);
  const progBar  = progWrap?.querySelector?.('.mp-prog-bar');
  const progLbl  = progWrap?.querySelector?.('.mp-prog-label');
  const sdot     = document.getElementById('mp-dot-' + key);
  const pullBtn  = document.getElementById('mp-pull-' + key);

  if (progWrap) progWrap.style.display = 'block';
  if (progLbl)  progLbl.textContent = 'Starting download…';
  if (progBar)  progBar.style.width = '0%';
  if (pullBtn)  { pullBtn.disabled = true; pullBtn.textContent = '⏳'; }
  if (sdot)     sdot.style.background = '#fa0';

  try {
    const res = await fetch('/api/ai/pull', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model_id: modelId}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const raw = p.slice(6);
        if (raw === '[DONE]') break;
        try {
          const obj = JSON.parse(raw);
          if (obj.error) throw new Error(obj.error);
          if (obj.total && obj.completed) {
            const pct = Math.round(obj.completed / obj.total * 100);
            if (progBar) progBar.style.width = pct + '%';
            if (progLbl) progLbl.textContent = `Downloading… ${pct}% (${(obj.completed/1e9).toFixed(1)} / ${(obj.total/1e9).toFixed(1)} GB)`;
          } else if (obj.status) {
            if (progLbl) progLbl.textContent = obj.status;
          }
        } catch(pe) {
          if (pe.message && !pe.message.includes('JSON')) throw pe;
        }
      }
    }
    if (progLbl)  progLbl.textContent = '✓ Download complete';
    if (progBar)  progBar.style.width = '100%';
    if (sdot)     sdot.style.background = '#4f4';
    setTimeout(() => mpLoad(), 1000);
  } catch(e) {
    if (progLbl) progLbl.textContent = '✗ Failed: ' + e.message;
    if (sdot)    sdot.style.background = '#c44';
    if (pullBtn) { pullBtn.disabled = false; pullBtn.textContent = '⬇ Retry'; }
  } finally {
    _mpPulling = false;
  }
}

async function mpDeleteModel(modelId, isLoaded, cardEl) {
  const deleteFromOllama = isLoaded &&
    confirm(`Remove "${modelId}" from Ollama storage?\n\nThis frees disk space but requires re-downloading to use again.`);
  try {
    await fetch('/api/ai/models/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model_id: modelId, delete_from_ollama: deleteFromOllama}),
    });
    if (activeModel === modelId) activeModel = '';
    mpLoad();
    loadModels();
  } catch(e) {
    alert('Failed to remove: ' + e.message);
  }
}

async function mpAddAndPull() {
  const inp = document.getElementById('mp-add-id');
  const id = (inp?.value || '').trim();
  if (!id) { inp?.focus(); return; }

  // Register the model first
  try {
    await fetch('/api/ai/models/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
  } catch(e) {}

  inp.value = '';

  // Show pull progress in the add-section bar
  const prog = document.getElementById('mp-pull-progress');
  const bar  = document.getElementById('mp-pull-bar');
  const lbl  = document.getElementById('mp-pull-label');
  prog.style.display = 'block';
  if (bar) bar.style.width = '0%';
  if (lbl) lbl.textContent = 'Starting download…';
  _mpPulling = true;

  try {
    const res = await fetch('/api/ai/pull', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model_id: id}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const raw = p.slice(6);
        if (raw === '[DONE]') break;
        try {
          const obj = JSON.parse(raw);
          if (obj.error) throw new Error(obj.error);
          if (obj.total && obj.completed) {
            const pct = Math.round(obj.completed / obj.total * 100);
            if (bar) bar.style.width = pct + '%';
            if (lbl) lbl.textContent = `Downloading ${id}… ${pct}% (${(obj.completed/1e9).toFixed(1)} / ${(obj.total/1e9).toFixed(1)} GB)`;
          } else if (obj.status) {
            if (lbl) lbl.textContent = obj.status;
          }
        } catch(pe) {
          if (pe.message && !pe.message.includes('JSON')) throw pe;
        }
      }
    }
    if (lbl) lbl.textContent = `✓ ${id} downloaded`;
    if (bar) bar.style.width = '100%';
    setTimeout(() => { prog.style.display = 'none'; mpLoad(); loadModels(); }, 2000);
  } catch(e) {
    if (lbl) lbl.textContent = '✗ Failed: ' + e.message;
  } finally {
    _mpPulling = false;
  }
}

function mpQuickPull(modelId) {
  document.getElementById('mp-add-id').value = modelId;
  mpAddAndPull();
}

function mpRefresh() { mpLoad(); loadModels(); }

// ── Search Hugging Face ──────────────────────────────────────────────────
// Discovery only — picking a result just fills in the same "Model ID"
// field the Pull & Add box above uses (model_id="hf.co/{repo}:{filename}",
// the exact form Ollama's own /api/pull already understands, see
// app.ai.search_huggingface_models's docstring) and reuses mpQuickPull's
// existing pull flow, so no separate download mechanism exists here.

function mpFmtBytes(n) {
  if (!n && n !== 0) return '';
  return n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB' : (n / 1e6).toFixed(0) + ' MB';
}

async function mpHfSearch() {
  const q = (document.getElementById('mp-hf-query').value || '').trim();
  const resultsEl = document.getElementById('mp-hf-results');
  if (!q) { resultsEl.innerHTML = ''; return; }
  resultsEl.innerHTML = '<div style="color:var(--text-dim);font-size:.8rem">⏳ Searching…</div>';
  try {
    const res = await fetch('/api/ai/ollama/hf-search?q=' + encodeURIComponent(q));
    const data = await res.json();
    const models = data.results || [];
    if (!models.length) {
      resultsEl.innerHTML = '<div style="color:var(--text-dim);font-size:.8rem">No GGUF models found.</div>';
      return;
    }
    resultsEl.innerHTML = '';
    models.forEach((m) => {
      const row = document.createElement('div');
      row.style.cssText = 'border:1px solid var(--border);border-radius:4px;padding:.5rem .65rem';
      row.innerHTML =
        `<div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem">` +
        `<span style="font-size:.82rem;word-break:break-all">${mpEsc(m.id)}</span>` +
        `<span style="font-size:.7rem;color:var(--text-dim);white-space:nowrap">⬇ ${m.downloads || 0} · ♥ ${m.likes || 0}</span>` +
        `</div><div class="mp-hf-files" style="margin-top:.4rem;font-size:.76rem;color:var(--text-dim)">Click to see available files…</div>`;
      row.style.cursor = 'pointer';
      row.onclick = () => mpHfShowFiles(m.id, row.querySelector('.mp-hf-files'));
      resultsEl.appendChild(row);
    });
  } catch (e) {
    resultsEl.innerHTML = '<div style="color:#f66;font-size:.8rem">Search failed: ' + mpEsc(e.message) + '</div>';
  }
}

function mpEsc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

let _mpHfFilesLoading = false;
async function mpHfShowFiles(repoId, containerEl) {
  if (_mpHfFilesLoading || containerEl.dataset.loaded) return;
  _mpHfFilesLoading = true;
  containerEl.textContent = '⏳ Loading files…';
  try {
    const res = await fetch('/api/ai/ollama/hf-files?repo=' + encodeURIComponent(repoId));
    const data = await res.json();
    const files = data.files || [];
    containerEl.dataset.loaded = '1';
    if (!files.length) {
      containerEl.textContent = 'No .gguf files found in this repo.';
      return;
    }
    containerEl.innerHTML = '';
    containerEl.style.display = 'flex';
    containerEl.style.flexWrap = 'wrap';
    containerEl.style.gap = '.3rem';
    files.forEach((f) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ig-preset-btn';
      btn.textContent = f.filename + (f.size_bytes ? ` (${mpFmtBytes(f.size_bytes)})` : '');
      btn.onclick = (e) => {
        e.stopPropagation();
        mpQuickPull(`hf.co/${repoId}:${f.filename}`);
      };
      containerEl.appendChild(btn);
    });
  } catch (e) {
    containerEl.textContent = 'Could not load files: ' + e.message;
  } finally {
    _mpHfFilesLoading = false;
  }
}

// ── Upload a local .gguf file ────────────────────────────────────────────

function mpUploadFileChosen() {
  const inp = document.getElementById('mp-upload-file');
  const f = inp.files[0];
  const label = document.getElementById('mp-upload-filename');
  const nameField = document.getElementById('mp-upload-name');
  const btn = document.getElementById('mp-upload-btn');
  if (!f) { label.textContent = ''; btn.disabled = true; return; }
  label.textContent = `${f.name} (${mpFmtBytes(f.size)})`;
  if (!nameField.value.trim()) {
    nameField.value = f.name.replace(/\.gguf$/i, '').toLowerCase().replace(/[^a-z0-9._-]+/g, '-');
  }
  btn.disabled = false;
}

let _mpUploading = false;
async function mpUploadModel() {
  if (_mpUploading) return;
  const inp = document.getElementById('mp-upload-file');
  const file = inp.files[0];
  const modelName = (document.getElementById('mp-upload-name').value || '').trim();
  if (!file) return;
  if (!modelName) { alert('Give the model a name first.'); return; }
  _mpUploading = true;
  const btn = document.getElementById('mp-upload-btn');
  const prog = document.getElementById('mp-upload-progress');
  const bar = document.getElementById('mp-upload-bar');
  const lbl = document.getElementById('mp-upload-label');
  btn.disabled = true;
  prog.style.display = 'block';
  bar.style.width = '0%';
  lbl.textContent = 'Starting upload…';
  try {
    const data = await ndChunkedUpload(file, {
      directUrl: '/api/ai/ollama/upload/direct',
      chunkUrl: '/api/ai/ollama/upload/chunk',
      completeUrl: '/api/ai/ollama/upload/complete',
      extraFields: { model_name: modelName },
      onProgress: ({ phase, percent }) => {
        if (phase === 'upload') {
          bar.style.width = percent + '%';
          lbl.textContent = `Uploading… ${percent}%`;
        } else {
          bar.style.width = '100%';
          lbl.textContent = 'Handing off to Ollama…';
        }
      },
    });
    if (data.error) throw new Error(data.error);
    if (!data.import_id) throw new Error('Server did not start the import');
    // The actual "push blob to Ollama + register" step runs as a server-side
    // background task (see _start_local_gguf_import in app/routers/ai.py) —
    // for a real multi-GB model that can take minutes, far longer than a
    // Cloudflare-tunneled request can stay open waiting for one response, so
    // we poll for progress instead of trusting the upload response itself.
    lbl.textContent = 'Importing into Ollama… (this can take a while for a large model)';
    let result = null;
    while (true) {
      await new Promise((r) => setTimeout(r, 1500));
      const res = await fetch(`/api/ai/ollama/upload/status/${data.import_id}`);
      if (!res.ok) throw new Error(`Lost track of the import (HTTP ${res.status})`);
      result = await res.json();
      if (result.detail) lbl.textContent = result.detail;
      if (result.status === 'done' || result.error) break;
    }
    if (result.error) throw new Error(result.error);
    lbl.textContent = `✓ ${modelName} imported`;
    inp.value = '';
    document.getElementById('mp-upload-filename').textContent = '';
    document.getElementById('mp-upload-name').value = '';
    setTimeout(() => { prog.style.display = 'none'; mpLoad(); loadModels(); }, 1500);
  } catch (e) {
    lbl.textContent = '✗ Failed: ' + e.message;
    btn.disabled = false;
  } finally {
    _mpUploading = false;
  }
}

