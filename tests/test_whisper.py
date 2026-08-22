"""Tests for the optional Whisper (whisper.cpp server) integration in
app/ai.py — whisper_status() and transcribe_audio(), used by
POST /api/ai/attachments/upload (app/routers/ai.py) to transcribe an audio
chat attachment into text at upload time. The actual whisper.cpp server is
mocked out here; these only exercise this app's own request/response
handling and its "never let a failure block the upload" fallback behavior.
"""
import pytest

from app import ai as ai_module


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

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


def _patch_httpx(monkeypatch, response=None, exc=None):
    class _Module:
        @staticmethod
        def AsyncClient(timeout=None):
            return _FakeAsyncClient(response=response, exc=exc)
    monkeypatch.setattr(ai_module, "_httpx", _Module)


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
