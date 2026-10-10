/* nd-world service worker - keeps the player's character page readable when the connection drops at the table.
 *
 *  - /static/*            cache-first (every asset URL is content-versioned, so a cached copy is never stale)
 *  - /characters/<id>     network-first; each good answer is kept, and when the network fails the last copy is served with
 *                         `window.__ND_OFFLINE__ = true` injected so the page can say so and queue HP changes
 *  - /api/characters/<id>/hub/now   network-first too, so the strip has numbers offline
 *
 * Nothing else is cached or touched (other pages, every POST, anything with ?embed=1 inside the in-page viewer).
 * Logging out sends `Clear-Site-Data: "cache", "storage"`, which empties these caches (see routers/auth.py). */
'use strict';
var VERSION = 'v1';
var PAGES = 'nd-pages-' + VERSION, ASSETS = 'nd-assets-' + VERSION;
var MAX_PAGES = 12;

/* what the worker does with a request: 'asset' | 'page' | 'api' | null (leave it to the network) */
function route(method, pathname, mode, search) {
  if (method !== 'GET') return null;
  if (pathname.indexOf('/static/') === 0) return 'asset';
  if (/^\/characters\/\d+\/?$/.test(pathname) && mode === 'navigate' && !/(^|[?&])embed=1(&|$)/.test(search || '')) return 'page';
  if (/^\/api\/characters\/\d+\/hub\/now$/.test(pathname)) return 'api';
  return null;
}
function cacheKey(url) {                       // ?w=<world> and friends must not make two copies of one sheet
  var u = new URL(url);
  return u.origin + u.pathname.replace(/\/$/, '');
}
function injectOffline(html) {
  var flag = '<script>window.__ND_OFFLINE__=true;</script>';
  return /<head[^>]*>/i.test(html) ? html.replace(/<head[^>]*>/i, function (m) { return m + flag; }) : flag + html;
}

if (typeof self !== 'undefined' && self.addEventListener) {
  self.addEventListener('install', function () { self.skipWaiting(); });
  self.addEventListener('activate', function (ev) {
    ev.waitUntil(caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== PAGES && k !== ASSETS && k.indexOf('nd-') === 0; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); }));
  });
  self.addEventListener('fetch', function (ev) {
    var req = ev.request, url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    var kind = route(req.method, url.pathname, req.mode, url.search);
    if (!kind) return;
    if (kind === 'asset') {
      ev.respondWith(caches.open(ASSETS).then(function (c) {
        return c.match(req).then(function (hit) {
          return hit || fetch(req).then(function (res) { if (res.ok) c.put(req, res.clone()); return res; });
        });
      }));
      return;
    }
    ev.respondWith(fetch(req).then(function (res) {
      if (res.ok && !res.redirected) {
        var copy = res.clone(), key = cacheKey(req.url);
        caches.open(PAGES).then(function (c) {
          return c.put(key, copy).then(function () { return c.keys(); }).then(function (keys) {
            return keys.length > MAX_PAGES ? c.delete(keys[0]) : null;
          });
        });
      }
      return res;
    }).catch(function () {
      return caches.open(PAGES).then(function (c) { return c.match(cacheKey(req.url)); }).then(function (hit) {
        if (!hit) return new Response(kind === 'api' ? '{}' : 'You are offline and this page was never opened on this device.',
                                      { status: 503, headers: { 'Content-Type': kind === 'api' ? 'application/json' : 'text/plain' } });
        if (kind === 'api') return hit;
        return hit.text().then(function (html) {
          return new Response(injectOffline(html), { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'X-ND-Offline': '1' } });
        });
      });
    }));
  });
}
if (typeof module !== 'undefined') module.exports = { route: route, cacheKey: cacheKey, injectOffline: injectOffline };
