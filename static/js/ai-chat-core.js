let history = [];
let activeModel = "";
let _presets = [];
let _chatPresetOptions = {};
let _chatPresetSystemExtra = '';
const chatAttachments = ndAiAttachments(document.getElementById('ai-attach-list'), document.getElementById('ai-jobs-panel'), () => {
  document.getElementById('ai-send').disabled = chatAttachments.hasPending();
});
let activeReader = null;
let currentSessionId = null;

function stopStream() {
  if (activeReader) activeReader.cancel();
}

// ── Chat history ──────────────────────────────────────────────────────────────

async function loadSessions(autoLoadLatest = false) {
  const el = document.getElementById('session-list');
  try {
    const r = await fetch('/api/ai/sessions');
    const d = await r.json();
    el.innerHTML = '';
    if (!d.sessions.length) {
      el.textContent = 'No saved chats yet.';
      return;
    }
    for (const s of d.sessions) {
      const item = document.createElement('div');
      item.className = 'session-item';
      item.id = 'session-' + s.id;

      const link = document.createElement('button');
      link.className = 'session-link' + (s.id === currentSessionId ? ' active' : '');
      link.textContent = s.title;
      link.title = s.title;
      link.onclick = () => loadSession(s.id, link);

      const del = document.createElement('button');
      del.className = 'session-del';
      del.textContent = '✕';
      del.title = 'Delete chat';
      del.onclick = (e) => { e.stopPropagation(); deleteSession(s.id, item); };

      item.appendChild(link);
      item.appendChild(del);
      el.appendChild(item);
    }
    if (autoLoadLatest && !currentSessionId && d.sessions.length) {
      const latest = d.sessions[0];
      const link = el.querySelector('.session-link');
      loadSession(latest.id, link);
    }
  } catch(_) {
    el.textContent = '✗ Unavailable';
  }
}

async function autoSave() {
  if (!history.length) return;
  try {
    const r = await fetch('/api/ai/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId, messages: history })
    });
    const d = await r.json();
    currentSessionId = d.id;
    loadSessions();
  } catch(_) {}
}

async function loadSession(id, linkEl) {
  try {
    const r = await fetch('/api/ai/sessions/' + id);
    const d = await r.json();
    currentSessionId = d.id;
    history = d.messages;
    const box = document.getElementById('ai-messages');
    box.innerHTML = '';
    for (const m of d.messages) {
      addMessage(m.role, m.content, false);
    }
    box.scrollTop = box.scrollHeight;
    document.querySelectorAll('.session-link').forEach(l => l.classList.remove('active'));
    if (linkEl) linkEl.classList.add('active');
  } catch(e) {
    alert('Failed to load session: ' + e.message);
  }
}

async function deleteSession(id, itemEl) {
  if (!confirm('Delete this chat?')) return;
  try {
    await fetch('/api/ai/sessions/' + id, { method: 'DELETE' });
    if (currentSessionId === id) {
      currentSessionId = null;
      history = [];
      const box = document.getElementById('ai-messages');
      box.innerHTML = '';
      addMessage('assistant', 'Chat cleared — ask me anything about your world.', false);
    }
    itemEl.remove();
    const el = document.getElementById('session-list');
    if (!el.children.length) el.textContent = 'No saved chats yet.';
  } catch(e) {
    alert('Failed to delete: ' + e.message);
  }
}

function newChat() {
  if (history.length && !currentSessionId) autoSave();
  currentSessionId = null;
  history = [];
  const box = document.getElementById('ai-messages');
  box.innerHTML = '';
  addMessage('assistant', 'New chat started — ask me anything about your world.', false);
  document.querySelectorAll('.session-link').forEach(l => l.classList.remove('active'));
  const ctxEl = document.getElementById('ctx-status');
  ctxEl.style.color = 'var(--text-dim)';
  ctxEl.textContent = '○ Ready';
}

loadSessions(true);

// ── Chat presets ─────────────────────────────────────────────────────────────
// A GM-defined {model, temperature/top_p, persona} bundle a conversation can
// switch to on the fly — e.g. "Lorekeeper" (low temperature, factual) vs
// "NPC improv" (high temperature, playful) — without a trip to Settings >
// System, which applies instance-wide to every AI feature, not just this
// conversation. Applying a preset sets the active model, layers its options
// over the instance-wide defaults for every message sent from here on (see
// sendMessage's options: _chatPresetOptions), and appends its persona text
// to the system prompt.

async function loadPresets() {
  try {
    const d = await fetch('/api/ai/presets').then(r => r.json());
    _presets = d.presets || [];
    const sel = document.getElementById('preset-select');
    const current = sel.value;
    sel.innerHTML = '<option value="">— none —</option>' +
      _presets.map(p => `<option value="${p.label}">${p.label}</option>`).join('');
    if (_presets.some(p => p.label === current)) sel.value = current;
  } catch (e) { /* presets are optional — a failed fetch just leaves none available */ }
}

function applyPreset(label) {
  if (!label) {
    _chatPresetOptions = {};
    _chatPresetSystemExtra = '';
    return;
  }
  const preset = _presets.find(p => p.label === label);
  if (!preset) return;
  _chatPresetOptions = preset.options || {};
  _chatPresetSystemExtra = preset.system_extra || '';
  if (preset.model) {
    activeModel = preset.model;
    const shortId = preset.model.split('/').pop().split(':')[0] || preset.model;
    const label2 = document.getElementById('active-model-label');
    if (label2) label2.textContent = shortId;
  }
  document.getElementById('preset-label').value = preset.label;
  document.getElementById('preset-temperature').value = preset.options && preset.options.temperature !== undefined ? preset.options.temperature : '';
  document.getElementById('preset-top-p').value = preset.options && preset.options.top_p !== undefined ? preset.options.top_p : '';
  document.getElementById('preset-system-extra').value = preset.system_extra || '';
}

function togglePresetManager() {
  const el = document.getElementById('preset-manager');
  el.style.display = el.style.display === 'none' ? 'flex' : 'none';
}

async function savePreset() {
  const label = document.getElementById('preset-label').value.trim();
  if (!label) { alert('Enter a preset name.'); return; }
  const temp = document.getElementById('preset-temperature').value;
  const topP = document.getElementById('preset-top-p').value;
  const options = {};
  if (temp !== '') options.temperature = parseFloat(temp);
  if (topP !== '') options.top_p = parseFloat(topP);
  try {
    const r = await fetch('/api/ai/presets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        label, model: activeModel || '', options,
        system_extra: document.getElementById('preset-system-extra').value.trim(),
      }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadPresets();
    document.getElementById('preset-select').value = label;
    applyPreset(label);
  } catch (e) {
    alert('Could not save preset: ' + e.message);
  }
}

async function deletePreset() {
  const label = document.getElementById('preset-select').value;
  if (!label) return;
  if (!confirm(`Delete preset "${label}"?`)) return;
  await fetch('/api/ai/presets/' + encodeURIComponent(label), { method: 'DELETE' });
  document.getElementById('preset-label').value = '';
  await loadPresets();
  applyPreset('');
}

loadPresets();

// ── Debug panel ───────────────────────────────────────────────────────────────

function toggleDebug() {
  const p = document.getElementById('debug-panel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
  if (p.style.display === 'block') loadDebug();
}

async function loadDebug() {
  const el = document.getElementById('debug-content');
  el.textContent = '⏳ Fetching…';
  try {
    const r = await fetch('/api/ai/debug');
    const d = await r.json();
    const w = d.whisper || {};
    const lines = [
      'URL: ' + d.ollama_url,
      'Reachable: ' + (d.ollama_reachable ? '✓ yes' : '✗ no'),
      d.error ? 'Error: ' + d.error : null,
      'Default: ' + d.default_model,
      '',
      'Downloaded (' + (d.loaded_models || []).length + '):',
      ...(d.loaded_models || []).map(m => '  · ' + m),
      '',
      'Whisper (audio transcription): ' + (w.url ? w.url : '(not configured)'),
      w.url ? 'Whisper reachable: ' + (w.ok ? '✓ yes' : '✗ no' + (w.reason ? ' — ' + w.reason : '')) : null,
    ].filter(l => l !== null);
    el.textContent = lines.join('\n');
  } catch(e) {
    el.textContent = '✗ ' + e.message;
  }
}

async function testModel() {
  const el = document.getElementById('debug-content');
  el.textContent = '⏳ Testing model: ' + (activeModel || '(default)') + '…';
  try {
    const url = '/api/ai/test-chat' + (activeModel ? '?model=' + encodeURIComponent(activeModel) : '');
    const r = await fetch(url);
    const d = await r.json();
    el.textContent = [
      'Requested: ' + (d.requested || '(empty)'),
      'Resolved:  ' + d.resolved,
      'Result:    ' + d.result,
    ].join('\n');
  } catch(e) {
    el.textContent = '✗ Test failed: ' + e.message;
  }
}

async function testSse() {
  const el = document.getElementById('debug-content');
  el.textContent = '⏳ Testing SSE…';
  try {
    const res = await fetch('/api/ai/ping');
    if (!res.ok) { el.textContent = '✗ HTTP ' + res.status; return; }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let tokens = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const payload = p.slice(6);
        if (payload === '[DONE]') break;
        try { const t = JSON.parse(payload).token; if (typeof t === 'string') tokens.push(t); } catch(_) {}
      }
    }
    el.textContent = tokens.length
      ? '✓ SSE OK — ' + tokens.length + ' tokens received:\n' + tokens.join('')
      : '✗ SSE connected but no tokens received';
  } catch(e) {
    el.textContent = '✗ SSE failed: ' + e.message;
  }
}

async function loadModels() {
  const list = document.getElementById('model-list');
  try {
    const r = await fetch('/api/ai/models');
    const d = await r.json();
    if (!activeModel) {
      const preferredId  = (d.defaults && d.defaults.chat) || d.default;
      const defaultLoaded = d.models.find(m => m.id === preferredId && m.loaded);
      const firstLoaded   = d.models.find(m => m.loaded);
      activeModel = (defaultLoaded || firstLoaded || d.models[0] || {id: preferredId}).id;
    }
    const shortId = activeModel.split('/').pop().split(':')[0] || activeModel;
    document.getElementById('active-model-label').textContent = shortId;
    list.innerHTML = '';
    for (const m of d.models) {
      const row = document.createElement('div');
      row.className = 'model-row';

      const dot = document.createElement('span');
      dot.className = 'model-dot ' + (m.loaded ? 'model-dot--loaded' : 'model-dot--unloaded');
      dot.id = 'dot-' + btoa(m.id).replace(/=/g,'');

      const label = document.createElement('button');
      label.className = 'model-label' + (m.id === activeModel ? ' active' : '');
      label.textContent = m.label;
      label.title = m.id;
      label.onclick = () => selectModel(m.id, label);

      row.appendChild(dot);
      row.appendChild(label);

      if (!m.loaded) {
        const btn = document.createElement('button');
        btn.className = 'model-pull-btn';
        btn.textContent = '⬇ Pull';
        btn.id = 'pull-' + btoa(m.id).replace(/=/g,'');
        btn.onclick = () => pullModel(m.id, btn, dot);
        row.appendChild(btn);
      } else {
        const btn = document.createElement('button');
        btn.className = 'model-pull-btn';
        btn.textContent = '↺ Re-dl';
        btn.title = 'Force re-download — fixes corrupted models';
        btn.onclick = () => forceRepull(m.id, btn, dot, row);
        row.appendChild(btn);
      }

      const del = document.createElement('button');
      del.className = 'model-del-btn';
      del.textContent = '✕';
      del.title = 'Remove model';
      del.onclick = () => removeModel(m.id, row, m.loaded);
      row.appendChild(del);

      list.appendChild(row);
    }

    // Add-model form
    const form = document.createElement('div');
    form.className = 'model-add-form';
    form.innerHTML =
      '<input id="model-add-id" placeholder="Model ID (e.g. llama3:8b or hf.co/…)" />' +
      '<button onclick="addModel()">+ Add model</button>' +
      '<button onclick="resetModels()" style="color:#888;border-color:#333">↺ Restore defaults</button>';
    list.appendChild(form);

    // Populate mobile model selector
    const mobSel  = document.getElementById('mobile-model-sel');
    const mobDot  = document.getElementById('mobile-ollama-dot');
    if (mobSel) {
      mobSel.innerHTML = d.models.map(m =>
        `<option value="${m.id}"${m.id === activeModel ? ' selected' : ''}>${m.label || m.id}${m.loaded ? '' : ' ⬇'}</option>`
      ).join('');
      if (!d.models.length) mobSel.innerHTML = '<option value="">No models — go to 🤖 Models tab</option>';
    }
    if (mobDot) {
      const hasLoaded = d.models.some(m => m.loaded);
      mobDot.style.background = hasLoaded ? '#4f4' : '#c44';
      mobDot.title = hasLoaded ? 'Ollama has loaded models' : 'No models loaded — visit 🤖 Models tab';
    }
  } catch(e) {
    list.textContent = '✗ Unavailable';
    const mobSel = document.getElementById('mobile-model-sel');
    if (mobSel) mobSel.innerHTML = '<option value="">Ollama unavailable</option>';
    const mobDot = document.getElementById('mobile-ollama-dot');
    if (mobDot) { mobDot.style.background = '#c44'; mobDot.title = 'Ollama unreachable'; }
  }
}

function selectModel(id, labelEl) {
  activeModel = id;
  document.querySelectorAll('.model-label').forEach(l => l.classList.remove('active'));
  labelEl.classList.add('active');
  const shortId = id.split('/').pop().split(':')[0] || id;
  document.getElementById('active-model-label').textContent = shortId;
  _syncMobileModelSel();
}

function mobileSelectModel(id) {
  if (!id) return;
  activeModel = id;
  const shortId = id.split('/').pop().split(':')[0] || id;
  document.getElementById('active-model-label').textContent = shortId;
  document.querySelectorAll('.model-label').forEach(l => {
    l.classList.toggle('active', l.title === id);
  });
}

function _syncMobileModelSel() {
  const sel = document.getElementById('mobile-model-sel');
  if (sel) sel.value = activeModel || '';
}

async function pullModel(modelId, btn, dot) {
  btn.disabled = true;
  btn.textContent = '⏳ 0%';
  try {
    const res = await fetch('/api/ai/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId })
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const payload = p.slice(6);
        if (payload === '[DONE]') break;
        try {
          const obj = JSON.parse(payload);
          if (obj.total && obj.completed) {
            btn.textContent = '⏳ ' + Math.round(obj.completed / obj.total * 100) + '%';
          } else if (obj.status) {
            btn.textContent = obj.status.slice(0, 12);
          }
        } catch(_) {}
      }
    }
    dot.className = 'model-dot model-dot--loaded';
    btn.remove();
  } catch(e) {
    btn.textContent = '✗ Failed';
    btn.disabled = false;
  }
}
async function forceRepull(modelId, btn, dot, row) {
  if (!confirm('Delete "' + modelId + '" from Ollama storage and re-download it?\n\nThis fixes corrupted model files. The model will be unavailable during download.')) return;
  btn.disabled = true;
  btn.textContent = '⏳ Removing…';
  dot.className = 'model-dot model-dot--unloaded';
  try {
    await fetch('/api/ai/models/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId, delete_from_ollama: true })
    });
  } catch(e) {
    btn.textContent = '✗ Remove failed';
    btn.disabled = false;
    return;
  }
  btn.textContent = '⏳ 0%';
  try {
    const res = await fetch('/api/ai/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId })
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const p of parts) {
        if (!p.startsWith('data: ')) continue;
        const payload = p.slice(6);
        if (payload === '[DONE]') break;
        try {
          const obj = JSON.parse(payload);
          if (obj.total && obj.completed) {
            btn.textContent = '⏳ ' + Math.round(obj.completed / obj.total * 100) + '%';
          } else if (obj.status) {
            btn.textContent = obj.status.slice(0, 12);
          }
        } catch(_) {}
      }
    }
    dot.className = 'model-dot model-dot--loaded';
    btn.textContent = '🔄';
    btn.disabled = false;
  } catch(e) {
    btn.textContent = '✗ Pull failed';
    btn.disabled = false;
  }
}

async function resetModels() {
  try {
    await fetch('/api/ai/models/reset', { method: 'POST' });
    loadModels();
  } catch(e) {
    alert('Failed: ' + e.message);
  }
}

async function addModel() {
  const input = document.getElementById('model-add-id');
  const id = (input?.value || '').trim();
  if (!id) return;
  try {
    await fetch('/api/ai/models/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
    input.value = '';
    loadModels();
  } catch(e) {
    alert('Failed to add model: ' + e.message);
  }
}

async function removeModel(modelId, row, isLoaded) {
  const deleteFromOllama = isLoaded && confirm('Also delete this model from Ollama storage?\n(Frees disk space but requires re-downloading to use again)');
  try {
    await fetch('/api/ai/models/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId, delete_from_ollama: deleteFromOllama })
    });
    if (activeModel === modelId) activeModel = '';
    loadModels(); // refreshes list and auto-selects first loaded model if activeModel is now empty
  } catch(e) {
    alert('Failed to remove model: ' + e.message);
  }
}

loadModels();

function mdToHtml(text) {
  let h = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/```[\s\S]*?```/g, m => '<pre><code>' + m.slice(3, -3).replace(/^\w+\n/,'') + '</code></pre>')
    .replace(/`([^`\n]+)`/g,'<code>$1</code>')
    .replace(/\*\*\*(.+?)\*\*\*/g,'<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\*(.+?)\*/g,'<em>$1</em>')
    .replace(/^### (.+)$/gm,'<h3>$1</h3>')
    .replace(/^## (.+)$/gm,'<h2>$1</h2>')
    .replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/^[-*] (.+)$/gm,'<li>$1</li>')
    .replace(/(<li>[\s\S]+?<\/li>)/g,'<ul>$1</ul>')
    .replace(/\n\n/g,'</p><p>')
    .replace(/\n/g,'<br>');
  return '<p>' + h + '</p>';
}

function addMessage(role, text, isThinking, attachments) {
  const box = document.getElementById('ai-messages');
  const wrap = document.createElement('div');
  wrap.className = 'ai-msg ai-msg--' + role + (isThinking ? ' ai-thinking' : '');
  const bubble = document.createElement('div');
  bubble.className = 'ai-bubble';
  bubble.innerHTML = isThinking ? text : (role === 'user' ? escHtml(text).replace(/\n/g,'<br>') : mdToHtml(text));
  if (attachments && attachments.length) bubble.appendChild(ndAiRenderAttachmentChips(attachments));
  wrap.appendChild(bubble);
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
  return bubble;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function addSaveBtn(wrap, text) {
  const btn = document.createElement('button');
  btn.className = 'save-note-btn';
  btn.textContent = '💾 Save as note';
  btn.onclick = () => saveNote(text, btn);
  wrap.appendChild(btn);
}

function addIllustrateBtn(bubble, wrap, text) {
  const btn = document.createElement('button');
  btn.className = 'save-note-btn';
  btn.style.marginLeft = '.6rem';
  btn.textContent = '🎨 Illustrate';
  btn.onclick = () => illustrateMessage(text, bubble, btn);
  wrap.appendChild(btn);
}

function addSaveEntityBtn(bubble, wrap, text) {
  const btn = document.createElement('button');
  btn.className = 'save-note-btn';
  btn.style.marginLeft = '.6rem';
  btn.textContent = '📥 Save as entity';
  btn.onclick = () => draftEntityFromMessage(text, bubble, btn);
  wrap.appendChild(btn);
}

// Drafts a world entity from an assistant reply (via app.ai.parse_entity_from_text,
// the same schema-constrained-Ollama pattern the Facts page uses for recaps),
// shows an editable review card in the message bubble, and only writes the
// entity once the GM confirms — via the existing /api/import/execute route
// (same one the Import page's paste-JSON flow uses), not a new write path.
async function draftEntityFromMessage(text, bubble, btn) {
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = '✨ Drafting…';
  try {
    const res = await fetch('/api/ai/entity-from-text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text.slice(0, 8000) }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    const draft = await res.json();
    bubble.appendChild(renderEntityDraft(draft));
  } catch (e) {
    alert('Save as entity failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
}

function renderEntityDraft(draft) {
  const card = document.createElement('div');
  card.className = 'entity-draft';

  const kindSel = document.createElement('select');
  ENTITY_KINDS.forEach((k) => {
    const opt = document.createElement('option');
    opt.value = k;
    opt.textContent = (ENTITY_KIND_ICONS[k] || '') + ' ' + k.charAt(0).toUpperCase() + k.slice(1);
    if (k === draft.kind) opt.selected = true;
    kindSel.appendChild(opt);
  });

  const nameInput = document.createElement('input');
  nameInput.placeholder = 'Name';
  nameInput.value = draft.name || '';

  const subtypeInput = document.createElement('input');
  subtypeInput.placeholder = 'Subtype (optional)';
  subtypeInput.value = draft.subtype || '';

  const folderInput = document.createElement('input');
  folderInput.placeholder = 'Folder (optional, "A/B" to nest)';
  folderInput.value = draft.folder || '';

  const row1 = document.createElement('div');
  row1.className = 'entity-draft-row';
  row1.append(kindSel, nameInput);
  const row2 = document.createElement('div');
  row2.className = 'entity-draft-row';
  row2.append(subtypeInput, folderInput);

  const tagsInput = document.createElement('input');
  tagsInput.placeholder = 'Tags (comma-separated)';
  tagsInput.value = draft.tags || '';

  const summaryInput = document.createElement('input');
  summaryInput.placeholder = 'One-line summary';
  summaryInput.value = draft.summary || '';

  const bodyTa = document.createElement('textarea');
  bodyTa.rows = 6;
  bodyTa.placeholder = 'Full write-up (Markdown)';
  bodyTa.value = draft.body || '';

  const visLabel = document.createElement('label');
  visLabel.className = 'entity-draft-check';
  const visCb = document.createElement('input');
  visCb.type = 'checkbox';
  visCb.checked = draft.visible_to_players !== false;
  visLabel.append(visCb, document.createTextNode('Visible to players'));

  const actions = document.createElement('div');
  actions.className = 'entity-draft-actions';
  const createBtn = document.createElement('button');
  createBtn.type = 'button';
  createBtn.className = 'save-note-btn';
  createBtn.textContent = '✓ Create Entity';
  const discardBtn = document.createElement('button');
  discardBtn.type = 'button';
  discardBtn.className = 'save-note-btn';
  discardBtn.textContent = '✕ Discard';
  discardBtn.onclick = () => card.remove();
  const status = document.createElement('span');
  status.style.cssText = 'font-size:.78rem;color:var(--text-dim)';

  createBtn.onclick = async () => {
    const name = nameInput.value.trim();
    if (!name) { alert('Name is required.'); return; }
    createBtn.disabled = true;
    status.textContent = 'Creating…';
    const entityJson = {
      kind: kindSel.value,
      name,
      subtype: subtypeInput.value.trim() || undefined,
      folder: folderInput.value.trim() || undefined,
      tags: tagsInput.value.trim() || undefined,
      summary: summaryInput.value.trim() || undefined,
      body: bodyTa.value.trim() || undefined,
      visible_to_players: visCb.checked,
    };
    try {
      const res = await fetch('/api/import/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ json_text: JSON.stringify(entityJson), kind: 'entity_single' }),
      });
      if (!res.ok) throw await ndApiErrorFrom(res);
      const data = await res.json();
      status.innerHTML = `✓ Created — <a href="${data.redirect}" target="_blank">view entity</a>`;
      createBtn.remove();
      discardBtn.textContent = 'Close';
    } catch (e) {
      status.textContent = 'Failed: ' + (e.message || e);
      createBtn.disabled = false;
    }
  };

  actions.append(createBtn, discardBtn, status);
  card.append(row1, row2, tagsInput, summaryInput, bodyTa, visLabel, actions);
  return card;
}

// Turns an assistant reply into an image: asks the chat model to condense it
// into an image-generation prompt (same idea as "Build from World Lore" in
// the Image Studio tab, just sourced from a chat message instead of a
// concept + lore lookup), then sends that prompt straight to the configured
// imagegen backend (SwarmUI or ComfyUI — see IMAGEGEN_TYPE) with default
// settings. The result is appended into the message bubble, same spot
// attachment chips render.
async function illustrateMessage(text, bubble, btn) {
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.textContent = '✨ Writing prompt…';
  try {
    const system = 'You are an image prompt writer for a cyberpunk-fantasy TTRPG. Given a passage of text, write ONE detailed image generation prompt depicting its central scene or subject — comma-separated tags and descriptive phrases, about 40-70 words. Output only the prompt, no explanation, no preamble, no quotes.';
    const promptRes = await fetch('/api/ai/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: text.slice(0, 4000) }], system, model: activeModel, surface: 'chat' }),
    });
    if (!promptRes.ok) throw new Error(`Server error ${promptRes.status}`);
    const promptData = await promptRes.json();
    const imgPrompt = (promptData.result || '').trim();
    if (!imgPrompt || imgPrompt.startsWith('[AI ')) throw new Error(imgPrompt || 'Could not write an image prompt');

    btn.textContent = '🎨 Generating…';
    const genRes = await fetch('/api/ai/imagegen/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: imgPrompt }),
    });
    if (!genRes.ok) {
      let detail = '';
      try { detail = (await genRes.json()).detail || ''; } catch (e) { /* non-JSON error body */ }
      throw new Error(detail || `Server error ${genRes.status}`);
    }
    const genData = await genRes.json();
    if (genData.error) throw new Error(genData.error);
    const url = genData.url || (genData.urls && genData.urls[0]) || '';
    if (!url) throw new Error('No image returned — is an image generation backend configured and running?');

    const card = document.createElement('div');
    card.className = 'illustrate-result';
    const img = document.createElement('img');
    img.src = url;
    img.alt = imgPrompt;
    img.title = imgPrompt;
    card.appendChild(img);
    bubble.appendChild(card);
  } catch (e) {
    alert('Illustrate failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
}

async function saveNote(text, btn) {
  const title = prompt('Note title:', 'AI note ' + new Date().toLocaleDateString());
  if (!title) return;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const r = await fetch('/api/ai/save-note', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, content: text })
    });
    const d = await r.json();
    btn.textContent = '✓ Saved — ' + d.name;
    btn.style.color = 'var(--neon)';
  } catch(e) {
    btn.textContent = '✗ Save failed';
    btn.disabled = false;
  }
}

function toggleMobileRag() {
  const bar = document.getElementById('mobile-rag-bar');
  const showing = bar.style.display === 'flex';
  bar.style.display = showing ? 'none' : 'flex';
}

function syncRagSliders(type, val) {
  if (type === 'entities') {
    const v = val == 250 ? 'max' : val;
    document.getElementById('ctx-limit-mob-val').textContent = v;
    const desk = document.getElementById('ctx-limit');
    if (desk) { desk.value = val; document.getElementById('ctx-limit-val').textContent = v; }
  } else {
    const v = val == 0 ? 'off' : val;
    document.getElementById('notes-limit-mob-val').textContent = v;
    const desk = document.getElementById('notes-limit');
    if (desk) { desk.value = val; document.getElementById('notes-limit-val').textContent = v; }
  }
}

function _getMobileCtxEl() {
  const isMobile = window.innerWidth <= 700;
  return isMobile
    ? document.getElementById('ctx-status-mob2')
    : document.getElementById('ctx-status');
}

function _setCtxStatus(color, text) {
  ['ctx-status','ctx-status-mob','ctx-status-mob2'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.style.color = color; el.textContent = text; }
  });
}

// ── RAG transparency + pinning ───────────────────────────────────────────────
// What actually got retrieved for the last message (see sendMessage's
// world-context-smart call), and which entities/notes the GM has pinned to
// be included — full body, not just a summary line — in EVERY message of
// this conversation, regardless of whether they'd match the RAG query.
// Pins live for this browser tab only (not saved with the chat session).
let _lastRetrievedEntities = [];
const _pinnedEntities = new Map(); // id -> {id, name, kind}

function toggleCtxPanel() {
  const el = document.getElementById('ctx-panel');
  const showing = el.style.display !== 'none';
  el.style.display = showing ? 'none' : 'block';
  if (!showing) renderCtxPanel();
}

function renderCtxPanel() {
  const retrievedEl = document.getElementById('ctx-panel-retrieved');
  if (!_lastRetrievedEntities.length) {
    retrievedEl.innerHTML = '<p style="color:var(--text-dim);font-style:italic;margin:0">Nothing retrieved yet — send a message first.</p>';
  } else {
    retrievedEl.innerHTML = '';
    const heading = document.createElement('div');
    heading.style.cssText = 'color:var(--text-dim);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.25rem';
    heading.textContent = 'Retrieved for last message';
    retrievedEl.appendChild(heading);
    _lastRetrievedEntities.forEach((e) => {
      retrievedEl.appendChild(_ctxPanelRow(e, _pinnedEntities.has(e.id)));
    });
  }
  const pinnedWrap = document.getElementById('ctx-panel-pinned-wrap');
  const pinnedEl = document.getElementById('ctx-panel-pinned');
  pinnedWrap.style.display = _pinnedEntities.size ? 'block' : 'none';
  pinnedEl.innerHTML = '';
  _pinnedEntities.forEach((e) => pinnedEl.appendChild(_ctxPanelRow(e, true)));
}

function _ctxPanelRow(e, isPinned) {
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:.4rem;padding:.1rem 0';
  const label = document.createElement('span');
  label.style.cssText = 'flex:1;overflow-wrap:anywhere';
  label.textContent = e.name + (e.kind ? ' (' + e.kind + ')' : '');
  const btn = document.createElement('button');
  btn.style.cssText = 'background:none;border:none;cursor:pointer;font-size:.85rem;padding:0';
  btn.title = isPinned ? 'Unpin — stop always including this' : 'Pin — always include this (full text) in every message';
  btn.textContent = isPinned ? '📌' : '📍';
  btn.onclick = () => { isPinned ? unpinEntity(e.id) : pinEntity(e); };
  row.append(label, btn);
  return row;
}

function pinEntity(e) {
  _pinnedEntities.set(e.id, e);
  renderCtxPanel();
}

function unpinEntity(id) {
  _pinnedEntities.delete(id);
  renderCtxPanel();
}

async function _pinnedEntitiesContext() {
  if (!_pinnedEntities.size) return '';
  const parts = await Promise.all([..._pinnedEntities.values()].map(async (e) => {
    try {
      const d = await fetch('/api/entity/' + e.id + '/preview').then(r => r.json());
      return `### ${d.name}${d.kind ? ' (' + d.kind + ')' : ''}\n${d.body || d.summary || ''}`;
    } catch (err) {
      return '';
    }
  }));
  return parts.filter(Boolean).join('\n\n');
}

function _getCtxLimit() {
  const mob = document.getElementById('ctx-limit-mob');
  const desk = document.getElementById('ctx-limit');
  const el = (window.innerWidth <= 700 && mob) ? mob : desk;
  return parseInt(el?.value) || 25;
}

function _getNotesLimit() {
  const mob = document.getElementById('notes-limit-mob');
  const desk = document.getElementById('notes-limit');
  const el = (window.innerWidth <= 700 && mob) ? mob : desk;
  return parseInt(el?.value) || 0;
}

// Shared by the live (streamed) send and the "process in background" chat-job
// send — builds the RAG-context-injected message array + system prompt
// exactly the same way for both, including the same context-panel/status-pill
// side effects, so a backgrounded question gets identical lore grounding to
// a live one. `extraUserMsg`, if given, is appended after `history` (for the
// background-job path, which doesn't push to `history` until "Use this" is
// clicked); omit it for the live path, where the new turn is already in
// `history` by the time this is called.
async function buildChatMessagesWithContext(extraUserMsg) {
  const queryText = extraUserMsg ? extraUserMsg.content : (history.length ? history[history.length - 1].content : '');
  let ctx = '';
  try {
    const cr = await fetch('/api/ai/world-context-smart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: queryText, limit: _getCtxLimit(), notes_limit: _getNotesLimit() })
    });
    const cd = await cr.json();
    ctx = cd.context || '';
    _lastRetrievedEntities = cd.entities || [];
    if (document.getElementById('ctx-panel').style.display !== 'none') renderCtxPanel();
    if (ctx) {
      const noteLabel = cd.notes ? ` + ${cd.notes} note${cd.notes > 1 ? 's' : ''}` : '';
      _setCtxStatus('#4f4', `✓ ${cd.count} entities${noteLabel}`);
    } else {
      _setCtxStatus('var(--text-dim)', '○ No matching lore');
    }
  } catch(_) {
    _setCtxStatus('#f55', '✗ Lore unavailable');
  }

  // Pinned entities/notes are always included in full, regardless of
  // whether they matched this message's RAG query.
  const pinnedCtx = await _pinnedEntitiesContext();
  if (pinnedCtx) ctx = ctx ? (pinnedCtx + '\n\n' + ctx) : pinnedCtx;

  const base = extraUserMsg ? [...history, extraUserMsg] : [...history];
  // The lore pair is injected immediately BEFORE the final (newest) user
  // turn, not at the very front of the array. It differs every send (a
  // fresh RAG query each time), so putting it first — as this used to do —
  // made the very first messages in the array diverge from the previous
  // send on every single turn, defeating Ollama's KV-prefix cache for the
  // ENTIRE conversation: the server has to re-prefill the whole history
  // from scratch each time instead of reusing what it already computed.
  // Keeping everything before the newest turn byte-stable turn to turn
  // means only the lore pair + that one new message ever need prefilling —
  // large savings on a long-running chat, and arguably better grounding
  // too (freshest instruction closest to what it's answering). The lore
  // pair is never pushed into `history` either way, so nothing persisted
  // changes.
  const messagesWithCtx = ctx ? [
    ...base.slice(0, -1),
    { role: 'user', content: 'Relevant world lore:\n\n' + ctx },
    { role: 'assistant', content: 'Got it.' },
    ...base.slice(-1),
  ] : base;

  const presetSystem = _chatPresetSystemExtra ? WORLD_SYSTEM + '\n\n' + _chatPresetSystemExtra : WORLD_SYSTEM;
  return { messages: messagesWithCtx, system: presetSystem };
}

async function sendMessage() {
  const input = document.getElementById('ai-input');
  const btn = document.getElementById('ai-send');
  const aiBar = document.getElementById('ai-bar');
  const text = input.value.trim();
  const attachments = chatAttachments.take();
  if ((!text && !attachments.length) || activeReader) return;
  input.value = '';
  autoResize(input);

  addMessage('user', text, false, attachments);
  history.push({ role: 'user', content: text, attachments });

  btn.textContent = 'Stop ■';
  btn.classList.add('stopping');
  btn.onclick = stopStream;
  aiBar.style.display = 'block'; aiBar.className = 'active';
  _setCtxStatus('var(--text-dim)', '⏳ Finding lore…');
  const thinking = addMessage('assistant', '<span class="thinking-dots"><span></span><span></span><span></span></span>', true);

  try {
    const { messages: messagesWithCtx, system: presetSystem } = await buildChatMessagesWithContext();

    const res = await fetch('/api/ai/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: messagesWithCtx, system: presetSystem, model: activeModel, surface: 'chat',
        options: _chatPresetOptions,
      })
    });
    if (!res.ok) throw new Error('Server error ' + res.status);

    thinking.parentElement.classList.remove('ai-thinking');
    thinking.innerHTML = '';
    let fullText = '';
    let noteText = '';
    let tokenCount = 0;
    let firstToken = true;
    let startTime = 0;
    const statsEl = document.getElementById('gen-stats');
    statsEl.textContent = '⏳ Connecting…';
    statsEl.style.color = 'var(--text-dim)';
    let statsTimer = null;

    // Pulse the active model dot
    const _dotKey = 'dot-' + btoa(activeModel || '').replace(/=/g,'');
    const _activeDot = document.getElementById(_dotKey);
    if (_activeDot) _activeDot.classList.add('model-dot--active');

    const reader = res.body.getReader();
    activeReader = reader;
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith('data: ')) continue;
        const payload = part.slice(6);
        if (payload === '[DONE]') break;
        const _obj = JSON.parse(payload);
        if (_obj.note) {
          // resolve_model substituted a different model than requested —
          // surface it instead of silently swapping (or, before this fix,
          // corrupting the reply with the literal string "undefined").
          noteText = _obj.note;
          thinking.innerHTML = `<div style="font-size:.75rem;color:var(--text-dim);margin-bottom:.4rem">ℹ️ ${escHtml(noteText)}</div>` + mdToHtml(fullText);
          continue;
        }
        if (typeof _obj.token !== 'string') continue;
        const token = _obj.token;
        if (firstToken) {
          firstToken = false;
          startTime = Date.now();
          statsEl.style.color = 'var(--neon)';
          statsTimer = setInterval(() => {
            const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
            statsEl.textContent = `⚡ ${tokenCount} tok · ${elapsed}s`;
          }, 250);
        }
        fullText += token;
        tokenCount++;
        thinking.innerHTML = (noteText ? `<div style="font-size:.75rem;color:var(--text-dim);margin-bottom:.4rem">ℹ️ ${escHtml(noteText)}</div>` : '') + mdToHtml(fullText);
        document.getElementById('ai-messages').scrollTop = 99999;
      }
    }

    clearInterval(statsTimer);
    if (_activeDot) _activeDot.classList.remove('model-dot--active');

    if (fullText && startTime) {
      const elapsedSec = (Date.now() - startTime) / 1000;
      const tps = elapsedSec > 0 ? (tokenCount / elapsedSec).toFixed(1) : '—';
      statsEl.textContent = `✓ ${tokenCount} tok · ${elapsedSec.toFixed(1)}s · ${tps}/s`;
      statsEl.style.color = 'var(--text-dim)';
    } else if (!fullText) {
      statsEl.textContent = '';
    }

    if (fullText) {
      history.push({ role: 'assistant', content: fullText });
      addSaveBtn(thinking.parentElement, fullText);
      addIllustrateBtn(thinking, thinking.parentElement, fullText);
      addSaveEntityBtn(thinking, thinking.parentElement, fullText);
      autoSave();
    }
  } catch (e) {
    thinking.parentElement.classList.remove('ai-thinking');
    if (e.name !== 'AbortError') {
      const msg = e.message || String(e);
      const isNet = msg.toLowerCase().includes('networkerror') || msg.toLowerCase().includes('fetch') || msg.toLowerCase().includes('failed to fetch');
      thinking.innerHTML = '<span style="color:#c44">⚠ ' + (isNet
        ? 'Cannot reach AI service — is Ollama running? Pull a model in the <strong>🤖 Models</strong> tab first.'
        : escHtml(msg)) + '</span>';
    }
  } finally {
    // Clean up any lingering timer / dot pulse
    if (typeof statsTimer !== 'undefined' && statsTimer) clearInterval(statsTimer);
    const _dotFinal = document.getElementById('dot-' + btoa(activeModel || '').replace(/=/g,''));
    if (_dotFinal) _dotFinal.classList.remove('model-dot--active');
    aiBar.className = 'done'; setTimeout(()=>{aiBar.style.display='none';aiBar.className='';},800);
    activeReader = null;
    btn.textContent = 'Send ↵';
    btn.classList.remove('stopping');
    btn.onclick = sendMessage;
    input.focus();
  }
}

// ── Background chat jobs ────────────────────────────────────────────────────
// An opt-in "process in background" alternative to the live-streamed send
// above — for a generation slow enough (a big local model, a long context)
// that keeping the tab open and connected isn't practical. Unlike a live
// send, nothing is added to the visible conversation/history until the GM
// clicks "Use this" on a finished job, mirroring the audio-attachment and
// image-generation background job panels' own "nothing happens until you
// explicitly use the result" convention.

let _cjJobs = [];
let _cjPollTimer = null;
const _CJ_JOB_IN_PROGRESS = new Set(['pending', 'generating']);
const _CJ_JOB_STATUS_LABEL = { pending: 'Queued…', generating: 'Generating…', done: '✓ Done', error: '✗ Failed', cancelled: 'Cancelled' };

async function sendMessageAsBackgroundJob() {
  const input = document.getElementById('ai-input');
  const btn = document.getElementById('ai-send-bg');
  const text = input.value.trim();
  const attachments = chatAttachments.take();
  if (!text && !attachments.length) return;
  input.value = '';
  autoResize(input);
  btn.disabled = true;
  try {
    const { messages, system } = await buildChatMessagesWithContext({ role: 'user', content: text, attachments });
    const res = await fetch('/api/ai/chat/jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages, system, model: activeModel, surface: 'chat', options: _chatPresetOptions,
      }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    await cjLoadJobs();
  } catch (e) {
    alert('Could not start background job: ' + (e.message || e));
    input.value = text; autoResize(input);
  } finally {
    btn.disabled = false;
  }
}

async function cjLoadJobs() {
  const panel = document.getElementById('chat-jobs-panel');
  if (!panel) return;
  try {
    _cjJobs = await fetch('/api/ai/chat/jobs').then(r => r.json());
  } catch (e) { return; }
  _cjRenderJobs();
  if (_cjPollTimer !== null) { clearTimeout(_cjPollTimer); _cjPollTimer = null; }
  if (_cjJobs.some(j => _CJ_JOB_IN_PROGRESS.has(j.status))) {
    _cjPollTimer = setTimeout(cjLoadJobs, 3000);
  }
}

function _cjRenderJobs() {
  const panel = document.getElementById('chat-jobs-panel');
  if (!panel) return;
  panel.innerHTML = '';
  if (!_cjJobs.length) { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  const title = document.createElement('div');
  title.className = 'nd-jobs-title';
  title.textContent = 'Background jobs';
  panel.appendChild(title);
  _cjJobs.forEach((job) => {
    const row = document.createElement('div');
    row.className = 'nd-job-row' + (job.status === 'error' ? ' nd-job-row--error' : '');
    const label = document.createElement('span');
    label.className = 'nd-job-label';
    const shortPrompt = (job.prompt || '').slice(0, 60) + ((job.prompt || '').length > 60 ? '…' : '');
    const cjBase = _CJ_JOB_STATUS_LABEL[job.status] || job.status;
    const cjLabel = _CJ_JOB_IN_PROGRESS.has(job.status) ? ndElapsedLabel(cjBase, job.created_at) : cjBase;
    label.textContent = `${shortPrompt || 'message'} — ${cjLabel}`;
    row.appendChild(label);
    if (job.status === 'error' && job.error) {
      const err = document.createElement('span');
      err.className = 'nd-job-error';
      err.textContent = job.error;
      row.appendChild(err);
    }
    if (job.status === 'done' && job.result) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'nd-job-use-btn';
      btn.textContent = 'Use this';
      btn.onclick = () => {
        addMessage('user', job.prompt, false);
        history.push({ role: 'user', content: job.prompt });
        const bubble = addMessage('assistant', job.result, false);
        history.push({ role: 'assistant', content: job.result });
        addSaveBtn(bubble.parentElement, job.result);
        addIllustrateBtn(bubble, bubble.parentElement, job.result);
        addSaveEntityBtn(bubble, bubble.parentElement, job.result);
        autoSave();
      };
      row.appendChild(btn);
    }
    if (_CJ_JOB_IN_PROGRESS.has(job.status)) {
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'nd-job-use-btn';
      cancelBtn.textContent = 'Cancel';
      cancelBtn.onclick = async () => {
        cancelBtn.disabled = true;
        try {
          await fetch('/api/ai/chat/jobs/' + job.id + '/cancel', { method: 'POST' });
        } finally {
          await cjLoadJobs();
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
          const res = await fetch('/api/ai/chat/jobs/' + job.id, { method: 'DELETE' });
          if (!res.ok) throw await ndApiErrorFrom(res);
        } catch (e) {
          alert('Could not delete job: ' + e.message);
          delBtn.disabled = false;
          return;
        }
        await cjLoadJobs();
      };
      row.appendChild(delBtn);
    }
    panel.appendChild(row);
  });
}

cjLoadJobs();

function quickPrompt(text) {
  const input = document.getElementById('ai-input');
  input.value = text;
  autoResize(input);
  input.focus();
}

// ── Quick Prompts library (GM-editable, per-world) ──────────────────────────
async function loadQuickPrompts() {
  const wrap = document.getElementById('quick-prompt-list');
  try {
    const d = await fetch('/api/ai/prompt-presets?scope=chat').then(r => r.json());
    const presets = d.presets || [];
    wrap.innerHTML = '';
    presets.forEach((p) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:.3rem';
      const btn = document.createElement('button');
      btn.className = 'quick-btn';
      btn.style.flex = '1';
      btn.textContent = (p.icon ? p.icon + ' ' : '') + p.label;
      btn.onclick = () => quickPrompt(p.text);
      const del = document.createElement('button');
      del.textContent = '✕';
      del.title = 'Delete this quick prompt';
      del.style.cssText = 'background:none;border:none;color:#c44;cursor:pointer;font-size:.8rem;display:none';
      del.onclick = () => deleteQuickPrompt(p.id);
      row.append(btn, del);
      row.dataset.presetId = p.id;
      wrap.appendChild(row);
    });
    _applyQuickPromptManagerVisibility();
  } catch (e) { /* quick prompts are optional — a failed fetch just leaves none available */ }
}

function _applyQuickPromptManagerVisibility() {
  const managing = document.getElementById('quick-prompt-manager').style.display !== 'none';
  document.querySelectorAll('#quick-prompt-list button[title="Delete this quick prompt"]').forEach((b) => {
    b.style.display = managing ? 'inline' : 'none';
  });
}

function toggleQuickPromptManager() {
  const el = document.getElementById('quick-prompt-manager');
  el.style.display = el.style.display === 'none' ? 'flex' : 'none';
  _applyQuickPromptManagerVisibility();
}

async function addQuickPrompt() {
  const label = document.getElementById('qp-label').value.trim();
  const text = document.getElementById('qp-text').value.trim();
  if (!label || !text) { alert('Enter both a label and a prompt.'); return; }
  const icon = document.getElementById('qp-icon').value.trim();
  try {
    const r = await fetch('/api/ai/prompt-presets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'chat', label, icon, text }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    document.getElementById('qp-icon').value = '';
    document.getElementById('qp-label').value = '';
    document.getElementById('qp-text').value = '';
    await loadQuickPrompts();
  } catch (e) {
    alert('Could not add quick prompt: ' + e.message);
  }
}

async function deleteQuickPrompt(id) {
  if (!confirm('Delete this quick prompt?')) return;
  await fetch('/api/ai/prompt-presets/' + id, { method: 'DELETE' });
  await loadQuickPrompts();
}

loadQuickPrompts();

function saveChat() {
  if (!history.length) return;
  const btn = document.getElementById('save-chat-btn');
  const worldName = WORLD_NAME;
  const date = new Date().toISOString().slice(0, 10);
  const lines = ['# AI Chat — ' + worldName + ' (' + date + ')\n'];
  for (const m of history) {
    lines.push(m.role === 'user' ? '**You:** ' + m.content : '**AI:** ' + m.content);
    if (m.attachments && m.attachments.length) {
      lines.push('*Attached: ' + m.attachments.map((a) => a.name).join(', ') + '*');
    }
    lines.push('');
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'chat-' + worldName.toLowerCase().replace(/\s+/g, '-') + '-' + date + '.md';
  a.click();
  URL.revokeObjectURL(a.href);
  const orig = btn.textContent;
  btn.textContent = '✓ Downloaded';
  setTimeout(() => { btn.textContent = orig; }, 1500);
}

function clearChat() {
  history = [];
  chatAttachments.take();  // drop any not-yet-sent pending attachments too
  const box = document.getElementById('ai-messages');
  box.innerHTML = '';
  addMessage('assistant', 'Chat cleared — ask me anything about your world.', false);
  const ctxEl = document.getElementById('ctx-status');
  ctxEl.style.color = 'var(--text-dim)';
  ctxEl.textContent = '○ Ready';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = el.scrollHeight + 'px';
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function switchTab(tab) {
  const chatPage      = document.querySelector('.ai-page');
  const imgPanel      = document.getElementById('image-panel');
  const modelsPanel   = document.getElementById('models-panel');
  const whisperPanel  = document.getElementById('whisper-panel');
  const starredPanel  = document.getElementById('starred-panel');

  chatPage.style.display     = tab === 'chat'    ? 'flex'  : 'none';
  imgPanel.style.display     = tab === 'image'   ? 'block' : 'none';
  modelsPanel.style.display  = tab === 'models'  ? 'block' : 'none';
  whisperPanel.style.display = tab === 'whisper' ? 'block' : 'none';
  starredPanel.style.display = tab === 'starred' ? 'block' : 'none';

  ['chat','image','models','whisper','starred'].forEach(t => {
    document.getElementById('tab-' + t)?.classList.toggle('active', t === tab);
  });

  if (tab === 'models')  { mpLoad(); wpLoadStatus(); }
  if (tab === 'whisper') { wtLoadStatus(); wlLoadLanguage(); wgLoadGlossary(); wdLoadDenoise(); riLoadInstructions(); }
  if (tab === 'starred') igLoadStarred();
  if (tab === 'image')   dlmLoadDownloaded();
}

