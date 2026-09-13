"""Tests for the Image Gen tab's "Krea 2 GGUF Quick Setup" panel:
app.ai.list_huggingface_repo_files_recursive (a recursive HF file listing —
unlike list_huggingface_gguf_files, which only sees the repo root, this one
finds files nested in subfolders like realrebelai/KREA-2_GGUFs' own TURBO/
and BASE/ — see city96/ComfyUI-GGUF#464), the GET
/api/ai/imagegen/models/hf-files route backing it, and the frontend wiring
in ai_chat/_tab_image.html + static/js/ai-chat-image.js that turns a picked
pair of files into two automatic downloads through the existing Download
Models flow (app.ai.download_swarmui_model).
"""
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
        self._responses = responses
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


# ── list_huggingface_repo_files_recursive() ─────────────────────────────────

@pytest.mark.asyncio
async def test_recursive_rejects_missing_or_invalid_repo_id():
    assert await ai_module.list_huggingface_repo_files_recursive("") == []
    assert await ai_module.list_huggingface_repo_files_recursive("no-slash") == []


@pytest.mark.asyncio
async def test_recursive_finds_files_nested_in_subfolders(monkeypatch):
    """The real-world case this exists for: realrebelai/KREA-2_GGUFs puts
    its files under TURBO/ and BASE/, not the repo root — the non-recursive
    list_huggingface_gguf_files would see none of them."""
    data = [
        {"path": "README.md", "type": "file", "size": 100},
        {"path": "TURBO", "type": "directory"},
        {"path": "TURBO/krea2-turbo-Q4_K_M.gguf", "type": "file", "size": 4_000_000_000},
        {"path": "BASE", "type": "directory"},
        {"path": "BASE/krea2-base-Q6_K.gguf", "type": "file", "size": 9_000_000_000},
    ]
    client = _patch_get(monkeypatch, _FakeGetResponse(200, data))
    files = await ai_module.list_huggingface_repo_files_recursive("realrebelai/KREA-2_GGUFs")
    assert files == [
        {"path": "TURBO/krea2-turbo-Q4_K_M.gguf", "size_bytes": 4_000_000_000},
        {"path": "BASE/krea2-base-Q6_K.gguf", "size_bytes": 9_000_000_000},
    ]
    url, params = client.calls[0]
    assert url.endswith("/models/realrebelai/KREA-2_GGUFs/tree/main")
    assert params["recursive"] == "true"


@pytest.mark.asyncio
async def test_recursive_respects_custom_suffix(monkeypatch):
    data = [
        {"path": "a.gguf", "type": "file", "size": 1},
        {"path": "sub/b.safetensors", "type": "file", "size": 2},
    ]
    _patch_get(monkeypatch, _FakeGetResponse(200, data))
    files = await ai_module.list_huggingface_repo_files_recursive("user/repo", suffix=".safetensors")
    assert files == [{"path": "sub/b.safetensors", "size_bytes": 2}]


@pytest.mark.asyncio
async def test_recursive_http_error_returns_empty(monkeypatch):
    _patch_get(monkeypatch, _FakeGetResponse(404, None))
    assert await ai_module.list_huggingface_repo_files_recursive("user/repo") == []


@pytest.mark.asyncio
async def test_recursive_network_failure_returns_empty(monkeypatch):
    _patch_get(monkeypatch, ConnectionError("no route to host"))
    assert await ai_module.list_huggingface_repo_files_recursive("user/repo") == []


@pytest.mark.asyncio
async def test_recursive_malformed_response_returns_empty(monkeypatch):
    _patch_get(monkeypatch, _FakeGetResponse(200, {"not": "a list"}))
    assert await ai_module.list_huggingface_repo_files_recursive("user/repo") == []


# ── GET /api/ai/imagegen/models/hf-files ────────────────────────────────────

def test_hf_files_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/ai/imagegen/models/hf-files?repo=realrebelai/KREA-2_GGUFs")
    assert r.status_code == 403


def test_hf_files_route_returns_files(client, seed, monkeypatch):
    async def fake_recursive(repo_id, suffix=".gguf"):
        assert repo_id == "realrebelai/KREA-2_GGUFs"
        return [{"path": "TURBO/krea2-turbo-Q4_K_M.gguf", "size_bytes": 4_000_000_000}]
    monkeypatch.setattr(ai_module, "list_huggingface_repo_files_recursive", fake_recursive)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/imagegen/models/hf-files?repo=realrebelai/KREA-2_GGUFs")
    assert r.status_code == 200
    assert r.json() == {"files": [{"path": "TURBO/krea2-turbo-Q4_K_M.gguf", "size_bytes": 4_000_000_000}]}


def test_hf_files_route_passes_custom_suffix(client, seed, monkeypatch):
    captured = {}
    async def fake_recursive(repo_id, suffix=".gguf"):
        captured["suffix"] = suffix
        return []
    monkeypatch.setattr(ai_module, "list_huggingface_repo_files_recursive", fake_recursive)

    login(client, seed.gm.email, GM_PASSWORD)
    client.get("/api/ai/imagegen/models/hf-files?repo=user/repo&suffix=.safetensors")
    assert captured["suffix"] == ".safetensors"


# ── UI: the Krea 2 Quick Setup panel and its JS ─────────────────────────────

def test_image_gen_tab_ships_krea2_quick_setup_panel(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "Krea 2 GGUF Quick Setup" in r.text
    assert 'id="k2-check-btn"' in r.text
    assert 'id="k2-unet-select"' in r.text
    assert 'id="k2-clip-select"' in r.text
    assert 'onclick="k2Setup()"' in r.text
    assert "ComfyUI-GGUF_KREA-2" in r.text


def test_toolbar_js_krea2_functions_reference_the_right_repos(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/static/js/ai-chat-image.js")
    assert r.status_code == 200
    js = r.text.replace("\r\n", "\n")
    assert "realrebelai/KREA-2_GGUFs" in js
    assert "unsloth/Qwen3-VL-4B-Instruct-GGUF" in js
    assert "/api/ai/imagegen/models/hf-files" in js
    fn_start = js.index("async function k2Setup")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "k2DownloadOne(K2_UNET_REPO" in fn_body
    assert "'diffusion_models'" in fn_body
    assert "k2DownloadOne(K2_CLIP_REPO" in fn_body
    assert "'clip'" in fn_body


def test_toolbar_js_k2_download_one_reuses_dlm_form(client, seed):
    """The whole point: one download code path (dlmStartDownload), not two —
    the quick-setup panel just fills in its inputs first."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/static/js/ai-chat-image.js")
    js = r.text.replace("\r\n", "\n")
    fn_start = js.index("async function k2DownloadOne")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "dlm-url" in fn_body
    assert "dlm-subfolder" in fn_body
    assert "dlm-filename" in fn_body
    assert "await dlmStartDownload()" in fn_body
