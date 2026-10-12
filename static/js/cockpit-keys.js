// The cockpit's keyboard shortcuts: ONE table that drives both the handler (cockpit.js) and the help overlay (F1), so the list on
// screen can never drift from what the keys do. Pure - no DOM - and tested under Node (tests/test_cockpit_keys.py).
//
// Why these keys:
//  - matched by `code` (the physical key), never by the character: a Russian, Hebrew or Greek layout types other letters on the
//    same keys, and Option on a Mac types a different one;
//  - Alt + letter / digit, NOT Ctrl + Alt (that is AltGr on many European keyboards and types characters there), and never the
//    ones a browser keeps for itself (Ctrl+T / N / W / Shift+T / L, Alt+Left / Right, Alt+D);
//  - Ctrl+K is the quick switcher (static/js/command-palette.js owns it - `external` here: listed for the help, never handled here).
(function (root) {
  'use strict';

  var DIGITS = ['Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5', 'Digit6', 'Digit7', 'Digit8', 'Digit9'];

  // keys: what the help shows | code / codes / key: what matches | ctrl, alt, meta, shift: the modifiers that must be down
  // (shift: false unless stated, `anyShift` to ignore it) | external: handled elsewhere | notTyping: not while typing in a field
  // | hidden: works, but is not listed | no action: documented only (Esc is handled where the menus live).
  var SHORTCUTS = [
    { keys: 'Ctrl + K', code: 'KeyK', ctrl: true, action: 'palette', external: true,
      what: 'Quick switcher: jump to a window, open an entity or a page, add a panel, run any command' },
    { keys: 'Alt + N', code: 'KeyN', alt: true, action: 'add', what: 'Add a panel' },
    { keys: 'Alt + W', code: 'KeyW', alt: true, action: 'windows', what: 'The list of all windows' },
    { keys: 'Alt + 1 … 9', codes: DIGITS, alt: true, action: 'show',
      what: 'Bring window 1-9 forward, in the order of the Windows list (on a laptop it swaps in from the dock)' },
    { keys: 'Alt + M', code: 'KeyM', alt: true, action: 'maximize', what: 'Fill the cockpit with the active window / give it back' },
    { keys: 'Alt + U', code: 'KeyU', alt: true, action: 'undo', what: 'Reopen the window closed last' },
    { keys: 'F1  or  ?', code: 'F1', action: 'help', what: 'This list' },
    { keys: '?', key: '?', anyShift: true, notTyping: true, action: 'help', hidden: true },
    { keys: 'Ctrl + Shift + T', code: 'KeyT', ctrl: true, shift: true, action: 'undo', hidden: true },
    { keys: 'Esc', what: 'Close the open menu or list, or give a maximised window back' },
  ];

  // Fields where a letter is text: a shortcut that would swallow typing must not fire there.
  var NOT_TEXT = { checkbox: 1, radio: 1, button: 1, submit: 1, reset: 1, file: 1, range: 1, color: 1, image: 1 };
  function isTypingTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    var tag = String(el.tagName || '').toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (tag === 'INPUT') return !NOT_TEXT[String(el.type || 'text').toLowerCase()];
    return false;
  }

  // ev: { key, code, ctrl, alt, meta, shift, typing, mac } -> { action, n? } or null
  function shortcutFor(ev) {
    for (var i = 0; i < SHORTCUTS.length; i++) {
      var s = SHORTCUTS[i];
      if (!s.action || s.external) continue;
      var n = 0;
      if (s.key !== undefined) { if (ev.key !== s.key) continue; }
      else if (s.codes) { n = s.codes.indexOf(ev.code) + 1; if (!n) continue; }
      else if (ev.code !== s.code) continue;
      if (!!ev.ctrl !== !!s.ctrl || !!ev.alt !== !!s.alt || !!ev.meta !== !!s.meta) continue;
      if (!s.anyShift && !!ev.shift !== !!s.shift) continue;
      if (s.notTyping && ev.typing) continue;
      if (ev.mac && ev.typing && s.alt && !s.ctrl) continue;       // Option + letter types a character on a Mac
      return n ? { action: s.action, n: n } : { action: s.action };
    }
    return null;
  }

  // What the help overlay lists (the working-but-unlisted ones left out).
  function helpRows() {
    return SHORTCUTS.filter(function (s) { return !s.hidden; }).map(function (s) { return { keys: s.keys, what: s.what }; });
  }

  root.ndCockpitKeys = { SHORTCUTS: SHORTCUTS, shortcutFor: shortcutFor, isTypingTarget: isTypingTarget, helpRows: helpRows };
})(typeof window !== 'undefined' ? window : globalThis);
