"use strict";
// Runs INSIDE the sandboxed character-sheet iframe (see app/routers/
// character_sheets.py's /render route, which injects a <script src> for
// this file plus window.__ND_SHEET_ID__/__ND_SHEET_DATA__ into the GM's
// uploaded template HTML, right before its own </body>). The iframe is
// sandboxed with sandbox="allow-scripts allow-popups" and deliberately NO
// allow-same-origin (same reasoning as app/templates/page_viewer.html) —
// this script has no access to the parent app's cookies/session/DOM, and
// can only talk to the parent page via postMessage. It never assumes the
// template's own author wrote any of this — it works generically against
// any standard HTML form.
//
// Two responsibilities:
//   1. On load, restore the sheet's previously-saved field values.
//   2. On any change, re-collect all field values and postMessage them to
//      the parent (which is same-origin, holds the session cookie, and is
//      the one that actually POSTs to /pages/sheets/{id}/save).
//
// Field capture is generic and name/id-keyed: every <input>/<textarea>/
// <select> with a `name` (preferred) or `id` becomes one entry in a flat
// {key: value} object. Checkboxes save as booleans; radio groups save the
// checked option's value under the shared name; <select multiple> saves
// an array; everything else saves its plain .value. A GM whose sheet needs
// something this can't express (canvas widgets, custom components) can
// define window.ndSheetGetData()/window.ndSheetSetData(data) in their own
// page script — those win over the generic walk when present, so the
// common case (a plain HTML form) needs zero special authoring while an
// advanced sheet can still fully opt out of the default behavior.
(function () {
  var SAVE_DEBOUNCE_MS = 800;
  var saveTimer = null;

  function isFormField(el) {
    var tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return true;
    if (tag !== "INPUT") return false;
    var t = (el.type || "text").toLowerCase();
    return ["button", "submit", "reset", "file", "image"].indexOf(t) === -1;
  }

  function fieldKey(el) {
    return el.name || el.id || null;
  }

  function collectGeneric() {
    var data = {};
    var els = document.querySelectorAll("input, textarea, select");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!isFormField(el)) continue;
      var key = fieldKey(el);
      if (!key) continue;
      var tag = el.tagName;
      var type = tag === "INPUT" ? (el.type || "text").toLowerCase() : "";
      if (type === "checkbox") {
        data[key] = el.checked;
      } else if (type === "radio") {
        if (el.checked) data[key] = el.value;
      } else if (tag === "SELECT" && el.multiple) {
        var vals = [];
        for (var j = 0; j < el.options.length; j++) {
          if (el.options[j].selected) vals.push(el.options[j].value);
        }
        data[key] = vals;
      } else {
        data[key] = el.value;
      }
    }
    return data;
  }

  function restoreGeneric(data) {
    if (!data || typeof data !== "object") return;
    var els = document.querySelectorAll("input, textarea, select");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!isFormField(el)) continue;
      var key = fieldKey(el);
      if (!key || !(key in data)) continue;
      var value = data[key];
      var tag = el.tagName;
      var type = tag === "INPUT" ? (el.type || "text").toLowerCase() : "";
      if (type === "checkbox") {
        el.checked = !!value;
      } else if (type === "radio") {
        el.checked = el.value === value;
      } else if (tag === "SELECT" && el.multiple) {
        var vals = Array.isArray(value) ? value : [];
        for (var j = 0; j < el.options.length; j++) {
          el.options[j].selected = vals.indexOf(el.options[j].value) !== -1;
        }
      } else {
        el.value = value == null ? "" : value;
      }
    }
  }

  function collect() {
    try {
      if (typeof window.ndSheetGetData === "function") return window.ndSheetGetData();
    } catch (e) { /* fall through to the generic walk */ }
    return collectGeneric();
  }

  function restore(data) {
    try {
      if (typeof window.ndSheetSetData === "function") {
        window.ndSheetSetData(data);
        return;
      }
    } catch (e) { /* fall through to the generic walk */ }
    restoreGeneric(data);
  }

  function flushSave() {
    saveTimer = null;
    try {
      window.parent.postMessage(
        { source: "nd-character-sheet", type: "nd-sheet-save", sheetId: window.__ND_SHEET_ID__, data: collect() },
        "*"
      );
    } catch (e) { /* best-effort — a save that can't be sent is no worse than one that isn't attempted */ }
  }

  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, SAVE_DEBOUNCE_MS);
  }

  function init() {
    try {
      restore(window.__ND_SHEET_DATA__ || {});
    } catch (e) { /* a malformed template shouldn't block the rest of the page */ }
    document.addEventListener("input", scheduleSave, true);
    document.addEventListener("change", scheduleSave, true);
    // Best-effort, undebounced flush on the ways a tab can disappear —
    // catches the last few hundred ms of typing the debounce would
    // otherwise drop if the player navigates away right after editing.
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flushSave();
    });
    window.addEventListener("pagehide", flushSave);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
