"""Tests for the player-facing AI character creator (create + import)."""
import io
import json
import time

from app import ai as ai_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _patch_gen(monkeypatch, reply):
    async def fake_generate_chat(messages, **kw):
        return reply(messages, kw)

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "UNSLOTH_API_KEY", "sk-test")


def _poll(client, job_id, seconds=10):
    deadline = time.time() + seconds
    data = {"status": "running"}
    while time.time() < deadline:
        data = client.get(f"/api/characters/ai/{job_id}").json()
        if data["status"] != "running":
            break
        time.sleep(0.05)
    return data


def test_page_requires_world_and_renders_guide(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/characters/ai-new")
    assert r.status_code == 200
    assert "Create a character with AI" in r.text
    assert "How this works" in r.text
    assert "Import a sheet" in r.text


def test_start_requires_prompt_or_file(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", data={"prompt": ""})
    assert r.status_code == 400


def test_create_flow_grounded_in_rules(client, seed, monkeypatch):
    """Prompt → draft following the WORLD's rules: rules markdown is in the
    prompt, thinking honored, numerics clamped to sheet ranges."""
    from app.models import World as W
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        w = db.get(W, seed.world_a.id)
        w.rules_md = "STATS: 8 stats, 3-8 each. HP = 10 + str."
        db.commit()
    finally:
        db.close()

    captured = {}

    def reply(messages, kw):
        captured["user_text"] = messages[0]["content"]
        captured["think"] = kw.get("think")
        return ('{"name": "Vex", "player_name": "Sam", "race": "Human", '
                '"char_class": "Fixer", "level": 99, "xp": -5, '
                '"stats": {"str": 4, "dex": 5, "bod": 999}, '
                '"max_hp": 14, "shock_max": 4, "backstory": "Born under neon.", '
                '"notes": "owes money", "equipment": [{"name": "Pistol", "qty": 2}]}')

    _patch_gen(monkeypatch, reply)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", data={
        "prompt": "grizzled ex-cop fixer", "think": "true", "use_rag": "false"})
    assert r.status_code == 200, r.text
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    draft = data["draft"]
    assert draft["name"] == "Vex"
    assert draft["level"] == 20          # clamped 99 → 20
    assert draft["xp"] == 0              # clamped
    assert draft["stats"]["bod"] == 30   # clamped to sheet range
    assert "STATS: 8 stats" in captured["user_text"]  # world rules grounded
    assert captured["think"] is True


def test_import_md_reaches_generator(client, seed, monkeypatch):
    seen = {}

    def reply(messages, kw):
        seen["user_text"] = messages[0]["content"]
        return '{"name": "Imported", "race": "Elf", "char_class": "Bard", "level": 3, "stats": {}, "backstory": "x"}'

    _patch_gen(monkeypatch, reply)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start",
                    files={"file": ("old-sheet.md", io.BytesIO(b"# Bardoc\nDex 16 charisma 14"), "text/markdown")},
                    data={"prompt": "translate to this world", "use_rag": "false"})
    assert r.status_code == 200, r.text
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    assert data["draft"]["name"] == "Imported"
    assert "Bardoc" in seen["user_text"]     # file text extracted + fed in
    assert "SOURCE SHEET" in seen["user_text"]


def test_import_docx_clean_error(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start",
                    files={"file": ("sheet.docx", io.BytesIO(b"PK\x03\x04junk"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                    data={"prompt": ""})
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"] or "paste" in r.json()["detail"]


def test_poll_is_starter_or_gm_only(client, seed, monkeypatch):
    def reply(messages, kw):
        return '{"name": "X", "stats": {}}'

    _patch_gen(monkeypatch, reply)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", data={"prompt": "a rogue", "use_rag": "false"})
    job_id = r.json()["job_id"]
    _poll(client, job_id)

    # a DIFFERENT player cannot poll someone else's job
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    assert client.get(f"/api/characters/ai/{job_id}").status_code == 403
    # the GM can
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/characters/ai/{job_id}").status_code == 200


def test_error_path(client, seed, monkeypatch):
    def reply(messages, kw):
        return "no json at all"

    _patch_gen(monkeypatch, reply)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/characters/ai/start", data={"prompt": "x", "use_rag": "false"})
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "error"
