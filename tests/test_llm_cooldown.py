"""Tests for the player-facing LLM cooldown guard (app/deps.py's
check_llm_cooldown) — a player mashing Chronicler's ask button, or the
session log's own Recap button, used to fire one real Ollama generation
per click with nothing to slow it down. GM callers are exempt, matching
every other player-facing AI gate in this app."""
from app.database import SessionLocal
from app.models import Fact, GameSession

from app import ai as ai_module
from app.deps import check_llm_cooldown

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_session(world_id):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _add_fact(world_id, session_id, content):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=world_id, game_session_id=session_id, content=content, visible_to_players=True))
        db.commit()
    finally:
        db.close()


def test_check_llm_cooldown_unit():
    # Large, unlikely-to-collide fake ids — real seeded test users always
    # get small autoincrement ids (the client fixture drops/recreates
    # tables every test), so these can't accidentally inherit state left
    # by an unrelated test elsewhere in the suite.
    check_llm_cooldown(999901)  # first call — no cooldown yet
    try:
        check_llm_cooldown(999901)
        assert False, "second call within the cooldown window should have raised"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 429
    check_llm_cooldown(999902)  # a different user id is unaffected


def test_check_llm_cooldown_respects_a_custom_window():
    check_llm_cooldown(999903, seconds=0)  # a zero-second window never blocks a follow-up call
    check_llm_cooldown(999903, seconds=0)


def test_chronicler_ask_second_call_from_same_player_is_rate_limited(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "An answer."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r1 = client.post("/api/chronicler/ask", json={"question": "anything?"})
    assert r1.status_code == 200
    r2 = client.post("/api/chronicler/ask", json={"question": "anything again?"})
    assert r2.status_code == 429


def test_chronicler_ask_gm_is_never_rate_limited(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "An answer."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for _ in range(3):
        r = client.post("/api/chronicler/ask", json={"question": "anything?"})
        assert r.status_code == 200


def test_session_log_recap_second_call_from_same_player_is_rate_limited(client, seed, monkeypatch):
    """Two DIFFERENT sessions, not a repeat of the same one — a repeat now
    legitimately hits the session-log recap cache (see AI 1.9 / tests/
    test_ai_answer_caching.py) rather than the cooldown gate, since serving
    a cached answer costs nothing. The cooldown still has to apply across
    two genuinely different (uncached) generation requests in the window,
    which is what this asserts."""
    session_id_1 = _make_session(seed.world_a.id)
    session_id_2 = _make_session(seed.world_a.id)
    _add_fact(seed.world_a.id, session_id_1, "The party arrived.")
    _add_fact(seed.world_a.id, session_id_2, "The party left.")

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        return "A recap."
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r1 = client.post(f"/api/session-log/{session_id_1}/recap")
    assert r1.status_code == 200
    r2 = client.post(f"/api/session-log/{session_id_2}/recap")
    assert r2.status_code == 429
