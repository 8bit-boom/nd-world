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
    def __init__(self, response=None, exc=None, stream_response=None, stream_exc=None, captured=None):
        self._response = response
        self._exc = exc
        self._stream_response = stream_response
        self._stream_exc = stream_exc
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        if self._exc:
            raise self._exc
        return self._response

    async def post(self, url, **kw):
        if self._captured is not None:
            self._captured["post_url"] = url
            self._captured["post_kwargs"] = kw
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
                                     stream_response=stream_response, stream_exc=stream_exc,
                                     captured=captured)
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
    with pytest.raises(ai_module.WhisperError):
        await ai_module.transcribe_audio(f)


@pytest.mark.asyncio
async def test_transcribe_audio_success(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "  the secret door is behind the waterfall  "}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    result = await ai_module.transcribe_audio(f)
    assert result == "the secret door is behind the waterfall"


@pytest.mark.asyncio
async def test_transcribe_audio_defaults_language_to_auto_not_omitted(tmp_path, monkeypatch):
    """whisper.cpp's own server hardcodes language="en" as its default and
    only overrides it when the client sends this field explicitly — so
    omitting it entirely silently forces English decoding on every clip,
    garbling (and often triggering repeating-phrase loops in) non-English
    audio. transcribe_audio must always send an explicit "language" field,
    defaulting to "auto" rather than leaving whisper.cpp's own "en" default
    in effect."""
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    captured = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "ok"}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    await ai_module.transcribe_audio(f)
    assert captured["post_kwargs"]["data"]["language"] == "auto"


@pytest.mark.asyncio
async def test_transcribe_audio_sends_pinned_language(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    captured = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "ok"}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    await ai_module.transcribe_audio(f, language="ru")
    assert captured["post_kwargs"]["data"]["language"] == "ru"


@pytest.mark.asyncio
async def test_transcribe_audio_blank_language_still_defaults_to_auto(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    captured = _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": "ok"}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    await ai_module.transcribe_audio(f, language="")
    assert captured["post_kwargs"]["data"]["language"] == "auto"


@pytest.mark.asyncio
async def test_transcribe_audio_http_error_raises_with_detail(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(500, text="internal error"))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    with pytest.raises(ai_module.WhisperError, match="500"):
        await ai_module.transcribe_audio(f)


@pytest.mark.asyncio
async def test_transcribe_audio_network_error_raises_with_detail(tmp_path, monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, exc=TimeoutError("timed out"))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    with pytest.raises(ai_module.WhisperError, match="timed out"):
        await ai_module.transcribe_audio(f)


@pytest.mark.asyncio
async def test_transcribe_audio_silent_clip_is_not_an_error(tmp_path, monkeypatch):
    """A successful transcription of a genuinely silent clip returns "" —
    distinct from a WhisperError, which is only for the request itself
    failing (network, non-200, unreadable response)."""
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"text": ""}))
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


# ── active_whisper_model() / set_active_whisper_model() ────────────────────
# The marker file (WHISPER_MODELS_DIR/active-model.txt) a GM's "★ Make
# active" click writes — read by both nd-world (active_whisper_model, so
# in-app status/downloads follow the GM's choice) and the "whisper" Compose
# service's own entrypoint (so it survives a container restart) — see
# docker-compose.yml.

def test_active_whisper_model_defaults_to_env_when_no_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    assert ai_module.active_whisper_model() == "ggml-base.bin"


def test_active_whisper_model_reads_marker_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    (tmp_path / "active-model.txt").write_text("ggml-tiny.bin")
    assert ai_module.active_whisper_model() == "ggml-tiny.bin"


def test_active_whisper_model_ignores_marker_with_unknown_filename(tmp_path, monkeypatch):
    """A marker naming something outside WHISPER_KNOWN_MODELS (corrupted,
    hand-edited, or a path-traversal attempt) is never trusted — falls back
    to the env default instead of resolving to an arbitrary path."""
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    (tmp_path / "active-model.txt").write_text("../../etc/passwd")
    assert ai_module.active_whisper_model() == "ggml-base.bin"


def test_active_whisper_model_ignores_blank_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    (tmp_path / "active-model.txt").write_text("   \n")
    assert ai_module.active_whisper_model() == "ggml-base.bin"


def test_active_whisper_model_never_raises_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    assert ai_module.active_whisper_model() == "ggml-base.bin"


def test_set_active_whisper_model_writes_marker_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"x")
    ai_module.set_active_whisper_model("ggml-tiny.bin")
    assert (tmp_path / "active-model.txt").read_text() == "ggml-tiny.bin"
    assert not (tmp_path / "active-model.txt.part").exists()
    assert ai_module.active_whisper_model() == "ggml-tiny.bin"


def test_set_active_whisper_model_rejects_unknown_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    with pytest.raises(ValueError, match="Unknown model filename"):
        ai_module.set_active_whisper_model("../../etc/passwd")


def test_set_active_whisper_model_rejects_model_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    with pytest.raises(ValueError, match="hasn't been downloaded"):
        ai_module.set_active_whisper_model("ggml-tiny.bin")


def test_whisper_model_status_active_source_is_env_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    assert ai_module.whisper_model_status()["active_source"] == "env"


def test_whisper_model_status_active_source_is_marker_once_set(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "WHISPER_MODEL_FILENAME", "ggml-base.bin")
    (tmp_path / "ggml-tiny.bin").write_bytes(b"x")
    ai_module.set_active_whisper_model("ggml-tiny.bin")
    status = ai_module.whisper_model_status()
    assert status["active_source"] == "marker"
    assert status["filename"] == "ggml-tiny.bin"
    by_name = {m["filename"]: m for m in status["models"]}
    assert by_name["ggml-tiny.bin"]["active"] is True
    assert by_name["ggml-base.bin"]["active"] is False


# ── _looks_like_ggml() ───────────────────────────────────────────────────────
# Guards load_whisper_model against the one failure mode this app has
# already hit in production: a GGUF-format file (a different, incompatible
# format) reaching whisper.cpp's /load, whose exit(1)-on-bad-file crashes
# the whole server.

def test_looks_like_ggml_accepts_large_non_gguf_file(tmp_path):
    f = tmp_path / "model.bin"
    f.write_bytes(b"\x00" * 2_000_000)
    assert ai_module._looks_like_ggml(f) is True


def test_looks_like_ggml_rejects_gguf_magic(tmp_path):
    f = tmp_path / "model.bin"
    f.write_bytes(b"GGUF" + b"\x00" * 2_000_000)
    assert ai_module._looks_like_ggml(f) is False


def test_looks_like_ggml_rejects_too_small_a_file(tmp_path):
    """Every real model in WHISPER_KNOWN_MODELS is at least 75 MiB — a tiny
    file is almost certainly an error page or truncated download, not a
    real model, even if it happens not to start with "GGUF"."""
    f = tmp_path / "model.bin"
    f.write_bytes(b"not a real model")
    assert ai_module._looks_like_ggml(f) is False


def test_looks_like_ggml_false_for_missing_file(tmp_path):
    assert ai_module._looks_like_ggml(tmp_path / "nope.bin") is False


# ── load_whisper_model() ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_whisper_model_posts_container_side_path(monkeypatch):
    """The path sent to /load must be resolved on whisper.cpp's OWN side
    (WHISPER_SERVER_MODELS_DIR), not nd-world's WHISPER_MODELS_DIR — the
    two only coincide by default; an externally-hosted whisper instance
    needs them to differ."""
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    monkeypatch.setattr(ai_module, "WHISPER_SERVER_MODELS_DIR", "/models")
    captured = _patch_httpx(monkeypatch, response=_FakeResponse(200, {}))
    result = await ai_module.load_whisper_model("ggml-tiny.bin")
    assert result["ok"] is True
    assert captured["post_kwargs"]["files"]["model"] == (None, "/models/ggml-tiny.bin")


@pytest.mark.asyncio
async def test_load_whisper_model_not_configured_returns_not_ok():
    result = await ai_module.load_whisper_model("ggml-tiny.bin")
    assert result["ok"] is False
    assert "isn't configured" in result["detail"].lower()


@pytest.mark.asyncio
async def test_load_whisper_model_http_400_reports_health_stuck(monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(400, text="model init failed"))
    result = await ai_module.load_whisper_model("ggml-tiny.bin")
    assert result["ok"] is False
    assert "restart" in result["detail"].lower()


@pytest.mark.asyncio
async def test_load_whisper_model_unreachable_returns_not_ok(monkeypatch):
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, exc=ConnectionError("refused"))
    result = await ai_module.load_whisper_model("ggml-tiny.bin")
    assert result["ok"] is False
    assert "refused" in result["detail"]


# ── POST /api/ai/whisper/activate ───────────────────────────────────────────

def test_whisper_activate_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "ggml-tiny.bin"})
    assert r.status_code == 403


def test_whisper_activate_route_rejects_unknown_filename(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "../../etc/passwd"})
    assert r.status_code == 400


def test_whisper_activate_route_rejects_model_not_downloaded(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "ggml-tiny.bin"})
    assert r.status_code == 400
    assert "hasn't been downloaded" in r.json()["detail"]


def test_whisper_activate_route_writes_marker_and_hot_swaps(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"\x00" * 2_000_000)  # passes _looks_like_ggml's size floor
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {}))

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "ggml-tiny.bin"})
    assert r.status_code == 200
    body = r.json()
    assert body["hot_swapped"] is True
    assert body["restart_required"] is False
    assert ai_module.active_whisper_model() == "ggml-tiny.bin"


def test_whisper_activate_route_writes_marker_when_whisper_unreachable(client, seed, tmp_path, monkeypatch):
    """Whisper being unreachable must not lose the GM's choice — the marker
    write happens first and always, so the model still takes effect on the
    next restart even though the immediate hot-swap couldn't happen."""
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"\x00" * 2_000_000)
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    _patch_httpx(monkeypatch, exc=ConnectionError("refused"))

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "ggml-tiny.bin"})
    assert r.status_code == 200
    body = r.json()
    assert body["hot_swapped"] is False
    assert body["restart_required"] is True
    assert ai_module.active_whisper_model() == "ggml-tiny.bin"


def test_whisper_activate_route_hot_swap_false_skips_load(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"\x00" * 2_000_000)
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    called = {"load": False}

    async def _fake_load(filename):
        called["load"] = True
        return {"ok": True, "detail": "should not be called"}
    monkeypatch.setattr(ai_module, "load_whisper_model", _fake_load)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "ggml-tiny.bin", "hot_swap": False})
    assert r.status_code == 200
    body = r.json()
    assert body["hot_swapped"] is False
    assert body["restart_required"] is True
    assert called["load"] is False
    assert ai_module.active_whisper_model() == "ggml-tiny.bin"


def test_whisper_activate_route_refuses_hot_swap_for_bad_format_but_still_saves(client, seed, tmp_path, monkeypatch):
    """A file that fails the format sanity check is never sent to /load
    (that's the whole point of the check — loading it would crash the
    whisper server) but the GM's choice is still saved for next restart."""
    monkeypatch.setattr(ai_module, "WHISPER_MODELS_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"GGUF" + b"\x00" * 2_000_000)
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    called = {"load": False}

    async def _fake_load(filename):
        called["load"] = True
        return {"ok": True, "detail": "should not be called"}
    monkeypatch.setattr(ai_module, "load_whisper_model", _fake_load)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/whisper/activate", json={"filename": "ggml-tiny.bin"})
    assert r.status_code == 200
    body = r.json()
    assert body["hot_swapped"] is False
    assert body["restart_required"] is True
    assert called["load"] is False
    assert ai_module.active_whisper_model() == "ggml-tiny.bin"
