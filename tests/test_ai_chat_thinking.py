"""Tests for the main AI Chat page (/ai) defaulting every Ollama model to
"thinking" mode and surfacing the model's live reasoning trace, closing two
gaps the entity detail page's "Ask this entity" panel already had (its own
ep-think-checkbox/epEnsureReasoning): this page never sent `think` at all,
and its SSE loop silently dropped any `thinking` piece the backend already
streams (POST /api/ai/stream always emits one when the model reasons — see
app/routers/ai.py's ai_stream). Source-assertion style, matching this
session's established convention for template-JS regression coverage (see
test_chat_context_usage_indicator.py)."""
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def test_ai_chat_page_ships_think_checkbox_checked_by_default(client, seed):
    _login_gm(client, seed)
    page = client.get("/ai").text
    assert 'id="ai-think-checkbox"' in page
    assert 'id="ai-think-checkbox" checked' in page


def test_think_preference_defaults_on_when_unset(client, seed):
    js = open("static/js/ai-chat-core.js").read()
    assert "function ndGetThinkEnabled()" in js
    assert "localStorage.getItem('nd_ai_think') !== '0'" in js
    assert "function ndSetThinkEnabled(enabled)" in js
    assert "localStorage.setItem('nd_ai_think'" in js


def test_think_checkbox_restored_from_storage_on_load(client, seed):
    js = open("static/js/ai-chat-core.js").read()
    assert "document.getElementById('ai-think-checkbox')" in js
    assert "cb.checked = ndGetThinkEnabled()" in js


def test_send_message_requests_thinking_by_default(client, seed):
    js = open("static/js/ai-chat-core.js").read()
    fn_start = js.index("async function sendMessage()")
    fn_end = js.index("\nasync function ", fn_start + 1)
    fn = js[fn_start:fn_end]
    assert "think: thinkCb ? thinkCb.checked : ndGetThinkEnabled()" in fn


def test_send_message_handles_thinking_pieces_and_collapses_on_first_token(client, seed):
    js = open("static/js/ai-chat-core.js").read()
    fn_start = js.index("async function sendMessage()")
    fn_end = js.index("\nasync function ", fn_start + 1)
    fn = js[fn_start:fn_end]
    assert "typeof _obj.thinking === 'string'" in fn
    assert "ndEnsureReasoning(thinking)" in fn
    assert "det.querySelector('summary').textContent = '🧠 Thought process'" in fn
    assert "det.open = false" in fn


def test_ndEnsureReasoning_inserts_details_before_bubble(client, seed):
    js = open("static/js/ai-chat-core.js").read()
    fn_start = js.index("function ndEnsureReasoning(bubble)")
    fn_end = js.index("\nfunction ", fn_start + 1)
    fn = js[fn_start:fn_end]
    assert "w.querySelector('.ai-reasoning')" in fn
    assert "w.insertBefore(det, bubble)" in fn


def test_ai_chat_css_styles_reasoning_trace_and_assistant_column_layout(client, seed):
    css = open("static/css/ai-chat.css").read()
    assert ".ai-reasoning {" in css
    assert ".ai-reasoning-text {" in css
    assert ".ai-msg--assistant { align-self: flex-start; flex-direction: column; }" in css


# ── Same default for the standalone player-facing /ai-chat page ────────────

def test_player_ai_chat_page_requests_thinking_by_default(client, seed):
    """No toggle on this simpler page (unlike the GM's main chat) — it's
    just always on, same posture as "by default all Ollama models use
    thinking" the GM asked for, extended to the player-facing surface too."""
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 200
    assert "surface: 'ask_ai', think: true" in r.text


def test_player_ai_chat_page_shows_reasoning_trace(client, seed):
    _set_world(seed.world_a.id, players_can_use_ai_chat=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/ai-chat").text
    assert "function pcEnsureReasoning(bubble)" in page
    assert "typeof obj.thinking === 'string'" in page
    assert "pcEnsureReasoning(bubble)" in page
    assert "det.querySelector('summary').textContent = '🧠 Thought process'" in page
