// A failed fetch()'s body isn't always ours: a proxy in front of nd-world
// (Cloudflare, nginx, etc.) can answer a 502/504 with its own full HTML
// error page instead of anything nd-world sent. Reading that as `res.text()`
// and showing it verbatim dumps a wall of raw markup into the UI. Try our
// own {"detail": "..."} shape first; anything else collapses to a short,
// safe "HTTP <code>" rather than whatever the body actually contained.
async function ndApiErrorFrom(res) {
  let detail = `HTTP ${res.status}`;
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string" && data.detail) detail = data.detail;
  } catch (_) { /* non-JSON error body — keep the plain HTTP <code> */ }
  return new Error(detail);
}

// Shared by every background-job panel (Background Jobs page, the audio-jobs
// widget, AI Chat's chat/image job panels): several job backends (speech-to-text's
// /inference, Ollama's non-streaming chat()) are a single blocking call with
// no interim progress signal, so there's no real percentage to show for a
// job in flight. Elapsed time IS real, though, and lets a GM judge "still
// working" from "probably stuck" — call this again on each poll tick (every
// panel already re-renders every few seconds while anything's in progress)
// rather than running a separate timer, so it updates live for free.
function ndElapsedLabel(baseLabel, createdAtIso) {
  if (!createdAtIso) return baseLabel;
  let elapsedSec;
  try { elapsedSec = Math.max(0, Math.round((Date.now() - new Date(createdAtIso + "Z").getTime()) / 1000)); }
  catch (e) { return baseLabel; }
  const m = Math.floor(elapsedSec / 60), s = elapsedSec % 60;
  return `${baseLabel} (${m > 0 ? m + "m " + s + "s" : s + "s"} elapsed)`;
}

// Companion to ndElapsedLabel above, for a job that has already finished: a
// fixed duration between two recorded timestamps (AudioJob.run_started_at/
// created_at and .finished_at) rather than a live count up to Date.now().
// Returns "" (not a placeholder string) if either timestamp is missing —
// e.g. a job finished before this existed, or one interrupted before it
// ever started running — so callers can just skip appending it.
function ndDurationLabel(startIso, endIso) {
  if (!startIso || !endIso) return "";
  let sec;
  try { sec = Math.max(0, Math.round((new Date(endIso + "Z").getTime() - new Date(startIso + "Z").getTime()) / 1000)); }
  catch (e) { return ""; }
  const m = Math.floor(sec / 60), s = sec % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

// Shared by the Background Jobs page and every Session page's Summary/
// transcript panels — a rough "how big is this AI text" indicator, mirroring
// app/ai.py's _chars_per_token_estimate heuristic (English ~4 chars/token,
// ~2 for a non-ASCII-heavy script like Cyrillic/Arabic/CJK, sampled from the
// first 4000 chars) so the number shown here is in the same ballpark as what
// actually drives the server's num_ctx sizing (see context_sized_options) —
// not a real tokenizer call, just consistent with it. Keep this in sync with
// _chars_per_token_estimate if that heuristic ever changes.
function ndEstimateTokens(text) {
  if (!text) return 0;
  const sample = text.slice(0, 4000);
  let nonAscii = 0;
  for (let i = 0; i < sample.length; i++) {
    if (sample.charCodeAt(i) > 127) nonAscii++;
  }
  const charsPerToken = (sample.length && nonAscii / sample.length > 0.3) ? 2 : 4;
  return Math.ceil(text.length / charsPerToken);
}

function ndTokenLabel(text) {
  return `~${ndEstimateTokens(text).toLocaleString()} tokens`;
}

// Convenience for a live-typed textarea/input (id `srcId`) whose token count
// should update as the GM types, into a small label element (id `labelId`) —
// used by Session pages' Summary/transcript panels. Wrap the label text in
// parens itself; this just fills in (or clears) it.
function ndUpdateTokenCount(srcId, labelId) {
  const src = document.getElementById(srcId);
  const label = document.getElementById(labelId);
  if (!src || !label) return;
  const text = ('value' in src) ? src.value : src.textContent;
  label.textContent = text ? `(${ndTokenLabel(text)})` : '';
}

// Shared by world_edit.html and worlds.html's accent-colour pickers — keeps
// the native <input type=color> swatch and a typed hex-code field in sync
// both ways, since the color input alone doesn't let you type/paste a code.
function ndAccentFromPicker(picker) {
  var hexEl = document.getElementById(picker.dataset.hexId);
  var prevEl = document.getElementById(picker.dataset.prevId);
  if (hexEl) hexEl.value = picker.value;
  if (prevEl) prevEl.style.background = picker.value;
}
function ndAccentFromHex(hexInput) {
  var HEX_RE = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
  if (!HEX_RE.test(hexInput.value)) { hexInput.setCustomValidity('Enter a hex color like #00f0ff'); return; }
  hexInput.setCustomValidity('');
  var hex = hexInput.value;
  if (hex.length === 4) hex = '#' + [...hex.slice(1)].map(function(c){ return c + c; }).join('');
  var pickEl = document.getElementById(hexInput.dataset.pickId);
  var prevEl = document.getElementById(hexInput.dataset.prevId);
  if (pickEl) pickEl.value = hex;
  if (prevEl) prevEl.style.background = hex;
}

// Shared microphone recorder used by the Sessions audio-recap dropzone, the
// AI Chat/Ask AI attachment picker, and the AI page's Speech tab — implemented
// once here instead of three times. onStop(blob, mimeType) fires with the
// finished recording once the caller stops it; onError(err) fires if the
// browser denies mic access or has no MediaRecorder support at all. Returns
// {start, stop, isRecording} — start() is async (awaits the mic permission
// prompt) and resolves to false (after calling onError) if it couldn't
// start, true otherwise.
//
// echoCancellation/noiseSuppression/autoGainControl: the browser's own
// baseline audio processing (same class of tech Discord/Zoom/Meet apply
// before any fancier ML add-on) — helps with steady background noise and,
// if background audio (e.g. ambience/music) is playing from this SAME
// device, echo cancellation can meaningfully suppress it since that's
// exactly what AEC is designed to cancel: sound the device itself just
// played. It is NOT real music/vocal separation, though — background
// audio from a different speaker/device in the room won't be reliably
// removed by this (or by any realtime noise suppressor); that needs to be
// handled upstream (lower the music, a closer/directional mic) rather than
// fixed after the fact here.
// `existingStream`, if given (see the Live Recording panel in sessions/
// detail.html), is an already-acquired MediaStream this recorder should
// use instead of calling getUserMedia itself — ownership of that stream
// stays with the caller, so onstop here does NOT stop its tracks; the
// caller stops them once, when it's actually done with the whole
// recording, not once per short segment. Without this, re-acquiring the
// mic via getUserMedia on every ~60s segment costs an audible gap of
// latency, flickers the browser's mic indicator, and — on browsers that
// re-prompt or fail re-acquisition after backgrounding (mobile Safari in
// particular) — can silently kill the recording between segments. The
// one-shot 🎤 Record button still calls this with no third argument and
// keeps its existing self-contained (acquire-then-release) behavior.
function ndMicRecorder(onStop, onError, existingStream) {
  var mediaRecorder = null;
  var chunks = [];
  var stream = existingStream || null;
  var ownsStream = !existingStream;

  async function start() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      if (onError) onError(new Error('Microphone recording is not supported in this browser'));
      return false;
    }
    if (!stream) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (e) {
        if (onError) onError(e);
        return false;
      }
    }
    chunks = [];
    // Both the constructor and start() are unguarded on purpose in most
    // MediaRecorder examples, but with a REUSED stream (existingStream — see
    // this function's own doc above) that stream can have gone inactive
    // between segments (an OS-level mic reclaim, a Bluetooth reconnect, a
    // backgrounded tab's capture pipeline being torn down). start() is the
    // spec's designated thrower for a recording of an inactive stream
    // (InvalidStateError) — and since this function is `async`, an uncaught
    // synchronous throw here becomes a REJECTED PROMISE, not an exception the
    // caller sees, unless it's caught right here. See docs/LIVE_RECORDING_AUDIT.md
    // item 1 — this is the actual cause of a live recording silently stopping
    // after one chunk with the UI still claiming "Recording…".
    try {
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      mediaRecorder.onstop = function () {
        if (ownsStream) stream.getTracks().forEach(function (t) { t.stop(); });
        var mimeType = mediaRecorder.mimeType || 'audio/webm';
        onStop(new Blob(chunks, { type: mimeType }), mimeType);
      };
      // Per spec, an error that ends the stream still fires a final `stop`
      // afterward, so onStop above still runs and the partial segment still
      // gets queued — this handler exists purely so the failure is reported
      // to the caller instead of looking like a clean, ordinary end.
      mediaRecorder.onerror = function (e) {
        if (onError) onError((e && e.error) || new Error('Recording error'));
      };
      mediaRecorder.start();
    } catch (e) {
      mediaRecorder = null;
      if (onError) onError(e);
      return false;
    }
    return true;
  }

  function stop() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  }

  function isRecording() {
    return !!mediaRecorder && mediaRecorder.state === 'recording';
  }

  return { start: start, stop: stop, isRecording: isRecording };
}

// Picks a filename+extension the server's extension allowlists (all of
// which include .webm/.ogg/.wav) will accept, based on the MediaRecorder
// mimeType actually used — browsers don't agree on one default.
function ndMicFilename(mimeType) {
  var m = (mimeType || '').toLowerCase();
  var ext = m.indexOf('ogg') !== -1 ? 'ogg' : (m.indexOf('wav') !== -1 ? 'wav' : 'webm');
  return 'recording.' + ext;
}

// "Record → feed into an ndAiAttachments() picker" toggle for a mic button —
// used by /ai's chat compose bar and an entity's Ask AI panel, one instance
// per btnId. The recorded clip becomes a normal audio attachment, which
// already gets auto-transcribed by Unsloth Studio on upload and its transcript
// folded into the message content when sent (see _build_ollama_messages) —
// so this is how a mic turns into input for Ollama too, not just speech-to-text.
var _ndMicRecorders = {};
function ndToggleAttachMic(btnId, attachments) {
  var btn = document.getElementById(btnId);
  var rec = _ndMicRecorders[btnId];
  if (rec && rec.isRecording()) { rec.stop(); return; }
  rec = ndMicRecorder(
    function (blob, mimeType) {
      btn.textContent = '🎤';
      btn.style.color = '';
      attachments.addFiles([new File([blob], ndMicFilename(mimeType), { type: mimeType })]);
    },
    function (err) {
      btn.textContent = '🎤';
      btn.style.color = '';
      alert('Microphone error: ' + err.message);
    }
  );
  _ndMicRecorders[btnId] = rec;
  rec.start().then(function (started) {
    if (!started) return;  // ndMicRecorder already invoked the onError above
    btn.textContent = '⏺';
    btn.style.color = '#ff2d78';
  }).catch(function (err) {
    btn.textContent = '🎤';
    btn.style.color = '';
    alert('Microphone error: ' + err.message);
  });
}
