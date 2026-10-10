/* Offline at the table (character page only): registers the service worker (static/sw.js), says so when the page being shown is
 * the saved copy, and keeps HP / Shock changes made without a connection - they show on screen straight away and are sent, in
 * order, when the connection is back. The decisions live in nd-offline-core.js. */
(function () {
  'use strict';
  var core = window.ndOfflineCore;
  if (!core) return;
  var KEY = 'ndOfflineQueue:v1', STATE = 'ndOfflineState:v1:';
  var realFetch = window.fetch.bind(window);

  function read(key, dflt) { try { var v = JSON.parse(localStorage.getItem(key)); return v == null ? dflt : v; } catch (e) { return dflt; } }
  function write(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* private mode: no queue, requests just fail */ } }
  var num = function (id) { var el = document.getElementById(id); var n = el ? parseInt(el.textContent, 10) : NaN; return isNaN(n) ? 0 : n; };

  /* what is on screen is the best guess of the numbers; every real answer refreshes it */
  function stateFor(pc) {
    var s = read(STATE + pc, null);
    if (!s) s = { hp: num('hp-current'), max_hp: num('hp-max'), temp_hp: 0, shock: num('shock-current'), shock_max: num('shock-max') };
    return s;
  }
  function remember(pc, d) {
    var s = stateFor(pc);
    if (d.current_hp !== undefined) { s.hp = d.current_hp; s.max_hp = d.max_hp; s.temp_hp = d.temp_hp || 0; }
    if (d.shock_current !== undefined) { s.shock = d.shock_current; s.shock_max = d.shock_max; }
    write(STATE + pc, s);
  }

  function banner() {
    var b = document.getElementById('nd-offline-banner');
    var offline = navigator.onLine === false || window.__ND_OFFLINE__;
    var queued = read(KEY, []).length;
    if (!offline && !queued) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement('div');
      b.id = 'nd-offline-banner';
      b.setAttribute('role', 'status');
      b.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2000;background:#3a2a00;color:#ffd54a;font-size:.8rem;text-align:center;padding:.3rem .6rem;border-bottom:1px solid #ffd54a';
      document.body.appendChild(b);
    }
    b.textContent = (offline ? '📴 Offline — ' + (window.__ND_OFFLINE__ ? 'this is your last saved copy. ' : '') : '↻ Sending your changes… ')
      + (queued ? queued + ' change' + (queued === 1 ? '' : 's') + ' will be sent when you are back online.' : (offline ? 'HP changes are kept and sent when you are back.' : ''));
  }

  window.fetch = function (input, init) {
    var url, method = 'GET', body = null;
    try {
      url = new URL(typeof input === 'string' ? input : input.url, location.origin);
      method = (init && init.method) || (typeof input !== 'string' && input.method) || 'GET';
      if (init && typeof init.body === 'string') body = JSON.parse(init.body);
    } catch (e) { return realFetch(input, init); }
    var change = body && url.origin === location.origin ? core.classify(method, url.pathname, body) : null;
    if (!change) return realFetch(input, init);
    var queueIt = function () {
      var s = stateFor(change.pc), res = core.apply(s, change);
      write(STATE + change.pc, s);
      var q = core.enqueue(read(KEY, []), change);
      write(KEY, q);
      banner();
      return new Response(JSON.stringify(res), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    if (navigator.onLine === false) return Promise.resolve(queueIt());
    return realFetch(input, init).then(function (r) {
      if (r.ok) r.clone().json().then(function (d) { remember(change.pc, d); }).catch(function () { /* not JSON */ });
      return r;
    }, function () { return queueIt(); });          // the request could not even leave: same as offline
  };

  var replaying = false;
  async function replay() {
    var q = read(KEY, []);
    if (replaying || !q.length || navigator.onLine === false) { banner(); return; }
    replaying = true;
    try {
      while (q.length) {
        var req = core.requestFor(q[0]);
        var r = await realFetch(req.url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req.body) });
        if (r.status >= 500) break;                // server trouble: keep the queue, try again later
        q.shift();                                 // sent, or refused for good (logged out, no longer yours): never retry it
        write(KEY, q);
        if (r.ok) r.json().then(function (d) { remember(parseInt(req.url.split('/')[3], 10), d); }).catch(function () {});
      }
    } catch (e) { /* still offline */ }
    replaying = false;
    banner();
    window.dispatchEvent(new CustomEvent('nd-live', { detail: { version: 'offline-replay' } }));   // the strip refreshes itself
  }

  window.addEventListener('online', replay);
  window.addEventListener('offline', banner);
  document.addEventListener('DOMContentLoaded', function () { banner(); replay(); });
  if (document.readyState !== 'loading') { banner(); replay(); }

  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () { /* unsupported / blocked: the page works as before */ });
  }
})();
