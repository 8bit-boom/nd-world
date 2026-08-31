"""Tests for app.ai.imagegen_generate's own SwarmUI/ComfyUI httpx call
chain — as opposed to tests/test_imagegen_generate.py, which only exercises
the /api/ai/imagegen/generate route with imagegen_generate itself mocked
out. These fake httpx.AsyncClient directly (same convention as
tests/test_swarmui_model_downloads.py's own _patch_httpx) so the real
response-parsing/error-handling code in imagegen_generate actually runs.

Covers the correctness/error-surfacing gaps an audit of the SwarmUI
integration found: an unconfigured backend, a non-2xx or unreadable
response, a "success" body with no images (must never look like it
worked), a connection failure, three different image-encoding shapes
SwarmUI can return (data: URL / raw base64 / a saved-file path), and
ComfyUI's own missing-prompt_id / generation-error / poll-timeout cases.
"""
import asyncio
import base64
import json

import httpx as _real_httpx
import pytest

from app import ai as ai_module

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png data"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=b""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


class _FakeAsyncClient:
    """Routes .post()/.get() by URL suffix — post_map/get_map values are
    either a _FakeResponse or a zero-arg callable returning one (for
    call-count-dependent behavior, e.g. ComfyUI's history poll)."""

    def __init__(self, post_map=None, get_map=None, post_exc=None):
        self._post_map = post_map or {}
        self._get_map = get_map or {}
        self._post_exc = post_exc
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        self.post_calls.append((url, json))
        if self._post_exc:
            raise self._post_exc
        for suffix, resp in self._post_map.items():
            if url.endswith(suffix):
                return resp() if callable(resp) else resp
        return _FakeResponse(200, {})

    async def get(self, url, params=None, **kw):
        self.get_calls.append((url, params))
        for suffix, resp in self._get_map.items():
            if suffix in url:
                return resp() if callable(resp) else resp
        return _FakeResponse(404, {}, text="not found")


class _FakeHttpxModule:
    """Overrides AsyncClient only — imagegen_generate's own error handling
    also references _httpx.HTTPError (to catch real transport failures),
    so anything not explicitly overridden here must still resolve to the
    real httpx module rather than raising AttributeError."""

    def __init__(self, fake_client):
        self._fake_client = fake_client

    def AsyncClient(self, **kw):
        return self._fake_client

    def __getattr__(self, name):
        return getattr(_real_httpx, name)


def _patch_httpx(monkeypatch, **kwargs):
    fake = _FakeAsyncClient(**kwargs)
    monkeypatch.setattr(ai_module, "_httpx", _FakeHttpxModule(fake))
    return fake


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """ComfyUI's poll loop does `await asyncio.sleep(1)` per iteration —
    make it instant so a poll-timeout test doesn't really take ~10 minutes.
    imagegen_generate imports asyncio locally, but that's the same module
    object as this top-level import, so patching it here reaches it too."""
    async def _fast_sleep(_seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)


@pytest.fixture(autouse=True)
def _no_real_websocket_by_default(monkeypatch):
    """The SwarmUI branch now tries a websocket connect before falling
    back to plain HTTP (see _try_swarmui_ws_generate) — every test in this
    file that doesn't care about that path needs it to fail INSTANTLY
    (not attempt a real connection to a fake host, which could hang or
    hit this sandbox's own network egress restrictions) so the existing
    HTTP-path assertions below aren't slowed down or made flaky. Tests
    that DO want to exercise the websocket path override this with their
    own monkeypatch.setattr call, which — same monkeypatch fixture
    instance, later in the same test — wins over this autouse one."""
    class _InstantFailConnect:
        def __call__(self, *a, **kw):
            return self

        async def __aenter__(self):
            raise ConnectionRefusedError("no real SwarmUI in tests")

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(ai_module._websockets, "connect", _InstantFailConnect())


@pytest.fixture(autouse=True)
def _reset_imagegen_progress():
    ai_module._reset_imagegen_progress()
    yield
    ai_module._reset_imagegen_progress()


def _gen_kwargs(tmp_path, **overrides):
    kwargs = dict(
        prompt="a neon dragon", negative="", model="model.safetensors",
        width=512, height=512, steps=20, cfg=7.0, seed=-1, uploads_dir=tmp_path,
    )
    kwargs.update(overrides)
    return kwargs


# ── unconfigured backend ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unconfigured_backend_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "")
    with pytest.raises(ValueError, match="not configured"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


# ── SwarmUI: image-encoding shapes ────────────────────────────────────────

@pytest.mark.asyncio
async def test_swarmui_data_url_image_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    b64 = base64.b64encode(_PNG_BYTES).decode()
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": [f"data:image/png;base64,{b64}"]}),
    })
    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1
    saved = tmp_path / "ai-images" / urls[0].split("/")[-1]
    assert saved.read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_swarmui_raw_base64_image_saved(tmp_path, monkeypatch):
    """No "data:" prefix — some SwarmUI versions/configs return bare
    base64 instead of a data URL."""
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    b64 = base64.b64encode(_PNG_BYTES).decode()
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": [b64]}),
    })
    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1
    saved = tmp_path / "ai-images" / urls[0].split("/")[-1]
    assert saved.read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_swarmui_path_response_fetched_and_saved(tmp_path, monkeypatch):
    """With saving enabled, SwarmUI can return a saved-file path (not
    base64 at all) — must be fetched as bytes from {url}/{path}, not
    treated as a corrupt base64 string."""
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    fake = _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": ["View/local/raw/2024/x.png"]}),
    }, get_map={
        "View/local/raw/2024/x.png": _FakeResponse(200, content=_PNG_BYTES),
    })
    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1
    saved = tmp_path / "ai-images" / urls[0].split("/")[-1]
    assert saved.read_bytes() == _PNG_BYTES
    assert any("View/local/raw/2024/x.png" in u for u, _ in fake.get_calls)


@pytest.mark.asyncio
async def test_swarmui_path_response_fetch_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": ["View/local/raw/2024/x.png"]}),
    }, get_map={
        "View/local/raw/2024/x.png": _FakeResponse(404, text="not found"),
    })
    with pytest.raises(ValueError, match="SwarmUI"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


# ── SwarmUI: error surfacing ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_swarmui_error_body_raises_with_message(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": [], "error": "model not loaded"}),
    })
    with pytest.raises(ValueError, match="model not loaded"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


@pytest.mark.asyncio
async def test_swarmui_http_error_status_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(500, text="Internal Server Error"),
    })
    with pytest.raises(ValueError, match="HTTP 500"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


@pytest.mark.asyncio
async def test_swarmui_unreadable_response_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, json_data=None, text="<html>not json</html>"),
    })
    with pytest.raises(ValueError, match="unreadable"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


@pytest.mark.asyncio
async def test_swarmui_connection_error_names_backend_and_url(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_exc=_real_httpx.ConnectError("Connection refused"))
    with pytest.raises(ValueError, match=r"SwarmUI unreachable at http://fake-swarmui"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


# ── ComfyUI ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_comfyui_missing_prompt_id_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    _patch_httpx(monkeypatch, post_map={
        "/prompt": _FakeResponse(400, {"error": "bad workflow", "node_errors": {"5": "missing input"}}),
    })
    with pytest.raises(ValueError, match="rejected the workflow"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


@pytest.mark.asyncio
async def test_comfyui_generation_error_status_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    _patch_httpx(monkeypatch, post_map={
        "/prompt": _FakeResponse(200, {"prompt_id": "p1"}),
    }, get_map={
        "/history/p1": _FakeResponse(200, {"p1": {"status": {"status_str": "error", "messages": ["OOM"]}}}),
    })
    with pytest.raises(ValueError, match="ComfyUI generation failed"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


@pytest.mark.asyncio
async def test_comfyui_success_saves_image(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    hist_body = {"p1": {"outputs": {"7": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}}
    _patch_httpx(monkeypatch, post_map={
        "/prompt": _FakeResponse(200, {"prompt_id": "p1"}),
    }, get_map={
        "/history/p1": _FakeResponse(200, hist_body),
        "/view": _FakeResponse(200, content=_PNG_BYTES),
    })
    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1
    saved = tmp_path / "ai-images" / urls[0].split("/")[-1]
    assert saved.read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_comfyui_poll_timeout_raises_instead_of_silently_returning_empty(tmp_path, monkeypatch):
    """Regression guard: the old code just `break`d out of the range(120)
    loop with no outputs and returned an empty list — a "done" job with
    zero images. Must now raise instead."""
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    _patch_httpx(monkeypatch, post_map={
        "/prompt": _FakeResponse(200, {"prompt_id": "p1"}),
    }, get_map={
        "/history/p1": _FakeResponse(200, {"p1": {}}),  # never produces outputs
    })
    with pytest.raises(ValueError, match="timed out"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


@pytest.mark.asyncio
async def test_comfyui_outputs_with_no_images_raises_not_silent_empty(tmp_path, monkeypatch):
    """hist.get("outputs") is truthy but the images list inside it is
    empty — the ladder must still not report success with zero urls."""
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    hist_body = {"p1": {"outputs": {"7": {"images": []}}}}
    _patch_httpx(monkeypatch, post_map={
        "/prompt": _FakeResponse(200, {"prompt_id": "p1"}),
    }, get_map={
        "/history/p1": _FakeResponse(200, hist_body),
    })
    with pytest.raises(ValueError, match="returned no images"):
        await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))


# ── SwarmUI websocket progress path ────────────────────────────────────────

class _FakeWSConnectCtx:
    """Fakes websockets.connect(...)'s async-context-manager-returning-an-
    async-iterable-with-.send() shape. `messages` are pre-serialized JSON
    strings, yielded in order via `async for` (matching real websockets'
    own iteration protocol); `raise_on_enter`, if set, simulates a
    connect-time failure (e.g. an older SwarmUI without this route)."""

    def __init__(self, messages=None, raise_on_enter=None):
        self._messages = messages or []
        self._raise_on_enter = raise_on_enter
        self.sent = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        if self._raise_on_enter:
            raise self._raise_on_enter
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


@pytest.mark.asyncio
async def test_swarmui_ws_path_used_when_available_reports_progress_and_saves_image(tmp_path, monkeypatch):
    """Happy path: the websocket connects, streams a gen_progress event
    then an image, and the plain HTTP /API/GenerateText2Image call is
    never made at all."""
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    b64 = base64.b64encode(_PNG_BYTES).decode()

    progress_snapshots = []

    class _TrackingWS(_FakeWSConnectCtx):
        def _gen(self):
            async def _inner():
                yield json.dumps({"gen_progress": {"overall_percent": 0.5, "current_percent": 0.9}})
                progress_snapshots.append(dict(ai_module._imagegen_progress_state))
                yield json.dumps({"image": f"data:image/png;base64,{b64}"})
            return _inner()

    ws = _TrackingWS()
    monkeypatch.setattr(ai_module._websockets, "connect", ws)
    fake_http = _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
    })

    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1
    saved = tmp_path / "ai-images" / urls[0].split("/")[-1]
    assert saved.read_bytes() == _PNG_BYTES

    assert progress_snapshots[0]["active"] is True
    assert progress_snapshots[0]["percent"] == 50.0
    assert progress_snapshots[0]["current_percent"] == 90.0
    # Reset back to idle once generation finished — no stale "in progress"
    # state left behind for the next poll of GET /imagegen/progress.
    assert ai_module._imagegen_progress_state["active"] is False

    assert not any(url.endswith("/API/GenerateText2Image") for url, _ in fake_http.post_calls)
    assert ws.sent  # the payload was actually sent over the websocket


@pytest.mark.asyncio
async def test_swarmui_falls_back_to_http_when_websocket_connect_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    b64 = base64.b64encode(_PNG_BYTES).decode()
    ws = _FakeWSConnectCtx(raise_on_enter=ConnectionRefusedError("older SwarmUI, no WS route"))
    monkeypatch.setattr(ai_module._websockets, "connect", ws)
    fake_http = _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": [f"data:image/png;base64,{b64}"]}),
    })

    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1
    assert any(url.endswith("/API/GenerateText2Image") for url, _ in fake_http.post_calls)
    # Falling back must never leave "active" progress state stuck on.
    assert ai_module._imagegen_progress_state["active"] is False


@pytest.mark.asyncio
async def test_swarmui_falls_back_to_http_when_websocket_closes_with_no_image(tmp_path, monkeypatch):
    """The websocket connects and streams progress but never sends an
    `image` message before closing (an unrecognized/changed message
    shape) — must fall back to plain HTTP rather than reporting success
    with zero images."""
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    b64 = base64.b64encode(_PNG_BYTES).decode()
    ws = _FakeWSConnectCtx(messages=[json.dumps({"gen_progress": {"overall_percent": 0.2}})])
    monkeypatch.setattr(ai_module._websockets, "connect", ws)
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": [f"data:image/png;base64,{b64}"]}),
    })

    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1  # recovered via the HTTP fallback


@pytest.mark.asyncio
async def test_swarmui_ws_error_message_falls_back_to_http(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    b64 = base64.b64encode(_PNG_BYTES).decode()
    ws = _FakeWSConnectCtx(messages=[json.dumps({"error": "model not loaded"})])
    monkeypatch.setattr(ai_module._websockets, "connect", ws)
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": _FakeResponse(200, {"session_id": "s1"}),
        "/API/GenerateText2Image": _FakeResponse(200, {"images": [f"data:image/png;base64,{b64}"]}),
    })

    urls = await ai_module.imagegen_generate(**_gen_kwargs(tmp_path))
    assert len(urls) == 1


# ── imagegen_progress() ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_imagegen_progress_reflects_live_state_for_swarmui(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    ai_module._imagegen_progress_state.update({
        "active": True, "percent": 42.0, "current_percent": 80.0, "preview": "data:x",
    })
    result = await ai_module.imagegen_progress()
    assert result == {"active": True, "percent": 42.0, "current_percent": 80.0, "preview": "data:x"}


@pytest.mark.asyncio
async def test_imagegen_progress_idle_for_non_swarmui_backend(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    ai_module._imagegen_progress_state.update({"active": True, "percent": 42.0})
    result = await ai_module.imagegen_progress()
    assert result == {"active": False, "percent": 0.0, "current_percent": 0.0, "preview": ""}
