"""Regression tests for two bugs in the live AI Chat send paths:

1. An AI failure sentinel (app.ai.stream_chat's own `{"type": "error", ...}`
   piece, relayed by /api/ai/stream as a `data: {"error": ...}` SSE frame)
   used to have no dedicated handling on the client — a caller that only
   checked `.token`/`.thinking` either silently dropped it (leaving a blank
   reply that still got saved to history) or, before that, treated it as a
   normal token and both displayed AND saved it as if it were a real
   answer. Each of the three live chat surfaces (the GM's main AI Chat page,
   the player chat page, and the entity Ask AI/Roleplay panel) must show it
   distinctly and never push it into the saved conversation history.

2. static/js/ai-chat-core.js's sendMessage() declared `let statsTimer` INSIDE
   the `try { }` block — invisible to the sibling `catch`/`finally` blocks
   (each `{ }` is its own lexical scope for `let`), so the `finally` block's
   own "clean up any lingering timer" line was silently a no-op on every
   exceptional exit, leaking a 250ms setInterval that kept rewriting
   #gen-stats with a stale elapsed time forever.

JS-source assertion tests, reading the static files directly — matches this
repo's established convention for template-JS regression coverage (see
test_ai_chat_pinned_entity_cap.py, test_chat_compact.py)."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CORE_JS = (_ROOT / "static" / "js" / "ai-chat-core.js").read_text()
_PLAYER_HTML = (_ROOT / "app" / "templates" / "ai_chat_player.html").read_text()
_ENTITY_HTML = (_ROOT / "app" / "templates" / "entities" / "detail.html").read_text()
_CHRONICLER_HTML = (_ROOT / "app" / "templates" / "chronicler.html").read_text()


# ── app/routers/ai.py's SSE relay (covered at the route level in
# tests/test_ai_stream.py's test_ai_stream_forwards_error_pieces_as_a_
# distinct_sse_field) — these tests are the client-side half. ──────────────


def _send_message_body():
    return _CORE_JS.split("async function sendMessage() {", 1)[1].split(
        "async function sendMessageAsBackgroundJob()", 1
    )[0]


def test_send_message_declares_stats_timer_above_the_try_block():
    """`let statsTimer` must be declared before `try {` starts, not inside
    it — otherwise the `finally` block's own cleanup reference is a
    different, always-undefined variable (see this file's own docstring)."""
    body = _send_message_body()
    before_try, _, _after = body.partition("\n  try {")
    assert "let statsTimer = null;" in before_try
    # And NOT re-declared inside the try block (that would shadow the outer
    # one again, reintroducing the exact same bug).
    inner = body.split("\n  try {", 1)[1]
    assert "let statsTimer" not in inner


def test_send_message_finally_actually_clears_the_outer_timer():
    body = _send_message_body()
    finally_block = body.split("} finally {", 1)[1]
    assert "if (statsTimer) clearInterval(statsTimer);" in finally_block


def test_send_message_handles_error_pieces_distinctly_from_tokens():
    body = _send_message_body()
    assert "let streamError = '';" in body
    assert "if (typeof _obj.error === 'string')" in body
    # The error branch must not fall through into the token-handling code
    # further down (which appends into fullText/history).
    error_branch = body.split("if (typeof _obj.error === 'string')", 1)[1][:400]
    assert "streamError = _obj.error;" in error_branch
    assert "continue;" in error_branch


def test_send_message_never_pushes_a_pure_error_into_history():
    """fullText only ever accumulates from real `token` pieces — an error
    piece is captured separately (streamError) and the existing
    `if (fullText)` history-push guard already excludes an all-error
    (zero real content) reply from ever landing in history."""
    body = _send_message_body()
    after_loop = body.split("clearInterval(statsTimer);", 1)[1]
    assert "if (streamError) {" in after_loop
    assert "if (fullText) {\n      history.push(" in after_loop


# ── app/templates/ai_chat_player.html (pcSend) ──────────────────────────────


def _pc_send_body():
    return _PLAYER_HTML.split("async function pcSend() {", 1)[1]


def test_player_chat_handles_error_pieces():
    body = _pc_send_body()
    assert "let streamError = '';" in body
    assert "if (typeof obj.error === 'string')" in body


def test_player_chat_never_pushes_empty_reply_into_history():
    body = _pc_send_body()
    assert "if (full) pcHistory.push(" in body
    # The old unconditional push must be gone.
    assert "\n    pcHistory.push({ role: 'assistant', content: full });" not in body


# ── app/templates/entities/detail.html (epSend) ─────────────────────────────


def _ep_send_body():
    return _ENTITY_HTML.split("window.epSend = async function() {", 1)[1]


def test_entity_ask_ai_handles_error_pieces():
    body = _ep_send_body()
    assert "let streamError = '';" in body
    assert "if (typeof obj.error === 'string')" in body


def test_entity_ask_ai_never_pushes_empty_reply_into_history():
    body = _ep_send_body()
    assert "if (full) epHistory.push(" in body
    assert "\n      epHistory.push({ role: 'assistant', content: full });" not in body


# ── app/templates/chronicler.html (chronSend) ───────────────────────────────


def _chron_send_body():
    return _CHRONICLER_HTML.split("window.chronSend = async function () {", 1)[1]


def test_chronicler_chat_handles_error_pieces():
    body = _chron_send_body()
    assert "let streamError = '';" in body
    assert "if (typeof obj.error === 'string')" in body
