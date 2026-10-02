"""Regression tests for the Unsloth-integration audit (every case here was reproduced against the code
before it was fixed): hostile/odd Studio responses, status honesty, the player recap model, the Settings
key field, the committed smoke-script key, compose defaults, and the STT duration cap."""
import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import pytest

from app import ai as _ai
from app import llm_client
from app import unsloth_extras as ux
from app.database import SessionLocal
from app.models import AppSettings, GameSession, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

ROOT = Path(__file__).parent.parent
REAL = httpx.AsyncClient


def _mock(monkeypatch, handler, seen_timeouts=None):
    def factory(*a, **kw):
        if seen_timeouts is not None:
            seen_timeouts.append(kw.get("timeout"))
        kw.pop("timeout", None)
        kw["transport"] = httpx.MockTransport(handler)
        return REAL(*a, **kw)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _studio(monkeypatch):
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "sk-test")
    monkeypatch.setattr(ux, "effective_llm_url", lambda: "http://studio")


# ── odd Studio responses ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [{"error": "plain string error"}, ["a", "list"], "just text", {"error": ["x"]}])
def test_error_bodies_of_any_shape_become_studio_errors(monkeypatch, body):
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(400, json=body))
    for call in (lambda: ux.hub_cached(), lambda: ux.tts("hi", model="m"), lambda: ux.stt(b"abc", "a.wav", "small"),
                 lambda: ux.video_content("v1")):
        with pytest.raises(ux.StudioError) as e:
            asyncio.run(call())
        assert e.value.status_code == 400 and str(e.value), body


def test_a_string_error_keeps_its_text(monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(400, json={"error": "plain string error"}))
    with pytest.raises(ux.StudioError, match="plain string error"):
        asyncio.run(ux.hub_cached())


def test_a_404_with_a_specific_message_is_not_a_missing_endpoint(monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(404, json={"detail": "Repository 'x/y' not found"}))
    with pytest.raises(ux.StudioError) as e:
        asyncio.run(ux.hub_download_progress("x/y"))
    assert not isinstance(e.value, ux.StudioEndpointMissing)
    assert "Repository 'x/y' not found" in str(e.value) and e.value.status_code == 404


@pytest.mark.parametrize("resp", [
    httpx.Response(404, json={"detail": "API endpoint not found"}),
    httpx.Response(404, json={"detail": "Not Found"}),
    httpx.Response(404, json={}),
    httpx.Response(404, text="<html>404</html>"),
])
def test_a_bare_404_is_still_a_missing_endpoint(monkeypatch, resp):
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: resp)
    with pytest.raises(ux.StudioEndpointMissing):
        asyncio.run(ux.hub_cached())


def test_update_only_ever_uses_post_and_needs_a_json_answer(monkeypatch):
    _studio(monkeypatch)
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/update" and req.method == "GET":
            return httpx.Response(200, text="<html>Studio SPA</html>", headers={"content-type": "text/html"})
        if req.url.path == "/update":
            return httpx.Response(405, json={"detail": "Method Not Allowed"}, headers={"allow": "GET, HEAD"})
        return httpx.Response(404, json={"detail": "API endpoint not found"})

    _mock(monkeypatch, handler)
    with pytest.raises(ux.StudioEndpointMissing):
        asyncio.run(ux.studio_update())
    assert all(m in ("POST", "PUT") for m, _p in calls), calls   # nothing mutating is ever attempted with GET


def test_update_accepts_a_real_json_answer_and_a_put_only_endpoint(monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"status": "updating"}) if req.method == "POST" and req.url.path == "/api/update"
          else httpx.Response(404, json={}))
    assert asyncio.run(ux.studio_update()) == {"status": "updating"}

    def put_only(req):
        if req.url.path == "/api/update" and req.method == "POST":
            return httpx.Response(405, json={"detail": "no"}, headers={"allow": "PUT, OPTIONS"})
        if req.url.path == "/api/update" and req.method == "PUT":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})

    _mock(monkeypatch, put_only)
    assert asyncio.run(ux.studio_update()) == {"ok": True}


def test_update_refuses_a_200_that_is_not_json(monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(200, text="<html>page</html>", headers={"content-type": "text/html"})
          if req.url.path == "/api/update" else httpx.Response(404, json={}))
    with pytest.raises(ux.StudioError):
        asyncio.run(ux.studio_update())


# ── streaming ────────────────────────────────────────────────────────────────

_SSE = ('data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        'data: {"error":{"message":"context overflow","code":500}}\n\n'
        'data: [DONE]\n\n')


def test_an_error_event_inside_a_stream_raises(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(200, text=_SSE, headers={"content-type": "text/event-stream"}))

    async def run():
        out = []
        async for ch in await llm_client.UnslothClient("http://studio", "k").chat("m", [{"role": "user", "content": "x"}], stream=True):
            out.append(ch.message.content)
        return out

    with pytest.raises(llm_client.UnslothResponseError, match="context overflow"):
        asyncio.run(run())


def test_stream_chat_ends_a_cut_off_answer_with_an_error_marker(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(200, text=_SSE, headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    monkeypatch.setattr(_ai, "_llm_url_override", "http://studio")

    async def run():
        return [p async for p in _ai.stream_chat([{"role": "user", "content": "x"}], model="m")]

    pieces = asyncio.run(run())
    assert pieces[0] == "Hello" and "context overflow" in pieces[-1] and pieces[-1].startswith("[AI error")


# ── backend resolution ───────────────────────────────────────────────────────

def test_a_studio_key_is_never_sent_to_the_ollama_address(monkeypatch):
    monkeypatch.setattr(_ai, "_llm_url_override", "")
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-secret")
    monkeypatch.setattr(_ai, "UNSLOTH_URL", "")
    assert _ai.effective_llm_url() == "http://unsloth:8000" != _ai.OLLAMA_URL
    monkeypatch.setattr(_ai, "UNSLOTH_URL", "http://studio.lan:8000")
    assert _ai.effective_llm_url() == "http://studio.lan:8000"
    monkeypatch.setattr(_ai, "_llm_url_override", "http://other:1")
    assert _ai.effective_llm_url() == "http://other:1"
    monkeypatch.setattr(_ai, "_llm_api_key_override", "")
    monkeypatch.setattr(_ai, "UNSLOTH_API_KEY", "")
    monkeypatch.setattr(_ai, "_llm_url_override", "")
    assert _ai.effective_llm_url() == _ai.OLLAMA_URL


# ── status honesty, bounds and dedupe ────────────────────────────────────────

def _gm_with_studio(client, seed, monkeypatch):
    login(client, seed.gm.email, GM_PASSWORD)
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    monkeypatch.setattr(_ai, "_llm_url_override", "http://studio")


def test_status_says_unreachable_when_studio_is_down(client, seed, monkeypatch):
    _gm_with_studio(client, seed, monkeypatch)

    def boom(req):
        raise httpx.ConnectError("refused")

    _mock(monkeypatch, boom)
    d = client.get("/api/ai/unsloth/status").json()
    assert d["reachable"] is False and d["loaded_models_error"]


def test_status_is_reachable_when_studio_answers(client, seed, monkeypatch):
    _gm_with_studio(client, seed, monkeypatch)

    def handler(req):
        if req.url.path == "/api/inference/loaded-models":
            return httpx.Response(200, json={"models": [{"model": "a/b"}]})
        if req.url.path == "/api/settings/openai-auto-switch":
            return httpx.Response(200, json={"enabled": True, "auto_unload_idle_seconds": 300})
        return httpx.Response(404, json={})

    _mock(monkeypatch, handler)
    d = client.get("/api/ai/unsloth/status").json()
    assert d["reachable"] is True and d["auto_switch"]["enabled"] is True


def test_the_status_card_shows_unreachable_and_warns_about_auto_switch_off():
    js = (ROOT / "static" / "js" / "ai-chat-models.js").read_text()
    assert "d.reachable === false" in js
    assert "model-by-name" in js or "by name" in js, "auto-switch off is explained, not just greyed"


def test_ai_status_is_bounded_and_does_not_stampede(monkeypatch):
    monkeypatch.setattr(_ai, "_status_cache", None)
    monkeypatch.setattr(_ai, "_status_inflight", None)
    monkeypatch.setattr(_ai, "_STATUS_TIMEOUT_SECONDS", 0.2)
    calls = []

    class Hung:
        async def list(self):
            calls.append(1)
            await asyncio.sleep(30)

    monkeypatch.setattr(_ai, "_client", lambda: Hung())

    async def run():
        t0 = time.monotonic()
        out = await asyncio.gather(*[_ai.status() for _ in range(5)])
        return out, time.monotonic() - t0

    out, took = asyncio.run(run())
    assert took < 3 and all(o["status"] == "unavailable" for o in out)
    assert len(calls) == 1, "five simultaneous polls share one probe"


def test_changing_the_backend_clears_the_status_cache(monkeypatch):
    monkeypatch.setattr(_ai, "_status_cache", (time.monotonic(), {"status": "ok"}))
    _ai.set_llm_override(url="http://x", model="", api_key="k", context_tokens=0)
    assert _ai._status_cache is None
    _ai.set_llm_override()


def test_studio_list_has_a_probe_timeout_not_the_one_hour_chat_timeout(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.extensions["timeout"]["read"])
        return httpx.Response(200, json={"data": []})

    _mock(monkeypatch, handler)
    asyncio.run(llm_client.UnslothClient("http://studio", "k").list())
    assert seen and seen[0] <= 60


# ── players cannot pick the model ────────────────────────────────────────────

def _session(seed):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="S", session_num=1)
        db.add(gs)
        db.commit()
        return gs.id
    finally:
        db.close()


def test_a_players_recap_model_is_ignored_but_a_gms_is_used(client, seed, monkeypatch):
    from app.routers import sessions as sess
    sid = _session(seed)
    seen = []

    def fake_create(**kw):
        seen.append(kw)
        return len(seen)

    monkeypatch.setattr(sess._audio_jobs, "create_session_log_recap_job", fake_create)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/session-log/{sid}/recap", json={"model": "some-org/huge-70B-GGUF", "think": True}).status_code == 200
    assert seen[-1]["model"] == "", "a player never chooses which model Studio loads"
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/api/session-log/{sid}/recap", json={"model": "gm-model", "think": True})
    assert seen[-1]["model"] == "gm-model"


def test_the_player_page_has_no_model_picker(client, seed):
    sid = _session(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert 'id="recap-model"' not in client.get(f"/session-log/{sid}").text
    client.cookies.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert 'id="recap-model"' in client.get(f"/session-log/{sid}").text


def test_song_performance_is_gm_only_like_the_tts_route(client, seed):
    sid = _session(seed)
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_a.id,
                                             WorldMembership.user_id == seed.player_a.id).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/sessions/{sid}/recap-song", json={"lyrics": "la la"}).status_code == 403


# ── Settings: the API key ────────────────────────────────────────────────────

KEY = "sk-unsloth-" + "ab12cd34ef56ab78cd90"


def _save_system(client, **extra):
    data = {"ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "llm_url": "", "llm_model": "",
            "llm_context_tokens": ""}
    data.update(extra)
    return client.post("/settings/system", data=data, follow_redirects=False)


def _stored_key():
    db = SessionLocal()
    try:
        s = db.query(AppSettings).first()
        return (s.llm_api_key or "") if s else ""
    finally:
        db.close()


def test_the_settings_page_never_prints_the_api_key(client, seed, monkeypatch):
    login(client, seed.gm.email, GM_PASSWORD)
    monkeypatch.setattr(_ai, "UNSLOTH_API_KEY", "sk-unsloth-ENVKEY0123456789")
    assert _save_system(client, llm_api_key=KEY).status_code == 303
    html = client.get("/settings?tab=system").text
    assert KEY not in html and "ENVKEY0123456789" not in html
    tag = re.search(r'<input[^>]*id="llm-api-key"[^>]*>', html).group(0)
    assert 'type="password"' in tag and "value=" not in tag.replace('autocomplete', '')
    assert KEY[-4:] in html, "the saved key is recognisable by its last characters"


def test_a_blank_key_field_keeps_the_saved_key_and_clear_removes_it(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _save_system(client, llm_api_key=KEY)
    assert _stored_key() == KEY
    _save_system(client, llm_api_key="")                      # the form is saved again without retyping the key
    assert _stored_key() == KEY
    _save_system(client, llm_api_key="", llm_api_key_clear="1")
    assert _stored_key() == ""
    assert _ai.effective_llm_api_key() == (_ai.UNSLOTH_API_KEY or "")


def test_a_new_key_replaces_the_old_one(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _save_system(client, llm_api_key=KEY)
    _save_system(client, llm_api_key=KEY[:-4] + "ffff")
    assert _stored_key().endswith("ffff")


def test_the_console_url_must_be_http_or_https(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example", "ftp://x"):
        assert client.post("/api/ai/unsloth/prefs", json={"studio_console_url": bad}).status_code == 400, bad
    assert client.post("/api/ai/unsloth/prefs", json={"studio_console_url": "https://studio.example:8000/"}).status_code == 200
    assert client.post("/api/ai/unsloth/prefs", json={"studio_console_url": ""}).status_code == 200


# ── repository hygiene ───────────────────────────────────────────────────────

def test_no_studio_api_key_is_committed():
    pat = re.compile(r"sk-unsloth-[0-9a-f]{16,}")
    hits = []
    for p in ROOT.rglob("*"):
        if p.is_dir() or ".git" in p.parts or "__pycache__" in p.parts or "_data" in p.parts or p.suffix in (".png", ".webp", ".db", ".pyc"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if pat.search(text):
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, hits


def test_the_smoke_script_reads_its_key_from_the_environment():
    src = (ROOT / "scripts" / "smoke_unsloth_live.py").read_text()
    assert 'os.environ["UNSLOTH_API_KEY"] = "' not in src
    assert 'os.environ.get("UNSLOTH_API_KEY")' in src or 'os.environ["UNSLOTH_API_KEY"]' in src


@pytest.mark.parametrize("name", ["docker-compose.yml", "truenas-compose.yml"])
def test_the_studio_image_is_pinnable_from_env(name):
    src = (ROOT / name).read_text()
    assert "image: ${UNSLOTH_IMAGE:-unsloth/unsloth:latest}" in src, name


def test_studio_listens_on_loopback_by_default_in_the_base_compose():
    src = (ROOT / "docker-compose.yml").read_text()
    assert '"${UNSLOTH_BIND:-127.0.0.1}:8000:8000"' in src
    env = (ROOT / ".env.example").read_text()
    assert "UNSLOTH_IMAGE=" in env and "UNSLOTH_BIND" in env


def test_the_provisioning_steps_mention_the_context_length():
    for name in ("docker-compose.yml", "truenas-compose.yml", ".env.example"):
        text = (ROOT / name).read_text()
        assert "LLM_CONTEXT_TOKENS" in text.split("One-time provisioning")[1][:2500], name


def test_no_comment_points_at_the_missing_migration_plan():
    assert "nd-world-unsloth-migration-plan" not in (ROOT / "app" / "ai.py").read_text()


# ── STT: a long file is split even when it is small ──────────────────────────

def test_a_long_but_small_audio_file_is_split_before_transcription(tmp_path, monkeypatch):
    f = tmp_path / "talk.mp3"
    f.write_bytes(b"x" * 1_000_000)          # far under Studio's 25 MiB request limit
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")
    split_args = {}

    async def fake_probe(p):
        return 7200.0                          # two hours

    async def fake_split(p, secs):
        split_args["secs"] = secs
        parts = []
        for i in range(12):
            q = tmp_path / f"part{i}.mp3"
            q.write_bytes(b"y")
            parts.append(q)
        return parts, None

    async def fake_transcode(p, d):
        return p

    sent = []

    async def fake_stt(audio, name, model="small"):
        sent.append(name)
        return f"text of {name}"

    monkeypatch.setattr(_ai, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(_ai, "_split_audio_into_chunks", fake_split)
    monkeypatch.setattr(_ai, "_transcode_audio_to_mp3", fake_transcode)
    monkeypatch.setattr(ux, "stt", fake_stt)
    out = asyncio.run(_ai._transcribe_one_file_unsloth(f))
    assert split_args["secs"] <= _ai._UNSLOTH_STT_CHUNK_SECONDS and len(sent) == 12
    assert out.count("\n") == 11


def test_a_short_small_file_is_sent_whole(tmp_path, monkeypatch):
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"x" * 100_000)
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")

    async def fake_probe(p):
        return 90.0

    sent = []

    async def fake_stt(audio, name, model="small"):
        sent.append(name)
        return "hi"

    monkeypatch.setattr(_ai, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(ux, "stt", fake_stt)
    assert asyncio.run(_ai._transcribe_one_file_unsloth(f)) == "hi" and sent == ["clip.mp3"]


@pytest.mark.parametrize("secs", [600.0, 600.008, 600.064, 750.0, 899.0])
def test_a_chunk_the_pipeline_cut_at_ten_minutes_is_one_request(tmp_path, monkeypatch, secs):
    """The generic pipeline cuts long audio at WHISPER_CHUNK_SECONDS (600) with a stream copy, and the
    pieces measure 600.004-600.064 s. They must go to Studio as ONE request each — a duration trigger
    without slack re-encoded every piece and sent Studio a second, few-millisecond fragment (measured
    with real ffmpeg: 600.008 s webm/mp3/m4a pieces each produced 2 requests)."""
    f = tmp_path / "chunk_0000.webm"
    f.write_bytes(b"x" * 100_000)
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")

    async def fake_probe(p):
        return secs

    async def boom(*a, **k):
        raise AssertionError("must not re-encode or re-split a chunk that is about ten minutes long")

    sent = []

    async def fake_stt(audio, name, model="small"):
        sent.append(name)
        return "hi"

    monkeypatch.setattr(_ai, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(_ai, "_split_audio_into_chunks", boom)
    monkeypatch.setattr(_ai, "_transcode_audio_to_mp3", boom)
    monkeypatch.setattr(ux, "stt", fake_stt)
    assert asyncio.run(_ai._transcribe_one_file_unsloth(f)) == "hi" and sent == ["chunk_0000.webm"]


def test_a_big_file_just_over_ten_minutes_is_not_split_into_a_sliver(tmp_path, monkeypatch):
    """Over the byte limit it is re-encoded (small, mono mp3) — and if the result is only ~10 minutes it is
    sent whole rather than as 600 s + a half-second remnant."""
    f = tmp_path / "long.flac"
    f.write_bytes(b"x" * (24 * 1024 * 1024))          # over Studio's 23 MiB budget
    monkeypatch.setattr(_ai, "_llm_api_key_override", "sk-test")

    async def fake_probe(p):
        return 600.5

    mp3 = tmp_path / "long.mp3"
    mp3.write_bytes(b"m" * 5_000_000)

    async def fake_transcode(p, d):
        return mp3

    async def boom(*a, **k):
        raise AssertionError("no split for a ten-minute recording")

    sent = []

    async def fake_stt(audio, name, model="small"):
        sent.append(name)
        return "hi"

    monkeypatch.setattr(_ai, "_probe_audio_duration", fake_probe)
    monkeypatch.setattr(_ai, "_transcode_audio_to_mp3", fake_transcode)
    monkeypatch.setattr(_ai, "_split_audio_into_chunks", boom)
    monkeypatch.setattr(ux, "stt", fake_stt)
    assert asyncio.run(_ai._transcribe_one_file_unsloth(f)) == "hi" and sent == ["long.mp3"]


# ── image timeouts ───────────────────────────────────────────────────────────

def test_image_generation_has_a_tunable_long_timeout(monkeypatch):
    assert ux._IMAGE_TIMEOUT.read >= 1800
    seen = []
    _studio(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"images": []}), seen_timeouts=seen)
    asyncio.run(ux.image_generate_native({"prompt": "x"}))
    assert seen[-1].read == ux._IMAGE_TIMEOUT.read
    assert "UNSLOTH_IMAGE_TIMEOUT_SECONDS" in (ROOT / "app" / "unsloth_extras.py").read_text()
    assert "timeout=600" not in (ROOT / "app" / "ai.py").read_text().split("async def imagegen_generate")[1][:3000]


# ── context-length mismatch ──────────────────────────────────────────────────

@pytest.mark.parametrize("data, expect", [
    ({"overrides": {"a/b": {"max_seq_length": 8192}}}, 8192),
    ({"overrides": [{"model_id": "a/b", "max_seq_length": 8192}]}, 8192),
    ([{"model_id": "a/b", "max_seq_length": "8192"}], 8192),
    ({"a/b": {"max_seq_length": 4096}}, 4096),
    ({"overrides": {"other/model": {"max_seq_length": 8192}}}, None),
    ({"overrides": {"a/b": {"llama_extra_args": "-x"}}}, None),
    ("junk", None), (None, None), ([], None),
])
def test_the_configured_context_is_found_in_whatever_shape_studio_answers(data, expect):
    assert ux.context_length_for(data, "a/b") == expect


def test_status_warns_when_studios_context_differs_from_what_nd_world_assumes(client, seed, monkeypatch):
    _gm_with_studio(client, seed, monkeypatch)
    monkeypatch.setattr(_ai, "_llm_model_override", "a/b")
    monkeypatch.setattr(_ai, "_llm_context_tokens_override", 16384)

    def handler(req):
        if req.url.path == "/api/settings/openai-auto-switch/overrides":
            return httpx.Response(200, json={"overrides": {"a/b": {"max_seq_length": 8192}}})
        if req.url.path == "/api/settings/openai-auto-switch":
            return httpx.Response(200, json={"enabled": True})
        if req.url.path == "/api/inference/loaded-models":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404, json={})

    _mock(monkeypatch, handler)
    d = client.get("/api/ai/unsloth/status").json()
    assert d["context_mismatch"] == {"studio": 8192, "nd_world": 16384, "model": "a/b"}
    monkeypatch.setattr(_ai, "_llm_context_tokens_override", 8192)
    assert "context_mismatch" not in client.get("/api/ai/unsloth/status").json()


def test_hub_download_passes_the_quant_variant(monkeypatch):
    _studio(monkeypatch)
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"accepted": True})

    _mock(monkeypatch, handler)
    asyncio.run(ux.hub_download_start("a/b", gguf_variant="UD-IQ4_NL"))
    assert seen["body"] == {"repo_id": "a/b", "gguf_variant": "UD-IQ4_NL"}
    asyncio.run(ux.hub_download_start("a/b"))
    assert seen["body"] == {"repo_id": "a/b"}
