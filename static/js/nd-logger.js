"use strict";
// nd-Logger — a client-side activity/error log for debugging live issues,
// downloadable as .md. Built directly out of this session's own debugging
// story: a GM on mobile with no devtools access reported "Use this does
// nothing," and the only way to find the real cause (two independent bugs
// — a click-swallowing re-render race and a null DOM lookup) was adding a
// one-off try/catch and asking the GM to paste back whatever it caught.
// This generalizes that: instead of instrumenting one button after the
// fact, always be recording, so a bug report can come with real evidence
// instead of "it doesn't work."
//
// Captures, in the order they happen:
//  - every uncaught JS error / unhandled promise rejection
//  - every console.warn/console.error call (monkey-patched; the real
//    devtools console still gets everything too — this never swallows or
//    alters what's printed there, only ALSO records a copy)
//  - every click on an interactive element (button/link/[onclick]) — tag,
//    id/first class, and a short snippet of its visible text, never form
//    field values
//  - every fetch() call that comes back non-ok or fails outright (network
//    error) — method + URL + status only, never request/response bodies
//    (which can hold passwords, recap text, tokens, etc.)
//  - every XMLHttpRequest that fails the same way (chunked-upload.js uses
//    XHR instead of fetch specifically for upload-progress events, so
//    fetch-wrapping alone would miss every failed upload)
//  - every <input type=file> selection (filename/size only, not content)
//  - a "Loaded <path>" line on every page load, so a downloaded log reads
//    as one continuous timeline across normal multi-page navigation
//    instead of resetting on every click that navigates somewhere
//
// Stored in localStorage (survives navigation and closing/reopening the
// tab, unlike an in-memory array a full page load would wipe) under one
// fixed key, capped at MAX_ENTRIES with the oldest trimmed first so it can
// never grow without bound. Loaded on every page (see base.html) but
// GM-only — this is a debugging tool, not a player-facing feature, and
// localStorage is plain unencrypted browser storage, never a place for
// anything security-sensitive regardless of who can reach it.
(function () {
  var STORAGE_KEY = "nd_logger_entries";
  var MAX_ENTRIES = 500;

  function load() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    } catch (e) {
      return [];
    }
  }

  function save(entries) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
    } catch (e) {
      // Quota exceeded, storage disabled (private browsing), etc. —
      // logging must never be the thing that breaks the page it exists to
      // help debug, so a failed save here is silently dropped.
    }
  }

  function log(kind, text) {
    var entries = load();
    entries.push({ t: new Date().toISOString(), k: kind, m: String(text).slice(0, 500) });
    if (entries.length > MAX_ENTRIES) entries = entries.slice(entries.length - MAX_ENTRIES);
    save(entries);
    renderIfOpen();
  }

  // ── Capture hooks — installed immediately. This file is loaded first,
  // in <head>, specifically so these are active before any other script
  // on the page (base.html's own inline scripts, or a child template's)
  // has a chance to run or throw. ──────────────────────────────────────

  window.addEventListener("error", function (e) {
    log("error", (e.message || "Script error") + (e.filename ? " @ " + e.filename + ":" + e.lineno : ""));
  });
  window.addEventListener("unhandledrejection", function (e) {
    var reason = e.reason;
    log("error", "Unhandled promise rejection: " + (reason && reason.message ? reason.message : String(reason)));
  });

  ["warn", "error"].forEach(function (level) {
    var orig = console[level];
    console[level] = function () {
      log(level === "error" ? "console.error" : "console.warn",
          Array.prototype.slice.call(arguments).map(String).join(" "));
      orig.apply(console, arguments);
    };
  });

  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var method = (init && init.method) || "GET";
      var url = typeof input === "string" ? input : (input && input.url) || String(input);
      return origFetch.apply(this, arguments).then(function (res) {
        if (!res.ok) log("fetch", method + " " + url + " → " + res.status);
        return res;
      }, function (err) {
        log("fetch", method + " " + url + " → network error: " + err.message);
        throw err;
      });
    };
  }

  var origXhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this._ndLoggerMethod = method;
    this._ndLoggerUrl = url;
    this.addEventListener("loadend", function () {
      if (this.status === 0 || this.status >= 400) {
        log("upload", (this._ndLoggerMethod || "") + " " + (this._ndLoggerUrl || "") +
            " → " + (this.status || "network error"));
      }
    });
    return origXhrOpen.apply(this, arguments);
  };

  document.addEventListener("click", function (e) {
    var el = e.target && e.target.closest
      ? e.target.closest("button, a, [onclick], input[type=submit], input[type=button]")
      : null;
    if (!el) return;
    var desc = el.tagName.toLowerCase();
    if (el.id) desc += "#" + el.id;
    else if (el.className && typeof el.className === "string" && el.className.trim()) {
      desc += "." + el.className.trim().split(/\s+/)[0];
    }
    var text = (el.textContent || el.value || "").trim().replace(/\s+/g, " ").slice(0, 60);
    log("click", desc + (text ? ' "' + text + '"' : ""));
  }, true);

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el && el.tagName === "INPUT" && el.type === "file" && el.files) {
      Array.prototype.forEach.call(el.files, function (f) {
        log("upload", (el.id || el.name || "file input") + ": " + f.name + " (" + f.size + " bytes)");
      });
    }
  }, true);

  log("nav", "Loaded " + location.pathname + location.search);

  // ── Floating button + panel UI — deferred to DOMContentLoaded since
  // this needs document.body, unlike the hooks above. ──────────────────

  var panelOpen = false;
  var listEl = null;

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function fmtTime(iso) {
    try { return new Date(iso).toLocaleTimeString(); } catch (e) { return iso; }
  }

  function renderList() {
    if (!listEl) return;
    var entries = load();
    listEl.innerHTML = entries.slice().reverse().map(function (e) {
      return '<div style="padding:.25rem 0;border-bottom:1px solid var(--border);font-size:.72rem;font-family:monospace;word-break:break-word">' +
        '<span style="color:var(--text-dim)">' + esc(fmtTime(e.t)) + '</span> ' +
        '<span style="color:var(--neon)">[' + esc(e.k) + ']</span> ' +
        esc(e.m) + '</div>';
    }).join("") || '<div style="color:var(--text-dim);font-size:.8rem;padding:.5rem 0">Nothing logged yet.</div>';
  }

  function renderIfOpen() {
    if (panelOpen) renderList();
  }

  function downloadMd() {
    var entries = load();
    var lines = [
      "# nd-world activity log", "",
      "Exported " + new Date().toISOString(),
      "Page: " + location.href, "",
    ];
    entries.forEach(function (e) {
      lines.push("- `" + e.t + "` **[" + e.k + "]** " + e.m.replace(/\n/g, " "));
    });
    var blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "nd-world-log-" + new Date().toISOString().replace(/[:.]/g, "-") + ".md";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
  }

  function init() {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.id = "nd-logger-toggle";
    btn.textContent = "🐞";
    btn.title = "Activity log (debugging)";
    btn.style.cssText = "position:fixed;bottom:14px;right:14px;z-index:9998;width:38px;height:38px;" +
      "border-radius:50%;background:var(--bg2);border:1px solid var(--border);color:var(--text-dim);" +
      "font-size:1.1rem;cursor:pointer;line-height:1";
    document.body.appendChild(btn);

    var panel = document.createElement("div");
    panel.id = "nd-logger-panel";
    panel.style.cssText = "position:fixed;bottom:60px;right:14px;z-index:9999;width:min(420px,92vw);" +
      "max-height:60vh;display:none;flex-direction:column;background:var(--bg2);border:1px solid var(--neon);" +
      "border-radius:6px;box-shadow:0 4px 20px rgba(0,0,0,.4)";
    panel.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;padding:.6rem .8rem;border-bottom:1px solid var(--border)">' +
        '<strong style="font-size:.82rem;color:var(--neon)">🐞 Activity log</strong>' +
        '<div style="display:flex;gap:.4rem">' +
          '<button type="button" id="nd-logger-dl" style="font-size:.72rem;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:3px;padding:.2rem .5rem;cursor:pointer">⬇ .md</button>' +
          '<button type="button" id="nd-logger-clear" style="font-size:.72rem;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:3px;padding:.2rem .5rem;cursor:pointer">🗑</button>' +
          '<button type="button" id="nd-logger-close" style="font-size:.72rem;background:var(--bg3);border:1px solid var(--border);color:var(--text);border-radius:3px;padding:.2rem .5rem;cursor:pointer">✕</button>' +
        '</div>' +
      '</div>' +
      '<div id="nd-logger-list" style="overflow-y:auto;padding:.5rem .8rem"></div>';
    document.body.appendChild(panel);
    listEl = panel.querySelector("#nd-logger-list");

    btn.addEventListener("click", function () {
      panelOpen = !panelOpen;
      panel.style.display = panelOpen ? "flex" : "none";
      if (panelOpen) renderList();
    });
    panel.querySelector("#nd-logger-close").addEventListener("click", function () {
      panelOpen = false;
      panel.style.display = "none";
    });
    panel.querySelector("#nd-logger-clear").addEventListener("click", function () {
      if (!confirm("Clear the activity log?")) return;
      save([]);
      renderList();
    });
    panel.querySelector("#nd-logger-dl").addEventListener("click", downloadMd);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
