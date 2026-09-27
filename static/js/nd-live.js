// Phase-1 live sync (client half) — see app/live.py for the server side.
//
// One elected page per browser holds the /api/live EventSource; every other
// open window (main tabs, floated panels, second monitor windows) receives
// the same version numbers relayed over a BroadcastChannel. Pages react to
// a version bump via the window event "nd-live" — typically
// ndLiveRefetch('#some-server-rendered-strip'), which re-fetches the current
// URL and swaps in the fresh HTML for that one element (the server render
// stays the single source of truth; no client-side re-implementations).
//
// Why the election: every nd-world page is a full document, and a GM during
// a session commonly has 3-5 nd-world windows open. Browsers cap concurrent
// HTTP/1.1 connections per host at six, so one SSE stream per window would
// starve image/form traffic on direct-LAN access (behind Cloudflare it's
// HTTP/2 and multiplexed, but the app is self-hosted first). Election is
// deliberately dumb and self-healing: pages probe the channel, an existing
// leader answers, otherwise (after a short grace period) the newest page
// takes over. If the leader tab closes, its stale localStorage heartbeat
// times out and any remaining page promotes itself. Two leaders can coexist
// briefly after a race — harmless, it just means two connections for a few
// seconds.
//
// This file is self-contained by design (same rule as nd-poll.js) and is
// included from base.html, so every page opts in automatically. Pages with
// no active world (data-world-slug missing) do nothing.
(function () {
  var HEARTBEAT_KEY = 'nd_live_leader_beat';
  var HEARTBEAT_MS = 5000;
  var STALE_MS = 16000; // just over 3 missed heartbeats
  var GRACE_MS = 300;   // how long we wait for an "I'm the leader" answer

  var worldSlug = document.body ? document.body.dataset.worldSlug : '';
  if (!worldSlug || typeof BroadcastChannel === 'undefined') {
    // No world context (login page, error pages) or no BroadcastChannel
    // (very old browser): nothing to do — pages just don't live-sync.
    return;
  }

  // Embedded documents (cockpit windows, floated-panel iframes — see
  // nd-float.js) NEVER hold the SSE connection: the top page leads, and
  // this document just listens for relayed versions on the channel.
  // Without this, a cockpit with N embed windows opened simultaneously
  // would race N leader elections and open N connections (each iframe
  // probes, none answers in the grace period, all self-crown), which is
  // exactly the per-browser connection multiplication the election exists
  // to prevent.
  var IN_IFRAME = false;
  try { IN_IFRAME = window.parent !== window; } catch (e) { IN_IFRAME = true; }

  var channel = new BroadcastChannel('nd-live-' + worldSlug);
  var es = null;
  var leader = false;
  var decided = false;
  var heartbeatTimer = null;
  var watchdogTimer = null;

  function dispatch(v) {
    window.dispatchEvent(new CustomEvent('nd-live', { detail: { version: v } }));
  }

  function becomeLeader() {
    if (leader || IN_IFRAME) return;
    leader = true;
    try { connect(); } catch (e) { /* SSE unavailable — page just won't sync */ }
    heartbeatTimer = setInterval(function () {
      try { localStorage.setItem(HEARTBEAT_KEY + ':' + worldSlug, String(Date.now())); } catch (e) {}
    }, HEARTBEAT_MS);
    try { localStorage.setItem(HEARTBEAT_KEY + ':' + worldSlug, String(Date.now())); } catch (e) {}
  }

  function connect() {
    es = new EventSource('/api/live');
    es.addEventListener('version', function (e) {
      dispatch(e.data);
      try { channel.postMessage({ type: 'version', version: e.data }); } catch (err) {}
    });
    // EventSource auto-reconnects on error; nothing to do.
  }

  function becomeFollower() {
    // Watch the leader's heartbeat; promote ourselves if it goes stale
    // (leader tab closed/crashed — EventSource dies with its document).
    watchdogTimer = setInterval(function () {
      var last = 0;
      try { last = parseInt(localStorage.getItem(HEARTBEAT_KEY + ':' + worldSlug) || '0', 10) || 0; } catch (e) {}
      if (!last || Date.now() - last > STALE_MS) {
        clearInterval(watchdogTimer);
        becomeLeader();
      }
    }, HEARTBEAT_MS);
  }

  channel.onmessage = function (ev) {
    var m = ev.data || {};
    if (m.type === 'probe') {
      if (leader) { try { channel.postMessage({ type: 'iam' }); } catch (e) {} }
    } else if (m.type === 'iam') {
      if (!decided) { decided = true; becomeFollower(); }
    } else if (m.type === 'version') {
      dispatch(m.version);
    }
  };

  // Elect (or find) a leader.
  try { channel.postMessage({ type: 'probe' }); } catch (e) {}
  setTimeout(function () {
    if (!decided) { decided = true; becomeLeader(); }
  }, GRACE_MS);

  // If we were the leader, vacate on the way out so another page takes
  // over immediately instead of after the heartbeat times out.
  window.addEventListener('pagehide', function () {
    if (!leader) return;
    try { localStorage.removeItem(HEARTBEAT_KEY + ':' + worldSlug); } catch (e) {}
    try { channel.postMessage({ type: 'probe' }); } catch (e) {}
  });
})();

// Re-fetch the CURRENT url and swap one server-rendered element for its
// fresh twin — the standard "react to nd-live" for any panel. Debounced so
// a burst of changes (bulk XP award, loot claim spree) triggers one fetch.
// Pass a CSS selector that exists in the normal page render; a selector the
// fresh HTML no longer contains (e.g. the party was deleted) leaves the
// page untouched rather than blanking it.
function ndLiveRefetch(selector) {
  var timer = null;
  function swap() {
    var cur = document.querySelector(selector);
    if (!cur) return;
    fetch(location.pathname + location.search, { headers: { 'X-ND-Partial': selector } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html || !document.querySelector(selector)) return;
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.querySelector(selector);
        if (fresh) document.querySelector(selector).replaceWith(document.importNode(fresh, true));
      })
      .catch(function () {});
  }
  window.addEventListener('nd-live', function () {
    clearTimeout(timer);
    timer = setTimeout(swap, 400);
  });
}
