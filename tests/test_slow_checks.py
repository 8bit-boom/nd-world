"""Test TTS / Test STT on a cold Studio: the first request loads the speech model (after unloading the chat
model), which can take minutes - longer than the 75 s synchronous budget, and longer than the ~100 s a
Cloudflare Tunnel keeps a request open. The two Settings checks therefore run in the background: the POST
answers within seconds either with the finished result (unchanged shape) or {pending, token, elapsed}, and the
page polls GET /api/ai/unsloth/check/{token} until it is done."""
import asyncio

import httpx
import pytest

from app import unsloth_extras as ux

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login
from .test_unsloth_extras import _patch_transport, _reset_auth_state


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    ux._CHECKS.clear()
    monkeypatch.setattr(ux, "_CHECK_WAIT_SECONDS", 0.05)
    yield
    for e in list(ux._CHECKS.values()):
        try:
            if not e["task"].done():
                e["task"].cancel()
        except RuntimeError:                  # the test's event loop is already closed
            pass
    ux._CHECKS.clear()


def _slow(result, delay):
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(delay)
        return result
    return factory, calls


# ── the runner ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_quick_check_answers_with_its_plain_result():
    factory, _ = _slow({"ok": True, "message": "ready"}, 0)
    out = await ux.run_check("tts", "k", factory)
    assert out == {"ok": True, "message": "ready"}, "the finished shape is exactly what the health check returned"


@pytest.mark.asyncio
async def test_a_slow_check_answers_pending_then_the_result_by_polling():
    factory, calls = _slow({"ok": True, "message": "loaded"}, 0.3)
    first = await ux.run_check("tts", "k", factory)
    assert first["pending"] is True and first["ok"] is None and first["token"] and "elapsed" in first
    assert "loading" in first["message"].lower()
    final = None
    for _ in range(20):
        final = await ux.poll_check(first["token"])
        if not final.get("pending"):
            break
    assert final == {"ok": True, "message": "loaded"} and len(calls) == 1


@pytest.mark.asyncio
async def test_pressing_again_while_it_runs_joins_the_same_check():
    factory, calls = _slow({"ok": True, "message": "x"}, 0.3)
    a = await ux.run_check("tts", "same", factory)
    b = await ux.run_check("tts", "same", factory)
    assert a["token"] == b["token"] and len(calls) == 1, "no second synthesis stacked on a Studio that is still loading"
    other = await ux.run_check("tts", "other model", factory)
    assert other["token"] != a["token"] and len(calls) == 2, "a different model/voice is a different check"


@pytest.mark.asyncio
async def test_an_unknown_or_expired_token_is_none(monkeypatch):
    assert await ux.poll_check("nope") is None
    factory, _ = _slow({"ok": True, "message": "x"}, 0)
    await ux.run_check("tts", "k", factory)
    token = next(iter(ux._CHECKS))
    monkeypatch.setattr(ux, "_CHECK_KEEP_SECONDS", -1)
    await ux.run_check("stt", "k2", factory)               # any new check prunes the expired ones
    assert await ux.poll_check(token) is None


@pytest.mark.asyncio
async def test_a_check_that_blows_up_is_a_failed_result_not_a_500():
    async def boom():
        raise RuntimeError("kaboom")
    out = await ux.run_check("tts", "k", boom)
    assert out["ok"] is False and "kaboom" in out["message"]


# ── the budget ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_hung_studio_ends_in_a_timeout_that_says_how_long_and_where_to_look(monkeypatch):
    _reset_auth_state(monkeypatch)

    async def hang(request):
        await asyncio.sleep(5)

    real = ux._httpx.AsyncClient

    class _Hanging:
        def __init__(self, *a, **k):
            self._c = real(transport=httpx.MockTransport(hang), timeout=10)
        async def __aenter__(self):
            return self._c
        async def __aexit__(self, *a):
            await self._c.aclose()

    monkeypatch.setattr(ux._httpx, "AsyncClient", _Hanging)
    monkeypatch.setattr(ux, "effective_llm_url", lambda: "http://studio")
    monkeypatch.setattr(ux, "effective_llm_api_key", lambda: "k")
    out = await ux.tts_health("m", voice="v", timeout=0.1)
    assert out["ok"] is False
    assert "0 s" in out["message"] or "0.1" in out["message"] or "1 s" in out["message"] or "within" in out["message"]
    assert "docker logs" in out["message"] and "UNSLOTH_SLOW_CHECK_TIMEOUT_SECONDS" in out["message"]


def test_the_background_budget_is_much_longer_than_the_synchronous_one():
    assert ux._SLOW_CHECK_TIMEOUT_SECONDS >= 240 > ux._HEALTHCHECK_TIMEOUT_SECONDS


def test_one_request_never_waits_as_long_as_a_tunnel_allows():
    """The real default (this file patches the live value small): a Cloudflare Tunnel closes a request at ~100 s."""
    assert ux._DEFAULT_CHECK_WAIT_SECONDS <= 30


# ── the routes ───────────────────────────────────────────────────────────────

def _gm(client, seed, monkeypatch):
    monkeypatch.setattr("app.routers.ai._unsloth_or_400", lambda: None)
    login(client, seed.gm.email, GM_PASSWORD)


def test_the_check_route_returns_pending_then_the_result_through_the_poll_route(client, seed, monkeypatch):
    async def slow_tts_health(model, voice="", instructions="", language="", timeout=None):
        await asyncio.sleep(0.4)
        return {"ok": True, "message": "Studio synthesized 5 bytes - model is ready."}

    monkeypatch.setattr("app.routers.ai._unsloth_extras.tts_health", slow_tts_health)
    _gm(client, seed, monkeypatch)
    r = client.post("/api/ai/unsloth/tts-check", json={"model": "unsloth/orpheus-3b-0.1-ft-GGUF"})
    assert r.status_code == 200 and r.json()["pending"] is True
    token = r.json()["token"]
    final = {}
    for _ in range(30):
        final = client.get(f"/api/ai/unsloth/check/{token}").json()
        if not final.get("pending"):
            break
    assert final == {"ok": True, "message": "Studio synthesized 5 bytes - model is ready."}


def test_the_poll_route_404s_for_a_token_it_does_not_know_and_is_gm_only(client, seed, monkeypatch):
    _gm(client, seed, monkeypatch)
    assert client.get("/api/ai/unsloth/check/not-a-token").status_code == 404
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/unsloth/check/not-a-token").status_code == 403


def test_the_settings_page_polls_a_pending_check():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app/templates/settings.html").read_text()
    assert "/api/ai/unsloth/check/" in src and "d.pending" in src
