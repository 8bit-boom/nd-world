"""Tests for POST /api/ai/stream's permission gate (app/routers/ai.py).

Unlike the download toggles, this is a single axis: GM always allowed, a
player only once the GM opts in per world via World.players_can_ask_ai (off
by default). Ollama itself is mocked out — these tests only exercise the
permission check and the SSE plumbing around it, not the model.
"""
from app import ai as ai_module
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


async def _fake_resolve_model(requested):
    return requested or "fake-model"


async def _fake_stream_chat(messages, system="", model=""):
    for tok in ["Hello", " world"]:
        yield tok


def _patch_ai(monkeypatch):
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)


def test_ai_stream_gm_always_allowed(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "Hello" in r.text
    assert "[DONE]" in r.text


def test_ai_stream_player_denied_by_default(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403


def test_ai_stream_player_allowed_once_gm_enables_it(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "Hello" in r.text


def test_ai_stream_player_toggle_is_per_world(client, seed, monkeypatch):
    """The toggle is per-world — enabling it for World A must not leak
    access to a player in World B."""
    _patch_ai(monkeypatch)
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_b.email, PLAYER_PASSWORD)  # player_b is only a member of world_b
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.post("/api/ai/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
