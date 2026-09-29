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


# ── over-25MiB auto-split ────────────────────────────────────────────────────

def test_plan_unsloth_chunks_math():
    from app.ai import _plan_unsloth_chunks

    LIMIT = 23 * 1024 * 1024
    # fits whole → no chunking
    assert _plan_unsloth_chunks(LIMIT, 3600.0) is None
    # 32 MiB over 1 hour → chunk length proportional, ≥ 30s floor
    cs = _plan_unsloth_chunks(32 * 1024 * 1024, 3600.0)
    assert cs is not None and cs >= 30.0
    # proportionality: bigger file → shorter chunks
    cs2 = _plan_unsloth_chunks(64 * 1024 * 1024, 3600.0)
    assert cs2 < cs
    # no duration → can't plan
    assert _plan_unsloth_chunks(32 * 1024 * 1024, None) is None
    assert _plan_unsloth_chunks(32 * 1024 * 1024, 0) is None


def test_transcribe_unsloth_small_file_single_call(client, seed, monkeypatch, tmp_path):
    """Under the limit: exactly one stt call with the file's own bytes."""
    import asyncio

    from app import ai as ai_module

    f = tmp_path / "clip.flac"
    f.write_bytes(b"a" * 1024)

    calls = []

    async def fake_stt(audio, filename, model="small"):
        calls.append((filename, len(audio)))
        return "hello table"

    monkeypatch.setattr("app.unsloth_extras.stt", fake_stt)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")

    text = asyncio.run(ai_module._transcribe_one_file_unsloth(f))
    assert text == "hello table"
    assert calls == [("clip.flac", 1024)]


def test_transcribe_unsloth_splits_and_joins(client, seed, monkeypatch, tmp_path):
    """Over the limit: ffprobe duration → ffmpeg split → per-part stt calls
    in order → newline-joined transcript, temp dir cleaned up."""
    import asyncio

    from app import ai as ai_module

    f = tmp_path / "session.flac"
    f.write_bytes(b"b" * (24 * 1024 * 1024))  # over the 23 MiB limit

    async def fake_probe(path):
        return 3600.0

    async def fake_split(path, chunk_seconds):
        p1 = tmp_path / "part-001.flac"; p1.write_bytes(b"x" * 1000)
        p2 = tmp_path / "part-002.flac"; p2.write_bytes(b"y" * 1000)
        return [p1, p2], tmp_path

    calls = []

    async def fake_stt(audio, filename, model="small"):
        calls.append(filename)
        return "part text"

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr("app.unsloth_extras.stt", fake_stt)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")

    text = asyncio.run(ai_module._transcribe_one_file_unsloth(f))
    assert calls == ["part-001.flac", "part-002.flac"]
    assert text == "part text\npart text"


def test_transcribe_unsloth_no_duration_clear_error(client, seed, monkeypatch, tmp_path):
    import asyncio

    from app import ai as ai_module
    from app.ai import WhisperError

    f = tmp_path / "big.flac"
    f.write_bytes(b"b" * (24 * 1024 * 1024))

    async def fake_probe(path):
        return None  # ffprobe missing / unreadable

    monkeypatch.setattr(ai_module, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")

    try:
        asyncio.run(ai_module._transcribe_one_file_unsloth(f))
        raised = None
    except WhisperError as e:
        raised = str(e)
    assert raised and "25 MiB" in raised and "MP3" in raised


def test_studio_version_falls_back_to_slash_version(client, seed, monkeypatch):
    """The live Docker build serves /version and 404s /api/version — the
    chain must find it (this is exactly what the GM's deployment does)."""
    import httpx
    from app import unsloth_extras as ux

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "dev"})
        return httpx.Response(404, json={"detail": "Not Found"})

    transport = httpx.MockTransport(handler)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            return transport.handle_request(
                httpx.Request(method, url, headers=kw.get("headers") or {}, json=kw.get("json_body")))

    monkeypatch.setattr(ux._httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "sk-test")

    import asyncio

    async def run():
        return await ux.studio_version()

    assert asyncio.run(run()) == "dev"


def test_update_route_tries_slash_update_after_api_update_fails(client, seed, monkeypatch):
    """The GM's live build: /api/update 405s POST and 404s GET — the chain
    must continue to /update (POST) instead of giving up."""
    import httpx
    from app import unsloth_extras as ux

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        if request.url.path == "/update" and request.method == "POST":
            return httpx.Response(200, json={"status": "updating"})
        return httpx.Response(404, json={"detail": "Not Found"})

    transport = httpx.MockTransport(handler)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            return transport.handle_request(httpx.Request(method, url, **kw))

    monkeypatch.setattr(ux._httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "sk-test")

    import asyncio

    async def run():
        return await ux.studio_update()

    result = asyncio.run(run())
    assert result == {"status": "updating"}
    # the chain stops at the first success — POST /update answered
    assert calls == ["POST /api/update", "GET /api/update", "POST /update"]

