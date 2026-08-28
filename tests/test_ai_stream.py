"""Tests for POST /api/ai/stream's permission gate (app/routers/ai.py).

Unlike the download toggles, this is a single axis: GM always allowed, a
player only once the GM opts in per world via World.players_can_ask_ai (off
by default). Ollama itself is mocked out — these tests only exercise the
permission check and the SSE plumbing around it, not the model.
"""
import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    """app.ai persists custom models / hidden ids / per-surface defaults to a
    JSON file next to the DB, not the DB itself — point it at a throwaway
    path per test so tests can't see each other's saved defaults."""
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


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
    return requested or "fake-model", None


async def _fake_stream_chat(messages, system="", model="", options=None):
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


# ── Per-surface default models (app/ai.py's get_defaults/set_default) ──────

def test_ai_defaults_empty_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/defaults")
    assert r.status_code == 200
    assert r.json() == {"chat": "", "ask_ai": "", "image": "", "recap": ""}


def test_ai_defaults_set_and_get_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/defaults", json={"surface": "ask_ai", "model_id": "gemma4:26b"})
    assert r.status_code == 200
    assert r.json()["defaults"]["ask_ai"] == "gemma4:26b"
    r = client.get("/api/ai/defaults")
    assert r.json() == {"chat": "", "ask_ai": "gemma4:26b", "image": "", "recap": ""}


def test_ai_defaults_rejects_unknown_surface(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/defaults", json={"surface": "bogus", "model_id": "x"})
    assert r.status_code == 400


def test_ai_defaults_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/ai/defaults")
    assert r.status_code == 403
    r = client.post("/api/ai/defaults", json={"surface": "chat", "model_id": "x"})
    assert r.status_code == 403


def test_ai_stream_falls_back_to_surface_default_when_model_blank(client, seed, monkeypatch):
    """A request with no explicit model uses the configured per-surface
    default instead of always falling through to the single system-wide
    default, so Chat and Ask AI can run different models."""
    captured = {}

    async def _capturing_resolve_model(requested):
        captured["requested"] = requested
        return "resolved-model", None
    monkeypatch.setattr(ai_module, "resolve_model", _capturing_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/api/ai/defaults", json={"surface": "ask_ai", "model_id": "ask-ai-model"})
    r = client.post("/api/ai/stream", json={
        "messages": [{"role": "user", "content": "hi"}], "surface": "ask_ai",
    })
    assert r.status_code == 200
    assert captured["requested"] == "ask-ai-model"
