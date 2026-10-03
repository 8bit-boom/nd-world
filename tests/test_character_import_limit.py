"""How much of an uploaded character sheet the AI creator reads — and the GM raising that limit.

The creator used to read the first 12,000 characters of an imported sheet and silently drop the rest, so a long sheet (a
full Hunt in the Moonlight page is already ~11.6k) lost its tail. The limit is now a Settings -> System value the GM can
raise (default 12,000, 2,000..60,000), takes effect on the next import without a restart, and a sheet that WAS cut
says so — to the model and to the player."""
import io
import time
from types import SimpleNamespace

import pytest

from app import ai as ai_module
from app.database import SessionLocal, get_app_settings
from app.routers import character_ai

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

DEFAULT = 12000


def _settings_post(client, **extra):
    data = {"ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "ollama_keep_alive": "",
            "ollama_use_mmap": ""}
    data.update(extra)
    return client.post("/settings/system", data=data, follow_redirects=False)


def _stored():
    db = SessionLocal()
    try:
        return get_app_settings(db).character_import_max_chars
    finally:
        db.close()


# ── the number ──────────────────────────────────────────────────────────────────────────────────

def test_default_is_twelve_thousand_and_a_saved_value_wins():
    f = character_ai.character_import_limit
    assert f(SimpleNamespace(character_import_max_chars=None)) == DEFAULT
    assert f(SimpleNamespace()) == DEFAULT
    assert f(SimpleNamespace(character_import_max_chars=30000)) == 30000


def test_a_hand_edited_value_outside_the_range_is_clamped_not_trusted():
    f = character_ai.character_import_limit
    assert f(SimpleNamespace(character_import_max_chars=5)) == 2000
    assert f(SimpleNamespace(character_import_max_chars=10**9)) == 60000
    assert f(SimpleNamespace(character_import_max_chars="junk")) == DEFAULT


# ── the setting (Settings -> System) ────────────────────────────────────────────────────────────

def test_gm_can_raise_it_and_clear_it(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert _settings_post(client, character_import_max_chars="30000").status_code == 303
    assert _stored() == 30000
    assert _settings_post(client, character_import_max_chars="").status_code == 303        # blank = back to the default
    assert _stored() is None


@pytest.mark.parametrize("bad", ["1999", "60001", "abc", "-5", "12.5"])
def test_out_of_range_or_junk_is_rejected_with_a_clear_message(client, seed, bad):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _settings_post(client, character_import_max_chars=bad)
    assert r.status_code == 400
    assert "sheet" in r.text.lower()
    assert _stored() is None


def test_players_cannot_change_it(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert _settings_post(client, character_import_max_chars="50000").status_code == 403
    assert _stored() is None


def test_the_settings_page_shows_the_field_with_guidance(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    _settings_post(client, character_import_max_chars="24000")
    page = client.get("/settings?tab=system").text
    assert 'name="character_import_max_chars"' in page and 'value="24000"' in page
    assert "12000" in page or "12,000" in page                       # the default is shown as the placeholder
    low = page.lower()
    assert "context window" in low and "imported" in low


# ── the effect on an import ─────────────────────────────────────────────────────────────────────

def _long_sheet(chars, tail="TAIL-OF-THE-SHEET-REACHED"):
    body = ("Line of a very long character sheet with plenty of words in it.\n" * (chars // 62 + 1))[: chars - len(tail) - 1]
    return (body + "\n" + tail).encode()


def _patch_gen(monkeypatch, seen):
    async def fake_generate_chat(messages, **kw):
        seen["user_text"] = messages[0]["content"]
        return '{"name": "Imported", "race": "Elf", "char_class": "Bard", "level": 3, "stats": {}, "backstory": "x"}'

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "sk-test")


def _poll(client, job_id, seconds=10):
    end = time.time() + seconds
    data = {"status": "running"}
    while time.time() < end:
        data = client.get(f"/api/characters/ai/{job_id}").json()
        if data["status"] != "running":
            break
        time.sleep(0.05)
    return data


def _import(client, seed, sheet, monkeypatch, name="long.txt"):
    seen = {}
    _patch_gen(monkeypatch, seen)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", files={"file": (name, io.BytesIO(sheet), "text/plain")},
                    data={"prompt": "", "use_rag": "false"})
    assert r.status_code == 200, r.text
    assert _poll(client, r.json()["job_id"])["status"] == "done"
    return r.json(), seen["user_text"]


def test_a_long_sheet_is_cut_at_the_default_and_the_model_is_told(client, seed, monkeypatch):
    info, sent = _import(client, seed, _long_sheet(25000), monkeypatch)
    assert "TAIL-OF-THE-SHEET-REACHED" not in sent
    assert "was cut off to fit" in sent
    assert info["source_limit"] == DEFAULT and info["source_chars"] >= 25000 - 5 and info["source_truncated"] is True


def test_after_the_gm_raises_the_limit_the_whole_sheet_goes_through(client, seed, monkeypatch):
    login(client, seed.gm.email, GM_PASSWORD)
    _settings_post(client, character_import_max_chars="30000")
    client.cookies.clear()
    info, sent = _import(client, seed, _long_sheet(25000), monkeypatch)
    assert "TAIL-OF-THE-SHEET-REACHED" in sent and "was cut off to fit" not in sent
    assert info["source_limit"] == 30000 and info["source_truncated"] is False


def test_a_short_sheet_reports_no_truncation(client, seed, monkeypatch):
    info, sent = _import(client, seed, b"# Bardoc\nDex 16", monkeypatch, name="short.md")
    assert info["source_truncated"] is False and info["source_chars"] == len(b"# Bardoc\nDex 16")


def test_a_prompt_only_start_has_no_source_info_to_show(client, seed, monkeypatch):
    _patch_gen(monkeypatch, {})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", data={"prompt": "a rogue", "use_rag": "false"})
    assert r.status_code == 200
    assert r.json().get("source_truncated") is False


def test_the_creator_page_warns_when_a_sheet_was_cut(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page = client.get("/characters/ai-new").text
    assert "source_truncated" in page and "source_limit" in page
