"""Tests for app.llm_client — the Unsloth OpenAI-dialect shim behind
app/ai.py's ollama-shaped client surface (migration plan §6).

The httpx layer is mocked with MockTransport; these tests pin the request
translation (ollama kwargs → OpenAI body), the response mapping (OpenAI
shapes → ollama-shaped objects), error behavior, and the integration points
in app.ai (dual-mode _client, backend-aware sentinels/ctx window).
"""

import json

import httpx
import ollama
import pytest

import app.ai as ai_module
from app.llm_client import UnslothClient, UnslothResponseError


def _client_with(handler) -> UnslothClient:
    """A UnslothClient whose _http() returns a MockTransport-backed client."""
    client = UnslothClient("http://unsloth:8000", "sk-test")

    def _http():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)

    client._http = _http  # instance attr shadows the method
    return client


def _capture(handler_state):
    """Handler that records the request and replies with `response`."""

    def handler(request: httpx.Request) -> httpx.Response:
        handler_state["request"] = request
        body = json.loads(request.content.decode())
        handler_state["body"] = body
        return handler_state["response"]

    return handler


_CHAT_RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "unsloth/gemma-4-26B-A4B-it-GGUF",
    "choices": [{
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": "Hello there."},
    }],
    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
}


@pytest.mark.asyncio
async def test_chat_translates_options_and_maps_response():
    state = {"response": httpx.Response(200, json=_CHAT_RESPONSE)}
    client = _client_with(_capture(state))
    resp = await client.chat(
        model="unsloth/gemma-4-26B-A4B-it-GGUF",
        messages=[{"role": "user", "content": "hi"}],
        options={"num_predict": 128, "repeat_penalty": 1.1, "temperature": 0.5,
                 "num_ctx": 8192, "mirostat": 2},
        think=False,
    )
    body = state["body"]
    assert body["model"] == "unsloth/gemma-4-26B-A4B-it-GGUF"
    assert body["stream"] is False
    assert body["max_tokens"] == 128                     # num_predict rename
    assert body["repetition_penalty"] == 1.1             # repeat_penalty rename
    assert body["temperature"] == 0.5                    # passthrough
    assert "num_ctx" not in body                         # dropped (load-time knob)
    assert "mirostat" not in body                        # dropped, not whitelisted
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # Response mapping
    assert resp.message.content == "Hello there."
    assert resp.message.thinking is None
    assert resp.done_reason == "stop"
    assert resp.eval_count == 4
    assert resp.prompt_eval_count == 12
    assert resp.total_duration >= 0  # wall clock; a mocked round trip can be ~0


@pytest.mark.asyncio
async def test_chat_always_sends_explicit_thinking_flag():
    state = {"response": httpx.Response(200, json=_CHAT_RESPONSE)}
    client = _client_with(_capture(state))
    await client.chat(model="m", messages=[{"role": "user", "content": "hi"}], think=True)
    assert state["body"]["chat_template_kwargs"] == {"enable_thinking": True}


@pytest.mark.asyncio
async def test_chat_json_schema_format():
    state = {"response": httpx.Response(200, json=_CHAT_RESPONSE)}
    client = _client_with(_capture(state))
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    await client.chat(model="m", messages=[{"role": "user", "content": "x"}], format=schema)
    rf = state["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == schema


@pytest.mark.asyncio
async def test_chat_json_string_format():
    state = {"response": httpx.Response(200, json=_CHAT_RESPONSE)}
    client = _client_with(_capture(state))
    await client.chat(model="m", messages=[{"role": "user", "content": "x"}], format="json")
    assert state["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_chat_translates_images_to_data_url_parts():
    state = {"response": httpx.Response(200, json=_CHAT_RESPONSE)}
    client = _client_with(_capture(state))
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    jpeg_b64 = "/9j/4AAQSkZJRgABAQEASABIAAD"
    await client.chat(
        model="m",
        messages=[{"role": "system", "content": "sys"},
                  {"role": "user", "content": "look", "images": [png_b64, jpeg_b64]}],
    )
    msgs = state["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    parts = msgs[1]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}}
    assert parts[2] == {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}}


@pytest.mark.asyncio
async def test_chat_streaming_maps_sse_chunks():
    sse_lines = "\n".join([
        'data: {"choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"hm"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"index":0,"delta":{"content":"!"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
        "",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_lines.encode(), headers={"content-type": "text/event-stream"})

    client = _client_with(handler)
    chunks = []
    async for chunk in await client.chat(model="m", messages=[{"role": "user", "content": "hi"}], stream=True):
        chunks.append(chunk)
    assert chunks[0].message.thinking == "hm"
    assert chunks[0].message.content == ""
    assert chunks[1].message.content == "Hi"
    assert chunks[2].message.content == "!"
    assert chunks[-1].done_reason == "stop"


@pytest.mark.asyncio
async def test_http_error_raises_response_error_subclass():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "No model loaded", "type": "bad_request"}})

    client = _client_with(handler)
    with pytest.raises(UnslothResponseError) as excinfo:
        await client.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 400
    assert "No model loaded" in excinfo.value.error
    # Subclassing promise: app.ai's `except _ollama.ResponseError` catches it.
    assert isinstance(excinfo.value, ollama.ResponseError)


@pytest.mark.asyncio
async def test_list_maps_openai_model_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "unsloth/gemma-4-26B-A4B-it-GGUF", "object": "model"},
        ]})

    client = _client_with(handler)
    resp = await client.list()
    assert [m.model for m in resp.models] == ["unsloth/gemma-4-26B-A4B-it-GGUF"]
    assert resp.models[0].size is None  # /v1/models carries no size detail


@pytest.mark.asyncio
async def test_generate_raises_not_implemented():
    client = UnslothClient("http://x", "sk-test")
    with pytest.raises(NotImplementedError):
        await client.generate(model="m", keep_alive=0)


# ── app.ai integration: Unsloth mode ────────────────────────────────────────


@pytest.fixture
def unsloth_mode(monkeypatch):
    """Put app.ai into Unsloth mode with a fake client for _client()."""
    monkeypatch.setattr(ai_module, "UNSLOTH_URL", "http://unsloth:8000")
    monkeypatch.setattr(ai_module, "UNSLOTH_MODEL", "unsloth/gemma-4-26B-A4B-it-GGUF")
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "sk-test")
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    return ai_module


@pytest.mark.asyncio
async def test_generate_chat_unsloth_error_sentinel(unsloth_mode, monkeypatch):
    class _Boom:
        async def chat(self, **kwargs):
            raise UnslothResponseError("No model loaded", 400)

    monkeypatch.setattr(ai_module, "_client", lambda: _Boom())
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert result == "[AI error: Unsloth 400: No model loaded]"
    assert ai_module.is_failure_sentinel(result)


@pytest.mark.asyncio
async def test_generate_chat_unsloth_success(unsloth_mode, monkeypatch):
    from types import SimpleNamespace

    class _OK:
        async def chat(self, **kwargs):
            return SimpleNamespace(
                message=SimpleNamespace(content="answer", thinking=None),
                done_reason="stop", eval_count=3,
            )

    monkeypatch.setattr(ai_module, "_client", lambda: _OK())
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert result == "answer"


@pytest.mark.asyncio
async def test_unsloth_mode_uses_configured_context_window(unsloth_mode, monkeypatch):
    assert ai_module._effective_ctx_tokens() == 16384
    monkeypatch.setattr(ai_module, "LLM_CONTEXT_TOKENS", 32768)
    assert ai_module._effective_ctx_tokens() == 32768
    # Clamped to MAX_AUTO_NUM_CTX like any computed ctx value.
    monkeypatch.setattr(ai_module, "LLM_CONTEXT_TOKENS", ai_module.MAX_AUTO_NUM_CTX * 4)
    assert ai_module._effective_ctx_tokens() == ai_module.MAX_AUTO_NUM_CTX


@pytest.mark.asyncio
async def test_unload_model_noop_under_unsloth(unsloth_mode):
    assert await ai_module.unload_model("anything") is False


@pytest.mark.asyncio
async def test_import_local_gguf_rejected_under_unsloth(unsloth_mode, tmp_path):
    from pathlib import Path
    f = tmp_path / "model.gguf"
    f.write_bytes(b"gguf")
    progress = []
    async for item in ai_module.import_local_gguf_model(Path(f), "mymodel"):
        progress.append(item)
    assert len(progress) == 1 and "Unsloth" in progress[0]["error"]


@pytest.mark.asyncio
async def test_known_model_thinks_unsloth_registry(unsloth_mode):
    assert ai_module._known_model_thinks("unsloth/gemma-4-26B-A4B-it-GGUF") is True
    assert ai_module._known_model_thinks("gemma4:26b") is False


def test_client_dual_mode(unsloth_mode):
    assert type(ai_module._client()).__name__ == "UnslothClient"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "")
    try:
        assert type(ai_module._client()).__name__ == "AsyncClient"
    finally:
        monkeypatch.undo()
