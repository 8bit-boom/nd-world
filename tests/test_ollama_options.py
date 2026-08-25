"""Tests for the Ollama per-request generation tuning added to Settings >
System (temperature, top_p, num_ctx, mirostat, keep_alive, ...) — covers the
app.ai override plumbing (set_ollama_generation_overrides/effective_*/
_chat_kwargs, and that generate_chat/stream_chat/parse_facts_from_recap
actually splat those kwargs into the (mocked) Ollama client call), plus the
Settings save/validation round-trip and that a save pushes the new values
into app.ai without a restart (mirroring test_settings_system.py's existing
ollama_url/ollama_model override tests).

Deliberately separate from the server-level env vars (OLLAMA_KV_CACHE_TYPE
etc.) documented in .env.example/docker-compose.yml — those configure the
Ollama container process itself and have no runtime/test surface in this
app at all.
"""
import types

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import AppSettings

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeResp:
    def __init__(self, content):
        self.message = types.SimpleNamespace(content=content)


class _FakeChatClient:
    """Records every kwargs dict passed to .chat() so tests can assert
    options=/keep_alive= were (or weren't) included."""

    def __init__(self, calls):
        self._calls = calls

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeResp("hi")
            return _gen()
        if kwargs.get("format"):
            return _FakeResp('{"facts": []}')
        return _FakeResp("hi")


@pytest.fixture(autouse=True)
def _reset_ollama_overrides():
    ai_module.set_ollama_generation_overrides({})
    yield
    ai_module.set_ollama_generation_overrides({})


# ── app.ai override plumbing ────────────────────────────────────────────────

def test_effective_options_empty_by_default():
    assert ai_module.effective_ollama_options() == {}
    assert ai_module.effective_ollama_keep_alive() == ""


def test_set_generation_overrides_roundtrip():
    ai_module.set_ollama_generation_overrides({"temperature": 0.5, "num_ctx": 8192}, "10m")
    assert ai_module.effective_ollama_options() == {"temperature": 0.5, "num_ctx": 8192}
    assert ai_module.effective_ollama_keep_alive() == "10m"


def test_set_generation_overrides_clears_back_to_empty():
    ai_module.set_ollama_generation_overrides({"temperature": 0.5}, "10m")
    ai_module.set_ollama_generation_overrides({})
    assert ai_module.effective_ollama_options() == {}
    assert ai_module.effective_ollama_keep_alive() == ""


@pytest.mark.asyncio
async def test_generate_chat_omits_kwargs_when_unset(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert result == "hi"
    assert "options" not in calls[0]
    assert "keep_alive" not in calls[0]
    assert calls[0]["think"] is False


@pytest.mark.asyncio
async def test_generate_chat_passes_options_and_keep_alive(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"temperature": 0.2, "top_k": 40}, "5m")
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert result == "hi"
    assert calls[0]["options"] == {"temperature": 0.2, "top_k": 40}
    assert calls[0]["keep_alive"] == "5m"


class _FakeRespFull:
    """Same shape as _FakeResp but also carries done_reason/eval_count and
    message.thinking, like a real ollama.ChatResponse — needed to exercise
    generate_chat's empty-content diagnostic message (see app.ai.generate_chat)."""

    def __init__(self, content, done_reason=None, eval_count=None, thinking=None):
        self.message = types.SimpleNamespace(content=content, thinking=thinking)
        self.done_reason = done_reason
        self.eval_count = eval_count


class _FakeFixedRespClient:
    def __init__(self, resp):
        self._resp = resp

    async def chat(self, **kwargs):
        return self._resp


@pytest.mark.asyncio
async def test_generate_chat_empty_content_reports_done_reason(monkeypatch):
    resp = _FakeRespFull("", done_reason="length", eval_count=512)
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeFixedRespClient(resp))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert "empty response" in result
    assert "done_reason=length" in result


@pytest.mark.asyncio
async def test_generate_chat_empty_content_without_done_reason(monkeypatch):
    resp = _FakeRespFull("", done_reason=None, eval_count=None)
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeFixedRespClient(resp))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert "empty response" in result
    assert "no done_reason reported" in result


@pytest.mark.asyncio
async def test_generate_chat_empty_content_with_thinking_reports_it_instead(monkeypatch):
    """A model that doesn't honor think=False can still burn its whole output
    budget on hidden reasoning — content ends up empty, but message.thinking
    (only present on a real ollama>=0.5 client) has text. That case should be
    reported specifically rather than falling through to the generic
    done_reason message."""
    resp = _FakeRespFull("", done_reason="length", eval_count=512, thinking="pondering deeply...")
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeFixedRespClient(resp))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert "empty response" in result
    assert "hidden" in result and "thinking" in result
    assert "done_reason=length" not in result


@pytest.mark.asyncio
async def test_stream_chat_passes_options(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"top_p": 0.9})
    tokens = [tok async for tok in ai_module.stream_chat([{"role": "user", "content": "hi"}])]
    assert tokens == ["hi"]
    assert calls[0]["options"] == {"top_p": 0.9}
    assert "keep_alive" not in calls[0]


@pytest.mark.asyncio
async def test_parse_facts_from_recap_passes_options_and_keep_alive(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"seed": 42}, "1h")
    facts = await ai_module.parse_facts_from_recap("some recap text")
    assert facts == []
    assert calls[0]["options"] == {"seed": 42}
    assert calls[0]["keep_alive"] == "1h"
    # format= (the JSON-schema constraint) must still be sent alongside.
    assert calls[0]["format"]


# ── Settings > System save/validation round-trip ────────────────────────────

def test_generation_settings_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_temperature": "0.5",
    })
    assert r.status_code == 403


def test_generation_settings_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_temperature": "0.7",
        "ollama_top_p": "0.95",
        "ollama_top_k": "40",
        "ollama_repeat_penalty": "1.1",
        "ollama_num_predict": "512",
        "ollama_num_ctx": "8192",
        "ollama_seed": "1234",
        "ollama_mirostat": "2",
        "ollama_mirostat_tau": "5.0",
        "ollama_mirostat_eta": "0.1",
        "ollama_num_gpu": "20",
        "ollama_keep_alive": "10m",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_temperature == 0.7
        assert settings.ollama_top_p == 0.95
        assert settings.ollama_top_k == 40
        assert settings.ollama_repeat_penalty == 1.1
        assert settings.ollama_num_predict == 512
        assert settings.ollama_num_ctx == 8192
        assert settings.ollama_seed == 1234
        assert settings.ollama_mirostat == 2
        assert settings.ollama_mirostat_tau == 5.0
        assert settings.ollama_mirostat_eta == 0.1
        assert settings.ollama_num_gpu == 20
        assert settings.ollama_keep_alive == "10m"
    finally:
        db.close()

    # Pushed live into app.ai without a restart, same as ollama_url/model.
    assert ai_module.effective_ollama_options() == {
        "temperature": 0.7, "top_p": 0.95, "top_k": 40, "repeat_penalty": 1.1,
        "num_predict": 512, "num_ctx": 8192, "seed": 1234, "mirostat": 2,
        "mirostat_tau": 5.0, "mirostat_eta": 0.1, "num_gpu": 20,
    }
    assert ai_module.effective_ollama_keep_alive() == "10m"

    page = client.get("/settings?tab=system")
    assert 'value="0.7"' in page.text
    assert 'value="10m"' in page.text


def test_generation_settings_blank_means_unset(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_temperature": "0.7", "ollama_num_ctx": "8192",
    }, follow_redirects=False)
    # Re-save with everything blank — should clear back to None/unset, not
    # silently keep the old values.
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_temperature is None
        assert settings.ollama_num_ctx is None
        assert settings.ollama_keep_alive == ""
    finally:
        db.close()
    assert ai_module.effective_ollama_options() == {}
    assert ai_module.effective_ollama_keep_alive() == ""


@pytest.mark.parametrize("field,value", [
    ("ollama_temperature", "not-a-number"),
    ("ollama_temperature", "5.0"),      # above the 0-2 bound
    ("ollama_top_p", "1.5"),            # above the 0-1 bound
    ("ollama_top_k", "-1"),             # below 0
    ("ollama_num_ctx", "0"),            # below 1
    ("ollama_mirostat", "3"),           # not in {0,1,2}
])
def test_generation_settings_out_of_range_rejected(client, seed, field, value):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        field: value,
    })
    assert r.status_code == 400

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert not settings or getattr(settings, field) is None
    finally:
        db.close()
