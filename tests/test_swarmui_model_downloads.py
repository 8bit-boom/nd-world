"""Tests for downloading SwarmUI checkpoints/VAEs/text-encoders/etc.
straight into the shared model volume through nd-world's own UI
(app.ai.download_swarmui_model / list_downloaded_swarmui_models /
delete_downloaded_swarmui_model, and the POST/GET/DELETE
/api/ai/imagegen/models* routes in app/routers/ai.py) — the same
shared-Docker-volume mechanic as download_whisper_model, but for
SWARMUI_MODELS_DIR instead of WHISPER_MODELS_DIR, and with a free-text
subfolder (SwarmUI's own model-type folders) since there's no one curated
trusted host for Stable-Diffusion-family models the way ggerganov/whisper.cpp
is for Whisper.
"""
import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

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


class _FakeAsyncClient:
    def __init__(self, stream_response=None, stream_exc=None):
        self._stream_response = stream_response
        self._stream_exc = stream_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        if self._stream_exc:
            raise self._stream_exc
        return _FakeStreamCtx(self._stream_response)


def _patch_httpx(monkeypatch, stream_response=None, stream_exc=None):
    class _Module:
        @staticmethod
        def AsyncClient(**kw):
            return _FakeAsyncClient(stream_response=stream_response, stream_exc=stream_exc)
    monkeypatch.setattr(ai_module, "_httpx", _Module)


async def _collect(agen):
    return [item async for item in agen]


# ── download_swarmui_model() ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_download_success_writes_file_and_yields_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    chunks = [b"a" * 10, b"b" * 10, b"c" * 5]
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={"content-length": "25"}, chunks=chunks))

    events = await _collect(ai_module.download_swarmui_model(
        "https://huggingface.co/x/y/resolve/main/model.safetensors",
    ))

    progress = [e for e in events if "completed" in e]
    assert [e["completed"] for e in progress] == [10, 20, 25]
    assert events[-1] == {"status": "done", "subfolder": "", "filename": "model.safetensors", "bytes": 25}
    dest = tmp_path / "model.safetensors"
    assert dest.is_file()
    assert dest.read_bytes() == b"a" * 10 + b"b" * 10 + b"c" * 5
    assert not (tmp_path / "model.safetensors.part").exists()


@pytest.mark.asyncio
async def test_download_filename_defaults_to_url_basename(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"x"]))

    events = await _collect(ai_module.download_swarmui_model("https://example.com/path/to/checkpoint.ckpt?x=1"))
    assert events[-1]["filename"] == "checkpoint.ckpt"
    assert (tmp_path / "checkpoint.ckpt").is_file()


@pytest.mark.asyncio
async def test_download_explicit_filename_overrides_url_basename(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"x"]))

    events = await _collect(ai_module.download_swarmui_model(
        "https://example.com/model.safetensors", filename="renamed.safetensors",
    ))
    assert events[-1]["filename"] == "renamed.safetensors"
    assert (tmp_path / "renamed.safetensors").is_file()
    assert not (tmp_path / "model.safetensors").exists()


@pytest.mark.asyncio
async def test_download_writes_into_given_subfolder(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"vae bytes"]))

    events = await _collect(ai_module.download_swarmui_model(
        "https://example.com/vae.safetensors", subfolder="VAE",
    ))
    assert events[-1] == {"status": "done", "subfolder": "VAE", "filename": "vae.safetensors", "bytes": 9}
    assert (tmp_path / "VAE" / "vae.safetensors").read_bytes() == b"vae bytes"


@pytest.mark.asyncio
async def test_download_rejects_path_traversal_in_subfolder(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    events = await _collect(ai_module.download_swarmui_model(
        "https://example.com/x.safetensors", subfolder="../../etc",
    ))
    assert events == [{"error": "Invalid subfolder/filename"}]
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.asyncio
async def test_download_rejects_filename_with_embedded_separator(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    events = await _collect(ai_module.download_swarmui_model(
        "https://example.com/x", filename="../../etc/passwd",
    ))
    assert events == [{"error": "Could not determine a safe filename from that URL — provide one explicitly"}]


@pytest.mark.asyncio
async def test_download_blank_url_rejected_without_any_request(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    events = await _collect(ai_module.download_swarmui_model(""))
    assert events == [{"error": "No URL given"}]


@pytest.mark.asyncio
async def test_download_http_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(404))
    events = await _collect(ai_module.download_swarmui_model("https://example.com/model.safetensors"))
    assert events == [{"error": "HTTP 404 fetching model file"}]
    assert not (tmp_path / "model.safetensors").exists()


@pytest.mark.asyncio
async def test_download_mid_stream_failure_cleans_up_part_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    chunks = [b"a" * 10, b"b" * 10, b"c" * 10]
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=chunks, fail_partway=1))
    events = await _collect(ai_module.download_swarmui_model("https://example.com/model.safetensors"))
    assert events[-1]["error"].startswith("ConnectionError")
    assert not (tmp_path / "model.safetensors").exists()
    assert not (tmp_path / "model.safetensors.part").exists()


# ── list_downloaded_swarmui_models() ────────────────────────────────────────

def test_list_downloaded_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path / "does-not-exist")
    assert ai_module.list_downloaded_swarmui_models() == []


def test_list_downloaded_finds_root_and_nested_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    (tmp_path / "checkpoint.safetensors").write_bytes(b"x" * 100)
    (tmp_path / "VAE").mkdir()
    (tmp_path / "VAE" / "vae.safetensors").write_bytes(b"y" * 50)

    models = {(m["subfolder"], m["filename"]): m["bytes"] for m in ai_module.list_downloaded_swarmui_models()}
    assert models[("", "checkpoint.safetensors")] == 100
    assert models[("VAE", "vae.safetensors")] == 50


def test_list_downloaded_excludes_in_progress_part_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    (tmp_path / "model.safetensors.part").write_bytes(b"partial")
    assert ai_module.list_downloaded_swarmui_models() == []


# ── delete_downloaded_swarmui_model() ───────────────────────────────────────

def test_delete_removes_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    (tmp_path / "LoRA").mkdir()
    (tmp_path / "LoRA" / "x.safetensors").write_bytes(b"x")
    assert ai_module.delete_downloaded_swarmui_model("LoRA", "x.safetensors") is True
    assert not (tmp_path / "LoRA" / "x.safetensors").exists()


def test_delete_returns_false_for_unknown_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    assert ai_module.delete_downloaded_swarmui_model("", "nope.safetensors") is False


def test_delete_returns_false_rather_than_raising_on_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    assert ai_module.delete_downloaded_swarmui_model("../../etc", "passwd") is False


# ── Routes: POST /download, GET/DELETE /downloaded ──────────────────────────
# None of these three routes has an explicit _is_player_safe entry, matching
# every sibling imagegen model-management route in this same router — the
# auth_gate middleware already denies any non-GM request under /api/ai/ that
# isn't explicitly allow-listed.

def test_download_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/ai/imagegen/models/download", json={"url": "https://example.com/x.safetensors"})
    assert r.status_code == 403


def test_downloaded_list_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/imagegen/models/downloaded").status_code == 403


def test_delete_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.delete("/api/ai/imagegen/models/downloaded", params={"filename": "x.safetensors"})
    assert r.status_code == 403


def test_download_route_streams_progress(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={"content-length": "10"}, chunks=[b"0123456789"]))

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/models/download", json={"url": "https://example.com/model.safetensors"})
    assert r.status_code == 200
    assert '"completed": 10' in r.text or '"completed":10' in r.text
    assert '"status": "done"' in r.text or '"status":"done"' in r.text
    assert "[DONE]" in r.text
    assert (tmp_path / "model.safetensors").read_bytes() == b"0123456789"


def test_download_route_passes_through_subfolder_and_filename(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"clip bytes"]))

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/models/download", json={
        "url": "https://example.com/x.safetensors", "subfolder": "clip", "filename": "t5xxl.safetensors",
    })
    assert r.status_code == 200
    assert (tmp_path / "clip" / "t5xxl.safetensors").read_bytes() == b"clip bytes"


def test_downloaded_list_route_returns_models_and_suggestions(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    (tmp_path / "checkpoint.safetensors").write_bytes(b"x" * 10)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/imagegen/models/downloaded")
    assert r.status_code == 200
    body = r.json()
    assert {"subfolder": "", "filename": "checkpoint.safetensors", "bytes": 10} in body["models"]
    assert body["folder_suggestions"] == ai_module.SWARMUI_MODEL_FOLDER_SUGGESTIONS


def test_delete_route_round_trip(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    (tmp_path / "VAE").mkdir()
    (tmp_path / "VAE" / "x.safetensors").write_bytes(b"x")

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.delete("/api/ai/imagegen/models/downloaded", params={"subfolder": "VAE", "filename": "x.safetensors"})
    assert r.status_code == 200
    assert not (tmp_path / "VAE" / "x.safetensors").exists()


def test_delete_route_404s_for_unknown_file(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.delete("/api/ai/imagegen/models/downloaded", params={"filename": "nope.safetensors"})
    assert r.status_code == 404


def test_delete_route_requires_filename(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.delete("/api/ai/imagegen/models/downloaded")
    assert r.status_code == 404
