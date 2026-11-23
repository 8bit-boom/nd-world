"use strict";
// The tools on an entity page's Media tab (templates/entities/_media.html), for whoever may edit the entity: files dropped on the tab or
// pasted, the galleries of this world to pick from, and on each item a "players can see" switch, a name and a remove.
//
// The tab's items are rendered by the SERVER (app/routers/entity_media.py, the page itself). After a change this script fetches the page
// once more and patches in what the server rendered - an item that arrived is inserted, one that is gone is removed, one that changed is
// replaced - so the markup is written in one place only, and whatever is playing keeps playing. The pure decisions (kindOf, plan) are at the top
// of this file; tests/test_entity_media.py runs them under Node.
(function (root) {
  const IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif'];
  const VIDEO_EXTS = ['.mp4', '.m4v', '.webm', '.ogv', '.mov', '.mkv', '.avi', '.ts', '.wmv', '.flv', '.mpg', '.mpeg'];
  const AUDIO_EXTS = ['.mp3', '.ogg', '.oga', '.wav', '.m4a', '.flac', '.opus', '.webm', '.aac'];

  // What a dropped file is: by its extension, and for the few that fit two (.webm) by the type the browser reports. "" for anything else.
  function kindOf(file) {
    const m = /\.[^.]+$/.exec((file && file.name) || '');
    const ext = m ? m[0].toLowerCase() : '';
    const isImage = IMAGE_EXTS.indexOf(ext) >= 0, isVideo = VIDEO_EXTS.indexOf(ext) >= 0, isAudio = AUDIO_EXTS.indexOf(ext) >= 0;
    if (isImage) return 'image';
    if (isVideo && isAudio) return String((file && file.type) || '').indexOf('audio/') === 0 ? 'audio' : 'video';
    if (isVideo) return 'video';
    if (isAudio) return 'audio';
    return '';
  }

  // How to get from what the page shows now (`live` keys, in order) to what the server renders (`fresh` keys, in order): the keys to
  // take out, and - for each key that has to come in - the key it must be placed before (null: at the end). Keys in both stay where they
  // are, so a player that is running there keeps running.
  function plan(live, fresh) {
    const have = (k) => live.indexOf(k) >= 0;
    return {
      remove: live.filter((k) => fresh.indexOf(k) < 0),
      insert: fresh.map((k, i) => (have(k) ? null : { key: k, before: fresh.slice(i + 1).find((k) => have(k)) || null })).filter(Boolean),
    };
  }

  const core = { kindOf, plan };
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  root.entityMediaCore = core;
  if (typeof document === 'undefined') return;

  const section = document.getElementById('entity-media');
  if (!section || !section.hasAttribute('data-em-manage')) return;
  const ENTITY_ID = section.getAttribute('data-em-entity');
  const BASE = '/api/entity/' + ENTITY_ID + '/media';
  const KIND = { pic: 'image', url: 'image', aud: 'audio', vid: 'video' };
  const SHOW = 300;                                        // picker rows / tiles drawn at once; the search narrows the rest
  let busy = false;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const groups = (scope) => Array.from(scope.querySelectorAll('[data-em-group]'));
  const itemsOf = (group) => Array.from(group.querySelectorAll('[data-em-items] > .em-item'));
  const keysOf = (group) => itemsOf(group).map((i) => i.getAttribute('data-em-key'));
  const progress = () => document.getElementById('em-progress');
  const say = (text) => { const p = progress(); if (p) p.textContent = text || ''; };
  const line = (text) => { const p = progress(); const d = el('div', '', text); if (p) p.appendChild(d); return d; };

  async function call(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) throw await ndApiErrorFrom(res);
    return res.json();
  }
  const post = (url, body) => call(url, body === undefined ? { method: 'POST' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  // What a change comes to in the item's own markup: hidden or not, its caption, its controls (not what the browser changed in it since)
  const signature = (item) => [item.hasAttribute('data-em-hidden') ? '1' : '', (item.querySelector('.em-caption') || {}).textContent, (item.querySelector('.em-ctrl') || {}).innerHTML].join('\u0001');

  // Make the live tab the one the server renders now
  async function refresh() {
    const res = await fetch('/entity/' + ENTITY_ID);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
    const freshSection = doc.getElementById('entity-media');
    if (!freshSection) return;
    for (const liveGroup of groups(section)) {
      const freshGroup = freshSection.querySelector('[data-em-group="' + liveGroup.getAttribute('data-em-group') + '"]');
      if (!freshGroup) continue;
      const box = liveGroup.querySelector('[data-em-items]');
      const find = (key) => itemsOf(liveGroup).find((i) => i.getAttribute('data-em-key') === key);
      const p = plan(keysOf(liveGroup), keysOf(freshGroup));
      p.remove.forEach((key) => { const i = find(key); if (i) i.remove(); });
      for (const step of p.insert) {
        const freshItem = itemsOf(freshGroup).find((i) => i.getAttribute('data-em-key') === step.key);
        if (freshItem) box.insertBefore(freshItem.cloneNode(true), step.before ? find(step.before) : null);
      }
      for (const freshItem of itemsOf(freshGroup)) {
        const liveItem = find(freshItem.getAttribute('data-em-key'));
        if (liveItem && signature(liveItem) !== signature(freshItem)) liveItem.replaceWith(freshItem.cloneNode(true));
      }
      const freshHead = freshGroup.querySelector('h3'), liveHead = liveGroup.querySelector('h3');
      if (freshHead) { if (liveHead) liveHead.replaceWith(freshHead.cloneNode(true)); else liveGroup.insertBefore(freshHead.cloneNode(true), liveGroup.firstChild); }
      const freshNone = freshGroup.querySelector('[data-em-none]'), liveNone = liveGroup.querySelector('[data-em-none]');
      if (freshNone) { if (liveNone) liveNone.replaceWith(freshNone.cloneNode(true)); else box.appendChild(freshNone.cloneNode(true)); }
      else if (liveNone) liveNone.remove();
    }
    section.dispatchEvent(new CustomEvent('nd-media-change', { bubbles: true }));
  }

  // The item a control belongs to: its kind and what the server names it by
  function targetOf(node) {
    const item = node.closest('.em-item');
    if (!item) return null;
    const parts = (item.getAttribute('data-em-key') || '').split(':');
    return { item, kind: KIND[parts[0]] || '', ref: parts.slice(1).join(':') };
  }

  async function act(job, okText) {
    if (busy) return;
    busy = true; say('Saving…');
    try { await job(); await refresh(); say(okText || ''); }
    catch (e) { say('Failed: ' + e.message); try { await refresh(); } catch (e2) { /* the page says what it said */ } }
    finally { busy = false; }
  }

  section.addEventListener('change', (e) => {
    const box = e.target.closest('[data-em-act="visibility"]');
    if (!box) return;
    const t = targetOf(box);
    act(() => post(BASE + '/' + t.kind + '/' + encodeURIComponent(t.ref) + '/visibility', { visible_to_players: box.checked }),
        box.checked ? 'Players can now see it.' : 'Hidden from players.');
  });
  section.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-em-act]');
    if (!btn) return;
    const t = targetOf(btn);
    if (!t || !t.kind) return;
    const part = BASE + '/' + t.kind + '/' + encodeURIComponent(t.ref);
    if (btn.getAttribute('data-em-act') === 'rename') {
      // a clip's caption starts with its icon ("🎵 Name")
      const current = ((t.item.querySelector('.em-caption') || {}).textContent || '').replace(/^[^ ]+ /, '');
      const next = prompt(t.kind === 'image' ? 'Caption' : 'Name', current);
      if (next === null || next.trim() === current.trim()) return;
      act(() => post(part + '/rename', { name: next }), 'Renamed.');
    } else if (btn.getAttribute('data-em-act') === 'remove') {
      if (!confirm(t.kind === 'image' ? 'Take this picture off the entity? It stays in the gallery.' : 'Take this off the entity? It stays in its library.')) return;
      act(() => post(part + '/remove'), '');
    } else if (btn.getAttribute('data-em-act') === 'play') {
      btn.disabled = true;
      fetch('/audio/' + t.ref + '/play-for-players', { method: 'POST' })
        .then((r) => (r.ok ? {} : ndApiErrorFrom(r).then((err) => Promise.reject(err))))
        .then(() => say('Playing for the players.'), (err) => say('Failed: ' + err.message))
        .finally(() => { btn.disabled = false; });
    }
  });

  // ── files: dropped, chosen, or pasted ─────────────────────────────────────────────────────────────────────────────
  // One after another, each with its kind worked out here and checked again on the server. Big files go in parts (ndChunkedUpload), as
  // everywhere in the app.
  async function addFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || busy) return;
    busy = true;
    say('');
    const visible = document.getElementById('em-visible').checked;
    let added = 0;
    try {
      for (const file of files) {
        const kind = kindOf(file);
        if (!kind) { line('✕ ' + file.name + ' — not a picture, an audio or a video file.'); continue; }
        const row = line('⬆ ' + file.name + ' …');
        try {
          await ndChunkedUpload(file, {
            directUrl: BASE + '/upload', chunkUrl: BASE + '/upload/chunk', completeUrl: BASE + '/upload/complete',
            extraFields: { name: file.name.replace(/\.[^.]+$/, ''), kind: kind, visible_to_players: visible ? '1' : '' },
            onProgress: (p) => { row.textContent = '⬆ ' + file.name + (p.phase === 'upload' ? ' — ' + p.percent + '%' : ' — processing…'); },
          });
          row.textContent = '✓ ' + file.name;
          added++;
        } catch (err) {
          row.textContent = '✕ ' + file.name + ' — ' + err.message;
        }
      }
      if (added) await refresh();
    } catch (e) {
      say('The tab could not be refreshed: ' + e.message);
    } finally {
      busy = false;
    }
    if (added) say(added + ' added' + (visible ? ' — players can see ' + (added === 1 ? 'it' : 'them') + '.' : ' — tick “players can see” on each to share ' + (added === 1 ? 'it' : 'them') + '.'));
  }

  (function wireDropZone() {
    const zone = document.getElementById('em-drop'), input = document.getElementById('em-file');
    if (!zone || !input) return;
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') >= 0;
    const on = () => zone.classList.add('em-over'), off = () => zone.classList.remove('em-over');
    let depth = 0;                                       // dragenter / leave fire for every child element: count them
    zone.addEventListener('dragenter', (e) => { if (hasFiles(e)) { e.preventDefault(); depth++; on(); } });
    zone.addEventListener('dragover', (e) => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
    zone.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; off(); } });
    zone.addEventListener('drop', (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; off(); addFiles(e.dataTransfer.files); });
    input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });

    // Ctrl+V of an ADDRESS (as "Copy" with Ctrl+C gives in the gallery and the libraries) adds the thing itself, not a second copy of
    // its file. Copied pictures, screenshots and files are left to the shared paste script, which hands them to the file field.
    document.addEventListener('paste', (e) => {
      const cd = e.clipboardData;
      if (!cd) return;
      const t = e.target, typing = !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
      const text = (cd.getData && cd.getData('text/plain') || '').trim();
      const addr = !typing && text && window.ndClipboard ? ndClipboard.addressOf(text, location.origin) : null;
      if (!addr) return;
      e.preventDefault();
      act(() => post(BASE + '/attach', { kind: addr.kind, url: addr.path }), 'Attached. Tick “players can see” on it to share it.');
    });
  })();

  // ── "Choose from this world": one picker for the pictures, videos and audio that already exist here ────────────────────────────
  // The server says what may be added (GET …/media); this only shows it.
  const sel = { image: new Set(), video: new Set(), audio: new Set() };
  let state = null, tab = 'image', lastFocus = null;

  // Makes the picker once; afterwards returns it. {root, draw}
  function picker() {
    const old = document.getElementById('emp');
    if (old) return old._emp;
    const root = el('div', 'emp'), card = el('div', 'emp-card');
    root.id = 'emp'; root.hidden = true; root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true');
    const head = el('div', 'emp-head');
    const close = el('button', 'emp-x', '✕'); close.type = 'button'; close.setAttribute('aria-label', 'Close');
    head.append(el('b', '', 'Choose from this world'), close);
    const tabs = el('div', 'emp-tabs'); tabs.setAttribute('role', 'tablist');
    [['image', '🖼 Pictures'], ['video', '🎬 Videos'], ['audio', '🎵 Audio']].forEach(([k, label]) => {
      const b = el('button', '', label); b.type = 'button'; b.dataset.t = k; b.setAttribute('role', 'tab');
      b.addEventListener('click', () => { tab = k; draw(); });
      tabs.appendChild(b);
    });
    const q = el('input', 'emp-q'); q.type = 'search'; q.placeholder = 'Search by name…'; q.setAttribute('aria-label', 'Search');
    const body = el('div', 'emp-body'), foot = el('div', 'emp-foot');
    const vis = el('input'); vis.type = 'checkbox'; vis.id = 'emp-vis';
    const visLabel = el('label'); visLabel.append(vis, document.createTextNode(' Players can see them straight away'));
    const count = el('span'); count.setAttribute('role', 'status');
    const add = el('button', 'emp-add', 'Add selected'); add.type = 'button'; add.disabled = true;
    foot.append(visLabel, count, add);
    card.append(head, tabs, q, body, foot);
    root.appendChild(card);
    document.body.appendChild(root);
    const shut = () => {
      if (root.hidden) return;
      body.querySelectorAll('audio,video').forEach((m) => { m.pause(); m.removeAttribute('src'); m.load(); });
      root.hidden = true; document.documentElement.style.overflow = '';
      if (lastFocus && lastFocus.focus) try { lastFocus.focus(); } catch (e) { /* gone */ }
    };
    const matches = (text) => !q.value.trim() || String(text).toLowerCase().indexOf(q.value.trim().toLowerCase()) >= 0;
    const thumb = (u) => u.replace(/\.[^./]+$/, '') + '_thumb.webp';
    const footer = () => {
      const n = sel.image.size + sel.video.size + sel.audio.size;
      count.textContent = n ? n + ' selected' : '';
      count.style.color = '';
      add.disabled = busy || !n;
    };
    const note = (text) => { body.replaceChildren(el('div', 'emp-msg', text)); };
    const more = (shown, all) => (shown < all ? el('div', 'emp-msg', 'Showing the first ' + shown + ' of ' + all + ' — type in the search box to narrow it.') : null);

    function drawPictures() {
      const all = ((state.library || {}).pictures || []).filter((p) => matches(p.name + ' ' + p.url));
      if (!all.length) { note((((state.library || {}).pictures || []).length ? 'No picture matches.' : 'This world has no picture left that is not here already — drop one on the tab, or add pictures in the gallery.')); return; }
      const grid = el('div', 'emp-grid');
      all.slice(0, SHOW).forEach((p) => {
        const t = el('button', 'emp-tile'); t.type = 'button'; t.title = p.name;
        t.setAttribute('aria-pressed', String(sel.image.has(p.url)));
        const img = el('img'); img.src = thumb(p.url); img.alt = ''; img.loading = 'lazy';
        img.onerror = () => { img.onerror = null; img.src = p.url; };
        t.append(img, el('span', '', p.name));
        t.addEventListener('click', () => { if (sel.image.has(p.url)) sel.image.delete(p.url); else sel.image.add(p.url); t.setAttribute('aria-pressed', String(sel.image.has(p.url))); footer(); });
        const z = el('button', 'emp-zoom', '🔍'); z.type = 'button'; z.title = 'Look at it larger'; z.setAttribute('aria-label', 'Look at ' + p.name + ' larger');
        z.addEventListener('click', (ev) => { ev.stopPropagation(); if (window.openLightbox) openLightbox(p.url, p.name); });
        const wrap = el('div'); wrap.style.position = 'relative'; wrap.append(t, z);
        grid.appendChild(wrap);
      });
      const m = more(Math.min(SHOW, all.length), all.length);
      body.replaceChildren(grid, ...(m ? [m] : []));
    }
    function drawClips(kind) {
      const lib = ((state.library || {})[kind] || []).filter((c) => matches(c.name));
      if (!lib.length) { note((((state.library || {})[kind] || []).length ? 'Nothing matches.' : 'Nothing left in the ' + kind + ' library that is not on this entity already.')); return; }
      const rows = lib.slice(0, SHOW).map((c) => {
        const row = el('div', 'emp-row');
        const box = el('input'); box.type = 'checkbox'; box.checked = sel[kind].has(c.id); box.setAttribute('aria-label', 'Select ' + c.name);
        box.addEventListener('change', () => { if (box.checked) sel[kind].add(c.id); else sel[kind].delete(c.id); footer(); });
        row.appendChild(box);
        if (kind === 'video' && c.poster_url) { const pi = el('img'); pi.src = c.poster_url; pi.alt = ''; pi.loading = 'lazy'; row.appendChild(pi); }
        const name = el('label', 'emp-name', (kind === 'video' ? '🎬 ' : '🎵 ') + c.name);
        name.addEventListener('click', (ev) => { ev.preventDefault(); box.checked = !box.checked; box.dispatchEvent(new Event('change')); });
        row.appendChild(name);
        if (!c.visible_to_players) row.appendChild(el('span', 'emp-tag', 'GM only'));
        if (c.entity_id) row.appendChild(el('span', 'emp-tag', 'on another entity — adding moves it here'));
        const media = el(kind === 'video' ? 'video' : 'audio'); media.controls = true; media.preload = 'none'; media.src = c.file_url;
        if (kind === 'video') { if (c.poster_url) media.poster = c.poster_url; media.style.maxHeight = '260px'; }
        media.addEventListener('play', () => body.querySelectorAll('audio,video').forEach((m) => { if (m !== media) m.pause(); }));
        row.appendChild(media);
        return row;
      });
      const m = more(Math.min(SHOW, lib.length), lib.length);
      body.replaceChildren(...rows, ...(m ? [m] : []));
    }
    function draw() {
      tabs.querySelectorAll('button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.t === tab)));
      body.querySelectorAll('audio,video').forEach((m) => m.pause());
      if (tab === 'image') drawPictures(); else drawClips(tab);
      footer();
    }
    async function addSelected() {
      const n = sel.image.size + sel.video.size + sel.audio.size;
      if (!n || busy) return;
      busy = true; add.disabled = true;
      const visible = vis.checked;
      const jobs = [...[...sel.image].map((u) => ({ kind: 'image', url: u })), ...[...sel.video].map((i) => ({ kind: 'video', id: i })), ...[...sel.audio].map((i) => ({ kind: 'audio', id: i }))];
      let done = 0; const failed = [];
      for (const job of jobs) {
        count.textContent = 'Adding ' + (done + failed.length + 1) + ' of ' + jobs.length + '…';
        try {
          await post(BASE + '/attach', visible ? Object.assign({ visible_to_players: true }, job) : job);
          done++;
          (job.kind === 'image' ? sel.image : sel[job.kind]).delete(job.kind === 'image' ? job.url : job.id);
        } catch (e) { failed.push(e.message); }
      }
      busy = false;
      try { await refresh(); } catch (e) { /* the tab says so */ }
      if (failed.length) { draw(); count.textContent = failed.length + ' could not be added: ' + failed[0]; count.style.color = '#e0b050'; }
      else {
        shut();
        say(done + ' added' + (visible ? ' — players can see them.' : ' — tick “players can see” on each to share it.'));
      }
    }
    close.addEventListener('click', shut);
    root.addEventListener('mousedown', (ev) => { if (ev.target === root) shut(); });
    document.addEventListener('keydown', (ev) => { if (!root.hidden && ev.key === 'Escape') { ev.preventDefault(); shut(); } });
    q.addEventListener('input', draw);
    add.addEventListener('click', addSelected);
    root._emp = { root, draw };
    return root._emp;
  }

  async function openPicker() {
    say('');
    try { state = await call(BASE); }
    catch (e) { say('Could not load what this world has: ' + e.message); return; }
    const { root, draw } = picker();
    lastFocus = document.activeElement;
    sel.image.clear(); sel.video.clear(); sel.audio.clear();
    const q = root.querySelector('.emp-q'); q.value = '';
    root.hidden = false; document.documentElement.style.overflow = 'hidden';
    draw();
    q.focus();
  }
  document.getElementById('em-pick').addEventListener('click', openPicker);
})(typeof window !== 'undefined' ? window : globalThis);
