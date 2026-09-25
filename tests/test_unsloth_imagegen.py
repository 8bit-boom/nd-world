"""Tests for the Unsloth image-generation backend (migration plan §7 Plan A).

app.ai's imagegen_* functions build their own httpx clients, so these tests
swap httpx.AsyncClient for a fake capturing requests and serving canned
responses — the same pattern the rest of this suite uses for SwarmUI/ComfyUI.
"""

import base64 as _b64
import json

import pytest

import app.ai as ai_module


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: records requests, serves the queued
    responses (or computes them via a handler)."""

    def __init__(self, state, handler):
        self._state = state
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        self._state["requests"].append(("GET", url, headers, None))
        return self._handler("GET", url, headers, None)

    async def post(self, url, json=None, headers=None):
        self._state["requests"].append(("POST", url, headers, json))
        return self._handler("POST", url, headers, json)


@pytest.fixture
def unsloth_image_mode(monkeypatch):
    monkeypatch.setattr(ai_module, "UNSLOTH_URL", "http://unsloth:8000")
    monkeypatch.setattr(ai_module, "UNSLOTH_MODEL", "unsloth/gemma-4-26B-A4B-it-GGUF")
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "sk-test")
    monkeypatch.setattr(ai_module, "UNSLOTH_IMAGE_MODEL", "unsloth/z-image-turbo-GGUF")
    state = {"requests": []}
    return state


def _patch_http(monkeypatch, state, handler):
    monkeypatch.setattr(ai_module._httpx, "AsyncClient",
                        lambda *a, **k: _FakeAsyncClient(state, handler))


_PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepng"


@pytest.mark.asyncio
async def test_imagegen_type_prefers_unsloth(unsloth_image_mode):
    assert ai_module._get_type() == "unsloth"
    assert ai_module._get_url() == "http://unsloth:8000"


def test_imagegen_type_falls_back_to_legacy(unsloth_image_mode, monkeypatch):
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "")
    monkeypatch.setattr(ai_module, "_IMAGEGEN_TYPE", "swarmui")
    assert ai_module._get_type() == "swarmui"


@pytest.mark.asyncio
async def test_imagegen_status_probes_v1_models_with_auth(unsloth_image_mode, monkeypatch):
    def handler(method, url, headers, body):
        assert url == "http://unsloth:8000/v1/models"
        assert headers == {"Authorization": "Bearer sk-test"}
        return _FakeResponse(200, payload={"data": []})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    status = await ai_module.imagegen_status()
    assert status == {"ok": True, "type": "unsloth", "url": "http://unsloth:8000"}


@pytest.mark.asyncio
async def test_imagegen_models_returns_configured_model(unsloth_image_mode):
    assert await ai_module.imagegen_models() == ["unsloth/z-image-turbo-GGUF"]
    assert await ai_module.imagegen_loras() == []
    assert await ai_module.imagegen_upscalers() == []


@pytest.mark.asyncio
async def test_imagegen_progress_is_indeterminate(unsloth_image_mode):
    assert await ai_module.imagegen_progress() == {
        "active": False, "percent": 0.0, "current_percent": 0.0, "preview": "",
    }


@pytest.mark.asyncio
async def test_imagegen_generate_posts_b64_json_and_saves_png(unsloth_image_mode, monkeypatch, tmp_path):
    def handler(method, url, headers, body):
        assert url == "http://unsloth:8000/v1/images/generations"
        assert body["model"] == "unsloth/z-image-turbo-GGUF"
        assert body["size"] == "1024x1024"
        assert body["response_format"] == "b64_json"
        assert body["seed"] == 42
        return _FakeResponse(200, payload={"data": [
            {"b64_json": _b64.b64encode(_PNG_BYTES).decode()},
            {"b64_json": _b64.b64encode(_PNG_BYTES).decode()},
        ]})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    urls = await ai_module.imagegen_generate(
        prompt="a dragon", negative="", model="ignored", width=1024, height=1024,
        steps=4, cfg=1.0, seed=42, uploads_dir=tmp_path,
    )
    assert len(urls) == 2
    for u in urls:
        assert u.startswith("/uploads/ai-images/")
        assert (tmp_path / "ai-images" / u.rsplit("/", 1)[1]).read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_imagegen_generate_503_explains_media_auto_switch(unsloth_image_mode, monkeypatch, tmp_path):
    def handler(method, url, headers, body):
        return _FakeResponse(503, payload={"error": {"message": "No image model loaded"}})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    with pytest.raises(ValueError) as excinfo:
        await ai_module.imagegen_generate(
            prompt="x", negative="", model="m", width=512, height=512,
            steps=4, cfg=1.0, seed=-1, uploads_dir=tmp_path,
        )
    assert "media auto-switch" in str(excinfo.value)


@pytest.mark.asyncio
async def test_imagegen_generate_empty_data_raises(unsloth_image_mode, monkeypatch, tmp_path):
    def handler(method, url, headers, body):
        return _FakeResponse(200, payload={"data": []})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    with pytest.raises(ValueError, match="returned no image"):
        await ai_module.imagegen_generate(
            prompt="x", negative="", model="m", width=512, height=512,
            steps=4, cfg=1.0, seed=-1, uploads_dir=tmp_path,
        )
