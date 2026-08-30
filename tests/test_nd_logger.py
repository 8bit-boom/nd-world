"""Tests for the client-side "Activity log" (static/js/nd-logger.js) —
records uncaught errors, console.warn/error, clicks, failed fetch/XHR
calls, and file-input selections into localStorage, downloadable as .md.
Built directly out of this session's own debugging story (see
tests/test_session_recap_ai.py's test_use_this_background_job_button_
surfaces_errors_instead_of_silent_no_op and tests/test_audio_jobs_inline_
panel_resume.py's delegated-click tests): a GM on mobile with no devtools
access needed a one-off try/catch added just to find out what was actually
failing. This is that made general and always-on instead.

GM-only (see base.html's gating) — a debugging tool, not a player-facing
feature; also covers the JS source itself via a static read, same
convention as test_audio_jobs_inline_panel_resume.py."""
from pathlib import Path

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "nd-logger.js").read_text()


# ── GM-only gating (base.html) ───────────────────────────────────────────

def test_script_shipped_to_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    page = client.get("/settings").text
    assert '<script src="/static/js/nd-logger.js"></script>' in page


def test_script_not_shipped_to_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    page = client.get("/worlds").text
    assert "nd-logger.js" not in page


def test_script_not_shipped_when_logged_out(client):
    page = client.get("/login").text
    assert "nd-logger.js" not in page


# ── JS source assertions ─────────────────────────────────────────────────

def test_stores_in_localstorage_with_a_cap():
    assert 'var STORAGE_KEY = "nd_logger_entries";' in _JS
    assert "var MAX_ENTRIES = 500;" in _JS
    assert "entries.length > MAX_ENTRIES" in _JS
    # Oldest trimmed first, not newest — a ring buffer, not a hard stop.
    assert "entries.slice(entries.length - MAX_ENTRIES)" in _JS


def test_save_never_throws_on_a_full_or_disabled_store():
    body = _JS.split("function save(entries)", 1)[1].split("\n  }\n", 1)[0]
    assert "try {" in body
    assert "catch (e)" in body


def test_captures_uncaught_errors_and_rejections():
    assert 'window.addEventListener("error"' in _JS
    assert 'window.addEventListener("unhandledrejection"' in _JS
    assert 'log("error"' in _JS


def test_wraps_console_warn_and_error_without_swallowing_them():
    assert '["warn", "error"].forEach' in _JS
    body = _JS.split('["warn", "error"].forEach', 1)[1][:600]
    assert "orig.apply(console, arguments)" in body
    assert 'log(level === "error" ? "console.error" : "console.warn"' in body


def test_fetch_wrapper_logs_failures_but_returns_the_real_response():
    assert "var origFetch = window.fetch;" in _JS
    body = _JS.split("window.fetch = function", 1)[1].split("\n  }\n", 1)[0]
    assert "origFetch.apply(this, arguments)" in body
    assert "if (!res.ok) log(" in body
    assert "return res;" in body
    assert "throw err;" in body


def test_xhr_wrapper_covers_chunked_uploads_which_never_use_fetch():
    # chunked-upload.js deliberately uses XMLHttpRequest, not fetch, for
    # upload-progress events — fetch-wrapping alone would never see a
    # failed upload, so this needs its own hook.
    assert "var origXhrOpen = XMLHttpRequest.prototype.open;" in _JS
    body = _JS.split("XMLHttpRequest.prototype.open = function", 1)[1][:700]
    assert 'addEventListener("loadend"' in body
    assert "origXhrOpen.apply(this, arguments)" in body


def test_click_capture_records_tag_id_and_short_text_not_form_values():
    body = _JS.split('document.addEventListener("click"', 1)[1][:900]
    assert 'closest("button, a, [onclick], input[type=submit], input[type=button]")' in body
    assert "slice(0, 60)" in body
    assert 'log("click"' in body


def test_file_input_change_logs_name_and_size_not_content():
    body = _JS.split('document.addEventListener("change"', 1)[1][:600]
    assert 'el.type === "file"' in body
    assert 'log("upload"' in body
    assert "f.name" in body
    assert "f.size" in body


def test_download_builds_markdown_from_entries():
    assert "function downloadMd()" in _JS
    body = _JS.split("function downloadMd()", 1)[1].split("\n  }\n", 1)[0]
    assert "# nd-world activity log" in body
    assert 'type: "text/markdown"' in body
    assert ".md" in body
    assert "URL.createObjectURL(blob)" in body
    assert "URL.revokeObjectURL(a.href)" in body


def test_panel_toggle_and_clear_wired():
    assert 'btn.addEventListener("click"' in _JS
    assert "panelOpen = !panelOpen" in _JS
    assert "nd-logger-clear" in _JS
    assert "confirm(" in _JS
    assert "nd-logger-dl" in _JS
