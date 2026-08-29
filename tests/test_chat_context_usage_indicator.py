"""Tests for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 4.2:
the AI Chat status area now shows an estimated "~N tokens sent" figure
(colored red once it's near the model's own context window) so a GM can
see *why* a long-running chat got slow or started forgetting early turns,
instead of history being trimmed silently. Covers the new
GET /api/ai/context-info route (app/routers/ai.py) at the route level, and
the shipped JS (static/js/ai-chat-core.js, app/templates/ai_chat/_tab_chat.html)
as a source assertion — matching this session's established convention for
template-JS regression coverage (see test_live_recording_wake_lock.py)."""
import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _reset_ollama_overrides():
    ai_module.set_ollama_generation_overrides({})
    yield
    ai_module.set_ollama_generation_overrides({})


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_context_info_returns_default_when_unset(client, seed):
    _login_gm(client, seed)
    r = client.get("/api/ai/context-info")
    assert r.status_code == 200
    assert r.json() == {"num_ctx": ai_module._DEFAULT_ASSUMED_CTX_TOKENS}


def test_context_info_returns_configured_num_ctx(client, seed):
    ai_module.set_ollama_generation_overrides({"num_ctx": 16384})
    _login_gm(client, seed)
    r = client.get("/api/ai/context-info")
    assert r.status_code == 200
    assert r.json() == {"num_ctx": 16384}


def test_ai_chat_page_ships_ctx_usage_element_and_wiring(client, seed):
    _login_gm(client, seed)
    page = client.get("/ai").text
    assert 'id="ctx-usage"' in page


def test_ai_chat_core_js_computes_and_colors_ctx_usage(client, seed):
    js = open("static/js/ai-chat-core.js").read()
    assert "async function _updateCtxUsage(" in js
    assert "/api/ai/context-info" in js
    assert "ndEstimateTokens(allText)" in js
    assert "const over = tokens > numCtx" in js
    assert "over ? '#f55'" in js
    # Wired into the live send path with what was actually sent (post-RAG).
    assert "_updateCtxUsage(messagesWithCtx, presetSystem)" in js
