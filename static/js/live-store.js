// A copy of every live-recording chunk in the browser's own storage (IndexedDB) until the server has transcribed it.
//
// A live chunk used to exist only in the page's memory from the moment it was cut until the server had it - and with a
// speech model slower than the table talks, that is a growing backlog. Reload the page, close the tab, a crash, a long
// outage (the upload ladder gives up after ~9 minutes): the tail of the session was gone. Now each chunk is written here
// as it is cut and removed when the server confirms it; on the next visit the page offers to upload what is left.
//
// Everything is best-effort and never throws: no IndexedDB (a private window), a full quota, a blocked database - the
// recording itself must never depend on this, so every method just resolves false / [] / 0. Keyed by
// ndLiveHealth.storeKey. Tested under Node with an in-memory fake (tests/test_live_health.py).
(function (root) {
  'use strict';
  var DB_NAME = 'nd-live-recording', STORE = 'segments';

  function create(idb) {
    var dbPromise = null, failed = false;

    function open() {
      if (failed || !idb) return Promise.resolve(null);
      if (dbPromise) return dbPromise;
      dbPromise = new Promise(function (resolve) {
        var req;
        try { req = idb.open(DB_NAME, 1); } catch (e) { failed = true; resolve(null); return; }
        req.onupgradeneeded = function () {
          var db = req.result;
          if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'key' });
        };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = req.onblocked = function () { failed = true; resolve(null); };
      });
      return dbPromise;
    }

    function write(fn) {
      return open().then(function (db) {
        if (!db) return false;
        return new Promise(function (resolve) {
          try {
            var tx = db.transaction(STORE, 'readwrite');
            fn(tx.objectStore(STORE));
            tx.oncomplete = function () { resolve(true); };
            tx.onerror = tx.onabort = function () { resolve(false); };
          } catch (e) { resolve(false); }
        });
      });
    }

    function put(rec) { return rec && rec.key ? write(function (s) { s.put(rec); }) : Promise.resolve(false); }
    function remove(key) { return write(function (s) { s.delete(key); }); }

    // This session's chunks: earlier recordings first (by when they were first saved), then by segment.
    function list(sessionId) {
      return open().then(function (db) {
        if (!db) return [];
        return new Promise(function (resolve) {
          try {
            var req = db.transaction(STORE, 'readonly').objectStore(STORE).getAll();
            req.onsuccess = function () {
              var rows = (req.result || []).filter(function (r) { return r && r.sessionId === sessionId; });
              var first = {};
              rows.forEach(function (r) { first[r.recordingId] = Math.min(first[r.recordingId] == null ? Infinity : first[r.recordingId], r.savedAt || 0); });
              rows.sort(function (a, b) {
                return (first[a.recordingId] - first[b.recordingId]) || (a.recordingId < b.recordingId ? -1 : a.recordingId > b.recordingId ? 1 : 0) || (a.index - b.index);
              });
              resolve(rows);
            };
            req.onerror = function () { resolve([]); };
          } catch (e) { resolve([]); }
        });
      });
    }

    function count(sessionId) { return list(sessionId).then(function (rows) { return rows.length; }); }
    return { put: put, remove: remove, list: list, count: count };
  }

  root.ndLiveStore = { create: create, shared: create(typeof indexedDB !== 'undefined' ? indexedDB : undefined) };
})(typeof window !== 'undefined' ? window : globalThis);
