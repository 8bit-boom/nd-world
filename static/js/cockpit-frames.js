// Cockpit window loader + window policy (see tests/test_cockpit_scaling.py, which runs this file under Node).
//
// A cockpit window is a whole nd-world page in an iframe, so N windows are N documents: ~15-35 MB each and ~15
// requests each. Measured with 48 windows: ~750 requests in the first 6 s and ~770 MB more browser memory. Three
// rules keep that in check, all driven from one IntersectionObserver per loader:
//   1. Stagger: a page starts loading only when a slot is free (4 at a time; 2 on a phone), in the order the
//      windows became visible - not all at once.
//   2. Show first: a window that is not showing (collapsed, an inactive phone tab, a hidden tab of the character
//      page) does not load until it is.
//   3. Give back: a window hidden BY THE LAYOUT (display:none -> a 0x0 box) for a minute is unloaded (about:blank)
//      and loads again when shown. A window merely scrolled out of view keeps its page - dropping it would lose
//      its state for nothing. Collapsing and switching tabs briefly costs nothing.
(function (root) {
  'use strict';

  function createFrameLoader(opts) {
    opts = opts || {};
    var concurrency = opts.concurrency || 4;
    var loadTimeoutMs = opts.loadTimeoutMs || 6000;   // a page that never answers must not hold a slot forever
    var hiddenGraceMs = opts.hiddenGraceMs == null ? 60000 : opts.hiddenGraceMs;
    var setT = opts.setTimeout || function (fn, ms) { return root.setTimeout(fn, ms); };
    var clearT = opts.clearTimeout || function (t) { return root.clearTimeout(t); };
    var IO = opts.IntersectionObserver === undefined ? root.IntersectionObserver : opts.IntersectionObserver;

    var entries = new Map();   // frame -> {url, state, hiddenTimer, loadTimer, release}
    var queue = [];            // frames waiting for a slot
    var active = 0;
    var io = IO ? new IO(onIntersect, { rootMargin: '300px' }) : null;

    // states: waiting (not showing yet) | queued | loading | loaded | suspended (given back)

    function enqueue(f) {
      var e = entries.get(f);
      if (!e || e.state === 'queued' || e.state === 'loading' || e.state === 'loaded') return;
      e.state = 'queued';
      queue.push(f);
      pump();
    }

    function pump() {
      while (active < concurrency && queue.length) start(queue.shift());
    }

    function start(f) {
      var e = entries.get(f);
      if (!e || e.state !== 'queued') return;
      e.state = 'loading';
      active++;
      var done = false;
      function release() {
        if (done) return;
        done = true;
        clearT(e.loadTimer);
        f.removeEventListener('load', release);
        f.removeEventListener('error', release);
        active--;
        if (e.state === 'loading') e.state = 'loaded';
        e.release = null;
        pump();
      }
      e.release = release;
      f.addEventListener('load', release);
      f.addEventListener('error', release);
      e.loadTimer = setT(release, loadTimeoutMs);
      f.src = e.url;
    }

    function suspend(f) {
      var e = entries.get(f);
      if (!e) return;
      clearT(e.hiddenTimer); e.hiddenTimer = null;
      var qi = queue.indexOf(f);
      if (qi !== -1) queue.splice(qi, 1);
      if (e.release) e.release();
      if (e.state === 'loaded' || e.state === 'loading') {
        f.src = 'about:blank';
        e.state = 'suspended';
      } else if (e.state === 'queued') {
        e.state = 'waiting';
      }
    }

    function onIntersect(list) {
      list.forEach(function (en) {
        var f = en.target, e = entries.get(f);
        if (!e) return;
        if (en.isIntersecting) {
          clearT(e.hiddenTimer); e.hiddenTimer = null;
          if (e.state === 'waiting' || e.state === 'suspended') enqueue(f);
          return;
        }
        var r = en.boundingClientRect || {};
        var hiddenByLayout = !r.width && !r.height;   // display:none -> a 0x0 box; scrolled away keeps its size
        if (!hiddenByLayout) { clearT(e.hiddenTimer); e.hiddenTimer = null; return; }
        if (e.state === 'queued') { suspend(f); return; }
        if ((e.state === 'loaded' || e.state === 'loading') && !e.hiddenTimer) {
          e.hiddenTimer = setT(function () { e.hiddenTimer = null; suspend(f); }, hiddenGraceMs);
        }
      });
    }

    return {
      // Load `url` into the iframe once it is showing (or right away, staggered, without an observer).
      mount: function (f, url) {
        entries.set(f, { url: url, state: 'waiting', hiddenTimer: null, loadTimer: null, release: null });
        if (io) io.observe(f); else enqueue(f);
      },
      // The ⟳ button: re-request the window's original page; wake it if it was given back.
      reload: function (f) {
        var e = entries.get(f);
        if (!e) { f.src = f.src; return; }
        if (e.state === 'loaded' || e.state === 'loading') { f.src = e.url; return; }
        if (e.state === 'suspended' || e.state === 'waiting') enqueue(f);
      },
      suspend: suspend,
      forget: function (f) {
        var e = entries.get(f);
        if (!e) return;
        clearT(e.hiddenTimer); clearT(e.loadTimer);
        var qi = queue.indexOf(f);
        if (qi !== -1) queue.splice(qi, 1);
        if (e.release) e.release();
        if (io) io.unobserve(f);
        entries.delete(f);
      },
      forgetAll: function () {
        var self = this;
        Array.from(entries.keys()).forEach(function (f) { self.forget(f); });
      },
      stats: function () {
        var s = { waiting: 0, queued: 0, loading: 0, loaded: 0, suspended: 0, live: 0 };
        entries.forEach(function (e) { s[e.state]++; });
        s.live = s.queued + s.loading + s.loaded;
        return s;
      },
    };
  }

  // May another window be added? `liveFrames` = pages currently held in memory. The server saves at most `max`
  // windows per layout (a larger layout is rejected whole), so the page stops there too; and it says so once, when
  // the number of live pages reaches a level that gets heavy (12 on a desktop, 5 on a phone / low-memory device).
  function cockpitWindowPolicy(o) {
    var warnAt = o.lowMemory ? 5 : 12;
    if (o.count >= o.max) {
      return { allow: false, warn: false, message: 'A cockpit holds up to ' + o.max + ' windows - close one first.' };
    }
    if (o.liveFrames + 1 === warnAt) {
      return { allow: true, warn: true, message: warnAt + ' live pages are open at once, and each is a whole page in memory. ' +
        'Collapse or close the ones you are not using - a collapsed window gives its page back after a minute.' };
    }
    return { allow: true, warn: false, message: '' };
  }

  root.ndCreateFrameLoader = createFrameLoader;
  root.ndCockpitWindowPolicy = cockpitWindowPolicy;
})(typeof window !== 'undefined' ? window : globalThis);
