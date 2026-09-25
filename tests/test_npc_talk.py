"""app.routers.npc_talk — persistent per-entity roleplay conversations.

These tests pin the security/correctness shape of the surface: the ask-AI
opt-in gate, per-viewer entity visibility, server-side [gmonly] stripping
in the composed prompt, the GM-only model/RAG rules, per-user conversation
isolation, and clean-finish-only persistence (a failure sentinel must
never become a saved reply). The AI itself is always monkeypatched —
same convention as tests/test_ai_assist.py.
"""
import json

import app.ai as ai_module
import app.retrieval as retrieval_module

from app.database import SessionLocal
from app.models import ChatSession, Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _npc(seed, **kw):
    db = SessionLocal()
    try:
        e = Entity(
            world_id=seed.world_a.id, kind=kw.get("kind", "character"),
            name=kw.get("name", "Elyra"), summary=kw.get("summary", ""),
            body=kw.get("body", ""), visible_to_players=kw.get("visible_to_players", True),
            custom_fields_json=kw.get("custom_fields_json") or "{}",
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _patch_ai(monkeypatch, reply="Greetings, traveler.", capture=None, error=None):
    """Patch resolve_model + stream_chat the way /api/ai/stream's own tests
    do; records the (msgs, system, model, options) the surface composed."""
    async def _resolve(model):
        if capture is not None:
            capture["requested_model"] = model
        return (model or "test-model"), ""

    async def _stream(msgs, system, model, options, think=False, emit_thinking=False):
        if capture is not None:
            capture["msgs"] = msgs
            capture["system"] = system
            capture["model"] = model
            capture["think"] = think
        if error is not None:
            yield {"type": "error", "text": error}
            return
        yield {"type": "token", "text": reply}

    monkeypatch.setattr(ai_module, "resolve_model", _resolve)
    monkeypatch.setattr(ai_module, "stream_chat", _stream)


def _pin(client, slug="world-a"):
    client.cookies.set("active_world", slug)


# ── GM happy path ─────────────────────────────────────────────────────────────

def test_gm_stream_replies_in_character_and_persists(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, reply="Hail.", capture=cap)
    eid = _npc(seed, body="Elyra is the harbor fox.")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "Hail." in r.text
    assert "data: [DONE]" in r.text
    # Roleplay directive + identity reach the model server-side
    assert "roleplaying AS" in cap["system"]
    assert "Elyra is the harbor fox." in cap["system"]
    assert cap["msgs"][-1]["content"] == "hello"

    # And the turn persisted for THIS user
    r = client.get(f"/api/npc-talk/{eid}/history")
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "Hail."


def test_player_blocked_without_world_opt_in(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    eid = _npc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.get("/npc-talk").status_code == 403
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 403


# ── Player access under the world opt-in ─────────────────────────────────────

def _opt_in(seed, column="players_can_ask_ai"):
    db = SessionLocal()
    try:
        w = db.get(type(seed.world_a), seed.world_a.id)
        setattr(w, column, True)
        db.commit()
    finally:
        db.close()


def test_opted_in_player_streams_and_threads_are_per_user(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, reply="The fox speaks.", capture=cap)
    eid = _npc(seed)
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.get("/npc-talk").status_code == 200
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "who are you?"})
    assert r.status_code == 200

    # The GM has a separate, empty thread with the same NPC
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get(f"/api/npc-talk/{eid}/history").json()["messages"] == []
    # ...and the player's thread is intact from their own login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    msgs = client.get(f"/api/npc-talk/{eid}/history").json()["messages"]
    assert len(msgs) == 2


def test_hidden_entity_not_talkable_by_player(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    eid = _npc(seed, visible_to_players=False)
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 404
    assert client.get(f"/api/npc-talk/{eid}/history").status_code == 404
    # ...and it isn't in the player's picker either
    assert "data-id=\"%d\"" % eid not in client.get("/npc-talk").text
    # The GM can still talk to their own hidden NPC
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 200


# ── Server-side [gmonly] stripping in the composed prompt ────────────────────

def test_gm_only_content_never_reaches_player_prompt(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, capture=cap)
    eid = _npc(
        seed,
        summary="A fox. [gmonly]she is the traitor[/gmonly]",
        body="Friendly ranger. [gmonly]Secretly works for the Syndicate-ZAROTH[/gmonly]",
        custom_fields_json='{"AC": "15", "Secret Contact": "[gmonly]Mister Nine[/gmonly]"}',
    )
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 200
    assert "ZAROTH" not in cap["system"]
    assert "Mister Nine" not in cap["system"]
    assert "Friendly ranger." in cap["system"]
    assert "AC: 15" in cap["system"]
    # The GM's own conversation gets the full picture
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"})
    assert "ZAROTH" in cap["system"]
    assert "Mister Nine" in cap["system"]


# ── GM-only knobs are ignored for non-GMs ────────────────────────────────────

def test_player_model_and_rag_choices_ignored(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, capture=cap)
    rag_calls = []

    def _rag(*a, **k):
        rag_calls.append(1)
        return "RAG LORE", [], []

    monkeypatch.setattr(retrieval_module, "smart_world_context", _rag)
    eid = _npc(seed)
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream",
                    json={"message": "hi", "model": "gm-only-model", "use_rag": True})
    assert r.status_code == 200
    assert rag_calls == []
    assert cap["requested_model"] != "gm-only-model"


def test_gm_use_rag_adds_lore_to_system(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, capture=cap)

    def _rag(*a, **k):
        return "LORE: the harbor is haunted", [], []

    monkeypatch.setattr(retrieval_module, "smart_world_context", _rag)
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "the harbor?", "use_rag": True})
    assert "the harbor is haunted" in cap["system"]


# ── Persistence semantics ─────────────────────────────────────────────────────

def test_failure_sentinel_is_never_saved_as_a_reply(client, seed, monkeypatch):
    _patch_ai(monkeypatch, error="model exploded")
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert "model exploded" in r.text
    assert client.get(f"/api/npc-talk/{eid}/history").json()["messages"] == []


def test_restart_clears_and_export_downloads(client, seed, monkeypatch):
    _patch_ai(monkeypatch, reply="One.")
    eid = _npc(seed, name="Elyra")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    r = client.get(f"/api/npc-talk/{eid}/export.md")
    assert r.status_code == 200
    assert "Conversation with Elyra" in r.text
    assert "hello" in r.text
    assert client.delete(f"/api/npc-talk/{eid}/history").status_code == 200
    assert client.get(f"/api/npc-talk/{eid}/history").json()["messages"] == []


def test_npc_sessions_are_isolated_from_the_ai_chat_history_surface(client, seed, monkeypatch):
    _patch_ai(monkeypatch, reply="Hi.")
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    # The /ai History sidebar only lists surface="chat"...
    assert client.get("/api/ai/sessions").json()["sessions"] == []
    # ...and a chat-surface save cannot hijack the npc row by id
    db = SessionLocal()
    try:
        npc_row = db.query(ChatSession).filter(ChatSession.surface == "npc").first()
        assert npc_row is not None
        npc_id = npc_row.id
    finally:
        db.close()
    r = client.post("/api/ai/sessions", json={"session_id": npc_id, "messages": [
        {"role": "user", "content": "hijack"},
    ]})
    assert r.status_code == 200  # creates a NEW chat row instead of overwriting
    db = SessionLocal()
    try:
        npc_row = db.get(ChatSession, npc_id)
        assert npc_row.surface == "npc"
        assert "hijack" not in npc_row.messages_json
    finally:
        db.close()
