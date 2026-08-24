"""Tests for the GM-editable, per-world prompt library (PromptPreset in
app/models.py, routes in app/routers/ai.py) — backs both AI Chat's Quick
Prompts sidebar (scope="chat", replacing a hardcoded list that used to fire
immediately on click) and Image Studio's Prompt Presets (scope="image",
replacing localStorage-only storage that vanished on a different browser).
"""
from app.database import SessionLocal
from app.models import PromptPreset

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_chat_scope_seeds_defaults_on_first_fetch(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/prompt-presets?scope=chat")
    assert r.status_code == 200
    presets = r.json()["presets"]
    assert len(presets) == 8
    assert presets[0]["label"] == "Random NPC"
    assert presets[0]["icon"] == "👤"

    db = SessionLocal()
    try:
        assert db.query(PromptPreset).filter(PromptPreset.world_id == seed.world_a.id, PromptPreset.scope == "chat").count() == 8
    finally:
        db.close()

    # A second fetch doesn't re-seed on top of the existing rows.
    r2 = client.get("/api/ai/prompt-presets?scope=chat")
    assert len(r2.json()["presets"]) == 8


def test_image_scope_starts_empty_no_seeding(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/prompt-presets?scope=image")
    assert r.status_code == 200
    assert r.json()["presets"] == []


def test_create_and_delete_image_preset(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/prompt-presets", json={
        "scope": "image", "label": "Neon portrait", "text": "cyberpunk portrait, neon lighting", "negative": "blurry",
    })
    assert r.status_code == 200
    preset_id = r.json()["id"]

    r = client.get("/api/ai/prompt-presets?scope=image")
    presets = r.json()["presets"]
    assert len(presets) == 1
    assert presets[0]["label"] == "Neon portrait"
    assert presets[0]["negative"] == "blurry"

    r = client.delete(f"/api/ai/prompt-presets/{preset_id}")
    assert r.status_code == 200
    assert client.get("/api/ai/prompt-presets?scope=image").json()["presets"] == []


def test_create_chat_preset_with_icon(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/prompt-presets", json={
        "scope": "chat", "label": "Campaign hook", "icon": "🎯", "text": "Write a hook tying into the corp war.",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["icon"] == "🎯"
    assert data["text"] == "Write a hook tying into the corp war."


def test_create_rejects_invalid_scope(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/prompt-presets", json={"scope": "bogus", "label": "x"})
    assert r.status_code == 400


def test_create_rejects_blank_label(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/prompt-presets", json={"scope": "chat", "label": "   "})
    assert r.status_code == 400


def test_list_rejects_invalid_scope(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/prompt-presets?scope=bogus")
    assert r.status_code == 400


def test_delete_404s_across_worlds(client, seed):
    """A GM whose active world is world_a can't delete a preset that
    belongs to world_b just by knowing its id."""
    db = SessionLocal()
    try:
        p = PromptPreset(world_id=seed.world_b.id, scope="chat", label="World B only", text="x")
        db.add(p)
        db.commit()
        db.refresh(p)
        preset_id = p.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/ai/prompt-presets/{preset_id}")
    assert r.status_code == 404

    db = SessionLocal()
    try:
        assert db.get(PromptPreset, preset_id) is not None
    finally:
        db.close()


def test_presets_scoped_per_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/prompt-presets", json={"scope": "image", "label": "World A preset", "text": "x"})

    client.cookies.set("active_world", seed.world_b.slug)
    presets = client.get("/api/ai/prompt-presets?scope=image").json()["presets"]
    assert presets == []


def test_player_cannot_manage_prompt_library(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/prompt-presets?scope=chat").status_code == 403
    assert client.post("/api/ai/prompt-presets", json={"scope": "chat", "label": "x", "text": "y"}).status_code == 403
    assert client.delete("/api/ai/prompt-presets/1").status_code == 403
