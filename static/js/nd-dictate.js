/* Dictation for a text box (the character journal): the browser's own speech recognition types into the box where the cursor
 * is. Nothing is recorded or kept by this app - the words arrive as text. Chrome / Edge / Safari do the listening (Chrome sends
 * the audio to its speech service; Firefox has none, so there the button is simply not shown).
 *
 * ndDictate.join(before, spoken)   - pure: add `spoken` to `before` with one space, a capital after a full stop, no doubles.
 * ndDictate.supported              - true when this browser can listen.
 * ndDictate.attach(textarea, btn)  - toggle listening for a textarea from a button; returns a stop() function.
 */
(function (root) {
  'use strict';
  function join(before, spoken) {
    var said = String(spoken || '').replace(/\s+/g, ' ').trim();
    if (!said) return String(before || '');
    var b = String(before || '');
    if (!b) return said.charAt(0).toUpperCase() + said.slice(1);
    var trimmed = b.replace(/[ \t]+$/, '');
    var last = trimmed.slice(-1);
    if (/[.!?]/.test(last) || last === '\n') said = said.charAt(0).toUpperCase() + said.slice(1);
    var sep = last === '\n' ? '' : ' ';
    return trimmed + sep + said;
  }
  var Rec = root && (root.SpeechRecognition || root.webkitSpeechRecognition);
  function attach(area, btn, opts) {
    if (!Rec) return function () {};
    var rec = null, on = false;
    var label = btn.textContent;
    function set(v) { on = v; btn.setAttribute('aria-pressed', String(v)); btn.textContent = v ? '⏹ Stop' : label; }
    function stop() { if (rec) { try { rec.stop(); } catch (e) { /* already stopped */ } } set(false); }
    btn.addEventListener('click', function () {
      if (on) { stop(); return; }
      rec = new Rec();
      rec.continuous = true; rec.interimResults = false;
      rec.lang = (opts && opts.lang) || (root.navigator && root.navigator.language) || 'en-US';
      rec.onresult = function (ev) {
        for (var i = ev.resultIndex; i < ev.results.length; i++) {
          if (ev.results[i].isFinal) {
            area.value = join(area.value, ev.results[i][0].transcript);
            area.dispatchEvent(new Event('input', { bubbles: true }));
          }
        }
      };
      rec.onerror = function (ev) {
        set(false);
        if (opts && opts.onError) opts.onError(ev && ev.error === 'not-allowed' ? 'Microphone permission was refused.' : 'Dictation stopped (' + ((ev && ev.error) || 'error') + ').');
      };
      rec.onend = function () { set(false); };
      try { rec.start(); set(true); } catch (e) { set(false); }
    });
    return stop;
  }
  var api = { join: join, supported: !!Rec, attach: attach };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ndDictate = api;
})(typeof window !== 'undefined' ? window : this);
