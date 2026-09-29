"""Tests for TTS/STT model discovery + Studio version/update routes.

Studio's audio task taxonomy was never verified live (Phase 0.5 confirmed
only text-generation / text-to-image), so classification tests pin the
TOLERANT behavior: known speech-ish tasks/repo_ids bucket correctly and
unknown tasks surface in tasks_seen instead of being dropped.
"""
import pytest

from app.unsloth_extras import StudioEndpointMissing, StudioMissing, _classify_audio_model

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _patch_key(monkeypatch):
    # the routes gate on _ai.effective_llm_api_key() which reads the module
    # attr captured at import time — patch the attr, not the env (the
    # quest-sync test pattern)
    monkeypatch.setattr("app.ai.UNSLOTH_API_KEY", "sk-test")


def _fake_hub(rows):
    async def fake_hub_cached():
        return rows
    return fake_hub_cached


# ── classification ───────────────────────────────────────────────────────────

def test_classify_tts_by_task():
    assert _classify_audio_model({"task": "text-to-speech", "repo_id": "x"}) == "tts"
    assert _classify_audio_model({"task": "TTS", "repo_id": "y"}) == "tts"


def test_classify_stt_beats_tts_on_specific_hints():
    # "speech-recognition" must bucket STT even though it contains "speech"
    assert _classify_audio_model({"task": "speech-recognition"}) == "stt"
    assert _classify_audio_model({"task": "automatic-speech-recognition"}) == "stt"
    assert _classify_audio_model({"repo_id": "unsloth/whisper-small"}) == "stt"


def test_classify_unknown_is_empty_not_dropped():
    assert _classify_audio_model({"task": "text-generation"}) == ""
    assert _classify_audio_model({"task": "text-to-image"}) == ""
    assert _classify_audio_model({}) == ""


# ── routes ───────────────────────────────────────────────────────────────────

def test_audio_models_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/unsloth/audio-models").status_code == 403


def test_audio_models_route_requires_backend(client, seed):
    # no key in the test env → the same 400 every unsloth route returns
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/unsloth/audio-models").status_code == 400


def test_audio_models_route_buckets_and_defaults(client, seed, monkeypatch):
    _patch_key(monkeypatch)
    monkeypatch.setattr("app.unsloth_extras.hub_cached", _fake_hub([
        {"repo_id": "unsloth/orpheus-3b", "task": "text-to-speech", "size_bytes": 1},
        {"repo_id": "unsloth/whisper-small", "task": "automatic-speech-recognition", "size_bytes": 2},
        {"repo_id": "unsloth/gemma-4-26B-A4B-it-GGUF", "task": "text-generation", "size_bytes": 3},
    ]))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/unsloth/audio-models")
    assert r.status_code == 200
    d = r.json()
    assert [m["repo_id"] for m in d["tts"]] == ["unsloth/orpheus-3b"]
    assert [m["repo_id"] for m in d["stt"]] == ["unsloth/whisper-small"]
    assert d["defaults"]["tts_model"]  # persisted defaults ride along
    # text-generation is NOT an audio task, but its task still surfaces so
    # the Settings UI can show what Studio actually reports
    assert "text-generation" in d["tasks_seen"]


def test_audio_models_route_tolerates_hub_rows_without_task(client, seed, monkeypatch):
    _patch_key(monkeypatch)
    monkeypatch.setattr("app.unsloth_extras.hub_cached", _fake_hub([
        {"repo_id": "mystery/model"},  # no task field at all
    ]))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/unsloth/audio-models")
    assert r.status_code == 200
    d = r.json()
    assert d["tts"] == [] and d["stt"] == []


def test_version_route_feature_detected(client, seed, monkeypatch):
    _patch_key(monkeypatch)

    async def missing(*a, **kw):
        raise StudioEndpointMissing("/api/version")

    monkeypatch.setattr("app.unsloth_extras.studio_version", missing)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/unsloth/version")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False and "version" in d["hint"].lower()


def test_version_route_reports_version(client, seed, monkeypatch):
    _patch_key(monkeypatch)

    async def fake_version():
        return "1.2.3"

    monkeypatch.setattr("app.unsloth_extras.studio_version", fake_version)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    d = client.get("/api/ai/unsloth/version").json()
    assert d == {"available": True, "version": "1.2.3"}


def test_update_route_degrades_cleanly(client, seed, monkeypatch):
    _patch_key(monkeypatch)

    async def missing(*a, **kw):
        raise StudioEndpointMissing("/api/update")

    monkeypatch.setattr("app.unsloth_extras.studio_update", missing)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/unsloth/update")
    assert r.status_code == 400
    assert "Docker" in r.json()["detail"]


def test_update_route_success_passthrough(client, seed, monkeypatch):
    _patch_key(monkeypatch)

    async def fake_update():
        return {"status": "updating"}

    monkeypatch.setattr("app.unsloth_extras.studio_update", fake_update)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/unsloth/update")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "result": {"status": "updating"}}


def test_tts_route_per_request_model_passthrough(client, seed, monkeypatch):
    """The TTS route's per-request model/voice (audio library chooser) must
    reach unsloth_extras.tts verbatim — blank falls back to the persisted
    default."""
    captured = {}

    async def fake_tts(text, model="", voice="", response_format="mp3", speed=1.0):
        captured["text"] = text
        captured["model"] = model
        captured["voice"] = voice
        return b"fake-audio", "audio/mpeg"

    monkeypatch.setattr("app.unsloth_extras.tts", fake_tts)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/ai/tts", json={"text": "hello table", "model": "custom-tts", "voice": "narrator"})
    assert r.status_code == 200, r.text
    assert captured == {"text": "hello table", "model": "custom-tts", "voice": "narrator"}

    # blank model → the persisted default from Settings
    r2 = client.post("/api/ai/tts", json={"text": "again"})
    assert r2.status_code == 200
    from app.ai import get_tts_model
    assert captured["model"] == get_tts_model()


def test_update_route_falls_back_to_allow_method(client, seed, monkeypatch):
    """Real-world finding from the GM's deployment: Studio's /api/update
    exists but 405s POST and 404s GET — the 405 response's standard Allow
    header is the only truthful way to learn the real method (here: PUT)."""
    from app.unsloth_extras import StudioError

    calls = []

    async def fake_request(method, path, **kw):
        calls.append(method)
        if method == "POST":
            raise StudioError("Method Not Allowed", 405,
                              headers={"allow": "PUT"})
        return {"status": "updating"}

    monkeypatch.setattr("app.unsloth_extras._request", fake_request)
    _patch_key(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/unsloth/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "result": {"status": "updating"}}
    assert calls == ["POST", "PUT"]  # POST first, then the Allow-named method


def test_update_route_get_fallback_when_allow_says_get(client, seed, monkeypatch):
    """Allow: GET, HEAD → the fallback picks GET."""
    from app.unsloth_extras import StudioError

    calls = []

    async def fake_request(method, path, **kw):
        calls.append(method)
        if method == "POST":
            raise StudioError("Method Not Allowed", 405,
                              headers={"allow": "GET, HEAD"})
        return {"status": "updating"}

    monkeypatch.setattr("app.unsloth_extras._request", fake_request)
    _patch_key(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/unsloth/update")
    assert r.status_code == 200
    assert calls == ["POST", "GET"]


def test_update_route_surfaces_persistent_405(client, seed, monkeypatch):
    """No Allow header to work with (or the retry also fails) — the 405
    surfaces as-is instead of a misleading message."""
    from app.unsloth_extras import StudioError

    async def both_rejected(*a, **kw):
        raise StudioError("Method Not Allowed", 405)  # no headers

    monkeypatch.setattr("app.unsloth_extras.studio_update", both_rejected)
    _patch_key(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/unsloth/update")
    assert r.status_code == 405
    assert "Method Not Allowed" in r.json()["detail"]


def test_update_route_surfaces_persistent_405(client, seed, monkeypatch):
    from app.unsloth_extras import StudioError

    async def both_rejected(*a, **kw):
        raise StudioError("Method Not Allowed", 405)

    monkeypatch.setattr("app.unsloth_extras.studio_update", both_rejected)
    _patch_key(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/unsloth/update")
    assert r.status_code == 405
    assert "Method Not Allowed" in r.json()["detail"]


def test_probe_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/unsloth/probe").status_code == 403


def test_probe_reports_404s_as_data(client, seed, monkeypatch):
    """404s are the ANSWER the probe collects, not errors: the live Docker
    latest exposes neither /api/version nor /api/update, and the probe's
    whole job is showing that in one click."""
    import httpx
    from app import unsloth_extras as ux

    _patch_key(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        return httpx.Response(404, json={"detail": "Not Found"})

    transport = httpx.MockTransport(handler)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return transport.handle_request(httpx.Request("GET", url, headers=headers or {}))

    monkeypatch.setattr(ux._httpx, "AsyncClient", FakeClient)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/unsloth/probe")
    assert r.status_code == 200, r.text
    d = r.json()
    by_path = {x["path"]: x["status"] for x in d["results"]}
    assert by_path["/api/version"] == 404       # missing = data
    assert by_path["/v1/models"] == 200         # known-good confirmed
    assert d["version"] is None


def test_probe_extracts_version_from_200(client, seed, monkeypatch):
    import httpx
    from app import unsloth_extras as ux

    _patch_key(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1.4.2-docker"})
        return httpx.Response(404, json={"detail": "Not Found"})

    transport = httpx.MockTransport(handler)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return transport.handle_request(httpx.Request("GET", url, headers=headers or {}))

    monkeypatch.setattr(ux._httpx, "AsyncClient", FakeClient)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    d = client.get("/api/ai/unsloth/probe").json()
    assert d["version"] == "1.4.2-docker"
