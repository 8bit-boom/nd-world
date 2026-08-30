"""Tests for two ways to get a model into Ollama besides the existing
ollama.com/library "Pull & Add" box:

- Search Hugging Face for GGUF models (app.ai.search_huggingface_models/
  list_huggingface_gguf_files, GET /api/ai/ollama/hf-search + /hf-files) —
  discovery only; picking a result reuses the EXISTING POST /api/ai/pull
  route with model_id="hf.co/{repo}:{filename}", the exact form Ollama's
  own pull already understands (one of KNOWN_MODELS already uses it).
- Upload a local .gguf file straight into Ollama (app.ai.
  import_local_gguf_model, using ollama.AsyncClient.create_blob/create —
  verified against the installed ollama==0.6.2 package's actual source,
  not guessed) via the same chunked-upload shape every other large-file
  upload on this app uses (see tests/test_ai_attachments_chunked_upload.py
  for the template this mirrors).
"""
import io

import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeGetResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeGetClient:
    def __init__(self, responses):
        self._responses = responses  # list of _FakeGetResponse, or an Exception to raise
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        resp = self._responses.pop(0) if self._responses else _FakeGetResponse(200, [])
        if isinstance(resp, Exception):
            raise resp
        return resp


def _patch_get(monkeypatch, *responses):
    client = _FakeGetClient(list(responses))

    class _Module:
        @staticmethod
        def AsyncClient(**kw):
            return client
    monkeypatch.setattr(ai_module, "_httpx", _Module)
    return client


# ── search_huggingface_models() ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_empty_query_short_circuits():
    assert await ai_module.search_huggingface_models("") == []
    assert await ai_module.search_huggingface_models("   ") == []


@pytest.mark.asyncio
async def test_search_parses_results(monkeypatch):
    data = [
        {"id": "user/model-a-GGUF", "downloads": 1234, "likes": 56},
        {"modelId": "user/model-b-GGUF", "downloads": 0, "likes": 0},
        {"downloads": 5},  # no id/modelId at all — dropped
    ]
    client = _patch_get(monkeypatch, _FakeGetResponse(200, data))
    results = await ai_module.search_huggingface_models("llama")
    assert results == [
        {"id": "user/model-a-GGUF", "downloads": 1234, "likes": 56},
        {"id": "user/model-b-GGUF", "downloads": 0, "likes": 0},
    ]
    url, params = client.calls[0]
    assert url.endswith("/models")
    assert params["search"] == "llama"
    assert "filter" not in params
    assert params["sort"] == "downloads"


@pytest.mark.asyncio
async def test_search_http_error_returns_empty(monkeypatch):
    _patch_get(monkeypatch, _FakeGetResponse(500, None))
    assert await ai_module.search_huggingface_models("llama") == []


@pytest.mark.asyncio
async def test_search_malformed_response_returns_empty(monkeypatch):
    _patch_get(monkeypatch, _FakeGetResponse(200, {"not": "a list"}))
    assert await ai_module.search_huggingface_models("llama") == []


@pytest.mark.asyncio
async def test_search_network_failure_returns_empty(monkeypatch):
    _patch_get(monkeypatch, ConnectionError("no route to host"))
    assert await ai_module.search_huggingface_models("llama") == []


# ── list_huggingface_gguf_files() ────────────────────────────────────────

@pytest.mark.asyncio
async def test_files_rejects_missing_or_invalid_repo_id():
    assert await ai_module.list_huggingface_gguf_files("") == []
    assert await ai_module.list_huggingface_gguf_files("no-slash") == []


@pytest.mark.asyncio
async def test_files_filters_to_gguf_only(monkeypatch):
    data = [
        {"path": "README.md", "size": 100},
        {"path": "model-Q4_K_M.gguf", "size": 4_000_000_000},
        {"path": "model-Q8_0.gguf", "size": 8_000_000_000},
        {"path": "config.json", "size": 50},
    ]
    client = _patch_get(monkeypatch, _FakeGetResponse(200, data))
    files = await ai_module.list_huggingface_gguf_files("user/repo")
    assert files == [
        {"filename": "model-Q4_K_M.gguf", "size_bytes": 4_000_000_000},
        {"filename": "model-Q8_0.gguf", "size_bytes": 8_000_000_000},
    ]
    url, _ = client.calls[0]
    assert url.endswith("/models/user/repo/tree/main")


@pytest.mark.asyncio
async def test_files_http_error_returns_empty(monkeypatch):
    _patch_get(monkeypatch, _FakeGetResponse(404, None))
    assert await ai_module.list_huggingface_gguf_files("user/repo") == []


# ── import_local_gguf_model() ────────────────────────────────────────────

class _FakeOllamaClient:
    def __init__(self, digest="sha256:deadbeef", create_blob_exc=None, create_exc=None):
        self._digest = digest
        self._create_blob_exc = create_blob_exc
        self._create_exc = create_exc
        self.blob_calls = []
        self.create_calls = []

    async def create_blob(self, path):
        self.blob_calls.append(path)
        if self._create_blob_exc:
            raise self._create_blob_exc
        return self._digest

    async def create(self, model, files):
        self.create_calls.append((model, files))
        if self._create_exc:
            raise self._create_exc


async def _collect(agen):
    return [item async for item in agen]


@pytest.mark.asyncio
async def test_import_blank_model_name_yields_error(tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"x")
    out = await _collect(ai_module.import_local_gguf_model(f, "  "))
    assert out == [{"error": "No model name given"}]


@pytest.mark.asyncio
async def test_import_missing_file_yields_error(tmp_path):
    out = await _collect(ai_module.import_local_gguf_model(tmp_path / "nope.gguf", "my-model"))
    assert out == [{"error": "Uploaded file is missing"}]


@pytest.mark.asyncio
async def test_import_success_pushes_blob_then_creates(tmp_path, monkeypatch):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"fake gguf bytes")
    fake = _FakeOllamaClient(digest="sha256:abc123")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    out = await _collect(ai_module.import_local_gguf_model(f, "my-model"))
    assert out[-1] == {"status": "done", "model": "my-model"}
    assert fake.blob_calls == [str(f)]
    assert fake.create_calls == [("my-model", {"m.gguf": "sha256:abc123"})]


@pytest.mark.asyncio
async def test_import_ollama_response_error_surfaces(tmp_path, monkeypatch):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"x")

    class _Exc(ai_module._ollama.ResponseError):
        def __init__(self):
            self.status_code = 500
            self.error = "disk full"

    fake = _FakeOllamaClient(create_blob_exc=_Exc())
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    out = await _collect(ai_module.import_local_gguf_model(f, "my-model"))
    assert out[-1] == {"error": "Ollama 500: disk full"}


@pytest.mark.asyncio
async def test_import_generic_failure_surfaces(tmp_path, monkeypatch):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"x")
    fake = _FakeOllamaClient(create_blob_exc=OSError("no space left on device"))
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    out = await _collect(ai_module.import_local_gguf_model(f, "my-model"))
    assert out[-1] == {"error": "OSError: no space left on device"}


# ── Routes ────────────────────────────────────────────────────────────────

def test_hf_search_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/ollama/hf-search?q=llama").status_code == 403


def test_hf_search_route_returns_results(client, seed, monkeypatch):
    async def fake_search(q, limit=20):
        assert q == "llama"
        return [{"id": "user/model", "downloads": 5, "likes": 1}]
    monkeypatch.setattr(ai_module, "search_huggingface_models", fake_search)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/ollama/hf-search?q=llama")
    assert r.status_code == 200
    assert r.json() == {"results": [{"id": "user/model", "downloads": 5, "likes": 1}]}


def test_hf_files_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/ollama/hf-files?repo=user/repo").status_code == 403


def test_hf_files_route_returns_files(client, seed, monkeypatch):
    async def fake_files(repo):
        assert repo == "user/repo"
        return [{"filename": "m-Q4_K_M.gguf", "size_bytes": 123}]
    monkeypatch.setattr(ai_module, "list_huggingface_gguf_files", fake_files)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/ollama/hf-files?repo=user/repo")
    assert r.status_code == 200
    assert r.json() == {"files": [{"filename": "m-Q4_K_M.gguf", "size_bytes": 123}]}


# ── Upload routes ─────────────────────────────────────────────────────────

def _fake_import(monkeypatch, result):
    async def _gen(path, model_name):
        yield result
    monkeypatch.setattr(ai_module, "import_local_gguf_model", _gen)


def test_upload_direct_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/ai/ollama/upload/direct", data={"model_name": "x"},
                     files={"file": ("m.gguf", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 403


def test_upload_direct_rejects_non_gguf(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/ollama/upload/direct", data={"model_name": "x"},
                     files={"file": ("m.bin", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 400


def test_upload_direct_success(client, seed, monkeypatch):
    _fake_import(monkeypatch, {"status": "done", "model": "my-model"})
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/ollama/upload/direct", data={"model_name": "my-model"},
                     files={"file": ("m.gguf", io.BytesIO(b"fake gguf"), "application/octet-stream")})
    assert r.status_code == 200
    assert r.json() == {"status": "done", "model": "my-model"}


def test_upload_chunk_and_complete_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    upload_id = "a" * 32
    r = client.post("/api/ai/ollama/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                     files={"file": ("part", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 403
    r2 = client.post("/api/ai/ollama/upload/complete",
                      data={"upload_id": upload_id, "filename": "m.gguf", "total_chunks": "1", "model_name": "x"})
    assert r2.status_code == 403


def test_upload_chunk_and_complete_success(client, seed, monkeypatch):
    _fake_import(monkeypatch, {"status": "done", "model": "my-model"})
    login(client, seed.gm.email, GM_PASSWORD)
    upload_id = "b" * 32
    part_a = b"\xaa" * 5000
    part_b = b"\xbb" * 5000
    r0 = client.post("/api/ai/ollama/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(part_a), "application/octet-stream")})
    r1 = client.post("/api/ai/ollama/upload/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files={"file": ("part", io.BytesIO(part_b), "application/octet-stream")})
    assert r0.status_code == 200 and r1.status_code == 200
    r2 = client.post("/api/ai/ollama/upload/complete", data={
        "upload_id": upload_id, "filename": "m.gguf", "total_chunks": "2", "model_name": "my-model",
    })
    assert r2.status_code == 200
    assert r2.json() == {"status": "done", "model": "my-model"}


def test_upload_complete_rejects_non_gguf(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/ollama/upload/complete", data={
        "upload_id": "c" * 32, "filename": "m.bin", "total_chunks": "1", "model_name": "x",
    })
    assert r.status_code == 400


def test_upload_complete_missing_chunks_400(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/ollama/upload/complete", data={
        "upload_id": "d" * 32, "filename": "m.gguf", "total_chunks": "1", "model_name": "x",
    })
    assert r.status_code == 400


def test_upload_direct_cleans_up_temp_file_on_success(client, seed, monkeypatch, tmp_path):
    captured = {}

    async def _gen(path, model_name):
        captured["path"] = path
        assert path.is_file()
        yield {"status": "done", "model": model_name}

    monkeypatch.setattr(ai_module, "import_local_gguf_model", _gen)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/ollama/upload/direct", data={"model_name": "my-model"},
                     files={"file": ("m.gguf", io.BytesIO(b"fake gguf"), "application/octet-stream")})
    assert r.status_code == 200
    assert not captured["path"].exists()


# ── JS/template source assertions ────────────────────────────────────────

def test_models_tab_ships_hf_search_and_upload_sections(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    page = client.get("/ai").text
    assert 'id="mp-hf-query"' in page
    assert 'onclick="mpHfSearch()"' in page
    assert 'id="mp-hf-results"' in page
    assert 'id="mp-upload-file"' in page
    assert 'accept=".gguf"' in page
    assert 'onclick="mpUploadModel()"' in page
    assert 'id="mp-upload-name"' in page


def test_js_hf_search_reuses_existing_pull_flow():
    js = open("static/js/ai-chat-models.js").read()
    assert "async function mpHfSearch()" in js
    assert "/api/ai/ollama/hf-search" in js
    assert "async function mpHfShowFiles(repoId, containerEl)" in js
    assert "/api/ai/ollama/hf-files" in js
    # Picking a file reuses the existing pull mechanism — no separate
    # download code path for Hugging Face results.
    assert "mpQuickPull(`hf.co/${repoId}:${f.filename}`)" in js


def test_js_upload_uses_shared_chunked_upload_helper():
    js = open("static/js/ai-chat-models.js").read()
    assert "async function mpUploadModel()" in js
    body = js.split("async function mpUploadModel()", 1)[1][:1200]
    assert "ndChunkedUpload(file" in body
    assert "directUrl: '/api/ai/ollama/upload/direct'" in body
    assert "chunkUrl: '/api/ai/ollama/upload/chunk'" in body
    assert "completeUrl: '/api/ai/ollama/upload/complete'" in body
    assert "model_name: modelName" in body
