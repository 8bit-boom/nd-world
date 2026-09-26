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

    async def _fake_tts(text, model, voice="", response_format="mp3", speed=1.0):
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
    async def _fake_tts(text, model, voice="", response_format="mp3", speed=1.0):
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
