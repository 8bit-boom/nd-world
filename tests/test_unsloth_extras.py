"""Tests for app.unsloth_extras — the Studio server surfaces beyond chat
(hub, image load/progress, auto-switch, TTS/STT) and the /api/ai/unsloth/*
+ /api/ai/tts routes that expose them.

HTTP is mocked with httpx.MockTransport (same convention as
tests/test_llm_client.py) pinning the live-verified shapes from
docs/UNSLOTH_PHASE0_FINDINGS.md's Phase 0.5 appendix. Route tests use the
standard client/seed fixtures with the Studio calls monkeypatched.
"""
import json

import httpx
import pytest

from app import unsloth_extras as ux


def _patch_transport(monkeypatch, handler):
    """Point every unsloth_extras request at a MockTransport, with a
    configured backend (UNSLOTH_URL/KEY)."""
    real_async_client = ux._httpx.AsyncClient

    def _fake_async_client(*a, **kw):
        kw.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handler), timeout=10)

    monkeypatch.setattr(ux._httpx, "AsyncClient", _fake_async_client)
    monkeypatch.setattr(ux, "effective_llm_url", lambda: "http://unsloth:8888")
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "sk-test")


def _json_response(payload, status=200):
    return httpx.Response(status, json=payload)


def _recorder(handler_state, response):
    def handler(request: httpx.Request) -> httpx.Response:
        handler_state["request"] = request
        handler_state["url"] = str(request.url)
        return response
    return handler


# ── Hub ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hub_cached_parses_verified_shape(monkeypatch):
    state = {}
    payload = {"cached": [{"repo_id": "a/B", "task": "text-generation", "size_bytes": 5,
                           "capabilities": {"can_chat": True}, "load_id": "a/B"}]}
    _patch_transport(monkeypatch, _recorder(state, _json_response(payload)))
    out = await ux.hub_cached()
    assert out[0]["repo_id"] == "a/B"
    assert "/api/hub/cached-gguf" in state["url"]
    assert state["request"].headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_hub_download_start_posts_repo_id(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, _json_response(
        {"job_key": "a/B::", "state": "running", "accepted": True})))
    out = await ux.hub_download_start("a/B")
    assert out["accepted"] is True
    assert json.loads(state["request"].content.decode()) == {"repo_id": "a/B"}


@pytest.mark.asyncio
async def test_missing_endpoint_maps_to_clean_error(monkeypatch):
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(404, json={"detail": "API endpoint not found"})))
    with pytest.raises(ux.StudioEndpointMissing):
        await ux.hub_cached()


@pytest.mark.asyncio
async def test_openai_error_shape_is_surfaced(monkeypatch):
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(409, json={
        "error": {"message": "STT model 'small' is not downloaded. Download it in Settings, then Voice."}})))
    with pytest.raises(ux.StudioError) as ei:
        await ux.stt(b"x", "clip.wav", model="small")
    assert "not downloaded" in str(ei.value)


# ── Auto-switch ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_switch_update_sends_only_given_fields(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, _json_response({"enabled": True, "auto_unload_idle_seconds": 300})))
    out = await ux.auto_switch_update(enabled=True, auto_unload_idle_seconds=300)
    assert out["enabled"] is True
    sent = json.loads(state["request"].content.decode())
    assert sent == {"enabled": True, "auto_unload_idle_seconds": 300}
    assert state["request"].method == "PUT"


# ── TTS ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_returns_audio_bytes_and_content_type(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, httpx.Response(200, content=b"AUDIODATA",
                                                                  headers={"content-type": "audio/mpeg"})))
    audio, ct = await ux.tts("hello there", model="unsloth/orpheus-3b", voice="alloy")
    assert audio == b"AUDIODATA"
    assert ct == "audio/mpeg"
    sent = json.loads(state["request"].content.decode())
    assert sent["input"] == "hello there"
    assert sent["model"] == "unsloth/orpheus-3b"
    assert sent["voice"] == "alloy"


@pytest.mark.asyncio
async def test_tts_without_model_raises_clean_error(monkeypatch):
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(200, content=b"x")))
    with pytest.raises(ux.StudioError) as ei:
        await ux.tts("hello", model="")
    assert "TTS model" in str(ei.value)


# ── Image load / native generate ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_image_load_sends_gguf_fields(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, _json_response({"loaded": True, "engine": "diffusers"})))
    out = await ux.image_load("a/Krea-GGUF", gguf_filename="krea-Q8_0.gguf")
    assert out["loaded"] is True
    sent = json.loads(state["request"].content.decode())
    assert sent == {"model_path": "a/Krea-GGUF", "model_kind": "gguf", "gguf_filename": "krea-Q8_0.gguf"}


@pytest.mark.asyncio
async def test_image_generate_native_returns_gallery_records(monkeypatch):
    state = {}
    gallery = {"images": [{"id": "img1", "url": "/api/inference/images/gallery/img1/file", "seed": 7}]}
    _patch_transport(monkeypatch, _recorder(state, _json_response(gallery)))
    out = await ux.image_generate_native({"prompt": "a castle", "model": "a/B"})
    assert out[0]["id"] == "img1"
    assert json.loads(state["request"].content.decode())["prompt"] == "a castle"


@pytest.mark.asyncio
async def test_image_gallery_file_fetches_record_url(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, httpx.Response(200, content=b"PNGBYTES")))
    raw = await ux.image_gallery_file({"url": "/api/inference/images/gallery/img1/file"})
    assert raw == b"PNGBYTES"
    assert "/api/inference/images/gallery/img1/file" in state["url"]


# ── Embeddings shim (the vault-RAG fix) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_unsloth_client_embed_parses_openai_shape():
    from app.llm_client import UnslothClient
    client = UnslothClient("http://unsloth:8888", "sk-test")
    state = {}

    def handler(request: httpx.Request) -> httpx.Response:
        state["url"] = str(request.url)
        state["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0,
                      "embedding": [0.1, 0.2, 0.3]}],
            "model": "unsloth/bge-small-en-v1.5",
        })

    client._http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    resp = await client.embed("unsloth/bge-small-en-v1.5", "hello world")
    assert resp.embeddings[0] == [0.1, 0.2, 0.3]  # ollama-shaped: resp.embeddings[0]
    assert state["body"]["input"] == ["hello world"]
    assert "/v1/embeddings" in state["url"]


# ── Route gating + TTS clip creation ─────────────────────────────────────────

def test_unsloth_routes_are_gm_only(client, seed):
    """None of the /api/ai/unsloth/* routes are in any allowlist — players
    and assistants are denied by the auth_gate's default-deny."""
    from .conftest import PLAYER_PASSWORD, login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/unsloth/models").status_code == 403
    assert client.post("/api/ai/unsloth/hub/download", json={"repo_id": "a/B"}).status_code == 403
    assert client.post("/api/ai/tts", json={"text": "hi"}).status_code == 403


def test_tts_route_saves_a_gm_only_audio_clip(client, seed, monkeypatch):
    """The TTS route turns Studio audio bytes into a normal AudioClip in the
    active world — GM-only-visible until the GM shares it."""
    from app.database import SessionLocal
    from app.models import AudioClip

    async def _fake_tts(text, model, voice="", response_format="mp3", speed=1.0,
                        instructions="", language=""):
        return b"AUDIOBYTES", "audio/mpeg"

    monkeypatch.setattr(ux, "tts", _fake_tts)
    monkeypatch.setattr("app.routers.ai._unsloth_extras.tts", _fake_tts)
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/tts", json={"text": "The harbor gates close at dusk.", "name": "Gate line"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "Gate line"
    assert data["file_url"].startswith("/uploads/audio/")
    db = SessionLocal()
    try:
        clip = db.get(AudioClip, data["id"])
        assert clip.world_id == seed.world_a.id
        assert clip.visible_to_players is False
        assert clip.file_url == data["file_url"]
    finally:
        db.close()


def test_tts_route_requires_world_and_text(client, seed, monkeypatch):
    async def _fake_tts(text, model, voice="", response_format="mp3", speed=1.0,
                        instructions="", language=""):
        return b"A", "audio/mpeg"
    monkeypatch.setattr("app.routers.ai._unsloth_extras.tts", _fake_tts)
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/ai/tts", json={"text": "   "}).status_code == 400


def test_studio_console_page_renders_for_gm(client, seed):
    """Regression: the /studio handler used the wrong module alias (_ai vs
    main.py's _ai_module) — NameError → 500 on every visit, uncaught by CI
    because the page itself had no test."""
    from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/studio")
    assert r.status_code == 200
    # Without a backend URL the page shows the not-configured card (seed has
    # no UNSLOTH key) — either way it must render, never 500.
    assert "Studio Console" in r.text
    # Players are denied (GM-only route).
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/studio").status_code == 403


# ── TTS instructions/language, health checks, auth tracking, overrides ────────

@pytest.mark.asyncio
async def test_tts_sends_instructions_and_language_only_when_set(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, httpx.Response(
        200, content=b"X", headers={"content-type": "audio/mpeg"})))
    await ux.tts("hi", model="m", voice="ann", instructions=" gruff ", language="de")
    body = json.loads(state["request"].content.decode())
    assert body["instructions"] == "gruff"
    assert body["language"] == "de"
    await ux.tts("hi", model="m")
    body = json.loads(state["request"].content.decode())
    assert "instructions" not in body and "language" not in body


def _reset_auth_state(monkeypatch):
    monkeypatch.setattr(ux, "_last_auth_failure", 0.0)
    monkeypatch.setattr(ux, "_last_auth_ok", 0.0)


@pytest.mark.asyncio
async def test_auth_failure_is_tracked_and_cleared_by_success(monkeypatch):
    _reset_auth_state(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"error": {"message": "Not authenticated"}})
        return _json_response({"models": []})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ux.StudioError) as exc:
        await ux.loaded_models()
    assert exc.value.status_code == 401
    st = ux.auth_status()
    assert st["key_failed"] is True
    assert "Settings → API" in st["hint"]
    await ux.loaded_models()
    assert ux.auth_status()["key_failed"] is False


def test_auth_status_when_no_backend_configured(monkeypatch):
    _reset_auth_state(monkeypatch)
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "")
    st = ux.auth_status()
    assert st["configured"] is False and st["key_failed"] is False


@pytest.mark.asyncio
async def test_stt_health_reports_ok_and_409(monkeypatch):
    _reset_auth_state(monkeypatch)
    # One stateful handler, not two _patch_transport calls — the helper's
    # fake AsyncClient closes over the FIRST handler, so re-patching within
    # a test silently keeps hitting it (see test_tts_health below, same).
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert b'name="model"' in request.content  # multipart form carries the model
        if calls["n"] == 1:
            return httpx.Response(200, json={"text": "Thank you."})
        return httpx.Response(409, json={
            "error": {"message": "STT model 'x' is not downloaded. Download it in Settings, then Voice."}})

    _patch_transport(monkeypatch, handler)
    assert (await ux.stt_health("large-v3-turbo"))["ok"] is True
    out = await ux.stt_health("x")
    assert out["ok"] is False
    assert out["status"] == 409
    assert "not downloaded" in out["message"]


@pytest.mark.asyncio
async def test_tts_health_reports_ok_and_missing_model(monkeypatch):
    _reset_auth_state(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, content=b"aud", headers={"content-type": "audio/mpeg"})
        return httpx.Response(400, json={"error": {"message": "No model loaded."}})

    _patch_transport(monkeypatch, handler)
    out = await ux.tts_health("m", voice="v")
    assert out["ok"] is True and "bytes" in out["message"]
    out = await ux.tts_health("m")
    assert out["ok"] is False and out["status"] == 400


@pytest.mark.asyncio
async def test_auto_switch_overrides_passthrough(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, _json_response(
        {"overrides": [{"model_id": "a/B", "max_seq_length": 8192}]})))
    out = await ux.auto_switch_overrides_get()
    assert "/api/settings/openai-auto-switch/overrides" in state["url"]
    await ux.auto_switch_overrides_set({"model_id": "a/B", "max_seq_length": 4096})
    assert state["request"].method == "PUT"
    assert json.loads(state["request"].content.decode())["max_seq_length"] == 4096


# ── Video helpers ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_video_generate_posts_body_verbatim(monkeypatch):
    state = {}
    _patch_transport(monkeypatch, _recorder(state, _json_response({"id": "vid-1"})))
    out = await ux.video_generate({"model": "m", "prompt": "p", "seconds": 5})
    assert out["id"] == "vid-1"
    assert state["url"].endswith("/v1/videos")
    assert json.loads(state["request"].content.decode())["seconds"] == 5


@pytest.mark.asyncio
async def test_video_content_returns_bytes(monkeypatch):
    _reset_auth_state(monkeypatch)
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(
        200, content=b"MP4BYTES", headers={"content-type": "video/mp4"})))
    data, ct = await ux.video_content("vid-1")
    assert data == b"MP4BYTES" and ct == "video/mp4"


@pytest.mark.asyncio
async def test_video_content_empty_is_an_error(monkeypatch):
    _reset_auth_state(monkeypatch)
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(200, content=b"")))
    with pytest.raises(ux.StudioError):
        await ux.video_content("vid-1")


# ── New routes: checks, auth-status, overrides, prefs fields ─────────────────

def test_check_and_auth_routes_are_gm_only(client, seed):
    from .conftest import PLAYER_PASSWORD, login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.post("/api/ai/unsloth/stt-check", json={}).status_code == 403
    assert client.post("/api/ai/unsloth/tts-check", json={}).status_code == 403
    assert client.get("/api/ai/unsloth/auth-status").status_code == 403
    assert client.get("/api/ai/unsloth/status").status_code == 403
    assert client.get("/api/ai/unsloth/context-overrides").status_code == 403


def test_stt_check_route_never_5xx(client, seed, monkeypatch):
    async def _fail(model, timeout=None):
        return {"ok": False, "status": 409, "message": "not downloaded"}
    monkeypatch.setattr("app.routers.ai._unsloth_or_400", lambda: None)
    monkeypatch.setattr("app.routers.ai._unsloth_extras.stt_health", _fail)
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/unsloth/stt-check", json={"model": "large-v3-turbo"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "status": 409, "message": "not downloaded"}


def test_auth_status_route_shape(client, seed):
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/unsloth/auth-status")
    assert r.status_code == 200
    d = r.json()
    assert set(d) >= {"configured", "url", "key_failed", "hint"}


def test_context_overrides_route_degrades_when_endpoint_missing(client, seed, monkeypatch):
    async def _missing():
        raise ux.StudioEndpointMissing("/api/settings/openai-auto-switch/overrides")
    monkeypatch.setattr("app.routers.ai._unsloth_or_400", lambda: None)
    monkeypatch.setattr("app.routers.ai._unsloth_extras.auto_switch_overrides_get", _missing)
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/unsloth/context-overrides")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_prefs_roundtrip_tts_instructions_and_language(client, seed):
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/unsloth/prefs", json={
        "tts_instructions": "gruff dockworker", "tts_language": "en"})
    assert r.status_code == 200
    d = client.get("/api/ai/unsloth/prefs").json()
    assert d["tts_instructions"] == "gruff dockworker"
    assert d["tts_language"] == "en"


# ── TTS output format: Studio's /v1/audio/speech only does WAV ───────────────

_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


def _strict_speech_studio(seen):
    """Studio as observed live: anything but wav is a 400 with this exact message."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body.get("response_format"))
        fmt = body.get("response_format")
        if fmt != "wav":
            return httpx.Response(400, json={"detail": f"Unsupported response_format '{fmt}'. Only 'wav' is supported."})
        return httpx.Response(200, content=_WAV, headers={"content-type": "audio/wav"})
    return handler


@pytest.mark.asyncio
async def test_tts_asks_studio_for_wav_the_only_format_it_supports(monkeypatch):
    seen = []
    _patch_transport(monkeypatch, _strict_speech_studio(seen))
    audio, ct = await ux.tts("hello there", model="unsloth/orpheus-3b", voice="tara")
    assert seen == ["wav"] and audio == _WAV and ct == "audio/wav"


@pytest.mark.asyncio
async def test_tts_health_passes_against_a_wav_only_studio(monkeypatch):
    """The Settings 'Test TTS' button: it showed "Unsupported response_format 'mp3'" for a working model."""
    _reset_auth_state(monkeypatch)
    seen = []
    _patch_transport(monkeypatch, _strict_speech_studio(seen))
    out = await ux.tts_health("unsloth/orpheus-3b", voice="tara")
    assert out["ok"] is True, out
    assert seen == ["wav"]


@pytest.mark.asyncio
async def test_tts_trusts_the_wav_bytes_over_a_generic_content_type(monkeypatch):
    """Callers pick the file extension from the content type; an octet-stream header must not turn WAV into .mp3."""
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(200, content=_WAV, headers={"content-type": "application/octet-stream"})))
    _audio, ct = await ux.tts("hi", model="m")
    assert "wav" in ct
    _patch_transport(monkeypatch, _recorder({}, httpx.Response(200, content=_WAV)))     # no header at all
    _audio, ct = await ux.tts("hi", model="m")
    assert "wav" in ct
