"""Tests for the AI Chat "Compact chat" feature — condenses the OLDER turns
of a long-running conversation into one summary message, keeping the most
recent turns verbatim, the same idea as this CLI's own auto-compaction.
Covers app.ai.condense_chat_history at the unit level (mocked Ollama
client, same pattern as tests/test_ollama_options.py), POST
/api/ai/chat/compact's permission gate and refuse-if-too-long guard at the
route level (same shape as tests/test_ai_stream.py's own permission
tests), and the shipped JS/template wiring for the button, the auto-suggest
link, the auto-compact opt-in checkbox, and the compacting dialog — as a
source assertion, matching this session's established convention for
template-JS regression coverage (see test_live_recording_wake_lock.py)."""
import types

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeResp:
    def __init__(self, content):
        self.message = types.SimpleNamespace(content=content)


class _FakeChatClient:
    """Records every kwargs dict passed to .chat() — same helper shape as
    test_ollama_options.py's own _FakeChatClient. .show() reports
    "thinking" support unconditionally so condense_chat_history's own
    think=True default (see _chat_kwargs' capability-gated downgrade)
    reaches the client the way these tests expect — capability gating
    itself is covered separately in test_ollama_options.py."""

    def __init__(self, calls, reply="A tight summary."):
        self._calls = calls
        self._reply = reply

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeResp(self._reply)

    async def show(self, model):
        return types.SimpleNamespace(capabilities=["thinking"])


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_ollama_overrides():
    ai_module.set_ollama_generation_overrides({})
    ai_module._model_capabilities_cache.clear()
    yield
    ai_module.set_ollama_generation_overrides({})
    ai_module._model_capabilities_cache.clear()


# ── app.ai.condense_chat_history (unit) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_condense_chat_history_empty_messages_returns_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    result = await ai_module.condense_chat_history([])
    assert result == ""
    assert calls == []


@pytest.mark.asyncio
async def test_condense_chat_history_labels_turns_and_calls_model(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    messages = [
        {"role": "user", "content": "What's the capital city called?"},
        {"role": "assistant", "content": "Its called Aerholm."},
    ]
    result = await ai_module.condense_chat_history(messages)
    assert result == "A tight summary."
    # messages[0] is the system prompt (folded in by generate_chat);
    # messages[1] is the single user turn condense_chat_history builds
    # (the whole conversation-as-text it hands to the model).
    sent = calls[0]["messages"][1]["content"]
    assert "GM: What's the capital city called?" in sent
    assert "Assistant: Its called Aerholm." in sent
    assert calls[0]["think"] is True  # think defaults to True, matching the recap family


@pytest.mark.asyncio
async def test_condense_chat_history_extra_instructions_reach_system_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_chat_history(
        [{"role": "user", "content": "hi"}], extra_instructions="focus on named NPCs",
    )
    # generate_chat folds `system` in as messages[0] (role="system") — same
    # shape test_ollama_options.py's own condense_recap test asserts on.
    system = calls[0]["messages"][0]["content"]
    assert "focus on named NPCs" in system


@pytest.mark.asyncio
async def test_condense_chat_history_widens_configured_num_predict_when_thinking(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    await ai_module.condense_chat_history([{"role": "user", "content": "hi"}], think=True)
    assert calls[0]["options"]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS


# ── POST /api/ai/chat/compact (route) ───────────────────────────────────────

def _patch_compact(monkeypatch, reply="Summary text."):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls, reply))
    return calls


def test_compact_gm_always_allowed(client, seed, monkeypatch):
    _patch_compact(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/compact", json={
        "messages": [{"role": "user", "content": "What happened last session?"},
                     {"role": "assistant", "content": "The party found a hidden door."}],
    })
    assert r.status_code == 200
    assert r.json() == {"summary": "Summary text."}


def test_compact_player_denied_by_default(client, seed, monkeypatch):
    _patch_compact(monkeypatch)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/compact", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 403


def test_compact_player_allowed_once_gm_enables_ask_ai(client, seed, monkeypatch):
    _patch_compact(monkeypatch)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/compact", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200


def test_compact_rejects_empty_messages(client, seed, monkeypatch):
    _patch_compact(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/compact", json={"messages": []})
    assert r.status_code == 400


def test_compact_refuses_oversized_input_instead_of_truncating(client, seed, monkeypatch):
    """Mirrors condense_recap's own single-call entry-point guard (docs/
    DYNAMIC_THINKING_AND_PIPELINE_PLAN.md item 3.3) — a compact call has
    nothing else (no chunking) protecting it from silently truncating the
    very history it's supposed to be preserving."""
    _patch_compact(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    huge = "x" * (ai_module.MAX_AUTO_NUM_CTX * 5)
    r = client.post("/api/ai/chat/compact", json={
        "messages": [{"role": "user", "content": huge}],
    })
    assert r.status_code == 400
    assert "too long" in r.json()["detail"].lower() or "too much" in r.json()["detail"].lower()


# ── Shipped JS/template wiring (source assertion) ───────────────────────────

def _get_ai_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    return r.text


def test_page_ships_compact_button_and_autocompact_checkbox(client, seed):
    page = _get_ai_page(client, seed)
    assert 'onclick="compactChat()"' in page
    assert 'id="autocompact-toggle"' in page
    assert "ndSetAutoCompact(this.checked)" in page


def test_page_ships_compact_dialog_and_suggest_link(client, seed):
    page = _get_ai_page(client, seed)
    assert 'id="compact-dialog-overlay"' in page
    assert 'id="ctx-compact-suggest"' in page


def test_js_defines_compact_chat_and_keeps_recent_turns_verbatim():
    js = open("static/js/ai-chat-core.js").read()
    assert "const COMPACT_KEEP_RECENT" in js
    assert "async function compactChat()" in js
    assert "/api/ai/chat/compact" in js
    body = js.split("async function compactChat()", 1)[1][:2500]
    assert "history.slice(0, -COMPACT_KEEP_RECENT)" in body
    assert "history.slice(-COMPACT_KEEP_RECENT)" in body


def test_js_auto_suggest_and_auto_compact_wired_into_ctx_usage():
    js = open("static/js/ai-chat-core.js").read()
    assert "function ndSetAutoCompact(" in js
    assert "function ndGetAutoCompact(" in js
    body = js.split("async function _updateCtxUsage(", 1)[1][:1500]
    assert "ndGetAutoCompact()" in body
    assert "compactChat()" in body
    assert "ctx-compact-suggest" in body
