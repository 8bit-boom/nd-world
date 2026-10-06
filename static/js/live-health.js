// Live recording health - pure functions, no DOM, so every decision is tested under Node (tests/test_live_health.py).
//
// The session page's live recording used to say "Stopped - transcript saved." the moment Stop was pressed, while the
// last chunk had not been cut yet and a backlog (a speech model slower than the table talks) still sat in the browser's
// memory. Everything here exists to tell the GM the truth: how much is recorded, how much is transcribed, how far
// behind it is, how long until it has caught up, and when something has gone quiet or stopped.
//   summarize         - the status line (text + level ok / busy / warn / bad) from the counters
//   updateSpeed/etaSec- how fast the speech model works through audio, and the time left to catch up
//   createSilenceMonitor - "the mic has heard nothing for N minutes" from a stream of loudness samples
//   reconnectDelayMs  - pacing for getting a lost mic back: quick at first, then every 15 s for about half an hour
//   detectPause       - the computer / tab being suspended (laptop sleep) from a gap between heartbeats
//   storeKey          - the key a chunk is kept under in the local store (live-store.js)
(function (root) {
  'use strict';
  var QUICK_ATTEMPTS = 3, SLOW_MS = 15000, SLOW_ATTEMPTS = 120;
  var RECONNECT_ATTEMPTS = QUICK_ATTEMPTS + SLOW_ATTEMPTS;

  function num(v) { v = Number(v); return isFinite(v) ? v : 0; }
  function pad(n, w) { n = String(n); while (n.length < w) n = '0' + n; return n; }
  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  // 3725 -> "1:02:05", 125 -> "2:05"
  function clock(sec) {
    sec = Math.max(0, Math.floor(num(sec)));
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return h ? h + ':' + pad(m, 2) + ':' + pad(s, 2) : m + ':' + pad(s, 2);
  }

  // 5400 -> "1 h 30 min", 20 -> "under a minute"
  function humanDuration(sec) {
    sec = num(sec);
    if (sec < 60) return 'under a minute';
    var mins = Math.round(sec / 60), h = Math.floor(mins / 60), m = mins % 60;
    return (h ? h + ' h' : '') + (h && m ? ' ' : '') + (m || !h ? m + ' min' : '');
  }

  // Audio seconds the speech model gets through per second of waiting, smoothed so one slow chunk does not swing it.
  function updateSpeed(prev, audioSec, tookSec) {
    audioSec = num(audioSec); tookSec = num(tookSec);
    if (!(audioSec > 0 && tookSec > 0)) return prev == null ? null : prev;
    var sample = audioSec / tookSec;
    return prev == null || !(prev > 0) ? sample : prev * 0.6 + sample * 0.4;
  }

  function etaSec(queuedSec, speed) {
    queuedSec = num(queuedSec);
    if (!(queuedSec > 0)) return 0;
    return speed > 0 ? queuedSec / speed : null;
  }

  // state: {recording, finalizing, recordedSec, doneSec, queuedSec, chunksWaiting, uploading, failedChunks, failedSec, speed}
  //   recordedSec - audio captured so far; doneSec - audio the server has transcribed; queuedSec - audio cut and waiting
  //   finalizing  - Stop was pressed and the last chunk has not been cut and queued yet
  function summarize(state) {
    state = state || {};
    var rec = num(state.recordedSec), behind = num(state.queuedSec), waiting = Math.max(0, Math.floor(num(state.chunksWaiting)));
    var failed = Math.max(0, Math.floor(num(state.failedChunks)));
    var eta = etaSec(behind, state.speed);
    var out = { text: '', level: 'ok', behindSec: behind, etaSec: eta };

    if (failed) {
      out.level = 'bad';
      out.text = '⚠ ' + plural(failed, 'chunk') + ' (' + clock(state.failedSec) + ' of audio) could not be sent and are NOT saved yet — ';
      out.text += state.recording ? 'recording continues; press Retry below once the problem is fixed.' : 'press Retry below.';
      return out;
    }
    if (state.recording) {
      out.text = 'Recording… ' + clock(rec);
      if (behind > 0 || waiting > 0) {
        out.text += ' · transcript is ' + clock(behind) + ' behind (' + plural(waiting, 'chunk') + ' waiting)';
        out.level = behind > 300 ? 'warn' : 'busy';
      } else {
        out.text += num(state.doneSec) > 0 ? ' · transcript saved up to ' + clock(state.doneSec) : ' · nothing sent to the transcriber yet';
      }
      return out;
    }
    if (state.finalizing) {
      out.level = 'warn';
      out.text = 'Stopped — finishing the last chunk (' + clock(rec) + ' recorded)… keep this page open.';
      return out;
    }
    if (waiting > 0 || behind > 0 || state.uploading) {
      out.level = 'warn';
      out.text = 'Stopped — still transcribing ' + plural(waiting, 'chunk') + ' (' + clock(behind) + ' of audio' +
        (eta ? (eta < 60 ? ', ' : ', about ') + humanDuration(eta) + ' left' : '') + '). Keep this page open until it says saved.';
      return out;
    }
    if (rec > 0) out.text = '✔ Stopped — all ' + clock(rec) + ' transcribed and saved.';
    return out;
  }

  // Loudness samples (RMS 0..1) with timestamps in; "has been quiet for >= silentAfterSec" out.
  function createSilenceMonitor(opts) {
    opts = opts || {};
    var threshold = opts.thresholdRms > 0 ? opts.thresholdRms : 0.004;
    var after = opts.silentAfterSec > 0 ? opts.silentAfterSec : 150;
    var quietSince = null, silent = false;
    return {
      push: function (rms, nowMs) {
        if (num(rms) >= threshold) {
          var recovered = silent;
          quietSince = null; silent = false;
          return { silent: false, silentForSec: 0, justBecameSilent: false, justRecovered: recovered };
        }
        if (quietSince == null) quietSince = nowMs;
        var forSec = (nowMs - quietSince) / 1000, became = false;
        if (!silent && forSec >= after) { silent = true; became = true; }
        return { silent: silent, silentForSec: forSec, justBecameSilent: became, justRecovered: false };
      },
    };
  }

  // Getting a lost mic back: 1 s, 2 s, 3 s, then every 15 s - about half an hour in all (RECONNECT_ATTEMPTS tries).
  function reconnectDelayMs(attempt) {
    attempt = Math.max(1, Math.floor(num(attempt)));
    return attempt <= QUICK_ATTEMPTS ? attempt * 1000 : SLOW_MS;
  }

  // A heartbeat that fires every second finding a gap this long between two beats means the computer or tab was suspended.
  function detectPause(lastBeatMs, nowMs, graceMs) {
    var gap = nowMs - lastBeatMs, grace = graceMs > 0 ? graceMs : 15000;
    return gap > grace ? { paused: true, seconds: Math.round(gap / 1000) } : { paused: false, seconds: 0 };
  }

  function pauseMessage(seconds) {
    return '⚠ This computer (or this tab) was paused for ' + clock(seconds) + ' — that stretch was not recorded.';
  }

  function storeKey(sessionId, recordingId, index) { return sessionId + ':' + recordingId + ':' + pad(Math.max(0, Math.floor(num(index))), 6); }
  function parseStoreKey(key) {
    var m = /^(\d+):([^:]+):(\d+)$/.exec(String(key || ''));
    return m ? { sessionId: parseInt(m[1], 10), recordingId: m[2], index: parseInt(m[3], 10) } : null;
  }

  root.ndLiveHealth = {
    clock: clock, humanDuration: humanDuration, updateSpeed: updateSpeed, etaSec: etaSec, summarize: summarize,
    createSilenceMonitor: createSilenceMonitor, reconnectDelayMs: reconnectDelayMs, RECONNECT_ATTEMPTS: RECONNECT_ATTEMPTS,
    detectPause: detectPause, pauseMessage: pauseMessage, storeKey: storeKey, parseStoreKey: parseStoreKey,
  };
})(typeof window !== 'undefined' ? window : globalThis);
