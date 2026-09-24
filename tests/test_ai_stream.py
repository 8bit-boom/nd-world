"""Tests for POST /api/ai/stream's permission gate (app/routers/ai.py).

Unlike the download toggles, this is a single axis: GM always allowed, a
player only once the GM opts in per world via World.players_can_ask_ai (off
by default). Ollama itself is mocked out — these tests only exercise the
permission check and the SSE plumbing around it, not the model.
"""
import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    """app.ai persists custom models / hidden ids / per-surface defaults to a
    JSON file next to the DB, not the DB itself — point it at a throwaway
    path per test so tests can't see each other's saved defaults."""
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


async def _fake_resolve_model(requested):
    return requested or "fake-model", None


async def _fake_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
    for tok in ["Hello", " world"]:
        yield {"type": "content", "text": tok} if emit_thinking else tok


def _patch_ai(monkeypatch):
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)


def test_ai_stream_gm_always_allowed(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "Hello" in r.text
    assert "[DONE]" in r.text


# ── Seeing the model's reasoning live (epEnsureReasoning in entities/
# detail.html's "Ask AI" panel) — the route always asks stream_chat for
# emit_thinking so a "thinking" frame reaches the client whenever the model
# actually produced one, distinct from the normal "token" frames.

def test_ai_stream_requests_emit_thinking_unconditionally(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["emit_thinking"] = emit_thinking
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert captured["emit_thinking"] is True


def test_ai_stream_emits_thinking_frames_ahead_of_the_answer(client, seed, monkeypatch):
    async def _thinking_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        yield {"type": "thinking", "text": "Pondering "}
        yield {"type": "thinking", "text": "the question."}
        yield {"type": "content", "text": "Final answer."}

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _thinking_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}], "think": True})
    assert r.status_code == 200
    assert '{"thinking": "Pondering "}' in r.text
    assert '{"thinking": "the question."}' in r.text
    assert '{"token": "Final answer."}' in r.text
    assert r.text.index('"Pondering "') < r.text.index('"Final answer."')


def test_ai_stream_forwards_error_pieces_as_a_distinct_sse_field(client, seed, monkeypatch):
    """stream_chat's failure sentinels (empty response, Ollama error, etc.)
    arrive as {"type": "error", ...} pieces — the SSE relay must forward
    them under their own "error" field, never merged into "token" where a
    client would save/display them as a real reply (see the JS surfaces'
    own `if (typeof _obj.error === 'string')` handling)."""
    async def _erroring_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        yield {"type": "content", "text": "partial "}
        yield {"type": "error", "text": "[AI unavailable: RuntimeError: boom]"}

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _erroring_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert '{"token": "partial "}' in r.text
    assert '{"error": "[AI unavailable: RuntimeError: boom]"}' in r.text
    # The failure text must never be wire-shaped as a "token" frame — a
    # client that only understands "token"/"thinking"/"note" would have no
    # way to tell it apart from real content.
    assert '{"token": "[AI unavailable: RuntimeError: boom]"}' not in r.text


def test_ai_stream_no_thinking_frame_for_a_plain_reply(client, seed, monkeypatch):
    """A model/request that never produces reasoning must not surface an
    empty or spurious thinking frame — nothing for the UI to show a panel
    for at all."""
    _patch_ai(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "thinking" not in r.text


def test_ai_stream_forwards_think_to_stream_chat(client, seed, monkeypatch):
    """The entity detail page's Ask AI panel is the one surface with a
    Thinking checkbox (epSend sends `think` in its POST body) — confirm
    ChatBody.think actually reaches stream_chat's `think` kwarg instead of
    being silently dropped somewhere in ai_stream's plumbing."""
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["think"] = think
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "think": True,
    })
    assert r.status_code == 200
    assert captured["think"] is True


def test_ai_stream_think_defaults_to_false(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["think"] = think
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert captured["think"] is False


# ── Interactive thinking widening (docs/DYNAMIC_THINKING_AND_PIPELINE_
# PLAN.md Part 1, item 1.4) — a thinking Ask AI request previously reached
# Ollama with the GM's configured num_predict completely un-widened, unlike
# every other think=True caller in this app.

def test_ai_stream_widens_num_predict_when_thinking(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    try:
        login(client, seed.gm.email, GM_PASSWORD)
        client.cookies.set("active_world", seed.world_a.slug)
        r = client.post("/api/ai/stream", json={
            "messages": [{"role": "user", "content": "hi"}], "think": True,
        })
        assert r.status_code == 200
        assert captured["options"]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_ai_stream_no_widening_when_not_thinking(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    try:
        login(client, seed.gm.email, GM_PASSWORD)
        client.cookies.set("active_world", seed.world_a.slug)
        r = client.post("/api/ai/stream", json={
            "messages": [{"role": "user", "content": "hi"}], "think": False,
        })
        assert r.status_code == 200
        assert "num_predict" not in captured["options"]
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_ai_stream_thinking_widening_does_not_override_an_explicit_num_predict(client, seed, monkeypatch):
    """A preset/caller that already set num_predict keeps its exact value —
    the widening only fills a gap, it never overrides an explicit choice."""
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "think": True,
        "options": {"num_predict": 200},
    })
    assert r.status_code == 200
    assert captured["options"]["num_predict"] == 200


# ── Interactive context sizing (docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md
# Part 2, item 3.4) — the one AI surface previously left with no num_ctx
# sizing at all: system + lore pair + full history + attachments could
# silently overflow the configured/default context.

def test_ai_stream_sizes_num_ctx_for_a_long_history(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    long_history = [{"role": "user", "content": "word " * 5000}]
    r = client.post("/api/ai/stream", json={"messages": long_history})
    assert r.status_code == 200
    assert captured["options"]["num_ctx"] > ai_module._DEFAULT_ASSUMED_CTX_TOKENS


def test_ai_stream_short_message_has_no_num_ctx_override(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert (captured["options"] or {}).get("num_ctx") is None


def test_ai_stream_explicit_num_ctx_wins_over_the_auto_sized_one(client, seed, monkeypatch):
    """A preset/caller that already set num_ctx keeps its exact value, even
    for a message long enough that auto-sizing would otherwise kick in."""
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    long_history = [{"role": "user", "content": "word " * 5000}]
    r = client.post("/api/ai/stream", json={"messages": long_history, "options": {"num_ctx": 4096}})
    assert r.status_code == 200
    assert captured["options"]["num_ctx"] == 4096


def test_ai_stream_num_ctx_clamped_to_the_max_auto_ceiling(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"

    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    huge_history = [{"role": "user", "content": "word " * 200000}]
    r = client.post("/api/ai/stream", json={"messages": huge_history})
    assert r.status_code == 200
    assert captured["options"]["num_ctx"] == ai_module.MAX_AUTO_NUM_CTX


def test_ai_stream_player_denied_by_default(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403


def test_ai_stream_player_allowed_once_gm_enables_it(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "Hello" in r.text


def test_ai_stream_player_toggle_is_per_world(client, seed, monkeypatch):
    """The toggle is per-world — enabling it for World A must not leak
    access to a player in World B."""
    _patch_ai(monkeypatch)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_b.email, PLAYER_PASSWORD)  # player_b is only a member of world_b
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403


# ── Per-surface default models (app/ai.py's get_defaults/set_default) ──────

def test_ai_defaults_empty_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/defaults")
    assert r.status_code == 200
    # "assist" joined DEFAULT_SURFACES with the AI-everywhere panel work —
    # every surface key is present (empty) by design.
    assert r.json() == {"chat": "", "ask_ai": "", "image": "", "recap": "", "assist": ""}


def test_ai_defaults_set_and_get_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/defaults", json={"surface": "ask_ai", "model_id": "gemma4:26b"})
    assert r.status_code == 200
    assert r.json()["defaults"]["ask_ai"] == "gemma4:26b"
    r = client.get("/api/ai/defaults")
    assert r.json() == {"chat": "", "ask_ai": "gemma4:26b", "image": "", "recap": "", "assist": ""}


def test_ai_defaults_rejects_unknown_surface(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/defaults", json={"surface": "bogus", "model_id": "x"})
    assert r.status_code == 400


def test_ai_defaults_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/ai/defaults")
    assert r.status_code == 403
    r = client.post("/api/ai/defaults", json={"surface": "chat", "model_id": "x"})
    assert r.status_code == 403


def test_ai_stream_falls_back_to_surface_default_when_model_blank(client, seed, monkeypatch):
    """A request with no explicit model uses the configured per-surface
    default instead of always falling through to the single system-wide
    default, so Chat and Ask AI can run different models."""
    captured = {}

    async def _capturing_resolve_model(requested):
        captured["requested"] = requested
        return "resolved-model", None
    monkeypatch.setattr(ai_module, "resolve_model", _capturing_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/api/ai/defaults", json={"surface": "ask_ai", "model_id": "ask-ai-model"})
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "surface": "ask_ai",
    })
    assert r.status_code == 200
    assert captured["requested"] == "ask-ai-model"


# ── A non-GM caller's model/system/options are never fully trusted ─────────
# POST /api/ai/stream is reachable by any player whose world opted into Ask
# AI/AI Chat — nothing validates that a request actually came from a page
# this app rendered (a direct devtools/curl call reaches the same route).
# The real secret-bearing content (RAG lore, GM-only AiInstructions) is
# already filtered server-side before it ever reaches a page's composed
# system string or messages, but the model/options/system fields
# themselves still shouldn't be fully client-controlled for a non-GM
# caller — see ai_stream's own comments for the reasoning on each.

def test_ai_stream_ignores_client_model_for_player(client, seed, monkeypatch):
    captured = {}

    async def _capturing_resolve_model(requested):
        captured["requested"] = requested
        return "resolved-model", None
    monkeypatch.setattr(ai_module, "resolve_model", _capturing_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)

    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "model": "some-expensive-model-i-picked-myself",
    })
    assert r.status_code == 200
    assert captured["requested"] != "some-expensive-model-i-picked-myself"


def test_ai_stream_honors_client_model_for_gm(client, seed, monkeypatch):
    captured = {}

    async def _capturing_resolve_model(requested):
        captured["requested"] = requested
        return "resolved-model", None
    monkeypatch.setattr(ai_module, "resolve_model", _capturing_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "model": "my-chosen-model",
    })
    assert r.status_code == 200
    assert captured["requested"] == "my-chosen-model"


def test_ai_stream_truncates_long_system_prompt_for_player(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["system"] = system
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)

    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    from app.routers.ai import _PLAYER_SYSTEM_PROMPT_MAX_CHARS
    huge = "x" * (_PLAYER_SYSTEM_PROMPT_MAX_CHARS + 500)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "system": huge,
    })
    assert r.status_code == 200
    assert len(captured["system"]) == _PLAYER_SYSTEM_PROMPT_MAX_CHARS


def test_ai_stream_does_not_truncate_system_prompt_for_gm(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["system"] = system
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    from app.routers.ai import _PLAYER_SYSTEM_PROMPT_MAX_CHARS
    huge = "x" * (_PLAYER_SYSTEM_PROMPT_MAX_CHARS + 500)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "system": huge,
    })
    assert r.status_code == 200
    assert len(captured["system"]) == len(huge)


def test_ai_stream_clamps_num_ctx_upper_bound(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "options": {"num_ctx": 999999999},
    })
    assert r.status_code == 200
    # Out of range -> _clamp_options drops it; the auto-sizing fallback
    # below (num_ctx not already in options) may then add its own, always
    # <= MAX_AUTO_NUM_CTX — either way the caller's absurd value must never
    # reach stream_chat.
    assert captured["options"].get("num_ctx") != 999999999


def test_ai_stream_clamps_num_gpu_upper_bound(client, seed, monkeypatch):
    captured = {}

    async def _capturing_stream_chat(messages, system="", model="", options=None, think=False, emit_thinking=False):
        captured["options"] = options
        yield {"type": "content", "text": "ok"} if emit_thinking else "ok"
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _capturing_stream_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "options": {"num_gpu": 5000},
    })
    assert r.status_code == 200
    assert "num_gpu" not in captured["options"]
