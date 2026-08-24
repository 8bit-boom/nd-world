"""Tests for POST /api/ai/imagegen/generate (app/routers/ai.py):
- a blank model falls back to the GM's configured "image" surface default
  (mirroring /stream's chat-surface fallback), instead of reaching
  imagegen_generate with an empty model string — this is what makes the
  Illustrate button (which never sends a model at all) pick up the
  configured default too, with no client-side change needed.
- a backend failure now returns a real error status (not 200 with an
  "error" key in the body, which a curl/monitoring caller — or anything
  that trusts HTTP status codes — would silently treat as success).
"""
import pytest

from app import ai as ai_module

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    """app.ai persists per-surface defaults to a JSON file next to the DB,
    not the DB itself — point it at a throwaway path per test so tests
    can't see each other's saved defaults."""
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


def _gen_body(**overrides):
    body = {"prompt": "a neon dragon"}
    body.update(overrides)
    return body


def test_blank_model_falls_back_to_configured_image_default(client, seed, monkeypatch):
    captured = {}

    async def fake_imagegen_generate(**kwargs):
        captured.update(kwargs)
        return ["/uploads/generated/x.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_imagegen_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/api/ai/defaults", json={"surface": "image", "model_id": "configured-image-model"})

    r = client.post("/api/ai/imagegen/generate", json=_gen_body())
    assert r.status_code == 200
    assert captured["model"] == "configured-image-model"


def test_explicit_model_overrides_the_default(client, seed, monkeypatch):
    captured = {}

    async def fake_imagegen_generate(**kwargs):
        captured.update(kwargs)
        return ["/uploads/generated/x.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_imagegen_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/api/ai/defaults", json={"surface": "image", "model_id": "configured-image-model"})

    r = client.post("/api/ai/imagegen/generate", json=_gen_body(model="explicit-model"))
    assert r.status_code == 200
    assert captured["model"] == "explicit-model"


def test_no_configured_default_leaves_model_blank(client, seed, monkeypatch):
    captured = {}

    async def fake_imagegen_generate(**kwargs):
        captured.update(kwargs)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_imagegen_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/generate", json=_gen_body())
    assert r.status_code == 200
    assert captured["model"] == ""


def test_backend_failure_returns_error_status_not_200(client, seed, monkeypatch):
    async def failing_imagegen_generate(**kwargs):
        raise RuntimeError("Cannot reach SwarmUI at http://127.0.0.1:7801")
    monkeypatch.setattr(ai_module, "imagegen_generate", failing_imagegen_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/ai/imagegen/generate", json=_gen_body())
    assert r.status_code != 200
    assert "SwarmUI" in r.json()["detail"]
