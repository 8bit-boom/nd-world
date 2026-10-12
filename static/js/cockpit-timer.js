// The cockpit's timer window: its state machine, pure (no DOM, no clock of its own - `now` is passed in) and tested under Node
// (tests/test_cockpit_timer.py). cockpit.js draws it and keeps the state in the window's data.timer, which is saved with the layout.
//
// Wall-clock, not ticks: a countdown is an END TIME and a stopwatch a START TIME (epoch milliseconds), so a background tab whose
// timers are throttled, a laptop that slept, a reload or an Undo-close all still show the true time left.
//
//   state: { mode: 'down'|'up', minutes, running, done, endAt, startAt, remainMs, elapsedMs }
//     down: while running the time left is endAt - now; paused / stopped it is remainMs
//     up:   while running the time elapsed is elapsedMs + (now - startAt); paused it is elapsedMs
(function (root) {
  'use strict';

  var MAX_MINUTES = 600, MAX_EPOCH = 4102444800000;       // 2100-01-01

  function num(v, lo, hi, d) {
    v = Number(v);
    return isFinite(v) ? Math.max(lo, Math.min(hi, Math.round(v))) : d;
  }

  // Whatever was saved (or tampered with) becomes a valid state.
  function normalize(t) {
    t = t && typeof t === 'object' && !Array.isArray(t) ? t : {};
    t.mode = t.mode === 'up' ? 'up' : 'down';
    t.minutes = num(t.minutes, 1, MAX_MINUTES, 5);
    t.running = !!t.running;
    t.done = !!t.done;
    t.endAt = num(t.endAt, 0, MAX_EPOCH, 0);
    t.startAt = num(t.startAt, 0, MAX_EPOCH, 0);
    t.remainMs = t.remainMs == null ? t.minutes * 60000 : num(t.remainMs, 0, MAX_MINUTES * 60000, t.minutes * 60000);
    t.elapsedMs = num(t.elapsedMs, 0, 100 * 3600000, 0);
    return t;
  }

  function left(t, now) { return t.running ? Math.max(0, t.endAt - now) : t.remainMs; }
  function elapsed(t, now) { return t.running ? t.elapsedMs + Math.max(0, now - t.startAt) : t.elapsedMs; }
  function expired(t, now) { return t.mode === 'down' && t.running && now >= t.endAt; }

  // A countdown shows whole seconds rounded UP, so 00:00 appears only when it is really over; a stopwatch rounds down.
  function fmt(ms, up) {
    var s = Math.max(0, up ? Math.floor(ms / 1000) : Math.ceil(ms / 1000));
    var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, r = s % 60;
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return (h ? h + ':' : '') + two(m) + ':' + two(r);
  }

  function reset(t) {
    t.running = false; t.done = false;
    t.remainMs = t.minutes * 60000; t.elapsedMs = 0;
    return t;
  }
  function setMinutes(t, n) {
    t.minutes = num(n, 1, MAX_MINUTES, 5);
    return t;
  }
  function setMode(t, mode) {
    t.mode = mode === 'up' ? 'up' : 'down';
    return reset(t);
  }

  // Start, or pause when it is running. A timer that has gone off starts again from the full length.
  function toggle(t, now) {
    if (t.done) reset(t);
    if (t.running) {
      if (t.mode === 'up') t.elapsedMs += Math.max(0, now - t.startAt);
      else t.remainMs = Math.max(0, t.endAt - now);
      t.running = false;
      return 'paused';
    }
    if (t.mode === 'up') t.startAt = now;
    else { if (t.remainMs <= 0) t.remainMs = t.minutes * 60000; t.endAt = now + t.remainMs; }
    t.running = true;
    return 'started';
  }

  // +/- a minute: on the end time while running, on what is left otherwise; never below one second, and a finished one starts over.
  function adjust(t, ms, now) {
    if (t.mode !== 'down') return t;
    if (t.done) reset(t);
    if (t.running) t.endAt = Math.max(now + 1000, t.endAt + ms);
    else t.remainMs = Math.max(1000, Math.min(MAX_MINUTES * 60000, t.remainMs + ms));
    return t;
  }

  // The countdown reached zero.
  function expire(t) {
    t.running = false; t.done = true; t.remainMs = 0;
    return t;
  }

  // Coming back to a saved timer (a reload, an Undo-close, another browser): what is it doing now?
  //   'running' carry on | 'expired' ran out while nobody was looking (shown as done, silently) | 'done' was already over | 'idle'
  function wake(t, now) {
    if (expired(t, now)) { expire(t); return 'expired'; }
    if (t.running) return 'running';
    return t.done ? 'done' : 'idle';
  }

  // What the start button says.
  function startLabel(t) {
    if (t.running) return 'Pause';
    if (t.done) return 'Again';
    var touched = t.mode === 'up' ? t.elapsedMs > 0 : t.remainMs < t.minutes * 60000;
    return touched ? 'Resume' : 'Start';
  }

  root.ndCockpitTimer = {
    MAX_MINUTES: MAX_MINUTES,
    normalize: normalize, left: left, elapsed: elapsed, expired: expired, fmt: fmt,
    reset: reset, setMinutes: setMinutes, setMode: setMode, toggle: toggle, adjust: adjust, expire: expire, wake: wake,
    startLabel: startLabel,
  };
})(typeof window !== 'undefined' ? window : globalThis);
