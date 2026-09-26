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
    def __init__(self, status_code=200, payload=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        # real responses always carry the body as content — unsloth_extras
        # treats empty content as "no body" and returns {}.
        self.content = content or self.text.encode()

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

    async def request(self, method, url, json=None, headers=None, params=None,
                      content=None, files=None):
        # unsloth_extras uses the generic c.request(...) form for its
        # /api/inference + /api/hub calls.
        self._state["requests"].append((method, url, headers, json))
        return self._handler(method, url, headers, json)


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
async def test_imagegen_models_falls_back_to_configured_model_when_nothing_cached(unsloth_image_mode, monkeypatch):
    def handler(method, url, headers, body):
        assert url == "http://unsloth:8000/api/hub/cached-gguf"
        assert headers == {"Authorization": "Bearer sk-test"}
        return _FakeResponse(200, payload={"cached": []})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    assert await ai_module.imagegen_models() == ["unsloth/z-image-turbo-GGUF"]
    assert await ai_module.imagegen_loras() == []
    assert await ai_module.imagegen_upscalers() == []


@pytest.mark.asyncio
async def test_imagegen_models_lists_cached_supported_image_models(unsloth_image_mode, monkeypatch):
    """Real-world shape from /api/hub/cached-gguf, verified against a live
    Studio instance across several actually-downloaded checkpoints: a
    working, natively-supported model (Krea 2 Turbo) reports task
    "text-to-image" — NOT plain "image-diffusion" as first assumed — an
    SDXL-family anime finetune Studio's pipeline can't run reports
    "image-diffusion-unsupported", a chat model reports "text-generation",
    and a not-yet-classified checkpoint reports task: null. Only the first
    should come back, taking priority over the configured
    UNSLOTH_IMAGE_MODEL fallback."""
    def handler(method, url, headers, body):
        return _FakeResponse(200, payload={"cached": [
            {"repo_id": "vantagewithai/Krea-2-Turbo-GGUF", "task": "text-to-image"},
            {"repo_id": "E-stick/anima-aesthetic-v1.1-GGUF", "task": "image-diffusion-unsupported"},
            {"repo_id": "kazzy1337/some-chat-model", "task": "text-generation"},
            {"repo_id": "voldemir/Kroma-GGUF", "task": None},
        ]})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    assert await ai_module.imagegen_models() == ["vantagewithai/Krea-2-Turbo-GGUF"]


@pytest.mark.asyncio
async def test_unsloth_cached_image_models_swallows_request_errors(unsloth_image_mode, monkeypatch):
    def handler(method, url, headers, body):
        raise RuntimeError("connection refused")

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    assert await ai_module.unsloth_cached_image_models() == []


@pytest.mark.asyncio
async def test_imagegen_progress_is_indeterminate(unsloth_image_mode):
    assert await ai_module.imagegen_progress() == {
        "active": False, "percent": 0.0, "current_percent": 0.0, "preview": "",
    }


@pytest.mark.asyncio
async def test_imagegen_generate_posts_b64_json_and_saves_png(unsloth_image_mode, monkeypatch, tmp_path):
    def handler(method, url, headers, body):
        if "/api/inference/images/generate" in url:
            # No native endpoint on this (simulated) Studio build → the code
            # must fall back to the /v1 flow.
            return _FakeResponse(404, payload={"detail": "API endpoint not found"})
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
        if "/api/inference/images/generate" in url:
            return _FakeResponse(404, payload={"detail": "API endpoint not found"})
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
        if "/api/inference/images/generate" in url:
            return _FakeResponse(404, payload={"detail": "API endpoint not found"})
        return _FakeResponse(200, payload={"data": []})

    _patch_http(monkeypatch, unsloth_image_mode, handler)
    with pytest.raises(ValueError, match="returned no image"):
        await ai_module.imagegen_generate(
            prompt="x", negative="", model="m", width=512, height=512,
            steps=4, cfg=1.0, seed=-1, uploads_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_imagegen_generate_uses_native_endpoint_and_gallery(monkeypatch, unsloth_image_mode, tmp_path):
    """When Studio has the native /api/inference/images/generate, generation
    goes through it — negative prompt and img2img reach the body, and the
    returned gallery records' files are fetched and saved as PNGs."""
    from app import unsloth_extras as _unsloth_extras

    def handler(method, url, headers, body):
        if "/api/inference/images/generate" in url and method == "POST":
            assert body["prompt"] == "a dragon"
            assert body["negative_prompt"] == "blurry"
            assert body["steps"] == 4
            assert body["init_image"].startswith("data:image/png;base64,")
            assert body["strength"] == 0.6
            return _FakeResponse(200, payload={"images": [
                {"id": "img1", "url": "/api/inference/images/gallery/img1/file"},
            ]})
        if "/api/inference/images/gallery/img1/file" in url:
            return _FakeResponse(200, content=_PNG_BYTES)
        raise AssertionError("unexpected call: " + url)

    _patch_http(monkeypatch, unsloth_image_mode, handler)

    init = tmp_path / "ai-images" / "init.png"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_bytes(_PNG_BYTES)

    urls = await ai_module.imagegen_generate(
        prompt="a dragon", negative="blurry", model="unsloth/z-image-turbo-GGUF",
        width=512, height=512, steps=4, cfg=1.0, seed=42, uploads_dir=tmp_path,
        init_image="/uploads/ai-images/init.png", init_strength=0.6,
    )
    assert len(urls) == 1
    assert (tmp_path / "ai-images" / urls[0].rsplit("/", 1)[1]).read_bytes() == _PNG_BYTES
