"""Tests for the model benchmark tool: app.ai.benchmark_model (parses
Ollama's own generation timing/token-count metadata into a tok/s figure)
and POST /api/ai/benchmark (app/routers/ai.py, GM-only).
"""
import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChatResponse:
    def __init__(self, **fields):
        self.message = _FakeMessage("A quiet street glistens under the rain.")
        for k, v in fields.items():
            setattr(self, k, v)


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp


@pytest.mark.asyncio
async def test_benchmark_model_computes_tokens_per_sec(monkeypatch):
    resp = _FakeChatResponse(
        eval_count=100, eval_duration=2_000_000_000,       # 100 tokens in 2s -> 50 tok/s
        prompt_eval_count=20, prompt_eval_duration=500_000_000,  # 20 tokens in 0.5s -> 40 tok/s
        load_duration=250_000_000, total_duration=2_750_000_000,
    )
    fake = _FakeClient(resp)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    result = await ai_module.benchmark_model("llama3:latest")
    assert result["model"] == "llama3:latest"
    assert result["tokens_per_sec"] == 50.0
    assert result["prompt_tokens_per_sec"] == 40.0
    assert result["eval_count"] == 100
    assert result["load_duration_ms"] == 250.0


@pytest.mark.asyncio
async def test_benchmark_model_handles_missing_timing_fields_gracefully(monkeypatch):
    """Older/different Ollama versions might not carry every timing field —
    must not crash, just report 0 throughput rather than dividing by zero."""
    resp = _FakeChatResponse()  # no eval_count/eval_duration/etc at all
    fake = _FakeClient(resp)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    result = await ai_module.benchmark_model("llama3:latest")
    assert result["tokens_per_sec"] == 0.0
    assert result["prompt_tokens_per_sec"] == 0.0


@pytest.mark.asyncio
async def test_benchmark_model_blank_uses_effective_default(monkeypatch):
    resp = _FakeChatResponse(eval_count=10, eval_duration=1_000_000_000)
    fake = _FakeClient(resp)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    monkeypatch.setattr(ai_module, "effective_ollama_model", lambda: "default-model:latest")

    result = await ai_module.benchmark_model("")
    assert result["model"] == "default-model:latest"
    assert fake.calls[0]["model"] == "default-model:latest"


@pytest.mark.asyncio
async def test_benchmark_model_raises_value_error_on_ollama_failure(monkeypatch):
    class _FailingClient:
        async def chat(self, **kwargs):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(ai_module, "_client", lambda: _FailingClient())

    with pytest.raises(ValueError, match="connection refused"):
        await ai_module.benchmark_model("llama3:latest")


# ── POST /api/ai/benchmark ──────────────────────────────────────────────────

def test_benchmark_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/ai/benchmark", json={"model": "llama3"})
    assert r.status_code == 403


def test_benchmark_route_returns_result_for_gm(client, seed, monkeypatch):
    async def fake_benchmark(model):
        return {"model": model or "default", "tokens_per_sec": 42.0}
    monkeypatch.setattr(ai_module, "benchmark_model", fake_benchmark)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/benchmark", json={"model": "llama3"})
    assert r.status_code == 200
    assert r.json() == {"model": "llama3", "tokens_per_sec": 42.0}


def test_benchmark_route_surfaces_failure_as_error_status(client, seed, monkeypatch):
    async def failing_benchmark(model):
        raise ValueError("AI unavailable: ConnectError: refused")
    monkeypatch.setattr(ai_module, "benchmark_model", failing_benchmark)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/benchmark", json={"model": "llama3"})
    assert r.status_code != 200
    assert "refused" in r.json()["detail"]
