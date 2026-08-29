"""Tests for the player-accessible Chronicler chat — the security boundary
is that GM-only fact/entity content never enters the prompt built for a
player's question, not just that the model is told to withhold it."""
from datetime import datetime, timedelta

from app import ai as ai_module
from app.database import SessionLocal
from app.models import Entity, Fact
from app.routers import chronicler as chronicler_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _seed_facts_and_entity(world):
    db = SessionLocal()
    try:
        db.add_all([
            Fact(world_id=world.id, content="The party arrived in Neon City.", visible_to_players=True),
            Fact(world_id=world.id, content="Elyra is secretly a cult agent.", visible_to_players=False),
        ])
        db.add(Entity(world_id=world.id, kind="character", name="Elyra", summary="A tavern owner",
                       visible_to_players=True))
        db.add(Entity(world_id=world.id, kind="character", name="Secret Cult Leader", summary="GM-only villain",
                       visible_to_players=False))
        db.commit()
    finally:
        db.close()


def test_chronicler_page_reachable_by_player(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/chronicler")
    assert r.status_code == 200


def test_chronicler_page_reachable_by_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/chronicler")
    assert r.status_code == 200


def test_player_question_excludes_gm_only_content(client, seed, monkeypatch):
    _seed_facts_and_entity(seed.world_a)
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["system"] = system
        return "Here is what I know."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/chronicler/ask", json={"question": "Tell me about the secret cult leader and Elyra"})
    assert r.status_code == 200
    assert r.json()["answer"] == "Here is what I know."

    system = captured["system"]
    assert "The party arrived in Neon City." in system
    assert "Elyra is secretly a cult agent." not in system
    assert "Secret Cult Leader" not in system
    assert "Elyra" in system  # the visible entity is still included


def test_gm_question_includes_gm_only_content(client, seed, monkeypatch):
    _seed_facts_and_entity(seed.world_a)
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["system"] = system
        return "Full truth revealed."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/chronicler/ask", json={"question": "Tell me about the secret cult leader and Elyra"})
    assert r.status_code == 200

    system = captured["system"]
    assert "Elyra is secretly a cult agent." in system
    assert "Secret Cult Leader" in system


def test_chronicler_scoped_to_active_world(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_b.id, content="World B only fact.", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["system"] = system
        return "ok"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/chronicler/ask", json={"question": "anything?"})
    assert r.status_code == 200
    assert "World B only fact." not in captured["system"]


# ── Wave 3 (token-usage plan item 2.2): context sizing + fact cap ──────────

def test_chronicler_ask_passes_context_sized_options(client, seed, monkeypatch):
    """Without this, a large assembled system prompt (many facts + entity
    excerpts) silently overflowing the GM's configured/assumed num_ctx gets
    truncated by Ollama instead of raising — see condense_call_options' own
    docstring for the garbage-output failure mode this exists to prevent."""
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["options"] = options
        captured["system"] = system
        return "An answer."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})
    assert r.status_code == 200
    assert captured["options"] is not None
    assert "num_ctx" in captured["options"]

    expected = ai_module.context_sized_options(captured["system"] + "Who is Elyra?")
    assert captured["options"] == expected


def test_chronicler_fact_prompt_is_capped_and_newest_first(client, seed, monkeypatch):
    limit = chronicler_module._CHRONICLER_FACT_LIMIT
    total = limit + 5  # more facts than the cap, so some are provably excluded
    db = SessionLocal()
    try:
        db.add_all([
            # Explicit, strictly increasing created_at — Fact.created_at.desc()
            # ordering must be deterministic here, not rely on facts inserted
            # in the same loop happening to get distinct default timestamps.
            Fact(
                world_id=seed.world_a.id, content=f"Fact number {i}.", visible_to_players=True,
                created_at=datetime(2024, 1, 1) + timedelta(minutes=i),
            )
            for i in range(total)
        ])
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["system"] = system
        return "An answer."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/chronicler/ask", json={"question": "anything?"})
    assert r.status_code == 200
    system = captured["system"]
    included = sum(1 for i in range(total) if f"Fact number {i}." in system)
    assert included == limit
    # Newest-first ordering (Fact.created_at.desc()) means the highest-
    # numbered (most recently inserted) facts survive the cap, not the
    # earliest ones.
    for i in range(total - limit, total):
        assert f"Fact number {i}." in system
    for i in range(total - limit):
        assert f"Fact number {i}." not in system
