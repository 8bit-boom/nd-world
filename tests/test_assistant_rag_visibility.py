"""Regression tests: GM-Assistants (WorldMembership.role == "assistant")
must get the same visibility-filtered/[gmonly]-stripped context a player
would, per AGENTS.md's own rule ("Assistants always see what players
see: visibility filters stay keyed on is_gm") — across every RAG-ish
route reachable from _is_assistant_safe. Before this fix, five routes
built their context unfiltered regardless of caller, so an Assistant
(who is NOT a GM — WorldMembership.role is a separate, per-world grant
from User.is_gm) could pull hidden entities and [gmonly] secrets straight
into an AI-assist call.

Covers: GET /api/ai/world-context, POST /api/ai/world-context-smart,
POST /api/ai/generate/entity-smart, POST /api/ai/assist,
POST /api/ai/assist-job (RAG force-disabled there, matching the
already-correct world-summary precedent — see that route's own
docstring for why _build_rag_context specifically can't be filtered).
"""
from app.database import SessionLocal
from app.models import Entity, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_assistant(seed, player):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == player.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


def _seed_entities(world_id):
    db = SessionLocal()
    try:
        db.add(Entity(
            world_id=world_id, kind="character", name="Hidden Villain",
            summary="A secret mastermind.", body="Full backstory.",
            visible_to_players=False,
        ))
        db.add(Entity(
            world_id=world_id, kind="character", name="Public Merchant",
            summary="Sells trinkets. [gmonly]Secretly a smuggler.[/gmonly]",
            body="Runs a stall. [gmonly]Hidden compartment holds contraband.[/gmonly]",
            visible_to_players=True,
        ))
        db.add(Entity(
            world_id=world_id, kind="note", name="GM Prep Notes",
            summary="Session prep.", body="Full plot twist details.",
            visible_to_players=False,
        ))
        db.commit()
    finally:
        db.close()


def _login_assistant(client, seed):
    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── GET /api/ai/world-context ────────────────────────────────────────────────

def test_world_context_hides_hidden_entities_from_assistant(client, seed):
    _seed_entities(seed.world_a.id)
    _login_assistant(client, seed)
    r = client.get("/api/ai/world-context")
    assert r.status_code == 200
    ctx = r.json()["context"]
    assert "Hidden Villain" not in ctx
    assert "Public Merchant" in ctx
    assert "Secretly a smuggler" not in ctx
    assert "GM Prep Notes" not in ctx


def test_world_context_gm_sees_everything(client, seed):
    _seed_entities(seed.world_a.id)
    _login_gm(client, seed)
    r = client.get("/api/ai/world-context")
    assert r.status_code == 200
    ctx = r.json()["context"]
    assert "Hidden Villain" in ctx
    assert "Secretly a smuggler" in ctx
    assert "GM Prep Notes" in ctx


# ── POST /api/ai/world-context-smart ─────────────────────────────────────────

def test_world_context_smart_hides_secrets_from_assistant(client, seed):
    _seed_entities(seed.world_a.id)
    _login_assistant(client, seed)
    r = client.post("/api/ai/world-context-smart", json={"query": "villain merchant smuggler"})
    assert r.status_code == 200
    ctx = r.json()["context"]
    assert "Hidden Villain" not in ctx
    assert "Secretly a smuggler" not in ctx


def test_world_context_smart_gm_sees_secrets(client, seed):
    _seed_entities(seed.world_a.id)
    _login_gm(client, seed)
    r = client.post("/api/ai/world-context-smart", json={"query": "villain merchant smuggler"})
    assert r.status_code == 200
    ctx = r.json()["context"]
    assert "Hidden Villain" in ctx or "Secretly a smuggler" in ctx


# ── POST /api/ai/generate/entity-smart ───────────────────────────────────────

def test_generate_entity_smart_hides_secrets_from_assistant(client, seed, monkeypatch):
    _seed_entities(seed.world_a.id)
    captured = {}

    async def fake_generate(prompt, system=None):
        captured["prompt"] = prompt
        return "generated text"

    import app.main as main_module
    monkeypatch.setattr(main_module._ai_module, "generate", fake_generate)

    _login_assistant(client, seed)
    r = client.post("/api/ai/generate/entity-smart", json={
        "name": "New NPC", "kind": "character", "summary": "villain merchant smuggler",
    })
    assert r.status_code == 200
    assert "Hidden Villain" not in captured["prompt"]
    assert "Secretly a smuggler" not in captured["prompt"]


# ── POST /api/ai/assist ──────────────────────────────────────────────────────

def test_assist_world_context_hides_secrets_from_assistant(client, seed, monkeypatch):
    _seed_entities(seed.world_a.id)
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["world_context"] = kwargs.get("world_context", "")
        return {"mode": "text", "text": "ok"}

    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._ai_assist, "run_assist", fake_run_assist)

    _login_assistant(client, seed)
    r = client.post("/api/ai/assist", json={
        "op": "improve", "kind": "character", "name": "Draft",
        "body": "villain merchant smuggler", "use_rag": True,
    })
    assert r.status_code == 200
    assert "Hidden Villain" not in captured["world_context"]
    assert "Secretly a smuggler" not in captured["world_context"]


def test_assist_world_context_gm_sees_secrets(client, seed, monkeypatch):
    _seed_entities(seed.world_a.id)
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["world_context"] = kwargs.get("world_context", "")
        return {"mode": "text", "text": "ok"}

    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._ai_assist, "run_assist", fake_run_assist)

    _login_gm(client, seed)
    r = client.post("/api/ai/assist", json={
        "op": "improve", "kind": "character", "name": "Draft",
        "body": "villain merchant smuggler", "use_rag": True,
    })
    assert r.status_code == 200
    assert "Hidden Villain" in captured["world_context"] or "Secretly a smuggler" in captured["world_context"]


# ── POST /api/ai/assist-job ──────────────────────────────────────────────────

def test_assist_job_force_disables_rag_for_assistant(client, seed, monkeypatch):
    _seed_entities(seed.world_a.id)
    captured = {}

    def fake_create_assist_job(world_id, **kwargs):
        captured.update(kwargs)
        return 1

    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._audio_jobs, "create_assist_job", fake_create_assist_job)

    _login_assistant(client, seed)
    r = client.post("/api/ai/assist-job", json={
        "op": "improve", "kind": "character", "name": "Draft",
        "body": "villain merchant smuggler", "use_rag": True,
    })
    assert r.status_code == 200
    assert captured["use_rag"] is False


def test_assist_job_keeps_rag_for_gm(client, seed, monkeypatch):
    _seed_entities(seed.world_a.id)
    captured = {}

    def fake_create_assist_job(world_id, **kwargs):
        captured.update(kwargs)
        return 1

    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router._audio_jobs, "create_assist_job", fake_create_assist_job)

    _login_gm(client, seed)
    r = client.post("/api/ai/assist-job", json={
        "op": "improve", "kind": "character", "name": "Draft",
        "body": "villain merchant smuggler", "use_rag": True,
    })
    assert r.status_code == 200
    assert captured["use_rag"] is True
