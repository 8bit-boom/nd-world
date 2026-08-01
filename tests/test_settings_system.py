"""The System/Integrations tab on /settings (GM-only) lets Ollama/SwarmUI config
be changed without a container restart. Covers the GM-only gate, the save
round-trip, URL validation, and that blank fields fall back to the env-var
defaults while non-blank ones take effect live (no restart) via app.ai's
effective_ollama_*() overrides and imagestudio()'s rendered iframe URL.
"""
from app import ai as ai_module
from app.database import SessionLocal
from app.models import AppSettings

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_settings_system_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/settings").status_code == 403
    r = client.post("/settings/system", data={
        "ollama_model": "llama3.1", "ollama_url": "http://127.0.0.1:11500", "swarmui_external_url": "",
    })
    assert r.status_code == 403


def test_settings_system_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "llama3.1",
        "ollama_url": "http://127.0.0.1:11500",
        "swarmui_external_url": "http://127.0.0.1:7801",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?tab=system"

    page = client.get("/settings?tab=system")
    assert "llama3.1" in page.text
    assert "127.0.0.1:11500" in page.text
    assert "127.0.0.1:7801" in page.text

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_model == "llama3.1"
        assert settings.ollama_url == "http://127.0.0.1:11500"
        assert settings.swarmui_external_url == "http://127.0.0.1:7801"
    finally:
        db.close()


def test_settings_system_invalid_url_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "not-a-url", "swarmui_external_url": "",
    })
    assert r.status_code == 400

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert not settings or settings.ollama_url in (None, "")
    finally:
        db.close()


def test_ollama_env_fallback_when_blank(client, seed):
    # AppSettings starts blank in a fresh test DB — effective_*() should return
    # the env-derived module constants, not blank strings.
    assert ai_module.effective_ollama_model() == ai_module.OLLAMA_MODEL
    assert ai_module.effective_ollama_url() == ai_module.OLLAMA_URL


def test_ollama_override_takes_effect_after_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "llama3.1", "ollama_url": "http://127.0.0.1:11500", "swarmui_external_url": "",
    })
    assert ai_module.effective_ollama_model() == "llama3.1"
    assert ai_module.effective_ollama_url() == "http://127.0.0.1:11500"

    r = client.get("/api/ai/models")
    assert r.status_code == 200
    assert r.json()["default"] == "llama3.1"


def test_swarmui_env_fallback_when_blank(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    from app.main import SWARMUI_EXTERNAL_URL
    r = client.get("/imagestudio")
    assert r.status_code == 200
    if SWARMUI_EXTERNAL_URL:
        assert SWARMUI_EXTERNAL_URL in r.text


def test_swarmui_override_reflected_in_imagestudio(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "http://127.0.0.1:7801",
    })
    r = client.get("/imagestudio")
    assert r.status_code == 200
    assert "127.0.0.1:7801" in r.text
