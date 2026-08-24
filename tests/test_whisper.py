"""Tests for the optional Whisper (whisper.cpp server) integration in
app/ai.py — whisper_status() and transcribe_audio(), used by
POST /api/ai/attachments/upload (app/routers/ai.py) to transcribe an audio
chat attachment into text at upload time. The actual whisper.cpp server is
mocked out here; these only exercise this app's own request/response
handling and its "never let a failure block the upload" fallback behavior.
"""
import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None, stream_response=None, stream_exc=None):
        self._response = response
        self._exc = exc
        self._stream_response = stream_response
        self._stream_exc = stream_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        if self._exc:
            raise self._exc
        return self._response

    async def post(self, url, **kw):
        if self._exc:
            raise self._exc
        return self._response

    def stream(self, method, url, **kw):
        if self._stream_exc:
            raise self._stream_exc
        return _FakeStreamCtx(self._stream_response, url)


class _FakeStreamCtx:
    def __init__(self, response, requested_url):
        self._response = response
        self.requested_url = requested_url

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *a):
        return False


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None, fail_partway=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._fail_partway = fail_partway

    async def aiter_bytes(self, chunk_size=None):
        for i, chunk in enumerate(self._chunks):
            if self._fail_partway is not None and i == self._fail_partway:
                raise ConnectionError("connection reset mid-download")
            yield chunk


def _patch_httpx(monkeypatch, response=None, exc=None, stream_response=None, stream_exc=None):
    captured = {}

    class _Module:
        @staticmethod
        def AsyncClient(**kw):
            captured["client_kwargs"] = kw
            return _FakeAsyncClient(response=response, exc=exc,
                                     stream_response=stream_response, stream_exc=stream_exc)
    monkeypatch.setattr(ai_module, "_httpx", _Module)
    return captured


@pytest.fixture(autouse=True)
def _reset_whisper_override():
    ai_module.set_whisper_override("")
    yield
    ai_module.set_whisper_override("")


@pytest.mark.asyncio
async def test_whisper_status_not_configured():
    assert await ai_module.whisper_status() == {"ok": False, "reason": "not configured"}


@pytest.mark.asyncio
async def test_whisper_status_ok(monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"status": "ok"}))
    result = await ai_module.whisper_status()
    assert result == {"ok": True, "url": "http://127.0.0.1:8090"}


@pytest.mark.asyncio
async def test_whisper_status_loading(monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(503, {"status": "loading model"}))
    result = await ai_module.whisper_status()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_whisper_status_unreachable(monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, exc=ConnectionError("refused"))
    result = await ai_module.whisper_status()
    assert result["ok"] is False
    assert "refused" in result["reason"]


@pytest.mark.asyncio
async def test_transcribe_audio_not_configured(tmp_path):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"RIFF....WAVEfmt ")
    assert await ai_module.transcribe_audio(f) == ""


@pytest.mark.asyncio
async def test_transcribe_audio_success(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "  the secret door is behind the waterfall  "}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    result = await ai_module.transcribe_audio(f)
    assert result == "the secret door is behind the waterfall"


@pytest.mark.asyncio
async def test_transcribe_audio_http_error_returns_empty(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(500, text="internal error"))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    assert await ai_module.transcribe_audio(f) == ""


@pytest.mark.asyncio
async def test_transcribe_audio_network_error_returns_empty(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, exc=TimeoutError("timed out"))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    assert await ai_module.transcribe_audio(f) == ""


def test_whisper_timeout_defaults_generously():
    """A hardcoded short timeout on the /inference call previously made a
    long session recording silently come back as an empty transcript —
    indistinguishable from "Whisper isn't configured" on the caller's
    side — once actual transcription took longer than the timeout. Default
    must be generous (not the old 120s), and env-overridable."""
    assert ai_module.WHISPER_TIMEOUT_SECONDS >= 3600


@pytest.mark.asyncio
async def test_transcribe_audio_uses_configured_timeout(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    monkeypatch.setattr(ai_module, "WHISPER_TIMEOUT_SECONDS", 42.0)
    captured = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "ok"}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    await ai_module.transcribe_audio(f)
    assert captured["client_kwargs"]["timeout"] == 42.0


# ── debug_info() surfaces Whisper status alongside Ollama's ────────────────

@pytest.mark.asyncio
async def test_debug_info_includes_whisper_not_configured(monkeypatch):
    # Ollama itself unreachable in this test env — debug_info must still
    # report Whisper's status rather than erroring out before getting there.
    result = await ai_module.debug_info()
    assert result["whisper"] == {"ok": False, "reason": "not configured"}


@pytest.mark.asyncio
async def test_debug_info_includes_whisper_reachable(monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"status": "ok"}))
    result = await ai_module.debug_info()
    assert result["whisper"] == {"ok": True, "url": "http://127.0.0.1:8090"}


# ── whisper_model_status() ──────────────────────────────────────────────────

def test_whisper_model_status_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    result = ai_module.whisper_model_status()
    assert result["downloaded"] is False
    assert result["filename"] == "model.gguf"
    assert result["bytes"] == 0
    # "model.gguf" isn't one of the known models, so none of them show as active
    assert all(not m["active"] for m in result["models"])
    assert all(not m["downloaded"] for m in result["models"])


def test_whisper_model_status_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    (tmp_path / "model.gguf").write_bytes(b"x" * 1234)
    result = ai_module.whisper_model_status()
    assert result["downloaded"] is True
    assert result["filename"] == "model.gguf"
    assert result["bytes"] == 1234


def test_whisper_model_status_lists_known_models(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    (tmp_path / "ggml-base.bin").write_bytes(b"x" * 999)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"x" * 111)
    result = ai_module.whisper_model_status()
    by_name = {m["filename"]: m for m in result["models"]}
    assert by_name["ggml-base.bin"]["downloaded"] is True
    assert by_name["ggml-base.bin"]["bytes"] == 999
    assert by_name["ggml-base.bin"]["active"] is True
    assert by_name["ggml-tiny.bin"]["downloaded"] is True
    assert by_name["ggml-tiny.bin"]["active"] is False
    assert by_name["ggml-small.bin"]["downloaded"] is False
    assert by_name["ggml-small.bin"]["bytes"] == 0


# ── download_whisper_model() ────────────────────────────────────────────────

async def _collect(agen):
    return [item async for item in agen]


@pytest.mark.asyncio
async def test_download_whisper_model_success(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    chunks = [b"a" * 10, b"b" * 10, b"c" * 5]
    stream_resp = _FakeStreamResponse(200, headers={"content-length": "25"}, chunks=chunks)
    captured = _patch_httpx(monkeypatch, stream_response=stream_resp)

    events = await _collect(ai_module.download_whisper_model())

    progress = [e for e in events if "completed" in e]
    assert [e["completed"] for e in progress] == [10, 20, 25]
    assert all(e["total"] == 25 for e in progress)
    assert events[-1] == {"status": "done", "filename": "model.gguf", "bytes": 25}

    dest = tmp_path / "model.gguf"
    assert dest.is_file()
    assert dest.read_bytes() == b"a" * 10 + b"b" * 10 + b"c" * 5
    assert not (tmp_path / "model.gguf.part").exists()
    # follow_redirects=True is required — HF file downloads redirect to a CDN.
    assert captured["client_kwargs"].get("follow_redirects") is True


@pytest.mark.asyncio
async def test_download_whisper_model_uses_default_url_when_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    stream_resp = _FakeStreamResponse(200, headers={}, chunks=[b"x"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    requested = {}
    orig_stream = _FakeAsyncClient.stream
    def _spying_stream(self, method, url, **kw):
        requested["url"] = url
        return orig_stream(self, method, url, **kw)
    monkeypatch.setattr(_FakeAsyncClient, "stream", _spying_stream)

    await _collect(ai_module.download_whisper_model(""))
    assert requested["url"] == ai_module.DEFAULT_WHISPER_MODEL_URL


@pytest.mark.asyncio
async def test_download_whisper_model_uses_custom_url(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    stream_resp = _FakeStreamResponse(200, headers={}, chunks=[b"x"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    requested = {}
    orig_stream = _FakeAsyncClient.stream
    def _spying_stream(self, method, url, **kw):
        requested["url"] = url
        return orig_stream(self, method, url, **kw)
    monkeypatch.setattr(_FakeAsyncClient, "stream", _spying_stream)

    await _collect(ai_module.download_whisper_model("https://example.com/custom.gguf"))
    assert requested["url"] == "https://example.com/custom.gguf"


@pytest.mark.asyncio
async def test_download_whisper_model_by_filename_downloads_to_its_own_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-large-v3-turbo.bin")
    (tmp_path / "ggml-large-v3-turbo.bin").write_bytes(b"already active model")
    stream_resp = _FakeStreamResponse(200, headers={"content-length": "5"}, chunks=[b"tiny!"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    requested = {}
    orig_stream = _FakeAsyncClient.stream
    def _spying_stream(self, method, url, **kw):
        requested["url"] = url
        return orig_stream(self, method, url, **kw)
    monkeypatch.setattr(_FakeAsyncClient, "stream", _spying_stream)

    events = await _collect(ai_module.download_whisper_model(filename="ggml-tiny.bin"))

    assert events[-1] == {"status": "done", "filename": "ggml-tiny.bin", "bytes": 5}
    assert (tmp_path / "ggml-tiny.bin").read_bytes() == b"tiny!"
    # the already-active model is untouched — this is what makes them coexist
    assert (tmp_path / "ggml-large-v3-turbo.bin").read_bytes() == b"already active model"
    assert requested["url"] == "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin"


@pytest.mark.asyncio
async def test_download_whisper_model_unknown_filename_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")

    events = await _collect(ai_module.download_whisper_model(filename="../../etc/passwd"))
    assert events == [{"error": "Unknown model filename: '../../etc/passwd'"}]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_download_whisper_model_filename_ignores_custom_url(tmp_path, monkeypatch):
    """A filename from the known list always fetches from the official
    ggerganov/whisper.cpp host — any `url` passed alongside it is ignored,
    so a known model can't be redirected to an arbitrary/untrusted URL."""
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    stream_resp = _FakeStreamResponse(200, headers={}, chunks=[b"x"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    requested = {}
    orig_stream = _FakeAsyncClient.stream
    def _spying_stream(self, method, url, **kw):
        requested["url"] = url
        return orig_stream(self, method, url, **kw)
    monkeypatch.setattr(_FakeAsyncClient, "stream", _spying_stream)

    await _collect(ai_module.download_whisper_model(url="https://evil.example/swap.bin", filename="ggml-base.bin"))
    assert requested["url"] == "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"


@pytest.mark.asyncio
async def test_download_whisper_model_http_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    stream_resp = _FakeStreamResponse(404)
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    events = await _collect(ai_module.download_whisper_model())
    assert events == [{"error": "HTTP 404 fetching model file"}]
    assert not (tmp_path / "model.gguf").exists()
    assert not (tmp_path / "model.gguf.part").exists()


@pytest.mark.asyncio
async def test_download_whisper_model_mid_stream_failure_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    chunks = [b"a" * 10, b"b" * 10, b"c" * 10]
    stream_resp = _FakeStreamResponse(200, headers={"content-length": "30"}, chunks=chunks, fail_partway=1)
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    events = await _collect(ai_module.download_whisper_model())
    assert events[-1]["error"].startswith("ConnectionError")
    assert not (tmp_path / "model.gguf").exists()
    assert not (tmp_path / "model.gguf.part").exists()


@pytest.mark.asyncio
async def test_download_whisper_model_connect_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    _patch_httpx(monkeypatch, stream_exc=ConnectionError("no route to host"))

    events = await _collect(ai_module.download_whisper_model())
    assert len(events) == 1
    assert "no route to host" in events[0]["error"]
    assert not (tmp_path / "model.gguf").exists()


# ── Routes: GET /api/ai/whisper/model-status, POST /api/ai/whisper/pull ────
# Neither route has an explicit _is_player_safe entry, matching the sibling
# model-management routes (/api/ai/pull, /api/ai/models/add, ...) in this
# same router — the auth_gate middleware already denies any non-GM POST/GET
# under /api/ai/ that isn't explicitly allow-listed, so no handler-level
# check is needed either (same as those siblings).

def test_whisper_model_status_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/whisper/model-status").status_code == 403


def test_whisper_pull_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/ai/whisper/pull", json={})
    assert r.status_code == 403


def test_whisper_model_status_route_returns_status(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    (tmp_path / "model.gguf").write_bytes(b"x" * 42)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/whisper/model-status")
    assert r.status_code == 200
    body = r.json()
    assert body["downloaded"] is True
    assert body["filename"] == "model.gguf"
    assert body["bytes"] == 42
    assert isinstance(body["models"], list) and len(body["models"]) == len(ai_module.WHISPER_KNOWN_MODELS)


def test_whisper_pull_route_streams_progress(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    stream_resp = _FakeStreamResponse(200, headers={"content-length": "10"}, chunks=[b"0123456789"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/pull", json={})
    assert r.status_code == 200
    assert '"completed": 10' in r.text or '"completed":10' in r.text
    assert '"status": "done"' in r.text or '"status":"done"' in r.text
    assert "[DONE]" in r.text
    assert (tmp_path / "model.gguf").read_bytes() == b"0123456789"


def test_whisper_pull_route_passes_through_filename(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-large-v3-turbo.bin")
    stream_resp = _FakeStreamResponse(200, headers={"content-length": "4"}, chunks=[b"tiny"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/pull", json={"filename": "ggml-tiny.bin"})
    assert r.status_code == 200
    assert '"filename": "ggml-tiny.bin"' in r.text or '"filename":"ggml-tiny.bin"' in r.text
    assert (tmp_path / "ggml-tiny.bin").read_bytes() == b"tiny"


def test_whisper_pull_route_rejects_unknown_filename(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/pull", json={"filename": "../../etc/passwd"})
    assert r.status_code == 200  # SSE stream itself always 200s; the error is in the stream
    assert "Unknown model filename" in r.text


def test_whisper_pull_route_passes_through_custom_url(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "model.gguf")
    stream_resp = _FakeStreamResponse(200, headers={}, chunks=[b"x"])
    _patch_httpx(monkeypatch, stream_response=stream_resp)

    requested = {}
    orig_stream = _FakeAsyncClient.stream
    def _spying_stream(self, method, url, **kw):
        requested["url"] = url
        return orig_stream(self, method, url, **kw)
    monkeypatch.setattr(_FakeAsyncClient, "stream", _spying_stream)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/pull", json={"url": "https://example.com/custom.gguf"})
    assert r.status_code == 200
    assert requested["url"] == "https://example.com/custom.gguf"
