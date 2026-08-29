"""Regression test for plan item 2.1 (Recap Thinking-Budget & Token-Usage
Plan): buildChatMessagesWithContext (static/js/ai-chat-core.js) used to
inject the fresh RAG "Relevant world lore" pair at the very FRONT of the
message array on every send. Since that block differs turn to turn (a new
RAG query each time), the token stream diverged right after the system
prompt every single send — Ollama has to re-prefill the ENTIRE prior
history from scratch instead of reusing its KV-prefix cache, for zero
informational gain.

The fix moves the lore pair to immediately before the final (newest) user
turn instead, so everything before it stays a byte-stable prefix turn to
turn. This is a pure JS-source assertion (no Python-side behavior to
exercise — the message array is assembled entirely client-side before ever
reaching a server route), same style as test_ai_chat_split.py's own static
asset checks.
"""
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"


def _core_js() -> str:
    return (STATIC / "js" / "ai-chat-core.js").read_text()


def test_lore_pair_is_injected_before_the_final_turn_not_the_front():
    content = _core_js()
    assert "async function buildChatMessagesWithContext(extraUserMsg) {" in content
    fn_start = content.index("async function buildChatMessagesWithContext(extraUserMsg) {")
    fn_body = content[fn_start:content.index("\nasync function sendMessage()", fn_start)]

    assert "...base.slice(0, -1)," in fn_body
    assert "...base.slice(-1)," in fn_body
    lore_idx = fn_body.index("Relevant world lore")
    slice_before_idx = fn_body.index("base.slice(0, -1)")
    slice_after_idx = fn_body.index("base.slice(-1)")
    # The stable "everything before the newest turn" slice comes first in
    # the array literal, then the lore pair, then the newest turn last —
    # NOT the old shape ([lore, gotIt, ...base]) which put lore first.
    assert slice_before_idx < lore_idx < slice_after_idx


def test_lore_pair_never_pushed_into_persistent_history():
    """The lore/"Got it." pair must stay transient (assembled fresh into
    the outgoing messages array only) — pushing it into `history` would
    permanently bake a stale RAG snapshot into the saved conversation."""
    content = _core_js()
    fn_start = content.index("async function buildChatMessagesWithContext(extraUserMsg) {")
    fn_body = content[fn_start:content.index("\nasync function sendMessage()", fn_start)]
    assert "history.push" not in fn_body
    assert "history.unshift" not in fn_body
