// ── Shared AI-assist panel behavior ────────────────────────────────────────
// Wired by each editor surface against its templates/_ai_assist_panel.html
// instance: the panel renders the controls, this file runs them, and the
// page supplies a cfg describing its fields:
//
//   ndAiAssist('ent', {
//     ops: ['improve', 'expand', 'summarize', 'suggest', 'translate', 'custom'],
//     surface: 'entity-form',          // rides onto the job row / logs only
//     contentSelector: '#body-field',  // the text ops work on
//     metaSelectors: { kind: '#kind-select', name: 'input[name="name"]',
//                      summary: 'input[name="summary"]', tags: 'input[name="tags"]' },
//     actions: ['replace', 'insert', 'copy'],   // result buttons (text ops)
//     job: false,                      // true = background-job variant (big content)
//     onData: (data) => {...},         // structured results (suggest/table_entries)
//   });
//
// All keys optional except ops. Defaults: content = the contentSelector's
// value (or meta fields when the op works without content), replace/insert
// target the content element, copy always available.

const ND_AA_OP_LABELS = {
  improve: '✨ Improve writing',
  expand: '➕ Expand draft',
  summarize: '📝 Write summary',
  suggest: '🏷 Suggest summary & tags',
  analyze: '🔍 Audit / analyze',
  translate: '🌐 Translate',
  custom: '🛠 Custom instruction',
  table_entries: '🎲 Generate entries',
  rules_rewrite: '📖 Rewrite rules (instruction)',
};

function ndAiAssist(pid, cfg) {
  const root = document.getElementById(pid + '-root');
  if (!root || root.dataset.init) return;
  root.dataset.init = '1';

  const $ = (suffix) => document.getElementById(pid + '-' + suffix);
  const opSel = $('op'), runBtn = $('run'), instr = $('instruction'),
        modelSel = $('model'), thinkCb = $('think'), ragCb = $('rag'),
        limits = $('limits'), entIn = $('ent'), notesIn = $('notes'),
        statusEl = $('status'), resultEl = $('result'),
        resultText = $('result-text'), resultData = $('result-data'),
        resultActions = $('result-actions');

  const val = (selOrFn) => {
    if (!selOrFn) return '';
    if (typeof selOrFn === 'function') return String(selOrFn() || '').trim();
    const el = document.querySelector(selOrFn);
    return el ? String(el.value || '').trim() : '';
  };

  const contentEl = () => {
    const sel = cfg.contentSelector;
    return sel ? document.querySelector(sel) : null;
  };
  // Pages whose content isn't a live form field (entity detail renders its
  // body as HTML — the page seeds a JS variable instead) pass getContent;
  // getMeta similarly overrides the metaSelectors mapping.
  const getContent = () => (cfg.getContent ? String(cfg.getContent() || '') : val(cfg.contentSelector));
  const getMeta = () => cfg.getMeta ? cfg.getMeta() : (() => {
    const m = cfg.metaSelectors || {};
    return { kind: val(m.kind), name: val(m.name), summary: val(m.summary), tags: val(m.tags) };
  })();

  // Op list
  (cfg.ops || []).forEach((op, i) => {
    const o = document.createElement('option');
    o.value = op;
    o.textContent = ND_AA_OP_LABELS[op] || op;
    opSel.appendChild(o);
  });
  if (cfg.defaultOp) opSel.value = cfg.defaultOp;

  ragCb.addEventListener('change', () => { limits.hidden = !ragCb.checked; });

  // Model picker — read-only catalog, same fetch the Facts/Sessions pickers use.
  fetch('/api/ai/models').then(r => r.json()).then(d => {
    (d.models || []).forEach(m => {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = (m.label || m.id) + (m.loaded ? '  ●' : '');
      modelSel.appendChild(o);
    });
  }).catch(() => {});

  const setStatus = (text, color) => {
    statusEl.textContent = text || '';
    statusEl.style.color = color || 'var(--text-dim)';
  };

  function insertAtCursor(el, text) {
    if (!el) return;
    const s = el.selectionStart ?? el.value.length;
    const e = el.selectionEnd ?? s;
    const pad = (s > 0 && el.value.slice(s - 1, s) !== '\n') ? '\n\n' : '';
    el.value = el.value.slice(0, s) + pad + text + el.value.slice(e);
    const at = s + pad.length + text.length;
    el.focus();
    el.selectionStart = el.selectionEnd = at;
  }

  function showTextResult(text) {
    resultData.innerHTML = '';
    resultText.textContent = text;
    resultText.style.display = '';
    resultActions.innerHTML = '';
    const actions = (cfg.actions && cfg.actions.length)
      ? cfg.actions
      : (cfg.contentSelector ? ['replace', 'insert', 'copy'] : ['copy']);
    actions.forEach(a => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ig-btn';
      if (a === 'replace') {
        b.textContent = '⬇ Replace';
        b.onclick = () => {
          const el = contentEl();
          if (el) { el.value = text; setStatus('Replaced.', '#4f4'); }
          else if (cfg.onReplace) cfg.onReplace(text);
        };
      } else if (a === 'insert') {
        b.textContent = '⤵ Insert';
        b.onclick = () => {
          const el = contentEl();
          if (el) { insertAtCursor(el, text); setStatus('Inserted.', '#4f4'); }
          else if (cfg.onInsert) cfg.onInsert(text);
        };
      } else {
        b.textContent = '📋 Copy';
        b.onclick = () => {
          navigator.clipboard.writeText(text).then(
            () => setStatus('Copied.', '#4f4'),
            () => setStatus('Copy failed — select and copy manually.', '#f55'),
          );
        };
      }
      resultActions.appendChild(b);
    });
    resultEl.hidden = false;
    resultEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function showDataResult(data) {
    resultText.style.display = 'none';
    resultActions.innerHTML = '';
    if (cfg.onData) {
      cfg.onData(data, { resultData, resultActions, setStatus });
      resultEl.hidden = false;
      return;
    }
    // Generic fallback: render key/value rows + a Copy-JSON button.
    resultData.innerHTML = '';
    Object.entries(data).forEach(([k, v]) => {
      const line = document.createElement('div');
      line.style.cssText = 'font-size:.8rem;color:var(--text);margin-bottom:.25rem';
      line.textContent = k + ': ' + (typeof v === 'object' ? JSON.stringify(v) : v);
      resultData.appendChild(line);
    });
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ig-btn';
    b.textContent = '📋 Copy JSON';
    b.onclick = () => navigator.clipboard.writeText(JSON.stringify(data, null, 2));
    resultActions.appendChild(b);
    resultEl.hidden = false;
  }

  function handleResult(result) {
    if (result.mode === 'data') { showDataResult(result.data || {}); }
    else { showTextResult(result.text || ''); }
    setStatus('Done (' + (result.model || '') + ').', '#4f4');
  }

  async function pollJob(jobId) {
    const started = Date.now();
    const MAX_MS = 40 * 60 * 1000;  // hard cap; the job page outlives the tab
    while (Date.now() - started < MAX_MS) {
      await new Promise(r => setTimeout(r, 3000));
      let d;
      try {
        const res = await fetch('/api/ai/assist-job/' + jobId);
        if (res.status === 404) throw new Error('job disappeared');
        d = await res.json();
      } catch (e) {
        setStatus('Polling failed (' + e.message + ') — retrying…', '#f80');
        continue;
      }
      if (d.status === 'done' && d.result) { handleResult(d.result); return; }
      if (d.status === 'error') { setStatus('Failed: ' + (d.error || 'unknown error'), '#f55'); return; }
      if (d.status === 'cancelled' || d.status === 'interrupted') {
        setStatus('Job was ' + d.status + ' — run again to retry.', '#f80');
        return;
      }
      setStatus('Working… (' + d.status + ')');
    }
    setStatus('Timed out waiting — check the Background Jobs page.', '#f80');
  }

  runBtn.addEventListener('click', async () => {
    const meta = getMeta();
    const payload = {
      op: opSel.value,
      surface: cfg.surface || 'assist',
      kind: meta.kind, name: meta.name, summary: meta.summary, tags: meta.tags,
      body: getContent(),
      instruction: instr.value.trim(),
      model: modelSel.value,
      think: thinkCb.checked,
      use_rag: ragCb.checked,
    };
    if (ragCb.checked) {
      payload.rag_entity_limit = parseInt(entIn.value, 10);
      payload.rag_notes_limit = parseInt(notesIn.value, 10);
    }
    resultEl.hidden = true;
    runBtn.disabled = true;
    setStatus('Working…');
    try {
      const endpoint = cfg.job ? '/api/ai/assist-job' : '/api/ai/assist';
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch (_) {}
        setStatus('Failed: ' + (detail || ('HTTP ' + res.status)), '#f55');
        return;
      }
      const d = await res.json();
      if (cfg.job) { pollJob(d.job_id); return; }
      handleResult(d);
    } catch (e) {
      setStatus('Error: ' + e.message, '#f55');
    } finally {
      runBtn.disabled = false;
    }
  });
}
