"""Tests for the Models tab's "Resident in VRAM" section — app.ai.resident_models
(via Ollama's /api/ps, distinct from _list_loaded/client.list() which only says
what's downloaded to disk) and unload_model (a generate(..., keep_alive=0) call,
Ollama's documented immediate-eviction idiom), plus the two new routes that
back the UI: GET /api/ai/resident and POST /api/ai/unload.
"""
import types
from datetime import datetime, timezone

import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _fake_process_model(model, size=17_000_000_000, size_vram=17_000_000_000, expires_at=None):
    return types.SimpleNamespace(model=model, size=size, size_vram=size_vram, expires_at=expires_at)


class _FakePsClient:
    def __init__(self, models=None, raise_exc=None):
        self._models = models or []
        self._raise = raise_exc

    async def ps(self):
        if self._raise:
            raise self._raise
        return types.SimpleNamespace(models=self._models)


class _FakeGenerateClient:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise:
            raise self._raise
        return types.SimpleNamespace()


# ── app.ai.resident_models ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resident_models_parses_ps_response(monkeypatch):
    expires = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fake = _FakePsClient(models=[_fake_process_model("gemma4:26b", size=17_000_000_000, size_vram=16_500_000_000, expires_at=expires)])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    result = await ai_module.resident_models()
    assert len(result) == 1
    assert result[0]["model"] == "gemma4:26b"
    assert result[0]["size_bytes"] == 17_000_000_000
    assert result[0]["size_vram_bytes"] == 16_500_000_000
    assert result[0]["size_ram_bytes"] == 500_000_000  # size - size_vram: offloaded to system RAM
    assert result[0]["expires_at"] == expires.isoformat()


@pytest.mark.asyncio
async def test_resident_models_fully_in_vram_has_zero_ram(monkeypatch):
    """A model that fits entirely in VRAM has nothing offloaded to system
    RAM — size_ram_bytes should be 0, not None or a stray negative number."""
    fake = _FakePsClient(models=[_fake_process_model("gemma4:26b", size=17_000_000_000, size_vram=17_000_000_000)])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    result = await ai_module.resident_models()
    assert result[0]["size_ram_bytes"] == 0


@pytest.mark.asyncio
async def test_resident_models_ram_none_when_sizes_unknown(monkeypatch):
    fake = _FakePsClient(models=[_fake_process_model("gemma4:26b", size=None, size_vram=None)])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    result = await ai_module.resident_models()
    assert result[0]["size_ram_bytes"] is None


@pytest.mark.asyncio
async def test_resident_models_empty_when_nothing_loaded(monkeypatch):
    monkeypatch.setattr(ai_module, "_client", lambda: _FakePsClient(models=[]))
    assert await ai_module.resident_models() == []


@pytest.mark.asyncio
async def test_resident_models_returns_empty_on_failure_not_an_exception(monkeypatch):
    monkeypatch.setattr(ai_module, "_client", lambda: _FakePsClient(raise_exc=ConnectionError("down")))
    assert await ai_module.resident_models() == []


# ── app.ai.unload_model ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unload_model_calls_generate_with_keep_alive_zero(monkeypatch):
    fake = _FakeGenerateClient()
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    ok = await ai_module.unload_model("gemma4:26b")
    assert ok is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "gemma4:26b"
    assert fake.calls[0]["keep_alive"] == 0


@pytest.mark.asyncio
async def test_unload_model_returns_false_not_an_exception_on_failure(monkeypatch):
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeGenerateClient(raise_exc=ConnectionError("down")))
    assert await ai_module.unload_model("gemma4:26b") is False


# ── Routes ───────────────────────────────────────────────────────────────────

def test_api_resident_returns_mocked_list(client, seed, monkeypatch):
    async def fake_resident():
        return [{"model": "gemma4:26b", "size_bytes": 17_000_000_000, "size_vram_bytes": 16_500_000_000, "expires_at": None}]
    monkeypatch.setattr(ai_module, "resident_models", fake_resident)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/resident")
    assert r.status_code == 200
    assert r.json()["models"][0]["model"] == "gemma4:26b"


def test_api_unload_success(client, seed, monkeypatch):
    async def fake_unload(model_id):
        assert model_id == "gemma4:26b"
        return True
    monkeypatch.setattr(ai_module, "unload_model", fake_unload)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/unload", json={"model_id": "gemma4:26b"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_api_unload_failure_returns_502(client, seed, monkeypatch):
    async def fake_unload(model_id):
        return False
    monkeypatch.setattr(ai_module, "unload_model", fake_unload)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/unload", json={"model_id": "gemma4:26b"})
    assert r.status_code == 502


def test_api_unload_requires_model_id(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/unload", json={"model_id": "  "})
    assert r.status_code == 400


def test_player_cannot_call_resident_or_unload(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/resident").status_code == 403
    assert client.post("/api/ai/unload", json={"model_id": "x"}).status_code == 403
