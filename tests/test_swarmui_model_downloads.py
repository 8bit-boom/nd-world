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


class _FakePostResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


# Default fake replies for the three swarmui_refresh_after_local_change()
# calls, used whenever a test doesn't care about the refresh outcome but the
# code path still reaches it (type=="swarmui").
_DEFAULT_POST_MAP = {
    "/API/GetNewSession": {"session_id": "sess1"},
    "/API/ListServerSettings": {"settings": {"paths.sdmodelfolder": {"value": "Models/Stable-Diffusion"}}},
    "/API/ChangeServerSettings": {"success": True},
}


class _FakeAsyncClient:
    def __init__(self, stream_response=None, stream_exc=None, post_map=None, post_calls=None):
        self._stream_response = stream_response
        self._stream_exc = stream_exc
        self._post_map = post_map if post_map is not None else _DEFAULT_POST_MAP
        self._post_calls = post_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        if self._stream_exc:
            raise self._stream_exc
        return _FakeStreamCtx(self._stream_response)

    async def post(self, url, json=None, **kw):
        if self._post_calls is not None:
            self._post_calls.append((url, json))
        for suffix, data in self._post_map.items():
            if url.endswith(suffix):
                return _FakePostResponse(data)
        return _FakePostResponse({})


def _patch_httpx(monkeypatch, stream_response=None, stream_exc=None, post_map=None, post_calls=None):
    class _Module:
        @staticmethod
        def AsyncClient(**kw):
            return _FakeAsyncClient(stream_response=stream_response, stream_exc=stream_exc,
                                     post_map=post_map, post_calls=post_calls)
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
    assert events[-1] == {"status": "done", "subfolder": "", "filename": "model.safetensors", "bytes": 25,
                           "model_list_refreshed": False}
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
    assert events[-1] == {"status": "done", "subfolder": "VAE", "filename": "vae.safetensors", "bytes": 9,
                           "model_list_refreshed": False}
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


# ── SwarmUI model-list refresh after a direct disk write ────────────────────
# SwarmUI caches its model list in memory and only rebuilds it at startup, or
# as a side effect of /API/ChangeServerSettings touching a paths.* key — see
# _swarmui_refresh_models's docstring in app/ai.py. A file nd-world writes
# straight into the shared volume (bypassing SwarmUI's own download API)
# would otherwise never show up in /API/ListModels until SwarmUI restarts;
# these tests cover the best-effort auto-refresh that closes that gap.

@pytest.mark.asyncio
async def test_download_triggers_refresh_when_backend_is_swarmui(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    post_calls = []
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"x"]),
                 post_calls=post_calls)

    events = await _collect(ai_module.download_swarmui_model("https://example.com/model.safetensors"))

    assert events[-1]["model_list_refreshed"] is True
    change_calls = [c for c in post_calls if c[0].endswith("/API/ChangeServerSettings")]
    assert len(change_calls) == 1
    assert change_calls[0][1]["rawData"]["settings"]["paths.sdmodelfolder"] == "Models/Stable-Diffusion"


@pytest.mark.asyncio
async def test_download_skips_refresh_when_backend_is_not_swarmui(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    post_calls = []
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"x"]),
                 post_calls=post_calls)

    events = await _collect(ai_module.download_swarmui_model("https://example.com/model.safetensors"))

    assert events[-1]["model_list_refreshed"] is False
    assert post_calls == []


@pytest.mark.asyncio
async def test_download_refresh_failure_is_swallowed_and_download_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    # No "settings" key in the ListServerSettings reply — as if the session
    # lacks edit_server_settings permission — should be caught, not raised.
    _patch_httpx(monkeypatch, stream_response=_FakeStreamResponse(200, headers={}, chunks=[b"x"]),
                 post_map={"/API/GetNewSession": {"session_id": "sess1"},
                           "/API/ListServerSettings": {"error": "no permission"}})

    events = await _collect(ai_module.download_swarmui_model("https://example.com/model.safetensors"))

    assert events[-1]["status"] == "done"
    assert events[-1]["model_list_refreshed"] is False
    assert (tmp_path / "model.safetensors").is_file()


@pytest.mark.asyncio
async def test_swarmui_refresh_after_local_change_resends_unchanged_path_setting(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    post_calls = []
    _patch_httpx(monkeypatch, post_calls=post_calls)

    assert await ai_module.swarmui_refresh_after_local_change() is True
    change_calls = [c for c in post_calls if c[0].endswith("/API/ChangeServerSettings")]
    assert change_calls[0][1]["rawData"]["settings"]["paths.sdmodelfolder"] == "Models/Stable-Diffusion"


@pytest.mark.asyncio
async def test_swarmui_refresh_after_local_change_false_when_not_configured(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "")
    post_calls = []
    _patch_httpx(monkeypatch, post_calls=post_calls)

    assert await ai_module.swarmui_refresh_after_local_change() is False
    assert post_calls == []


@pytest.mark.asyncio
async def test_swarmui_refresh_after_local_change_false_when_change_settings_rejected(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/ListServerSettings": {"settings": {"paths.sdmodelfolder": {"value": "Models/Stable-Diffusion"}}},
        "/API/ChangeServerSettings": {"error": "not authorized"},
    })

    assert await ai_module.swarmui_refresh_after_local_change() is False


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


def test_delete_route_reports_refresh_true_when_swarmui_configured(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch)
    (tmp_path / "x.safetensors").write_bytes(b"x")

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.delete("/api/ai/imagegen/models/downloaded", params={"filename": "x.safetensors"})
    assert r.status_code == 200
    assert r.json()["model_list_refreshed"] is True


def test_delete_route_reports_refresh_false_when_not_swarmui(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "SWARMUI_MODELS_DIR", tmp_path)
    (tmp_path / "x.safetensors").write_bytes(b"x")

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.delete("/api/ai/imagegen/models/downloaded", params={"filename": "x.safetensors"})
    assert r.status_code == 200
    assert r.json()["model_list_refreshed"] is False


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


# ── swarmui_restart() / POST /imagegen/restart & /imagegen/update ──────────
# A GM's recovery button for when swarmui_refresh_after_local_change's own
# best-effort rescan trick doesn't pan out (see that function's docstring:
# "SwarmUI has no dedicated 'rescan now' route") — calls SwarmUI's own Admin
# API (/API/UpdateAndRestart) instead of nd-world needing Docker control
# over a sibling container to achieve the same thing. Real parameter names
# are doUpdateServer/aggressive/force (verified against SwarmUI's own
# AdminAPI.cs) — an earlier version of this used made-up
# updateExtensions/updateBackends keys that silently did nothing.

@pytest.mark.asyncio
async def test_swarmui_restart_plain_forces_restart_without_updating_server(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    post_calls = []
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/UpdateAndRestart": {"success": True, "result": "Restarting..."},
    }, post_calls=post_calls)

    result = await ai_module.swarmui_restart()
    assert result == {"ok": True, "result": "Restarting..."}
    restart_calls = [c for c in post_calls if c[0].endswith("/API/UpdateAndRestart")]
    assert len(restart_calls) == 1
    body = restart_calls[0][1]
    assert body["session_id"] == "sess1"
    assert body["doUpdateServer"] is False
    assert body["aggressive"] is False
    # force=True is required for a plain restart — without it, a restart
    # request with nothing new to pull just reports "No changes found"
    # instead of actually restarting (see swarmui_restart's own docstring).
    assert body["force"] is True


@pytest.mark.asyncio
async def test_swarmui_restart_update_server_sets_doUpdateServer_and_leaves_force_off(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    post_calls = []
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/UpdateAndRestart": {"success": True, "result": "Update successful. Restarting..."},
    }, post_calls=post_calls)

    result = await ai_module.swarmui_restart(update_server=True)
    assert result == {"ok": True, "result": "Update successful. Restarting..."}
    body = [c for c in post_calls if c[0].endswith("/API/UpdateAndRestart")][0][1]
    assert body["doUpdateServer"] is True
    assert body["force"] is False


@pytest.mark.asyncio
async def test_swarmui_restart_not_configured(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "")
    post_calls = []
    _patch_httpx(monkeypatch, post_calls=post_calls)

    assert await ai_module.swarmui_restart() == {"ok": False}
    assert post_calls == []


@pytest.mark.asyncio
async def test_swarmui_restart_false_when_backend_is_not_swarmui(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "comfyui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-comfy")
    post_calls = []
    _patch_httpx(monkeypatch, post_calls=post_calls)

    assert await ai_module.swarmui_restart() == {"ok": False}
    assert post_calls == []


@pytest.mark.asyncio
async def test_swarmui_restart_reports_failure_result(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/UpdateAndRestart": {"success": False, "result": "No changes found."},
    })

    assert await ai_module.swarmui_restart() == {"ok": False, "result": "No changes found."}


def test_restart_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.post("/api/ai/imagegen/restart").status_code == 403


def test_restart_route_returns_ok_true_on_success(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/UpdateAndRestart": {"success": True, "result": "Restarting..."},
    })

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/restart")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "result": "Restarting..."}


def test_restart_route_returns_ok_false_when_not_configured(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "")
    _patch_httpx(monkeypatch)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/restart")
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_update_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.post("/api/ai/imagegen/update").status_code == 403


def test_update_route_sets_doUpdateServer_true(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    post_calls = []
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/UpdateAndRestart": {"success": True, "result": "Update successful. Restarting..."},
    }, post_calls=post_calls)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/update")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "result": "Update successful. Restarting..."}
    body = [c for c in post_calls if c[0].endswith("/API/UpdateAndRestart")][0][1]
    assert body["doUpdateServer"] is True


# ── swarmui_check_for_updates() / GET /imagegen/updates ─────────────────────

@pytest.mark.asyncio
async def test_check_for_updates_returns_swarmui_shape_verbatim(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    payload = {
        "server": {"count": 3, "preview": ["a", "b", "c"]},
        "extensions": {"MyExt": {"count": 1, "preview": ["x"]}},
        "backends": {},
    }
    post_calls = []
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/CheckForUpdates": payload,
    }, post_calls=post_calls)

    assert await ai_module.swarmui_check_for_updates() == payload
    check_calls = [c for c in post_calls if c[0].endswith("/API/CheckForUpdates")]
    assert check_calls[0][1] == {"session_id": "sess1"}


@pytest.mark.asyncio
async def test_check_for_updates_empty_when_not_configured(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "")
    post_calls = []
    _patch_httpx(monkeypatch, post_calls=post_calls)

    assert await ai_module.swarmui_check_for_updates() == {}
    assert post_calls == []


@pytest.mark.asyncio
async def test_check_for_updates_empty_when_response_malformed(monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/CheckForUpdates": {"error": "not authorized"},
    })

    assert await ai_module.swarmui_check_for_updates() == {}


def test_updates_route_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/imagegen/updates").status_code == 403


def test_updates_route_returns_check_result(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "_get_type", lambda: "swarmui")
    monkeypatch.setattr(ai_module, "_get_url", lambda: "http://fake-swarmui")
    payload = {"server": {"count": 0, "preview": []}, "extensions": {}, "backends": {}}
    _patch_httpx(monkeypatch, post_map={
        "/API/GetNewSession": {"session_id": "sess1"},
        "/API/CheckForUpdates": payload,
    })

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/imagegen/updates")
    assert r.status_code == 200
    assert r.json() == {"updates": payload}


# ── Shipped JS/template wiring (source assertion) ───────────────────────────

def test_image_gen_tab_ships_the_restart_and_update_buttons(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/ai").text
    assert 'onclick="igRestartSwarmUI()"' in page
    assert 'id="ig-restart-swarmui-status"' in page
    assert 'onclick="igCheckSwarmUIUpdates()"' in page
    assert 'onclick="igUpdateAndRestartSwarmUI()"' in page
    assert 'id="ig-check-updates-status"' in page


def test_ai_chat_image_js_defines_restart_and_confirms_first():
    js = open("static/js/ai-chat-image.js").read()
    assert "async function igRestartSwarmUI()" in js
    assert "/api/ai/imagegen/restart" in js
    body = js.split("async function igRestartSwarmUI()", 1)[1][:600]
    assert "confirm(" in body


def test_ai_chat_image_js_defines_check_and_update_flow():
    js = open("static/js/ai-chat-image.js").read()
    assert "async function igCheckSwarmUIUpdates()" in js
    assert "/api/ai/imagegen/updates" in js
    assert "async function igUpdateAndRestartSwarmUI()" in js
    assert "/api/ai/imagegen/update" in js
    body = js.split("async function igUpdateAndRestartSwarmUI()", 1)[1][:600]
    assert "confirm(" in body
