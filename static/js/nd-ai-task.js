/* ndAiFetch — a drop-in `fetch` for the AI task routes (app/ai_background.py).
 *
 * The model can take minutes (thinking on, a cold model load, a long recap) and a request held open that long is
 * killed by Cloudflare (~100 s), a phone that backgrounds the tab, or a flaky link. So the server is asked to run
 * the task in the BACKGROUND (`X-ND-Background: 1`): it answers 202 with a task id at once and keeps working whether
 * or not this page is still there. This helper then polls GET /api/ai/tasks/{id} (short, cheap requests that survive
 * blips and a suspended tab) and resolves with a real `Response` carrying exactly what the route returned — status,
 * content type, body — so a call site only swaps `fetch(` for `ndAiFetch(`:
 *
 *     const res = await ndAiFetch('/api/ai/chat', {method: 'POST', headers: {...}, body: ...}, {label: 'Chat'});
 *     if (!res.ok) throw new Error((await res.json()).detail);
 *
 * Options (third argument): label (shown in the tray), onProgress({status, elapsed}).
 * Aborting `init.signal` cancels the task on the server too and rejects with an AbortError, like fetch.
 * If the server did not background the call (an older server, a route it does not know) the plain response is returned.
 *
 * A task whose page went away (reload, navigation) is remembered in localStorage; the next page shows it in a small
 * pill — "✨ AI task running" / "✨ <label> ready — view" — so a long result is never lost with the tab.
 */
(function () {
  'use strict';
  var KEY = 'nd_ai_tasks';
  var KEEP_MS = 3600 * 1000;
  var live = {};            // task ids THIS page is waiting on itself (the tray leaves those alone)
  var wakers = [];          // sleepers to nudge when the tab becomes visible again

  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]') || []; } catch (e) { return []; } }
  function save(list) { try { localStorage.setItem(KEY, JSON.stringify(list.slice(-20))); } catch (e) {} }
  function remember(id, label) { save(load().filter(function (x) { return x.id !== id; }).concat([{ id: id, label: label, t: Date.now() }])); }
  function forget(id) { save(load().filter(function (x) { return x.id !== id; })); }

  function abortError() {
    try { return new DOMException('The operation was aborted.', 'AbortError'); }
    catch (e) { var er = new Error('The operation was aborted.'); er.name = 'AbortError'; return er; }
  }

  function wait(ms, signal) {
    return new Promise(function (resolve) {
      var done = false;
      function fin() { if (done) return; done = true; clearTimeout(t); wakers = wakers.filter(function (w) { return w !== fin; }); resolve(); }
      var t = setTimeout(fin, ms);
      wakers.push(fin);
      if (signal) { if (signal.aborted) fin(); else signal.addEventListener('abort', fin); }
    });
  }
  try { document.addEventListener('visibilitychange', function () { if (!document.hidden) wakers.slice().forEach(function (w) { w(); }); }); } catch (e) {}

  /* "waiting its turn" pill: AI tasks run one at a time, so a task started behind a long one says where it is */
  var waiting = {};
  function renderWait() {
    try {
      var ids = Object.keys(waiting), el = document.getElementById('nd-ai-wait');
      if (!ids.length) { if (el) el.remove(); return; }
      if (!document.body || !document.createElement) return;
      if (!el) {
        el = document.createElement('div'); el.id = 'nd-ai-wait'; el.setAttribute('role', 'status');
        el.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:calc(var(--nd-fab-bottom,14px) + 44px);z-index:9997;' +
          'background:var(--bg2,#181820);border:1px solid var(--border,#444);border-radius:999px;padding:.35rem .8rem;' +
          'color:var(--text-dim,#999);font-size:.8rem;box-shadow:0 2px 10px rgba(0,0,0,.5)';
        document.body.appendChild(el);
      }
      var ahead = Math.min.apply(null, ids.map(function (i) { return waiting[i]; }));
      el.textContent = '⏳ Waiting for the AI — ' + ahead + (ahead === 1 ? ' task' : ' tasks') + ' ahead (they run one at a time)';
    } catch (e) {}
  }
  function setWaiting(id, position) { if (position > 0) waiting[id] = position; else delete waiting[id]; renderWait(); }

  function synthetic(status, detail) {
    return new Response(JSON.stringify({ detail: detail }), { status: status, headers: { 'Content-Type': 'application/json' } });
  }

  function asResponse(d) {
    var status = d.http_status || 200;
    var ct = d.content_type || 'application/json';
    var body = typeof d.body === 'string' ? d.body : JSON.stringify(d.body);
    var nullBody = status === 204 || status === 205 || status === 304;
    var h = { 'Content-Type': ct };
    if (d.location) h['Location'] = d.location;
    return new Response(nullBody ? null : body, { status: status, headers: h });
  }

  function cancelOnServer(id) {
    try { fetch('/api/ai/tasks/' + id, { method: 'DELETE', credentials: 'same-origin' }).catch(function () {}); } catch (e) {}
  }

  window.ndAiFetch = async function (url, init, opts) {
    init = Object.assign({}, init || {});
    opts = opts || {};
    var headers = new Headers(init.headers || {});
    headers.set('X-ND-Background', '1');
    var label = String(opts.label || '').replace(/[^\x20-\x7e]/g, '').slice(0, 60);
    if (label) headers.set('X-ND-Task-Label', label);
    init.headers = headers;

    var res = await fetch(url, init);
    var id = res.headers.get('X-ND-Task');
    if (res.status !== 202 || !id) return res;               // not backgrounded: an ordinary response

    var signal = init.signal;
    if (!label) { try { label = (await res.clone().json()).label || ''; } catch (e) {} }
    live[id] = true;
    remember(id, label || 'AI task');
    var delay = 800, failures = 0;
    try {
      for (;;) {
        if (signal && signal.aborted) { cancelOnServer(id); forget(id); throw abortError(); }
        await wait(document.hidden ? 5000 : delay, signal);
        if (signal && signal.aborted) { cancelOnServer(id); forget(id); throw abortError(); }
        delay = Math.min(3000, Math.round(delay * 1.4));
        var r;
        try {
          r = await fetch('/api/ai/tasks/' + id, { cache: 'no-store', credentials: 'same-origin' });
        } catch (e) {                                        // offline / tab suspended: keep trying
          if (++failures > 150) throw e;
          continue;
        }
        if (r.status === 404) {
          forget(id);
          return synthetic(410, 'The server restarted while this was running (or the result expired) — please run it again.');
        }
        if (r.status === 401 || r.status === 403) { forget(id); return r; }
        if (!r.ok) { if (++failures > 150) { forget(id); return r; } continue; }
        failures = 0;
        var d = await r.json();
        setWaiting(id, d.status === 'queued' ? d.position : 0);
        if (opts.onProgress) { try { opts.onProgress(d); } catch (e) {} }
        if (d.status === 'running' || d.status === 'queued') continue;     // queued = waiting its turn (one AI task at a time)
        forget(id);
        if (d.status === 'cancelled') return synthetic(499, 'Cancelled.');
        return asResponse(d);
      }
    } finally {
      delete live[id];
      setWaiting(id, 0);
    }
  };

  /* ── plain HTML forms that start an AI task (e.g. "✨ AI Table"): <form data-nd-ai-form …> ──────────────
   * A normal form post freezes the whole page on the server's answer for as long as the model takes. With this
   * attribute the submit is run as a background task instead and the page follows the route's redirect when it
   * finishes (or shows the route's error). Any inline `onsubmit` (a prompt() that fills a hidden field, say) still
   * runs first and can cancel. */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || !form.hasAttribute || !form.hasAttribute('data-nd-ai-form') || ev.defaultPrevented) return;
    ev.preventDefault();
    var btn = form.querySelector('button[type=submit], button:not([type])');
    var original = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Working in the background…'; }
    function restore() { if (btn) { btn.disabled = false; btn.innerHTML = original; } }
    var body = new URLSearchParams(new FormData(form));
    window.ndAiFetch(form.getAttribute('action') || location.href, {
      method: (form.getAttribute('method') || 'POST').toUpperCase(), body: body, redirect: 'manual',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    }, { label: (btn && btn.textContent || '').trim() || 'AI task' }).then(async function (res) {
      var where = res.headers.get('Location');
      if (where && res.status >= 300 && res.status < 400) { location.assign(where); return; }
      if (res.type === 'opaqueredirect' || (res.status === 0)) { location.reload(); return; }
      if (res.ok) { location.reload(); return; }
      var msg = '';
      try { var j = await res.json(); msg = j.detail || ''; } catch (e) {}
      restore();
      alert(msg || ('Failed (' + res.status + ')'));
    }).catch(function (e) { restore(); if (e && e.name !== 'AbortError') alert(e.message || e); });
  });

  /* ── the tray: results of tasks whose page went away ─────────────────────────────────────────────── */
  function textOf(body) {
    if (typeof body === 'string') return body;
    var keys = ['recap', 'summary', 'text', 'result', 'content', 'description', 'body', 'prep', 'tactics', 'insights', 'response'];
    for (var i = 0; i < keys.length; i++) if (body && typeof body[keys[i]] === 'string' && body[keys[i]].trim()) return body[keys[i]];
    if (body && body.detail) return String(body.detail);
    return JSON.stringify(body, null, 2);
  }

  function mountTray() {
    try { if (window.self !== window.top || document.body.classList.contains('nd-embed')) return; } catch (e) { return; }
    var now = Date.now();
    var items = load().filter(function (x) { return now - x.t < KEEP_MS; });
    save(items);
    items = items.filter(function (x) { return !live[x.id]; });
    if (!items.length) return;

    var pill = document.createElement('div');
    pill.id = 'nd-ai-tray';
    pill.setAttribute('role', 'status');
    pill.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:var(--nd-fab-bottom,14px);z-index:9997;' +
      'display:flex;flex-direction:column;gap:.35rem;align-items:center;max-width:92vw;font-size:.8rem';
    document.body.appendChild(pill);

    var state = {};          // id -> {label, status, data}
    function drop(id) { delete state[id]; forget(id); render(); }

    function view(id) {
      var s = state[id]; if (!s || !s.data) return;
      var ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;padding:1rem';
      var box = document.createElement('div');
      box.style.cssText = 'background:var(--bg2,#181820);border:1px solid var(--neon,#0ff);border-radius:8px;max-width:760px;width:100%;max-height:85vh;display:flex;flex-direction:column;padding:1rem';
      var head = document.createElement('div');
      head.style.cssText = 'display:flex;gap:.5rem;align-items:center;margin-bottom:.6rem';
      head.innerHTML = '<strong style="flex:1;color:var(--neon,#0ff)"></strong>';
      head.firstChild.textContent = '✨ ' + s.label + (s.data.http_status >= 400 ? ' — failed (' + s.data.http_status + ')' : '');
      var pre = document.createElement('pre');
      pre.style.cssText = 'white-space:pre-wrap;overflow:auto;margin:0;flex:1;font-family:inherit;font-size:.85rem;line-height:1.5';
      pre.textContent = textOf(s.data.body);
      function btn(text, fn) {
        var b = document.createElement('button'); b.type = 'button'; b.textContent = text; b.className = 'btn-secondary';
        b.style.cssText = 'font-size:.78rem'; b.onclick = fn; return b;
      }
      head.appendChild(btn('Copy', function () { try { navigator.clipboard.writeText(pre.textContent); } catch (e) {} }));
      head.appendChild(btn('Close', function () { ov.remove(); }));
      head.appendChild(btn('Done with it', function () { ov.remove(); cancelOnServer(id); drop(id); }));
      box.appendChild(head);
      var audio = s.data.body && (s.data.body.audio_url || s.data.body.file_url);    // a voice line / TTS clip: play it
      if (audio && typeof audio === 'string') {
        var a = document.createElement('audio'); a.controls = true; a.src = audio; a.style.cssText = 'width:100%;margin:0 0 .6rem';
        box.appendChild(a);
      }
      box.appendChild(pre); ov.appendChild(box);
      ov.addEventListener('click', function (e) { if (e.target === ov) ov.remove(); });
      document.body.appendChild(ov);
    }

    function render() {
      pill.innerHTML = '';
      var ids = Object.keys(state);
      var running = ids.filter(function (i) { return state[i].status === 'running'; });
      if (running.length) {
        var p = document.createElement('div');
        p.style.cssText = 'background:var(--bg2,#181820);border:1px solid var(--border,#444);border-radius:999px;padding:.35rem .8rem;color:var(--text-dim,#999);box-shadow:0 2px 10px rgba(0,0,0,.5)';
        var waiting = running.filter(function (i) { return state[i].position > 0; }).length;
        p.textContent = '✨ AI is working in the background (' + running.length + ')…' +
          (waiting ? ' ' + waiting + ' waiting in line' : '');
        pill.appendChild(p);
      }
      ids.filter(function (i) { return state[i].status === 'done'; }).forEach(function (i) {
        var b = document.createElement('button'); b.type = 'button';
        b.style.cssText = 'background:var(--bg2,#181820);border:1px solid var(--neon,#0ff);border-radius:999px;padding:.35rem .8rem;color:var(--neon,#0ff);cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.5);font:inherit';
        b.textContent = '✨ ' + state[i].label + ' ready — view';
        b.onclick = function () { view(i); };
        pill.appendChild(b);
      });
      if (!ids.length) pill.remove();
    }

    items.forEach(function (x) { state[x.id] = { label: x.label || 'AI task', status: 'running', data: null }; });
    render();

    var timer = setInterval(async function () {
      var pending = Object.keys(state).filter(function (i) { return state[i].status === 'running'; });
      if (!pending.length) { clearInterval(timer); return; }
      for (var k = 0; k < pending.length; k++) {
        var id = pending[k];
        try {
          var r = await fetch('/api/ai/tasks/' + id, { cache: 'no-store', credentials: 'same-origin' });
          if (r.status === 404 || r.status === 401 || r.status === 403) { drop(id); continue; }
          if (!r.ok) continue;
          var d = await r.json();
          if (d.status === 'running' || d.status === 'queued') { state[id].position = d.status === 'queued' ? d.position : 0; continue; }
          if (d.status === 'cancelled') { drop(id); continue; }
          state[id].status = 'done'; state[id].data = d; render();
        } catch (e) { /* offline: try again next tick */ }
      }
    }, 3000);
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountTray);
    else if (document.body) mountTray();
  }
})();
