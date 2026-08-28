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
        "android_emulator_url": "",
    })
    assert r.status_code == 403


def test_settings_system_roundtrip(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "llama3.1",
        "ollama_url": "http://127.0.0.1:11500",
        "swarmui_external_url": "http://127.0.0.1:7801",
        "android_emulator_url": "http://127.0.0.1:6080",
        "editor_external_url": "http://127.0.0.1:6081",
        "whisper_url": "http://127.0.0.1:8090",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?tab=system"

    page = client.get("/settings?tab=system")
    assert "llama3.1" in page.text
    assert "127.0.0.1:11500" in page.text
    assert "127.0.0.1:7801" in page.text
    assert "127.0.0.1:6080" in page.text
    assert "127.0.0.1:6081" in page.text
    assert "127.0.0.1:8090" in page.text

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_model == "llama3.1"
        assert settings.ollama_url == "http://127.0.0.1:11500"
        assert settings.swarmui_external_url == "http://127.0.0.1:7801"
        assert settings.android_emulator_url == "http://127.0.0.1:6080"
        assert settings.editor_external_url == "http://127.0.0.1:6081"
        assert settings.whisper_url == "http://127.0.0.1:8090"
        assert settings.dreamlands_enabled is False
        assert settings.king_in_yellow_enabled is False
    finally:
        db.close()


def test_lore_extras_toggles_default_off_and_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)

    # Unchecked checkboxes aren't sent by a real browser at all — omitting
    # the keys must persist as disabled, not error or leave a stale value.
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "android_emulator_url": "", "editor_external_url": "",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.dreamlands_enabled is False
        assert settings.king_in_yellow_enabled is False
    finally:
        db.close()

    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "android_emulator_url": "", "editor_external_url": "",
        "dreamlands_enabled": "1", "king_in_yellow_enabled": "1",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.dreamlands_enabled is True
        assert settings.king_in_yellow_enabled is True
    finally:
        db.close()

    page = client.get("/settings?tab=system")
    assert 'name="dreamlands_enabled"' in page.text
    assert 'name="king_in_yellow_enabled"' in page.text


def test_settings_system_invalid_url_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "not-a-url", "swarmui_external_url": "",
        "android_emulator_url": "",
    })
    assert r.status_code == 400


def test_settings_system_invalid_android_url_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "android_emulator_url": "not-a-url",
    })
    assert r.status_code == 400

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert not settings or settings.android_emulator_url in (None, "")
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


def test_whisper_env_fallback_when_blank(client, seed):
    assert ai_module.effective_whisper_url() == ai_module.WHISPER_URL


def test_whisper_override_takes_effect_after_save(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "whisper_url": "http://127.0.0.1:8090",
    })
    assert ai_module.effective_whisper_url() == "http://127.0.0.1:8090"


def test_settings_system_invalid_whisper_url_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "whisper_url": "not-a-url",
    })
    assert r.status_code == 400

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert not settings or settings.whisper_url in (None, "")
    finally:
        db.close()


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


def test_android_env_fallback_when_blank(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    from app.main import ANDROID_EMULATOR_URL
    r = client.get("/androidapp")
    assert r.status_code == 200
    if ANDROID_EMULATOR_URL:
        assert ANDROID_EMULATOR_URL in r.text
    else:
        assert "not configured" in r.text.lower()


def test_android_override_reflected_in_androidapp(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "android_emulator_url": "http://127.0.0.1:6080",
    })
    r = client.get("/androidapp")
    assert r.status_code == 200
    assert "127.0.0.1:6080" in r.text


def test_androidapp_accessible_to_players(client, seed):
    """Unlike Image Studio/Settings (GM-only), the embedded Android app is
    meant for players — it's the whole point of not needing every player to
    install the APK — so a non-GM must be able to load /androidapp."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/androidapp")
    assert r.status_code == 200


def test_editor_env_fallback_when_blank(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    from app.main import EDITOR_EXTERNAL_URL
    r = client.get("/editor")
    assert r.status_code == 200
    if EDITOR_EXTERNAL_URL:
        assert EDITOR_EXTERNAL_URL in r.text
    else:
        assert "not configured" in r.text.lower()


def test_editor_override_reflected_in_editor_embed(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "android_emulator_url": "", "editor_external_url": "http://127.0.0.1:6081",
    })
    r = client.get("/editor")
    assert r.status_code == 200
    assert "127.0.0.1:6081" in r.text


def test_editor_is_gm_only(client, seed):
    """Unlike /androidapp, the embedded Content Editor is a GM-only tool —
    it edits world content, not something a player needs."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/editor")
    assert r.status_code == 403
