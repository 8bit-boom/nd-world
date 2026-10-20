// The GM session page's script for a saved session (transcript, media, recap audio, NPCs, live recording, prep, loot, XP); loaded after session-detail-common.js - moved out of app/templates/sessions/detail.html (docs/LEFTOVERS_PLAN.md, step 3) so the browser caches it. The page's own values
// (session id, title, prep, loot, ...) come from the <script type="application/json" id="nd-session-data"> block as ND_SESSION.
// The transcript (often hundreds of KB) is not embedded in the page: it is fetched once, after the page is up (efficiency plan, step 6).
let _liveTranscriptShown = false;
if (ND_SESSION.has_live_transcript) {
fetch('/sessions/' + ND_SESSION.id + '/transcript.md').then(r => r.ok ? r.text() : '').then(t => { if (!_liveTranscriptShown) liveSetTranscriptDisplay(t); }).catch(() => {});
} else {
liveSetTranscriptDisplay('');
}

// ── Embedded Facts panel — see the "🗒 Facts" section below. Shares its
// Recap→Facts parse/draft-review logic with the standalone /facts page via
// facts-recap-parser.js (fixedSessionId means every parse/save from this
// panel is scoped to THIS session, unlike /facts's own session picker).
function toggleFactEdit(id) {
  const view = document.getElementById('fact-view-' + id);
  const form = document.getElementById('fact-edit-' + id);
  const editing = form.style.display !== 'none';
  form.style.display = editing ? 'none' : 'block';
  view.style.display = editing ? '' : 'none';
}

// This panel's own list is already scoped to one session (no groups to
// collapse/expand the way /facts's world-wide list needs), so filtering is
// just show/hide by text match — a tag chip click reuses it exactly like
// /facts's factsFilterByTag does with factsSearch.
function sessionFactsSearch(v) {
  const q = (v || '').trim().toLowerCase();
  const countEl = document.getElementById('session-facts-search-count');
  const cards = document.querySelectorAll('[data-facts-card]');
  if (!q) {
    cards.forEach(c => { c.style.display = ''; });
    if (countEl) { countEl.style.display = 'none'; countEl.textContent = ''; }
    return;
  }
  let matches = 0;
  cards.forEach(card => {
    const hit = card.textContent.toLowerCase().indexOf(q) >= 0;
    card.style.display = hit ? '' : 'none';
    if (hit) matches++;
  });
  if (countEl) {
    countEl.style.display = '';
    countEl.textContent = matches === 1 ? '1 fact matches' : matches + ' facts match';
  }
}

function sessionFactsFilterByTag(tag) {
  const input = document.getElementById('session-facts-search');
  input.value = tag;
  sessionFactsSearch(tag);
  input.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

ndFactsRecapParser({ fixedSessionId: ND_SESSION.id, enableHandoff: true }).init();
// ── Session media (app/routers/session_media.py, app/routers/session_audio.py) ──────────────────────────────
const RA_BASE = '/api/sessions/' + ND_SESSION.id + '/recap-audio';
const SM_BASE = '/api/sessions/' + ND_SESSION.id + '/media';
// ✏ beside an attached item: name it, and the Session Log shows players that name.
function smRenameButton(current, label, url) {
  const b = document.createElement('button');
  b.type = 'button'; b.textContent = '✏'; b.title = 'Rename'; b.setAttribute('aria-label', 'Rename');
  b.style.cssText = 'min-width:32px;min-height:32px;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:4px;cursor:pointer';
  b.addEventListener('click', async () => {
    const next = prompt(label, current);
    if (next === null || next.trim() === (current || '').trim()) return;
    try { raRender(await raCall(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: next }) })); raStatus('Renamed.'); }
    catch (e) { raStatus('Failed: ' + e.message); }
  });
  return b;
}

let smLast = null;                       // the last panel the server sent: what is attached, and how much room is left

function raStatus(msg) { document.getElementById('ra-status').textContent = msg || ''; }

async function raCall(url, opts) {
  const res = await ndAiFetch(url, opts);
  if (!res.ok) throw await ndApiErrorFrom(res);
  return res.json();
}

const SM_IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif'];
const SM_VIDEO_EXTS = ['.mp4', '.m4v', '.webm', '.ogv', '.mov', '.mkv', '.avi', '.ts', '.wmv', '.flv', '.mpg', '.mpeg'];
const SM_AUDIO_EXTS = ['.mp3', '.ogg', '.oga', '.wav', '.m4a', '.flac', '.opus', '.webm', '.aac'];
// What a dropped file is: by its extension, and for the few that fit two (.webm) by the type the browser reports.
function smKindOf(file) {
  const m = /\.[^.]+$/.exec(file.name || '');
  const ext = m ? m[0].toLowerCase() : '';
  const isImage = SM_IMAGE_EXTS.includes(ext), isVideo = SM_VIDEO_EXTS.includes(ext), isAudio = SM_AUDIO_EXTS.includes(ext);
  if (isImage) return 'image';
  if (isVideo && isAudio) return (file.type || '').startsWith('audio/') ? 'audio' : 'video';
  if (isVideo) return 'video';
  if (isAudio) return 'audio';
  return '';
}

function smThumb(url) {                   // the small preview the gallery writes beside every upload, else the picture itself
  return url.replace(/\.[^./]+$/, '') + '_thumb.webp';
}

function smSwitch(labelText, checked, onChange) {
  const label = document.createElement('label');
  label.style.cssText = 'display:inline-flex;gap:.3rem;align-items:center;font-size:.76rem;color:var(--text-dim);cursor:pointer';
  const box = document.createElement('input');
  box.type = 'checkbox'; box.checked = !!checked; box.style.width = 'auto';
  box.addEventListener('change', () => onChange(box));
  label.append(box, document.createTextNode(labelText));
  return label;
}

function smRemoveButton(title, onClick) {
  const rm = document.createElement('button');
  rm.type = 'button'; rm.className = 'ai-recap-btn'; rm.textContent = '✕ Remove'; rm.title = title;
  rm.addEventListener('click', onClick);
  return rm;
}

async function smSetVisible(url, box, okText) {
  raStatus('Saving…');
  try {
    raRender(await raCall(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify({ visible_to_players: box.checked }) }));
    raStatus(box.checked ? okText : 'Hidden from players.');
  } catch (e) { box.checked = !box.checked; raStatus('Failed: ' + e.message); }
}

function raRender(d) {
  smLast = d;
  const room = Math.max(0, (d.max || 0) - (d.used || 0));
  const count = document.getElementById('sm-count');
  count.textContent = (d.used || 0) + ' of ' + (d.max || 0) + ' used';
  count.style.color = room ? '' : '#e0b050';
  document.getElementById('sm-drop').style.opacity = room ? '' : '.55';

  // pictures
  const images = document.getElementById('sm-images');
  images.textContent = '';
  (d.images || []).forEach(im => {
    const card = document.createElement('div');
    card.style.cssText = 'border:1px solid var(--border);border-radius:5px;padding:.4rem;background:var(--bg3)';
    const img = document.createElement('img');
    img.src = smThumb(im.url); img.alt = im.caption || 'Session picture'; img.loading = 'lazy';
    img.onerror = () => { img.onerror = null; img.src = im.url; };
    img.style.cssText = 'width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:3px;cursor:zoom-in;display:block';
    img.addEventListener('click', () => openLightbox(im.url, img.alt));
    const cap = document.createElement('div');
    cap.textContent = im.caption || ''; cap.style.cssText = 'font-size:.78rem;margin:.3rem 0;word-break:break-word';
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;justify-content:space-between;gap:.4rem;align-items:center;flex-wrap:wrap';
    row.append(
      smSwitch('players can see', im.visible_to_players, box => smSetVisible(SM_BASE + '/image/' + im.uid + '/visibility', box, 'Players can now see it.')),
      smRemoveButton(im.own ? 'Remove it from this session — the picture is deleted too' : 'Take it off this session — it stays in the gallery', async () => {
        try { raRender(await raCall(SM_BASE + '/image/' + im.uid + '/remove', { method: 'POST' })); raStatus(''); }
        catch (e) { raStatus('Failed: ' + e.message); }
      }));
    row.prepend(smRenameButton(im.caption || '', 'Caption (players see it in the Session Log)', SM_BASE + '/image/' + im.uid + '/rename'));
    card.append(img, cap, row);
    images.appendChild(card);
  });

  // videos
  const videos = document.getElementById('sm-videos');
  videos.textContent = '';
  (d.videos || []).forEach(v => {
    const row = document.createElement('div');
    row.style.cssText = 'padding:.45rem 0;border-bottom:1px solid var(--border)';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-bottom:.3rem;font-size:.85rem';
    const name = document.createElement('strong');
    name.textContent = '🎬 ' + v.name;
    head.append(
      name, smRenameButton(v.name, 'Name (players see it in the Session Log and the library)', SM_BASE + '/video/' + v.id + '/rename'),
      smSwitch('players can watch this (also in the Video library)', v.visible_to_players,
               box => smSetVisible(SM_BASE + '/video/' + v.id + '/visibility', box, 'Players can now watch it.')),
      smRemoveButton('Detach it from this session — the video stays in the Video library', async () => {
        try { raRender(await raCall(SM_BASE + '/video/' + v.id + '/remove', { method: 'POST' })); raStatus(''); }
        catch (e) { raStatus('Failed: ' + e.message); }
      }));
    const video = document.createElement('video');
    video.controls = true; video.preload = 'none'; video.src = v.file_url;
    if (v.poster_url) video.poster = v.poster_url;
    video.style.cssText = 'width:100%;max-height:340px;background:#000;border-radius:4px';
    row.append(head, video);
    videos.appendChild(row);
  });

  // audio (as it always was)
  const list = document.getElementById('ra-list');
  list.textContent = '';
  (d.clips || []).forEach(c => {
    const row = document.createElement('div');
    row.style.cssText = 'padding:.45rem 0;border-bottom:1px solid var(--border)';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-bottom:.3rem;font-size:.85rem';
    const name = document.createElement('strong');
    name.textContent = '🎵 ' + c.name;
    head.append(
      name, smRenameButton(c.name, 'Name (players see it in the Session Log and the library)', SM_BASE + '/audio/' + c.id + '/rename'),
      smSwitch('players can hear this (also in the Audio library)', c.visible_to_players,
               box => smSetVisible(RA_BASE + '/' + c.id + '/visibility', box, 'Players can now hear it.')),
      smRemoveButton('Detach it from this session — the clip stays in the Audio library', async () => {
        try { raRender(await raCall(RA_BASE + '/' + c.id + '/remove', { method: 'POST' })); raStatus(''); }
        catch (e) { raStatus('Failed: ' + e.message); }
      }));
    const audio = document.createElement('audio');
    audio.controls = true; audio.preload = 'none'; audio.src = c.file_url; audio.style.width = '100%';
    row.append(head, audio);
    if (c.transcript) {
      const det = document.createElement('details');
      const sum = document.createElement('summary');
      sum.textContent = 'Lyrics / text'; sum.style.cssText = 'cursor:pointer;font-size:.76rem;color:var(--text-dim)';
      const pre = document.createElement('pre');
      pre.textContent = c.transcript; pre.style.cssText = 'white-space:pre-wrap;font-family:var(--font);font-size:.82rem;margin:.3rem 0 0';
      det.append(sum, pre);
      row.appendChild(det);
    }
    list.appendChild(row);
  });
  if (!(d.used || 0)) {
    const none = document.createElement('div');
    none.style.cssText = 'font-size:.8rem;color:var(--text-dim);margin-top:.4rem';
    none.textContent = 'Nothing attached yet.';
    list.appendChild(none);
  }
}

async function raLoad() {
  try { raRender(await raCall(SM_BASE)); } catch (e) { raStatus('Could not load: ' + e.message); }
}

// Dropped (or chosen) files, one after another: each goes to the server with its kind worked out here and checked there.
// Big files are cut into parts by ndChunkedUpload, as everywhere else in the app. A file that would not fit under the
// limit is not even sent - the server would refuse it anyway, after the bytes had travelled.
let smBusy = false;
async function smDropFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length || smBusy) return;
  smBusy = true;
  const progress = document.getElementById('sm-progress');
  progress.textContent = '';
  const line = text => { const el = document.createElement('div'); el.textContent = text; progress.appendChild(el); return el; };
  let room = smLast ? Math.max(0, smLast.max - smLast.used) : Infinity;
  const visible = document.getElementById('sm-visible').checked;
  let added = 0;
  try {
    for (const file of files) {
      const kind = smKindOf(file);
      if (!kind) { line('✕ ' + file.name + ' — not a picture, video or audio file.'); continue; }
      if (room <= 0) { line('✕ ' + file.name + ' — the session is full (' + (smLast ? smLast.max : '?') + ' items; raise the limit in Settings > System).'); continue; }
      const el = line('⬆ ' + file.name + ' …');
      try {
        const d = await ndChunkedUpload(file, {
          directUrl: SM_BASE + '/upload', chunkUrl: SM_BASE + '/upload/chunk', completeUrl: SM_BASE + '/upload/complete',
          extraFields: { name: file.name.replace(/\.[^.]+$/, ''), kind: kind, visible_to_players: visible ? '1' : '' },
          onProgress: p => { el.textContent = '⬆ ' + file.name + (p.phase === 'upload' ? ' — ' + p.percent + '%' : ' — processing…'); },
        });
        raRender(d);
        room--; added++;
        el.textContent = '✓ ' + file.name;
      } catch (e) {
        el.textContent = '✕ ' + file.name + ' — ' + e.message;
      }
    }
  } finally {
    smBusy = false;
  }
  raStatus(added ? (visible ? added + ' added — players can see them.' : added + ' added — tick “players can see” on each to share it.') : '');
}

(function smWireDropZone() {
  const panel = document.getElementById('session-media-panel');
  const zone = document.getElementById('sm-drop');
  const input = document.getElementById('sm-file');
  if (!panel || !zone || !input) return;
  const hasFiles = e => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  const on = () => { zone.style.borderColor = 'var(--neon)'; zone.style.background = 'var(--bg3)'; };
  const off = () => { zone.style.borderColor = ''; zone.style.background = ''; };
  let depth = 0;                         // dragenter/leave fire for every child element: count them
  panel.addEventListener('dragenter', e => { if (hasFiles(e)) { e.preventDefault(); depth++; on(); } });
  panel.addEventListener('dragover', e => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  panel.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; off(); } });
  panel.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; off();
    smDropFiles(e.dataTransfer.files);
  });
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  input.addEventListener('change', () => { smDropFiles(input.files); input.value = ''; });

  // Ctrl+V: the copied ADDRESS of a picture / audio / video that is already in this world ("Copy" with Ctrl+C on it in the gallery or a
  // library) attaches that item - no second copy is uploaded; otherwise a copied picture, a screenshot or copied files are uploaded.
  // Text going into a text box is left alone. (static/js/nd-clipboard.js does the same for every other page's upload field.)
  document.addEventListener('paste', async e => {
    const cd = e.clipboardData;
    if (!cd) return;
    const t = e.target, editable = !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    const text = (cd.getData && cd.getData('text/plain') || '').trim();
    const files = Array.from(cd.files || []);
    if (editable && text) return;                                  // ordinary text paste into a field
    const addr = window.ndClipboard ? ndClipboard.addressOf(text, location.origin) : null;
    if (addr) {
      e.preventDefault();
      raStatus('Attaching the pasted ' + addr.kind + '…');
      try {
        raRender(await raCall(SM_BASE + '/attach', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                     body: JSON.stringify({ kind: addr.kind, url: addr.path }) }));
        raStatus('Attached. Tick “players can ' + (addr.kind === 'image' ? 'see' : addr.kind === 'video' ? 'watch this' : 'hear this') + '” to share it.');
      } catch (err) { raStatus('Could not attach it: ' + err.message); }
      return;
    }
    if (files.length) {
      e.preventDefault();
      const stamp = new Date();
      const named = files.map((f, i) => new File([f], window.ndClipboard ? ndClipboard.pastedName(f.name, f.type, stamp, i, files.length) : (f.name || 'pasted'), { type: f.type }));
      raStatus('Pasting ' + named.length + ' file' + (named.length === 1 ? '' : 's') + '…');
      smDropFiles(named);
    }
  });
})();

async function aiPerformTale() {
  const lyrics = document.getElementById('tale-result-text').textContent.trim();
  if (!lyrics) return;
  const btn = document.getElementById('tale-perform-btn');
  const status = document.getElementById('tale-perform-status');
  btn.disabled = true;
  status.textContent = 'The voice is singing… (this can take a minute)';
  try {
    const d = await raCall('/api/sessions/' + ND_SESSION.id + '/recap-song', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lyrics: lyrics, name: document.getElementById('tale-name-field').value.trim(),
                             style: document.getElementById('tale-style').value }) });
    raRender(d);
    status.textContent = '✓ Attached to the recap audio below — listen, then tick “players can hear this”.';
    document.getElementById('recap-audio-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', raLoad);

// "Summarize from Facts" runs through the session-log recap JOB pipeline
// (POST /api/session-log/{id}/recap — audience resolves to "gm" for this
// page's caller) instead of the old blocking summarize-from-facts request:
// a think=True weave against a CPU-local model runs minutes, which is a
// routine Cloudflare 524 (the ~100s proxy timeout) waiting to happen, and
// the old route had no sentinel check — a failed call handed back its
// "[AI unavailable: ...]" string as a draft the Apply button would happily
// paste into the Summary field. The pipeline is sentinel-safe end to end
// (a failed generation lands as an errored job the route refuses to serve)
// and this loop loads the finished recap into the exact same review/Replace
// affordance aiRunRecap uses, so Apply/Discard behavior is unchanged.
async function aiSummarizeFromFacts() {
  const status = document.getElementById('ai-recap-status');
  const modelSel = document.getElementById('audio-bgjob-model');
  const POLL_MS = 4000;
  const POLL_CAP_MS = 10 * 60 * 1000; // give up (with an explanation) after ~10 minutes
  status.textContent = 'Summarizing from facts…';
  _aiRecapButtons().forEach(b => b.disabled = true);
  try {
    let waited = 0;
    while (true) {
      const res = await fetch('/api/session-log/' + ND_SESSION.id + '/recap', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        // The GM's panel options: Thinking, the background-job model
        // picker, and the RAG opt-in (the pipeline retrieves lore for name
        // accuracy the same way Condense/Process in Background do).
        body: JSON.stringify({
          think: _recapThinkEnabled(),
          model: modelSel ? modelSel.value : '',
          ..._recapRagOptions(),
        }),
      });
      if (!res.ok) throw await ndApiErrorFrom(res);
      const data = await res.json();
      if (data.pending) {
        if (waited >= POLL_CAP_MS) {
          status.textContent = 'The recap is still generating after 10 minutes — it may be stuck. Check Background Jobs below.';
          return;
        }
        status.textContent = 'Summarizing from facts in the background… (this can take a few minutes on a local model)';
        await new Promise(r => setTimeout(r, POLL_MS));
        waited += POLL_MS;
        continue;
      }
      if (data.failed) {
        // The generation errored server-side (its sentinel never gets
        // cached as a done recap). Show the error through the same
        // retry-style panel _showRecapFailed uses for a summarize-from-
        // audio failure — never offer the error text as an applyable
        // draft. Returning stops the loop; the route's short backoff
        // window means a deliberate Retry only comes from pressing the
        // button again after fixing the underlying issue (usually Ollama).
        _aiRecapDraft = '';
        _showRecapFailed(data.error || '(no error detail — check Background Jobs)');
        status.textContent = '';
        return;
      }
      _aiRecapDraft = data.recap || '';
      if (!_aiRecapDraft) {
        status.textContent = data.empty ? 'No facts logged for this session yet.' : 'No recap produced.';
        return;
      }
      // _showRecapDraft (not a bare _setRecapPreviewText+display) so the
      // Apply/Retry button visibility always resets here too — same
      // reasoning as aiRunRecap's own call.
      _showRecapDraft(_aiRecapDraft);
      status.textContent = '';
      return;
    }
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    _aiRecapButtons().forEach(b => b.disabled = false);
  }
}

// ── Player recap (the publish model) ────────────────────────────────────
// "Use AI recap" runs the SAME session-log recap job the Session Log's own
// fallback runs (POST /api/session-log/{id}/recap), but lands the finished
// draft in the player-summary field for the GM to edit — the publish model
// exists precisely so the GM curates what players read rather than
// consuming raw model output. "Save & Publish" posts text + the published
// flag in one form post; the Session Log then serves the published text
// verbatim with no AI involved.
async function playerRecapUseAi() {
  const status = document.getElementById('player-recap-status');
  const field = document.getElementById('player-summary-field');
  const modelSel = document.getElementById('audio-bgjob-model');
  const POLL_MS = 4000;
  const POLL_CAP_MS = 10 * 60 * 1000;
  status.textContent = 'Generating the AI recap…';
  let waited = 0;
  try {
    while (true) {
      const res = await fetch('/api/session-log/' + ND_SESSION.id + '/recap', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          think: _recapThinkEnabled(),
          model: modelSel ? modelSel.value : '',
          ..._recapRagOptions(),
        }),
      });
      if (!res.ok) throw await ndApiErrorFrom(res);
      const data = await res.json();
      if (data.pending) {
        if (waited >= POLL_CAP_MS) {
          status.textContent = 'Still generating after 10 minutes — check Background Jobs, then try again.';
          return;
        }
        status.textContent = 'Generating in the background…';
        await new Promise(r => setTimeout(r, POLL_MS));
        waited += POLL_MS;
        continue;
      }
      if (data.failed) {
        status.textContent = 'Recap generation failed: ' + (data.error || 'check Background Jobs');
        return;
      }
      if (!data.recap) {
        status.textContent = data.empty
          ? 'No facts logged for this session yet — write the recap yourself or add facts first.'
          : 'No recap produced.';
        return;
      }
      field.value = data.recap;
      status.textContent = 'AI recap loaded — edit it if you like, then Save & Publish.';
      return;
    }
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  }
}

function playerRecapPublish(publish) {
  const field = document.getElementById('player-summary-field');
  const status = document.getElementById('player-recap-status');
  const fd = new FormData();
  fd.append('player_summary', field.value);
  if (publish) fd.append('publish', '1');
  fetch('/sessions/' + ND_SESSION.id + '/recap-publish', { method: 'POST', body: fd }).then(res => {
    if (res.ok) location.reload();
    else res.json().then(d => { status.textContent = d.detail || 'Failed to save.'; }).catch(() => { status.textContent = 'Failed to save.'; });
  });
}

// ── NPCs Featured picker: folder tree + live search ─────────────────────
// Same "flat folder-path strings -> nested tree" approach as
// entities/list.html's buildFolderTree/renderFolderNode, adapted to render
// checkbox rows instead of folder-count cards, embedded directly in this
// form rather than a full page — the search filters the already-rendered
// DOM client-side (no reload) so the rest of the recap form's unsaved
// state (Summary, etc.) is never disturbed while picking NPCs.
// The candidate list (every entity of the world) is NOT embedded in the page - it is fetched the first time the picker is used.
// A PlayerCharacter pick's id is "pc:N" (see _PC_ID_PREFIX in
// routers/sessions.py) — computed server-side (npc_selected_json), not
// via a plain map(attribute='entity_id') here, since that alone can't
// distinguish a PlayerCharacter pick from an Entity one.
const NPC_SELECTED = ND_SESSION.npc_selected;

let _npcLoad = null;
function npcPickerLoad() {
  if (_npcLoad) return _npcLoad;
  const tree = document.getElementById('npc-picker-tree');
  tree.textContent = 'Loading…';
  _npcLoad = fetch('/sessions/' + ND_SESSION.id + '/npc-candidates' + ND_SESSION.npc_query).then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))).then(d => {
    tree.textContent = '';
    const hidden = document.getElementById('npc-hidden'); if (hidden) hidden.remove();      // the real checkboxes take over from the hidden copies
    ndEntityPicker(tree, document.getElementById('npc-search-input'), {
      entities: d.candidates, mode: 'multi', selected: NPC_SELECTED, fieldName: 'npc_entity_ids',
      emptyText: 'No eligible entities in this world yet.',
    });
  }).catch(e => { _npcLoad = null; tree.textContent = '⚠ Could not load the list (' + e.message + ') - click to retry. Your saved picks are kept.'; });
  return _npcLoad;
}
(function () {
  const tree = document.getElementById('npc-picker-tree'), q = document.getElementById('npc-search-input');
  tree.addEventListener('click', npcPickerLoad);
  q.addEventListener('focus', npcPickerLoad);
  q.addEventListener('input', npcPickerLoad);          // typing before the list arrives: the search is applied once it is built (entity-picker wires the input itself)
})();

// QoL 3.4 — deterministic exact-name-match scan of the Summary field
// against every candidate's name, offered as a one-click "check all
// mentioned" shortcut. Reads .entity-picker-row's own lowercased
// dataset.name (already computed by entity-picker.js) rather than
// re-deriving it from NPC_CANDIDATES, so it can't drift from what's
// actually rendered. Deliberately dumb (whole-word substring match, no
// AI) — the value here is speed and 100% predictability for names that
// did get typed verbatim, not fuzzy recall.
async function checkMentionedEntities() {
  await npcPickerLoad();
  const text = (document.getElementById('summary-field').value || '').toLowerCase();
  if (!text.trim()) { alert('Summary is empty — nothing to scan.'); return; }
  let matched = 0;
  document.querySelectorAll('#npc-picker-tree .entity-picker-row').forEach((row) => {
    const cb = row.querySelector('input[type=checkbox]');
    const name = row.dataset.name;
    if (!cb || !name || cb.checked) return;
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (new RegExp('\\b' + escaped + '\\b', 'i').test(text)) {
      cb.checked = true;
      matched++;
    }
  });
  alert(matched
    ? `✓ Checked ${matched} mentioned entit${matched === 1 ? 'y' : 'ies'}.`
    : 'No new exact name matches found in the summary.');
}
const SESSION_ID = ND_SESSION.id;
let prep = ND_SESSION.prep;
let loot = ND_SESSION.loot;

function esc(s) { return String(s).replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

// ── Live session recording — records in short chunks (length GM-configurable
// via #live-chunk-seconds above; the segments-stay-separate-complete-containers
// reasoning lives in liveStartSegment below), uploading each one to
// /api/sessions/{id}/live-transcript/append as soon as it finishes and
// immediately starting the next segment, so a multi-hour session never
// holds more than one chunk's worth of unsaved audio at once. Uploads are
// processed through a sequential queue (one at a time, in order) rather
// than fired in parallel, both so the transcript can't end up scrambled
// out of order and so a Studio backend that's slower than
// realtime just falls behind gracefully instead of being hit with a burst
// of concurrent requests.

// Longer chunks mean fewer chunk boundaries, and chunk boundaries are where
// the transcription artifacts live: speech-model hallucination loops (music and
// silence around a seam feed them), duplicated passages straddling a seam,
// repeated-name loops — a real 3.5-hour session at the old fixed 60s produced
// pages of "Редактор субтитров…" spam. Against that, a longer chunk means the
// transcript lags further behind the table and a failed upload puts more
// audio at risk before its retry — hence a control rather than a new
// constant. Read fresh at EVERY segment start (never cached at recording
// start), so a change applies from the next chunk, mid-recording included.
// The value persists in localStorage and is validated against the select's
// own options, so a stale/hand-edited entry falls back to the 60s default.
function liveChunkSeconds() {
  const v = parseInt(localStorage.getItem('liveChunkSeconds'), 10);
  return [60, 120, 180, 300, 600, 900].includes(v) ? v : 60;
}
let _liveRecording = false;
let _liveCurrentRecorder = null;
let _liveChunkTimer = null;
// Minted ONCE per recording (in toggleLiveRecording's Start branch, via
// chunked-upload.js's ndChunkUploadRandomId — the same 32-hex shape the
// server's chunked-upload endpoints validate) and stamped onto every segment
// as it's captured, so the server can keep the opt-in raw-audio archive of
// this recording grouped and in order even when a failed chunk is retried
// long after later chunks uploaded fine. Together with the per-segment index
// below it doubles as the idempotency key for the server's save path: a
// retry of the same segment overwrites its file instead of duplicating it.
let _liveRecordingId = null;
// Zero-based position of the NEXT segment to be captured this recording —
// read-and-incremented at capture time (not upload time) so a chunk that
// fails all 3 upload attempts and sits in _liveFailedChunks keeps its
// original index for the retry.
let _liveSegmentIndex = 0;

// Restore + persist the two recording controls (this script sits at the end
// of <body>, so the elements exist). Read on load and written on every
// change — nothing here is captured at recording start, matching how
// liveChunkSeconds() is re-read per segment.
const saveChk = document.getElementById('live-save-audio');
saveChk.checked = localStorage.getItem('liveSaveAudio') !== '0';  // absent = never chosen = the checked default
saveChk.addEventListener('change', () => {
  localStorage.setItem('liveSaveAudio', saveChk.checked ? '1' : '0');
});
const chunkSel = document.getElementById('live-chunk-seconds');
chunkSel.value = String(liveChunkSeconds());
chunkSel.addEventListener('change', () => {
  localStorage.setItem('liveChunkSeconds', chunkSel.value);
});
// Acquired ONCE in toggleLiveRecording's Start branch and reused by every
// segment's ndMicRecorder (see its own comment in base.html for why:
// re-acquiring the mic every segment cost an audible gap, a flickering mic
// indicator, and could silently kill the recording on mobile Safari).
// Released in liveStopMicStream, called once the final segment's onStop
// fires (not immediately on Stop — the in-flight segment still needs the
// stream alive to finish flushing its last chunk).
let _liveMicStream = null;

function liveStopMicStream() {
  liveStopMeter();
  if (_liveMicStream) {
    _liveMicStream.getTracks().forEach(t => t.stop());
    _liveMicStream = null;
  }
}
let _liveQueue = [];
let _liveQueueBusy = false;

// ── Honest counters (static/js/live-health.js, tests/test_live_health.py) ──
// Audio captured / transcribed this page load, in seconds, from each chunk's REAL length. The status line used to say
// "saved" the instant Stop was pressed, while the last chunk had not even been cut and a whole backlog could still be
// waiting in this tab.
let _liveRecordedSeconds = 0;
let _liveDoneSeconds = 0;
let _liveFinalizing = false;        // Stop pressed, the last chunk not yet cut and queued
let _liveSegStartedAt = 0;          // when the chunk being recorded right now began (0 between chunks / while the mic is lost)
let _liveSpeed = null;              // audio seconds the speech model gets through per second of waiting
let _liveSayUntil = 0;              // a transient message ("retrying in 20 s…") owns the status line until then
let _liveGaps = [];                 // [{at, seconds}] stretches that were not recorded (computer asleep, mic gone)
let _liveOffline = false;           // the last upload could not reach the server at all (no internet, Wi-Fi gone, VPN down)
let _liveOfflineSince = 0, _liveOfflineAlerted = false;
let _liveUploadAbort = null;        // aborts the upload in flight when the browser goes offline (it would hang until a TCP timeout)
const _liveOrigTitle = document.title;
const _liveStore = window.ndLiveStore ? ndLiveStore.shared : null;   // a local copy of every chunk until the server has it

let _liveSayText = '';
function liveSay(text, holdMs) {
  document.getElementById('live-record-status').textContent = text;
  _liveSayText = text;
  _liveSayUntil = Date.now() + (holdMs || 0);
}

// The one moment worth a loud, positive message: Stop was pressed and EVERYTHING recorded is now transcribed and saved
// on the server. If part of the time was not recorded (computer asleep, mic gone) that is said here too - it is the
// last thing the GM sees before closing the tab.
let _liveDoneAnnounced = true;
function liveAnnounceIfDone() {
  if (_liveRecording || _liveFinalizing || _liveQueue.length || _liveQueueBusy || _liveFailedChunks.length) return;
  if (_liveDoneAnnounced || _liveRecordedSeconds <= 0) return;
  _liveDoneAnnounced = true;
  let text = ndLiveHealth.summarize(liveHealthState()).text.replace(/^✔\s*/, '');
  if (_liveGaps.length) {
    const lost = _liveGaps.reduce((sum, g) => sum + g.seconds, 0);
    liveRaiseAlert('✔ ' + text + ' BUT ' + ndLiveHealth.clock(lost) + ' in ' + _liveGaps.length + ' stretch(es) was NOT recorded (this computer was paused or the microphone was lost).', { beep: false });
  } else {
    liveRaiseAlert('✔ ' + text, { good: true });
  }
}

function liveHealthState() {
  const secs = list => list.reduce((sum, f) => sum + (f.ndSeconds || 0), 0);
  return {
    recording: _liveRecording, finalizing: _liveFinalizing, doneSec: _liveDoneSeconds,
    // what is on the clock: chunks already cut plus the one still being recorded (up to a whole chunk length)
    recordedSec: _liveRecordedSeconds + ((_liveRecording || _liveFinalizing) && _liveSegStartedAt && !_liveRecovering
      ? Math.max(0, Math.round((Date.now() - _liveSegStartedAt) / 1000)) : 0),
    queuedSec: secs(_liveQueue), chunksWaiting: _liveQueue.length, uploading: _liveQueueBusy,
    offline: _liveOffline || navigator.onLine === false,
    failedChunks: _liveFailedChunks.length, failedSec: secs(_liveFailedChunks), speed: _liveSpeed,
  };
}
// Chunks that failed all 3 upload attempts — kept (not discarded) so the GM
// can retry them instead of silently losing ~1min of transcript. Retried
// chunks append at the current end of the queue, so the transcript may gain
// them out of chronological order relative to whatever was recorded after
// the failure — better a late paragraph than permanent silence.
let _liveFailedChunks = [];
// Why the most recent chunk failed (the server's own message — "STT model
// 'small' is not downloaded…", "Studio API key rejected…") so the status line
// can say what to fix instead of only "failed to upload".
let _liveLastFailure = '';
let _liveSlowStreak = 0;

// ── Loud alerts: a recording that stops itself, or chunks that cannot be sent, must not hide in small grey text ──
let _liveAudioCtx = null;
let _liveAlertActive = false;
function liveBeep() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    _liveAudioCtx = _liveAudioCtx || new AC();
    if (_liveAudioCtx.state === 'suspended') _liveAudioCtx.resume().catch(() => {});
    [0, 0.35].forEach(delay => {
      const osc = _liveAudioCtx.createOscillator(), gain = _liveAudioCtx.createGain();
      osc.frequency.value = 880; gain.gain.value = 0.15;
      osc.connect(gain); gain.connect(_liveAudioCtx.destination);
      osc.start(_liveAudioCtx.currentTime + delay); osc.stop(_liveAudioCtx.currentTime + delay + 0.22);
    });
  } catch (e) { /* a sound is a bonus */ }
}
function liveRaiseAlert(text, opts) {
  opts = opts || {};
  const el = document.getElementById('live-alert');
  if (!el) return;
  el.textContent = text;
  el.style.background = opts.good ? '#1f5a34' : '#8a1c2b';
  el.style.borderColor = opts.good ? '#4c8' : '#e55';
  el.style.display = '';
  _liveAlertActive = !opts.good;
  document.title = (opts.good ? '✔ ' : '⚠ ') + text.slice(0, 70) + ' — ' + _liveOrigTitle;
  if (!opts.good && opts.beep !== false) liveBeep();
  try {
    if (!opts.good && 'Notification' in window && Notification.permission === 'granted') new Notification('Live recording', { body: text });
  } catch (e) { /* best effort */ }
}
function liveClearAlert() {
  const el = document.getElementById('live-alert');
  if (el) el.style.display = 'none';
  _liveAlertActive = false;
  document.title = _liveOrigTitle;
}
function liveMicWarning(text) {
  const el = document.getElementById('live-mic-warning');
  if (el) el.textContent = text ? '⚠ ' + text : '';
}

// What the microphone is hearing (the meter) — and the warning when it has heard nothing for minutes, which is what a
// muted or wrong device looks like: the recording "runs" but every chunk transcribes to nothing.
let _liveMeterTimer = null, _liveMeterSource = null, _liveSilence = null;
function liveStartMeter(stream) {
  liveStopMeter();
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || !stream) return;
    _liveAudioCtx = _liveAudioCtx || new AC();
    if (_liveAudioCtx.state === 'suspended') _liveAudioCtx.resume().catch(() => {});
    _liveMeterSource = _liveAudioCtx.createMediaStreamSource(stream);
    const analyser = _liveAudioCtx.createAnalyser();
    analyser.fftSize = 2048;
    _liveMeterSource.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const bar = document.querySelector('#live-level > span');
    _liveSilence = ndLiveHealth.createSilenceMonitor({ thresholdRms: 0.004, silentAfterSec: 150 });
    let shown = 0;   // the bar falls slowly, so a short word is still visible a moment later
    _liveMeterTimer = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      shown = Math.max(shown * 0.85, Math.min(100, Math.sqrt(rms) * 140));
      if (bar) bar.style.width = Math.round(shown) + '%';
      const r = _liveSilence.push(rms, Date.now());
      if (r.justBecameSilent) liveMicWarning('No sound from the microphone for ' + ndLiveHealth.humanDuration(r.silentForSec) + ' — is it muted, or the wrong device?');
      if (r.justRecovered) liveMicWarning('');
    }, 100);
  } catch (e) { /* the meter is a bonus: recording never depends on it */ }
}
function liveStopMeter() {
  clearInterval(_liveMeterTimer); _liveMeterTimer = null;
  try { if (_liveMeterSource) _liveMeterSource.disconnect(); } catch (e) { /* already gone */ }
  _liveMeterSource = null;
  const bar = document.querySelector('#live-level > span');
  if (bar) bar.style.width = '0';
  liveMicWarning('');
}

// A heartbeat every second: keeps the counters ticking, notices the computer or tab being suspended (a sleeping laptop
// records nothing, and used to say nothing), and restarts a recorder that died without telling anyone.
let _liveBeatTimer = null, _liveLastBeat = Date.now(), _liveRecorderIdle = 0;
function liveBeat() {
  const now = Date.now();
  const pause = ndLiveHealth.detectPause(_liveLastBeat, now);
  _liveLastBeat = now;
  if (_liveRecording && pause.paused) {
    _liveGaps.push({ at: now, seconds: pause.seconds });
    liveRaiseAlert(ndLiveHealth.pauseMessage(pause.seconds), { beep: false });
  }
  if (_liveRecording && !_liveRecovering && _liveMicStream) {
    if (_liveCurrentRecorder && _liveCurrentRecorder.isRecording()) {
      _liveRecorderIdle = 0;
    } else if (++_liveRecorderIdle >= 4) {      // 4 beats, not 1: the instant between two chunks is not a death
      _liveRecorderIdle = 0;
      liveStartSegment();
    }
  }
  // A long outage: the recording is safe, but the only copy is in this browser - say so once, quietly, after 10 minutes.
  if (_liveOffline && _liveOfflineSince && !_liveOfflineAlerted && now - _liveOfflineSince > 600000) {
    _liveOfflineAlerted = true;
    const waitingSec = _liveQueue.reduce((sum, f) => sum + (f.ndSeconds || 0), 0);
    liveRaiseAlert(`Still offline after ${ndLiveHealth.humanDuration((now - _liveOfflineSince) / 1000)} — ${ndLiveHealth.clock(waitingSec)} of audio is stored only in this browser ` +
      'until the connection is back. Recording carries on; keep this browser open and do not clear its site data.', { beep: false });
  }
  if (_liveRecording || _liveFinalizing || _liveQueue.length || _liveQueueBusy) liveRefreshStatus(true);
}

// One amber line under the controls for things the GM should know before
// they cost transcript: the STT backend isn't ready, or it is falling
// behind. `kind` lets a later success clear only its own kind of warning.
function liveSetWarning(text, kind) {
  const el = document.getElementById('live-stt-warning');
  if (!el) return;
  el.textContent = text ? '⚠ ' + text : '';
  el.dataset.kind = text ? (kind || '') : '';
  el.style.display = text ? '' : 'none';
}

// Pre-flight, run when a recording starts: asks the server to push a tiny
// WebM/Opus clip (the exact format this panel records) through the
// configured speech-to-text backend. Best effort and non-blocking — recording
// is never held up by it, and the chunks themselves still report real failures.
async function liveCheckBackend() {
  try {
    const res = await fetch(`/api/sessions/${SESSION_ID}/live-transcript/check`);
    if (!res.ok) return;
    const d = await res.json();
    if (d.ok) { const el = document.getElementById('live-stt-warning'); if (el && el.dataset.kind === 'backend') liveSetWarning(''); return; }
    let msg = `Transcription isn't ready — ${d.message || 'the speech-to-text backend did not accept a test clip'}`;
    msg += (saveChk && saveChk.checked)
      ? ' Recording continues and the audio is being kept on the server; use Retry on the failed chunks once this is fixed.'
      : ' "Save raw audio" is off, so chunks only exist in this tab until this works — turn it on to keep them safe on the server.';
    liveSetWarning(msg, 'backend');
  } catch (e) { /* best effort */ }
}

// Called after every chunk that transcribed. Any success proves the backend
// works (clears a pre-flight warning); two chunks in a row that took longer
// to transcribe than they lasted mean the transcript is falling behind.
function liveNoteTranscriptionSpeed(file, tookSeconds) {
  const el = document.getElementById('live-stt-warning');
  if (el && el.dataset.kind === 'backend') liveSetWarning('');
  const audioSeconds = file && file.ndSeconds;
  if (!audioSeconds) return;
  _liveSlowStreak = tookSeconds > audioSeconds ? _liveSlowStreak + 1 : 0;
  if (_liveSlowStreak >= 2) {
    liveSetWarning(`Transcription is running slower than the recording (${Math.round(tookSeconds)} s for a ${audioSeconds} s chunk), so the transcript will keep falling behind. Pick a shorter chunk length, or a faster speech model.`, 'slow');
  } else if (_liveSlowStreak === 0 && el && el.dataset.kind === 'slow') {
    liveSetWarning('');
  }
}

function liveSetTranscriptDisplay(text) {
  _liveTranscriptShown = true;
  const el = document.getElementById('live-transcript-text');
  el.textContent = text && text.trim() ? text : '(nothing recorded yet)';
  el.scrollTop = el.scrollHeight;
  const countEl = document.getElementById('live-transcript-token-count');
  if (countEl) countEl.textContent = text ? `(${ndTokenLabel(text)})` : '';
  const downloadLink = document.getElementById('live-transcript-download-link');
  if (downloadLink) downloadLink.style.display = text && text.trim() ? '' : 'none';
}

// The single source of truth for #live-record-status once nothing is
// actively uploading — call after every state change (queue drained, Stop
// pressed with nothing in flight, a retry resolves) so the status line
// never gets stuck showing a transient message ("Stopped — finishing the
// last chunk…") forever once there's nothing left to finish.
function liveRefreshStatus(tick) {
  const status = document.getElementById('live-record-status');
  // A message meant to stay up for a while ("retrying in 20 s…") keeps the line - and is put back if a state change
  // (Stop pressed, the last chunk cut) wrote over it, so the status never goes stale while the server is unreachable.
  if (Date.now() < _liveSayUntil) {
    if (status.textContent !== _liveSayText) status.textContent = _liveSayText;
    return;
  }
  if (_liveFailedChunks.length) {
    if (tick) return;
    status.textContent = '';
    status.append(`⚠ ${_liveFailedChunks.length} chunk(s) failed to upload — `);
    if (_liveLastFailure) status.append(`${_liveLastFailure.slice(0, 300)} `);
    const retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.className = 'bg-job-btn';
    retryBtn.textContent = 'Retry';
    retryBtn.onclick = liveRetryFailedChunks;
    status.appendChild(retryBtn);
    return;
  }
  // A sticky mic-recovery failure (liveHandleMicFailure gave up) takes
  // priority over the reassuring default below — cleared only by starting a
  // fresh recording, so it doesn't get silently overwritten mid-display.
  if (_liveLastError) {
    status.textContent = '⚠ ' + _liveLastError;
    return;
  }
  // Otherwise the honest counters: recorded vs transcribed, how far behind, and — after Stop — that it is NOT saved
  // until the backlog has drained.
  const summary = ndLiveHealth.summarize(liveHealthState());
  status.textContent = summary.text;
  status.style.color = summary.level === 'warn' ? '#e0b050' : summary.level === 'bad' ? '#e07070' : '';
  if (!_liveAlertActive) {
    document.title = _liveRecording ? '🔴 ' + _liveOrigTitle
      : (summary.level === 'warn' ? '⏳ ' + _liveOrigTitle : _liveOrigTitle);
  }
}

function liveRetryFailedChunks() {
  const chunks = _liveFailedChunks;
  _liveFailedChunks = [];
  _liveLastFailure = '';
  _liveQueue.push(...chunks);
  liveProcessQueue().catch(e => console.error('live upload queue', e));
}

// Waits `ms`, or less: the moment the browser reports it is back online. Recording never depended on the network (the
// microphone is captured locally); the uploads wait it out and carry on by themselves.
function liveWaitOrOnline(ms) {
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); window.removeEventListener('online', done); resolve(); };
    const timer = setTimeout(done, ms);
    window.addEventListener('online', done);
  });
}
window.addEventListener('offline', () => {
  if (_liveUploadAbort) _liveUploadAbort.abort();     // a request on a dead connection would hang for minutes: retry cleanly instead
  liveRefreshStatus(true);
});
window.addEventListener('online', () => { liveRefreshStatus(true); });

// BEGIN live-upload-policy
// What an upload failure means for the chunk. "wait": the same audio can succeed shortly - the transcription
// backend is down (503), a reverse proxy answers 502/504/52x while nd-world's container restarts (Watchtower swapping
// the image mid-session takes longer than the three quick attempts), or the connection dropped (fetch rejects with a
// TypeError). Waited out with a growing pause, ~9 minutes in all, without using up the failure ladder. "park": a setup
// problem repeating cannot fix (4xx: no key, model not downloaded, login expired). "retry": anything else is a real
// failure that spends one of the three ladder slots.
function liveUploadFailureKind(e) {
  const st = e && e.httpStatus;
  if (st === 502 || st === 503 || st === 504 || (st >= 520 && st <= 524)) return 'wait';
  if (e instanceof TypeError && !st) return 'wait';
  if (st >= 400 && st < 500) return 'park';
  return 'retry';
}
// "342 KB" / "3.5 MB": a few one-minute Opus segments are a few hundred KB, which "~0 MB" hid.
function liveFormatBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1048576) return Math.round(n / 1024) + ' KB';
  if (n < 10 * 1048576) return (n / 1048576).toFixed(1) + ' MB';
  return Math.round(n / 1048576) + ' MB';
}
// END live-upload-policy

async function liveProcessQueue() {
  if (_liveQueueBusy) return;
  _liveQueueBusy = true;
  const status = document.getElementById('live-record-status');
  // Patient polling for the server's in-flight 409 (docs/
  // STT_LIVE_AUDIT_2026-09.md finding 1): a slow STT backend (CPU-only
  // Studio) can't finish a chunk inside one request/response cycle when a
  // reverse proxy cuts responses at ~100s. The first upload keeps
  // transcribing server-side; every retry of the SAME segment is answered
  // by the server's in-flight guard with a cheap 409 until it commits.
  // Those waits don't consume the 3-attempt failure ladder (they're not
  // failures), but they're capped so a genuinely wedged transcription
  // still parks the chunk for manual Retry instead of polling forever.
  const INFLIGHT_WAIT_MS = 15000;
  const MAX_INFLIGHT_WAITS = 40;  // ~10 minutes per chunk, then park it
  // A 503 means the transcription backend is down, restarting or overloaded
  // (the server marks exactly those retryable) — the same audio can succeed
  // later, so wait with a growing pause (5 s → 60 s, ~9 minutes in all)
  // instead of burning the 3-attempt ladder in a few seconds.
  const MAX_TRANSIENT_WAITS = 12;
  try {
    while (_liveQueue.length) {
      // (A slower-than-realtime backend's backlog is shown by liveRefreshStatus: "transcript is 4:00 behind (3 chunks
      // waiting for transcription)".)
      const file = _liveQueue[0];
      let uploaded = false;
      let transcriptText = null;
      let inflightWaits = 0;
      let transientWaits = 0;
      let offlineWaits = 0;
      const uploadStartedAt = Date.now();
      for (let attempt = 1; attempt <= 3 && !uploaded; ) {
        try {
          const fd = new FormData();
          fd.append('file', file, file.name);
          // Raw-audio archive tagging: the checkbox is read at upload time (the
          // GM can toggle it mid-recording), while recording_id/segment_index
          // were stamped onto the File at capture time — so a chunk retried
          // after a failure still lands in its original recording slot, and
          // the server can overwrite that slot instead of duplicating it.
          fd.append('save_audio', saveChk && saveChk.checked ? '1' : '');
          fd.append('recording_id', file.ndRecordingId || '');
          fd.append('segment_index', String(typeof file.ndSegmentIndex === 'number' ? file.ndSegmentIndex : -1));
          _liveUploadAbort = new AbortController();
          const res = await fetch(`/api/sessions/${SESSION_ID}/live-transcript/append`, { method: 'POST', body: fd, signal: _liveUploadAbort.signal });
          if (res.status === 409) {
            // The server's in-flight guard: this exact segment is already
            // being transcribed by an earlier upload of ours (or its proxy-
            // orphaned twin). Not a failure — wait and re-ask.
            if (inflightWaits++ >= MAX_INFLIGHT_WAITS) throw await ndApiErrorFrom(res);
            liveSay(`The server is still transcribing a chunk (${_liveQueue.length} waiting) — checking again in ${Math.round(INFLIGHT_WAIT_MS / 1000)} s…`, INFLIGHT_WAIT_MS + 3000);
            await new Promise(r => setTimeout(r, INFLIGHT_WAIT_MS));
            continue;  // attempt NOT incremented: this isn't a failure
          }
          if (!res.ok) {
            const err = await ndApiErrorFrom(res);
            err.httpStatus = res.status;   // lets the catch below tell "wait" from "fix the setup"
            throw err;
          }
          const data = await res.json();
          transcriptText = data.transcript;
          uploaded = true;
        } catch (e) {
          _liveLastFailure = (e && e.message) ? e.message : String(e);
          const kind = liveUploadFailureKind(e);
          // The browser could not reach the server at all (or aborted because it went offline): not a failure, not a deadline.
          const offlineErr = (kind === 'wait' && !(e && e.httpStatus)) || (e && e.name === 'AbortError');
          if (offlineErr) {
            // The chunk is safe (in memory and in the local store), recording carries on, and the upload resumes by itself:
            // at once when the browser says it is back online, otherwise every 5-30 s. It is never parked behind Retry.
            _liveOffline = true;
            if (!_liveOfflineSince) _liveOfflineSince = Date.now();
            _liveSayUntil = 0;
            liveRefreshStatus();
            await liveWaitOrOnline(Math.min(30000, 5000 * 2 ** Math.min(offlineWaits++, 3)));
            continue;  // attempt NOT incremented
          }
          if (kind === 'wait' && transientWaits++ < MAX_TRANSIENT_WAITS) {
            const pause = Math.min(60000, 5000 * 2 ** Math.min(transientWaits - 1, 4));
            liveSay((e && e.httpStatus === 503)
              ? `⚠ Transcription backend unavailable (${_liveLastFailure.slice(0, 160)}) — retrying in ${Math.round(pause / 1000)} s…`
              : `⚠ Can't reach the server (${e && e.httpStatus ? 'HTTP ' + e.httpStatus : _liveLastFailure.slice(0, 80)}) — it may be restarting. The chunk is kept; retrying in ${Math.round(pause / 1000)} s…`, pause + 3000);
            await new Promise(r => setTimeout(r, pause));
            continue;  // attempt NOT incremented: waiting for the server/backend isn't a failed upload
          }
          if (kind === 'park') {
            attempt = 4;  // a setup problem (no key, model not downloaded, login expired…) — repeating cannot fix it; park the chunk with its reason showing
          } else {
            attempt++;  // a real failure (or the waits ran out) — consumes one of the 3 ladder slots
          }
          if (attempt > 3) {
            _liveFailedChunks.push(file);
            liveRaiseAlert(`${_liveFailedChunks.length} recorded chunk(s) could not be sent to the server (${_liveLastFailure.slice(0, 140)}). `
              + 'They are kept in this browser — fix the problem, then press Retry.');
          } else {
            await new Promise(r => setTimeout(r, 1500 * attempt));
          }
        }
      }
      // Rendering happens OUTSIDE the upload's try above — a display bug
      // (e.g. ndTokenLabel throwing) must never be mistaken for an upload
      // failure, which would re-upload a chunk the server already saved and
      // duplicate its text in the transcript (docs/LIVE_RECORDING_AUDIT.md
      // items 3 and 4).
      if (uploaded) {
        _liveDoneSeconds += file.ndSeconds || 0;
        _liveSayUntil = 0;   // a success makes any "retrying…" message obsolete
        _liveOffline = false; _liveOfflineSince = 0; _liveOfflineAlerted = false;   // the connection works again
        _liveSpeed = ndLiveHealth.updateSpeed(_liveSpeed, file.ndSeconds, (Date.now() - uploadStartedAt) / 1000);
        if (_liveStore && file.ndStoreKey) _liveStore.remove(file.ndStoreKey);   // the server has it: the local copy can go
        try { liveSetTranscriptDisplay(transcriptText); } catch (e) { /* display-only — the chunk itself is already saved */ }
        try { liveNoteTranscriptionSpeed(file, (Date.now() - uploadStartedAt) / 1000); } catch (e) { /* hint only */ }
      }
      _liveQueue.shift();
    }
  } finally {
    // In a `finally` so nothing above — an unexpected throw, not just a
    // caught upload failure — can leave this flag stuck true, which would
    // silently wedge every future chunk behind the early-return at the top
    // of this function (docs/LIVE_RECORDING_AUDIT.md item 4).
    _liveQueueBusy = false;
    _liveSayUntil = 0;
    liveRefreshStatus();
    liveAnnounceIfDone();
  }
  // The post-stop flush (recording already stopped) is the moment the saved
  // segment count/bytes settle — refresh the raw-audio line then, and only
  // then: drains while still recording fire every chunk, and polling this
  // from there would turn a one-shot refresh into a silent poll loop.
  if (!_liveRecording) liveRefreshAudioStatus();
}

function liveStartSegment() {
  // No stream and still "recording" means a mic recovery is in flight (see
  // liveHandleMicFailure) — it owns restarting the chain once it re-acquires,
  // and a recorder started here with a null/dead stream would just throw
  // (or worse, silently acquire its OWN mic — a second concurrent chain).
  if (_liveRecording && !_liveMicStream) return;
  // Segments stay separate MediaRecorder instances producing complete
  // standalone containers (each is transcribed independently server-side)
  // — deliberately NOT switched to a single long-running recorder with
  // timeslice chunks, whose interior chunks lack container headers the transcriber
  // needs. Only the underlying mic STREAM is now shared across segments —
  // see _liveMicStream's own comment.
  // segStream pins WHICH stream this segment belongs to: if the mic dies
  // and recovery replaces _liveMicStream mid-flight, a still-draining old
  // recorder's onStop must neither chain a SECOND segment loop onto the
  // new stream nor release the new stream thinking it was the last
  // segment — both were possible when onStop inferred liveness from
  // _liveRecording alone (docs/STT_LIVE_AUDIT_2026-09.md finding 3).
  const segStream = _liveMicStream;
  let segStartedAt = Date.now();
  _liveCurrentRecorder = ndMicRecorder(
    (blob, mimeType) => {
      // The segment's archive identity is stamped at capture time, not
      // upload time — see _liveRecordingId/_liveSegmentIndex above — so a
      // retried upload reproduces exactly the tag the first attempt had.
      const segFile = new File([blob], ndMicFilename(mimeType), { type: mimeType });
      segFile.ndRecordingId = _liveRecordingId;
      segFile.ndSegmentIndex = _liveSegmentIndex++;
      // The chunk's REAL length (the final one is shorter, a throttled tab's can be longer) — feeds the
      // recorded / transcribed counters and the "transcription is slower than the recording" hint.
      segFile.ndSeconds = Math.max(1, Math.round((Date.now() - segStartedAt) / 1000));
      segFile.ndStoreKey = ndLiveHealth.storeKey(SESSION_ID, segFile.ndRecordingId, segFile.ndSegmentIndex);
      _liveRecordedSeconds += segFile.ndSeconds;
      _liveSegStartedAt = 0;   // that chunk is counted above; the next one starts the clock again
      // A copy in this browser until the server has transcribed it: a reload, a crash or a long outage no longer
      // loses the chunk (the page offers to send leftovers on the next visit). Best effort, never blocks recording.
      if (_liveStore) _liveStore.put({
        key: segFile.ndStoreKey, sessionId: SESSION_ID, recordingId: segFile.ndRecordingId, index: segFile.ndSegmentIndex,
        seconds: segFile.ndSeconds, name: segFile.name, mimeType: mimeType, savedAt: Date.now(), blob: blob,
      });
      _liveQueue.push(segFile);
      if (!_liveRecording) { _liveFinalizing = false; liveRefreshStatus(); }   // that was the final chunk: it is queued now
      liveProcessQueue().catch(e => console.error('live upload queue', e));
      if (_liveMicStream !== segStream) return;  // a superseded generation — recovery owns the chain now
      if (_liveRecording) {
        liveStartSegment();  // immediately pick up the next segment, no gap
      } else {
        liveStopMicStream();  // that was the final segment — release the mic now
      }
    },
    (err) => liveHandleMicFailure(err),
    segStream,
  );
  _liveCurrentRecorder.start().then(started => {
    if (!started) return;  // ndMicRecorder already invoked onError above
    segStartedAt = Date.now();
    _liveSegStartedAt = segStartedAt;
    _liveRecoverAttempts = 0;  // a segment started cleanly — reset the retry ladder
    clearTimeout(_liveChunkTimer);
    // liveChunkSeconds() is read HERE, per segment, not once at recording
    // start — a GM changing the selector mid-recording applies from the
    // very next segment.
    _liveChunkTimer = setTimeout(() => {
      if (_liveCurrentRecorder) _liveCurrentRecorder.stop();
    }, liveChunkSeconds() * 1000);
  }).catch(err => liveHandleMicFailure(err));
}

// Bounded re-acquisition ladder for a mic that dies mid-recording (OS-level
// reclaim, a Bluetooth headset reconnect, a backgrounded tab's capture
// pipeline being torn down — see docs/LIVE_RECORDING_AUDIT.md item 1). The
// shared _liveMicStream (see its own comment above) removed the old
// per-segment getUserMedia call, which was also the only thing that ever
// recovered a dead mic — this puts recovery back, but only on the failure
// path, so the no-gap benefit of the shared stream is kept for the common
// case. Reached from ndMicRecorder's onError (a failed start) and from
// liveWatchMicTracks (the stream dying mid-segment, not just at a boundary).
let _liveRecoverAttempts = 0;
const LIVE_MAX_RECOVER_ATTEMPTS = 3;                       // the quick tries (1 s, 2 s, 3 s apart) ...
const LIVE_RECOVER_BUDGET = ndLiveHealth.RECONNECT_ATTEMPTS;   // ... then one every 15 s, ~30 minutes in all, before giving up
let _liveLastError = '';
// Single-flight mutex: ONE mic death reaches this handler from TWO places
// at once — track.onended AND the dead segment's own chained
// liveStartSegment failing on the inactive stream (or, when recovery has
// already nulled _liveMicStream, that chain acquiring a mic of its own —
// closed separately by the segStream generation check in liveStartSegment).
// Without the mutex, two concurrent ladders could each acquire a
// getUserMedia stream (one orphaned with its tracks never stopped — the
// mic indicator stays hot), each call liveStartSegment (two chains racing
// the shared _liveCurrentRecorder/_liveSegmentIndex/_liveChunkTimer), and
// double-increment the attempt counter toward a spurious "Mic lost"
// (docs/STT_LIVE_AUDIT_2026-09.md finding 3).
let _liveRecovering = false;

async function liveHandleMicFailure(err) {
  if (!_liveRecording) return;  // an ordinary Stop, not a failure — nothing to recover
  if (_liveRecovering) return;  // a twin invocation — the ladder already in flight owns recovery
  _liveRecovering = true;
  const status = document.getElementById('live-record-status');
  const lostAt = Date.now();
  clearTimeout(_liveChunkTimer);
  _liveSegStartedAt = 0;
  // Loud, at once: from this moment NOTHING is being recorded, and a grey status line is not how anyone should learn it.
  liveRaiseAlert('⚠ The microphone stopped — nothing is being recorded until it is back. Trying to reconnect…');
  try {
    // A loop, not a recursive self-call: recursion would now hit the
    // mutex above and silently do nothing, stranding the failure.
    while (true) {
      if (_liveRecoverAttempts >= LIVE_RECOVER_BUDGET) {
        _liveRecording = false;
        _liveFinalizing = false;
        clearTimeout(_liveChunkTimer);
        liveStopMicStream();
        liveReleaseWakeLock();
        document.getElementById('live-record-btn').textContent = '🔴 Start Recording';
        _liveLastError = 'Mic lost and could not be reconnected: ' + (err && err.message ? err.message : err) +
                          ' — recording stopped. Press Start to resume.';
        liveRaiseAlert('⛔ Recording STOPPED: the microphone could not be reconnected for ' +
          ndLiveHealth.humanDuration((Date.now() - lostAt) / 1000) + '. What was captured before is still being transcribed. ' +
          'Check the microphone, then press Start Recording to carry on.');
        _liveSayUntil = 0;
        liveRefreshStatus();
        return;
      }
      _liveRecoverAttempts++;
      liveSay(`⚠ Mic dropped — reconnecting (${_liveRecoverAttempts}/${LIVE_MAX_RECOVER_ATTEMPTS}${_liveRecoverAttempts > LIVE_MAX_RECOVER_ATTEMPTS ? '+' : ''})…`, 4000);
      liveStopMicStream();  // drop the dead stream before asking for a new one
      try {
        _liveMicStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        break;  // re-acquired
      } catch (e) {
        err = e;  // report the LATEST failure if the ladder gives up
        // Sliced waits, so Stop pressed during a 15 s pause is answered at once, and the status keeps counting.
        const pause = ndLiveHealth.reconnectDelayMs(_liveRecoverAttempts);
        for (let waited = 0; waited < pause && _liveRecording; waited += 500) {
          liveSay(`⚠ Microphone lost ${ndLiveHealth.clock((Date.now() - lostAt) / 1000)} ago — still trying to reconnect (try ${_liveRecoverAttempts}). Nothing is recorded until it is back.`, 2000);
          await new Promise(r => setTimeout(r, Math.min(500, pause - waited)));
        }
        // loop: re-check the attempt budget and try again
      }
      if (!_liveRecording) { liveStopMicStream(); return; }  // GM pressed Stop while we were reconnecting
    }
    if (!_liveRecording) { liveStopMicStream(); return; }  // GM pressed Stop while we were reconnecting
    liveWatchMicTracks();
    liveStartMeter(_liveMicStream);
    const lostFor = Math.max(1, Math.round((Date.now() - lostAt) / 1000));
    _liveGaps.push({ at: Date.now(), seconds: lostFor });
    liveRaiseAlert(`The microphone is back after ${ndLiveHealth.clock(lostFor)} — that stretch was not recorded. Recording continues.`, { beep: false });
    liveSay('Recording… (mic reconnected)', 4000);
    liveStartSegment();
  } finally {
    _liveRecovering = false;
  }
}

// Detects the mic dying DURING a segment (not just at the next chunk
// boundary, which could be up to 15 minutes away) by listening for the
// underlying track ending.
function liveWatchMicTracks() {
  if (!_liveMicStream) return;
  _liveMicStream.getAudioTracks().forEach(t => {
    t.onended = () => {
      if (_liveRecording) liveHandleMicFailure(new Error('Microphone disconnected'));
    };
    // A system- or app-level mute keeps the track alive and delivers silence: the recording "runs" and says nothing.
    t.onmute = () => { if (_liveRecording) liveMicWarning('The microphone is muted (by the system or another app) — it is sending silence.'); };
    t.onunmute = () => liveMicWarning('');
  });
}

// Best-effort only — keeps the screen (and, on most platforms, this tab's
// timers) from being throttled by the OS during a long recording. Silently
// does nothing on browsers without the Wake Lock API; the recording itself
// doesn't depend on it. The lock is released automatically by the browser
// whenever the tab is hidden, so it's re-requested on visibilitychange.
let _liveWakeLock = null;
async function liveAcquireWakeLock() {
  try {
    if ('wakeLock' in navigator) _liveWakeLock = await navigator.wakeLock.request('screen');
  } catch (e) { /* best-effort only */ }
}
function liveReleaseWakeLock() {
  if (_liveWakeLock) { _liveWakeLock.release().catch(() => {}); _liveWakeLock = null; }
}
document.addEventListener('visibilitychange', () => {
  if (_liveRecording && document.visibilityState === 'visible') liveAcquireWakeLock();
});

// Guards the async gap between clicking Start and getUserMedia resolving —
// without it, a double-click there raced two independent recording chains
// against a single _liveMicStream/_liveRecordingId (see
// docs/LIVE_RECORDING_AUDIT.md item 5): one stream leaked (mic left hot,
// nothing ever released it), the second chain's clearTimeout cancelled the
// first chain's pending stop (so the first segment never uploaded), and both
// chains wrote colliding segment_index values into the same recording folder.
let _liveStarting = false;

async function toggleLiveRecording() {
  const btn = document.getElementById('live-record-btn');
  const status = document.getElementById('live-record-status');
  if (_liveRecording) {
    _liveRecording = false;
    clearTimeout(_liveChunkTimer);
    // Ask the recorder whether it's actually capturing rather than just
    // checking it exists — after a failed segment start (see
    // liveHandleMicFailure) the object can be non-null but its inner
    // MediaRecorder is null/inactive, so .stop() would silently no-op,
    // onStop would never fire, and the mic would stay hot forever (item 2).
    if (_liveCurrentRecorder && _liveCurrentRecorder.isRecording()) {
      _liveFinalizing = true;       // the last chunk is not cut yet: "saved" would be a lie until it is queued and sent
      _liveCurrentRecorder.stop();  // its onStop (liveStartSegment) releases the mic once the last chunk is captured
      setTimeout(() => {
        if (_liveFinalizing) {
          _liveFinalizing = false;
          liveRaiseAlert('⚠ The last chunk could not be finished — the final seconds before Stop may be missing.');
          liveRefreshStatus();
        }
      }, 30000);
    } else {
      _liveCurrentRecorder = null;
      liveStopMicStream();
    }
    liveReleaseWakeLock();
    btn.textContent = '🔴 Start Recording';
    // Refresh the raw-audio line now; if a segment is still uploading, the
    // queue drain below refreshes it again once the server's count/bytes
    // have actually settled.
    liveRefreshAudioStatus();
    liveRefreshStatus();
    return;
  }
  if (_liveStarting) return;
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    status.textContent = 'Microphone recording is not supported in this browser';
    return;
  }
  _liveStarting = true;
  btn.disabled = true;
  // Asked inside the click, so the browser allows it: a desktop notification is how a problem reaches a GM who has
  // switched to another window during a long session.
  try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) { /* optional */ }
  try {
    // Acquired ONCE here for the whole recording — see _liveMicStream's
    // own comment for why, and ndMicRecorder's own comment (base.html) for
    // how each segment reuses it instead of re-acquiring.
    _liveMicStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    status.textContent = 'Mic error: ' + e.message;
    btn.disabled = false;
    _liveStarting = false;
    return;
  }
  _liveLastError = '';
  _liveRecoverAttempts = 0;
  _liveRecording = true;
  liveClearAlert();
  _liveGaps = [];
  _liveFinalizing = false;
  _liveDoneAnnounced = false;
  // A fresh set of counters, unless the previous recording's backlog is still being worked off in this tab.
  if (!_liveQueue.length && !_liveQueueBusy && !_liveFailedChunks.length) { _liveRecordedSeconds = 0; _liveDoneSeconds = 0; }
  _liveLastBeat = Date.now();
  // One id per recording (not per segment, not per page load) — the server
  // groups a recording's saved raw segments under it, and a fresh recording
  // in the same session never collides with a previous one's files.
  _liveRecordingId = ndChunkUploadRandomId();
  _liveSegmentIndex = 0;
  btn.textContent = '⏹ Stop Recording';
  btn.disabled = false;
  _liveStarting = false;
  status.textContent = 'Recording…';
  liveAcquireWakeLock();
  liveWatchMicTracks();
  liveStartMeter(_liveMicStream);
  liveStartSegment();
  liveSetWarning('');
  _liveSlowStreak = 0;
  liveCheckBackend();   // non-blocking — recording is already running
}

// Populates the panel's raw-audio line (segment count, total size, download
// link) from /live-audio — fetched only when a recording stops and on page
// load, never polled, so it stays lightweight. Hidden entirely when nothing
// has been saved ("Save raw audio" was off, or nothing recorded yet).
async function liveRefreshAudioStatus() {
  const el = document.getElementById('live-audio-status');
  if (!el) return;
  try {
    const res = await fetch(`/api/sessions/${SESSION_ID}/live-audio`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.count) { el.style.display = 'none'; el.textContent = ''; return; }
    el.style.display = '';
    el.textContent = '';
    el.append(`🎥 Raw audio: ${data.count} segment(s) (${liveFormatBytes(data.total_bytes)}) — `);
    const a = document.createElement('a');
    a.href = `/api/sessions/${SESSION_ID}/live-audio/download`;
    a.textContent = 'Download recording';
    el.appendChild(a);
  } catch (e) { /* best-effort: the transcript itself never depends on this */ }
}

// ── Chunks that never reached the server (tab closed or crashed mid-backlog, a long outage): offered on the next visit ──
// Each is a complete recording of its own; the server files it under its original (recording, index) slot, so sending one
// the server already has just replaces it - nothing is ever duplicated in the transcript.
async function liveRecoverUnsent() {
  const box = document.getElementById('live-recover');
  if (!box || !_liveStore) return;
  let rows = [];
  try { rows = await _liveStore.list(SESSION_ID); } catch (e) { return; }
  const inHand = new Set([..._liveQueue, ..._liveFailedChunks].map(f => f.ndStoreKey));
  rows = rows.filter(r => r && r.blob && !inHand.has(r.key));
  if (!rows.length) { box.style.display = 'none'; box.textContent = ''; return; }
  const secs = rows.reduce((sum, r) => sum + (r.seconds || 0), 0);
  box.textContent = '';
  box.append(`💾 ${rows.length} recorded chunk(s) (${ndLiveHealth.clock(secs)} of audio) from an earlier visit never reached the server — they were still waiting in this browser. `);
  const send = document.createElement('button');
  send.type = 'button'; send.className = 'bg-job-btn'; send.textContent = 'Send them now';
  send.onclick = () => {
    rows.forEach(r => {
      const f = new File([r.blob], r.name || 'recovered.webm', { type: r.mimeType || r.blob.type || 'audio/webm' });
      f.ndRecordingId = r.recordingId; f.ndSegmentIndex = r.index; f.ndSeconds = r.seconds || 0; f.ndStoreKey = r.key;
      _liveRecordedSeconds += f.ndSeconds;
      _liveQueue.push(f);
    });
    _liveDoneAnnounced = false;
    box.style.display = 'none';
    liveProcessQueue().catch(e => console.error('live upload queue', e));
  };
  const drop = document.createElement('button');
  drop.type = 'button'; drop.className = 'bg-job-btn'; drop.style.marginLeft = '.4rem'; drop.textContent = 'Discard';
  drop.onclick = async () => {
    if (!confirm('Throw away these unsent recordings? They cannot be brought back.')) return;
    for (const r of rows) await _liveStore.remove(r.key);
    box.style.display = 'none';
  };
  box.append(send, drop);
  box.style.display = '';
}

if (_liveStore) liveRecoverUnsent();
_liveBeatTimer = setInterval(liveBeat, 1000);

async function clearLiveTranscript() {
  if (!confirm('Clear the live transcript? This cannot be undone.')) return;
  const res = await fetch(`/api/sessions/${SESSION_ID}/live-transcript/clear`, { method: 'POST' });
  const data = await res.json();
  liveSetTranscriptDisplay(data.transcript);
}

// Both live-transcript summarize buttons run the DURABLE JOB now (via
// aiStartLiveTranscriptJob below) — the old plain button fired the
// blocking .../summarize-live-transcript route inline, and a multi-hour
// transcript against a slow local model blew past the Cloudflare tunnel's
// ~100s ceiling: "Server error 524" with all the work lost. The job gets
// session_recap's checkpointing/RAG/retry-ladder protection and shows up
// in the session's job panel like every other recap job; the blocking
// route stays up as a documented API.
function summarizeLiveTranscript() {
  aiStartLiveTranscriptJob();
}

// Same durable-background-job treatment Condense already gets
// (aiStartCondenseJob above) — keeps running after this tab closes, with
// session_recap's own checkpointing/RAG/retry-ladder protection, instead
// of the blocking route's single inline request. Reuses the audio panel's
// own model picker, same as Condense does.
async function aiStartLiveTranscriptJob() {
  const status = document.getElementById('live-record-status');
  const modelSel = document.getElementById('audio-bgjob-model');
  status.textContent = 'Starting…';
  try {
    const res = await fetch(`/api/sessions/${SESSION_ID}/ai/summarize-live-transcript-job`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        think: _recapThinkEnabled(),
        model: modelSel ? modelSel.value : '',
        ..._recapRagOptions(),
      }),
    });
    if (!res.ok) throw await ndApiErrorFrom(res);
    status.textContent = 'Summarizing in the background — see Background Jobs below for the result.';
    await sessionAudioJobs.refreshList();
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  }
}

window.addEventListener('beforeunload', (e) => {
  // Stop clears _liveRecording immediately, but the final segment can still
  // be uploading (or retrying, or sitting in _liveFailedChunks waiting for
  // the GM to click Retry) well after that — closing the tab then loses it
  // silently, contradicting the panel's own "nothing is lost beyond the
  // current chunk" promise (docs/LIVE_RECORDING_AUDIT.md item 6).
  if (_liveRecording || _liveFinalizing || _liveQueue.length || _liveFailedChunks.length || _liveQueueBusy) {
    e.preventDefault();
    e.returnValue = '';
  }
});

function renderPrep() {
  const list = document.getElementById('prep-list');
  list.innerHTML = prep.length ? '' : '<p style="color:var(--text-dim);font-size:.82rem;font-style:italic">No prep tasks yet.</p>';
  prep.forEach((item, i) => {
    const row = document.createElement('label');
    row.style.cssText = 'display:flex;align-items:center;gap:.5rem;font-size:.85rem;padding:.15rem 0';
    row.innerHTML = `<input type="checkbox" ${item.done ? 'checked' : ''} data-i="${i}" style="width:auto"/>
      <span style="${item.done ? 'text-decoration:line-through;color:var(--text-dim)' : ''}">${esc(item.task)}</span>
      <button type="button" data-i="${i}" style="margin-left:auto;background:none;border:none;color:#c44;cursor:pointer">✕</button>`;
    row.querySelector('input').addEventListener('change', () => togglePrep(i));
    row.querySelector('button').addEventListener('click', () => deletePrep(i));
    list.appendChild(row);
  });
}

async function togglePrep(i) {
  const res = await fetch('/api/sessions/' + SESSION_ID + '/prep/toggle', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ index: i }),
  });
  prep = (await res.json()).prep; renderPrep();
}
async function deletePrep(i) {
  const res = await fetch('/api/sessions/' + SESSION_ID + '/prep/' + i + '/delete', { method: 'POST' });
  prep = (await res.json()).prep; renderPrep();
}
document.getElementById('prep-add-btn').addEventListener('click', async () => {
  const input = document.getElementById('prep-input');
  const task = input.value.trim();
  if (!task) return;
  const res = await fetch('/api/sessions/' + SESSION_ID + '/prep/add', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task }),
  });
  prep = (await res.json()).prep;
  input.value = '';
  renderPrep();
});

async function generatePrep() {
  const btn = document.getElementById('prep-generate-btn');
  const status = document.getElementById('prep-generate-status');
  const draftWrap = document.getElementById('prep-draft-list');
  btn.disabled = true;
  status.textContent = 'Thinking…';
  draftWrap.style.display = 'none';
  draftWrap.innerHTML = '';
  try {
    const res = await ndAiFetch('/api/sessions/' + SESSION_ID + '/prep/generate', { method: 'POST' });
    if (!res.ok) throw await ndApiErrorFrom(res);
    const data = await res.json();
    const tasks = data.tasks || [];
    if (!tasks.length) {
      status.textContent = 'No suggestions — try adding some quests, facts, or a party first.';
      return;
    }
    status.textContent = `${tasks.length} suggestion(s) — review below.`;
    draftWrap.style.display = 'flex';
    tasks.forEach((task, i) => {
      const row = document.createElement('label');
      row.style.cssText = 'display:flex;align-items:center;gap:.5rem;font-size:.83rem;padding:.15rem 0';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      cb.id = 'prep-draft-' + i;
      cb.style.width = 'auto';
      row.append(cb, document.createTextNode(task));
      draftWrap.appendChild(row);
    });
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.textContent = '+ Add checked';
    addBtn.style.cssText = 'margin-top:.4rem;background:var(--bg2);border:1px solid var(--neon);color:var(--neon);padding:.3rem .7rem;font-size:.78rem;border-radius:3px;cursor:pointer';
    addBtn.onclick = async () => {
      addBtn.disabled = true;
      const checked = tasks.filter((_, i) => document.getElementById('prep-draft-' + i).checked);
      for (const task of checked) {
        const res = await fetch('/api/sessions/' + SESSION_ID + '/prep/add', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ task }),
        });
        prep = (await res.json()).prep;
      }
      renderPrep();
      draftWrap.style.display = 'none';
      draftWrap.innerHTML = '';
      status.textContent = checked.length ? `Added ${checked.length}.` : '';
    };
    draftWrap.appendChild(addBtn);
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderLoot() {
  const list = document.getElementById('loot-list');
  list.innerHTML = loot.length ? '' : '<p style="color:var(--text-dim);font-size:.82rem;font-style:italic">No loot logged yet.</p>';
  loot.forEach((item, i) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:.5rem;background:var(--bg3);border:1px solid var(--border);border-radius:3px;padding:.4rem .6rem;font-size:.85rem';
    row.innerHTML = `<span style="flex:1">${esc(item.name)} <span style="color:var(--text-dim)">×${item.qty}</span></span>
      ${ND_SESSION.party_id ? `<button type="button" data-i="${i}" data-action="transfer" style="background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:3px;padding:.1rem .5rem;font-size:.72rem;cursor:pointer">→ party</button>` : ''}
      <button type="button" data-i="${i}" data-action="remove" style="background:none;border:none;color:#c44;cursor:pointer">✕</button>`;
    row.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => lootAction(i, btn.dataset.action)));
    list.appendChild(row);
  });
}

async function lootAction(i, action) {
  const res = await fetch('/api/sessions/' + SESSION_ID + '/loot/transfer', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action: action === 'transfer' ? 'transfer' : 'remove', index: i }),
  });
  loot = (await res.json()).loot; renderLoot();
}

document.getElementById('loot-add-btn').addEventListener('click', async () => {
  const name = document.getElementById('loot-name').value.trim();
  if (!name) return;
  const qty = parseInt(document.getElementById('loot-qty').value, 10) || 1;
  const res = await fetch('/api/sessions/' + SESSION_ID + '/loot/transfer', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ action: 'add', name, qty }),
  });
  loot = (await res.json()).loot;
  document.getElementById('loot-name').value = '';
  document.getElementById('loot-qty').value = 1;
  renderLoot();
});

const xpBtn = document.getElementById('xp-award-btn');
if (xpBtn) {
  xpBtn.addEventListener('click', async () => {
    const delta = parseInt(document.getElementById('xp-delta').value, 10) || 0;
    const res = await fetch('/api/sessions/' + SESSION_ID + '/xp', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ delta }),
    });
    const data = await res.json();
    document.getElementById('xp-total').textContent = data.xp_awarded;
  });
}

renderPrep();
renderLoot();
// On page load, show the saved-raw-audio line if this session has one (a GM
// coming back the next day can download the recording without re-recording).
liveRefreshAudioStatus();
