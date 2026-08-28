"""ai.status() (backing POST /api/ai/status, polled once per open tab per
page load via base.html) now caches its result briefly rather than hitting
Ollama's /api/tags on every call — see the module-level _status_cache in
app/ai.py."""
import pytest

from app import ai as ai_module


@pytest.fixture(autouse=True)
def _reset_status_cache():
    ai_module._status_cache = None
    yield
    ai_module._status_cache = None


class _FakeModel:
    def __init__(self, name):
        self.model = name


class _FakeListResp:
    def __init__(self, names):
        self.models = [_FakeModel(n) for n in names]


class _FakeClient:
    def __init__(self, names, calls):
        self._names = names
        self._calls = calls

    async def list(self):
        self._calls.append(1)
        return _FakeListResp(self._names)


@pytest.mark.asyncio
async def test_status_hits_ollama_once_across_repeated_calls_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeClient(["gemma3:12b"], calls))

    r1 = await ai_module.status()
    r2 = await ai_module.status()
    assert r1 == r2 == {"status": "ok", "model": ai_module.effective_ollama_model(), "loaded_models": ["gemma3:12b"]}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_status_refreshes_after_the_ttl_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeClient(["gemma3:12b"], calls))
    await ai_module.status()
    assert len(calls) == 1

    # Simulate the TTL having elapsed without actually sleeping.
    ts, result = ai_module._status_cache
    ai_module._status_cache = (ts - ai_module._STATUS_CACHE_TTL - 1, result)

    await ai_module.status()
    assert len(calls) == 2
