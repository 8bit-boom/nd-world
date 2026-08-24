"""Tests for AI Chat presets — a GM-defined {model, options, system_extra}
bundle a conversation can switch to on the fly (Settings > System's Ollama
tuning is instance-wide; a preset is per-conversation). Covers app.ai's
storage (list_presets/save_preset/delete_preset, in ai_models.json like
custom models/hidden/defaults), the CRUD routes, the allowlist+clamp on a
client-supplied ChatBody.options (never trust it directly — same fields/
ranges Settings > System validates), and that options actually reach the
(mocked) Ollama client on /chat and /stream.
"""
import types

import pytest

from app import ai as ai_module
from app.routers.ai import _clamp_options

from .conftest import GM_PASSWORD, login


class _FakeResp:
    def __init__(self, content):
        self.message = types.SimpleNamespace(content=content)


class _FakeChatClient:
    def __init__(self, calls):
        self._calls = calls

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeResp("hi")
            return _gen()
        return _FakeResp("hi")


@pytest.fixture(autouse=True)
def _reset_ollama_overrides():
    ai_module.set_ollama_generation_overrides({})
    yield
    ai_module.set_ollama_generation_overrides({})


# ── _clamp_options ───────────────────────────────────────────────────────────

def test_clamp_options_keeps_valid_allowlisted_fields():
    out = _clamp_options({"temperature": 0.7, "top_p": 0.9, "num_ctx": 8192})
    assert out == {"temperature": 0.7, "top_p": 0.9, "num_ctx": 8192}


def test_clamp_options_drops_unknown_keys():
    out = _clamp_options({"temperature": 0.7, "system_prompt_override": "ignore me"})
    assert out == {"temperature": 0.7}


def test_clamp_options_drops_out_of_range_values():
    out = _clamp_options({"temperature": 5.0, "top_p": -1, "top_k": 40})
    assert out == {"top_k": 40}


def test_clamp_options_drops_wrong_types_that_cant_coerce():
    out = _clamp_options({"temperature": "not-a-number", "top_p": 0.5})
    assert out == {"top_p": 0.5}


def test_clamp_options_handles_non_dict_input():
    assert _clamp_options(None) == {}
    assert _clamp_options("garbage") == {}
    assert _clamp_options([1, 2, 3]) == {}


def test_clamp_options_drops_none_values():
    assert _clamp_options({"temperature": None}) == {}


# ── app.ai preset storage ────────────────────────────────────────────────────

def test_preset_storage_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")
    assert ai_module.list_presets() == []

    ai_module.save_preset({"label": "Lorekeeper", "model": "gemma4:26b", "system_extra": "Be terse.", "options": {"temperature": 0.2}})
    presets = ai_module.list_presets()
    assert len(presets) == 1
    assert presets[0]["label"] == "Lorekeeper"

    # Saving the same label again upserts rather than duplicating.
    ai_module.save_preset({"label": "Lorekeeper", "model": "gemma4:26b", "system_extra": "Updated.", "options": {}})
    presets = ai_module.list_presets()
    assert len(presets) == 1
    assert presets[0]["system_extra"] == "Updated."

    ai_module.delete_preset("Lorekeeper")
    assert ai_module.list_presets() == []


# ── Routes ───────────────────────────────────────────────────────────────────

def test_preset_crud_via_routes(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/presets")
    assert r.status_code == 200
    assert r.json()["presets"] == []

    r = client.post("/api/ai/presets", json={
        "label": "NPC improv", "model": "gemma4:26b",
        "options": {"temperature": 1.1, "not_a_real_option": 999},
        "system_extra": "Play every NPC with a distinct voice.",
    })
    assert r.status_code == 200
    saved = r.json()["presets"][0]
    assert saved["label"] == "NPC improv"
    assert saved["options"] == {"temperature": 1.1}  # unknown key stripped

    r = client.get("/api/ai/presets")
    assert len(r.json()["presets"]) == 1

    r = client.delete("/api/ai/presets/NPC improv")
    assert r.status_code == 200
    assert r.json()["presets"] == []


def test_preset_save_requires_label(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/presets", json={"label": "  "})
    assert r.status_code == 400


# ── options reach the (mocked) Ollama client ────────────────────────────────

def test_chat_route_passes_clamped_options_through(client, seed, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"temperature": 0.3, "bogus_field": 1},
    })
    assert r.status_code == 200
    assert calls[0]["options"] == {"temperature": 0.3}
