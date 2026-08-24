"""Tests for the AI Chat page's saved-conversation history (ChatSession in
app/models.py, routes in app/routers/ai.py) — the client's History sidebar
(ai_chat.html's loadSessions/autoSave/loadSession/deleteSession) has always
called GET/POST/DELETE /api/ai/sessions*, but until now no such route
existed anywhere in the backend, so every save/load/delete silently 404'd.
These tests cover the round trip those functions actually expect.
"""
import json

from app.database import SessionLocal
from app.models import ChatSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _msgs(*pairs):
    """pairs of (role, content) -> the wire shape ai_chat.html's `history`
    sends: [{role, content, attachments: []}, ...]."""
    return [{"role": r, "content": c, "attachments": []} for r, c in pairs]


def test_chat_sessions_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/sessions").status_code == 403
    assert client.post("/api/ai/sessions", json={"session_id": None, "messages": []}).status_code == 403
    assert client.get("/api/ai/sessions/1").status_code == 403
    assert client.delete("/api/ai/sessions/1").status_code == 403


def test_chat_sessions_empty_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_chat_session_create_derives_title_from_first_user_message(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    msgs = _msgs(("user", "Tell me about a neon-lit alley market.\nSecond line."),
                 ("assistant", "Sure — here's a market..."))
    r = client.post("/api/ai/sessions", json={"session_id": None, "messages": msgs})
    assert r.status_code == 200, r.text
    session_id = r.json()["id"]
    assert session_id

    r = client.get("/api/ai/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["title"] == "Tell me about a neon-lit alley market."

    r = client.get(f"/api/ai/sessions/{session_id}")
    assert r.status_code == 200
    assert r.json()["messages"] == msgs


def test_chat_session_title_falls_back_when_no_user_message(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/sessions", json={
        "session_id": None,
        "messages": _msgs(("assistant", "New chat started — ask me anything about your world.")),
    })
    session_id = r.json()["id"]
    r = client.get("/api/ai/sessions")
    assert r.json()["sessions"][0]["title"] == "New chat"


def test_chat_session_resave_updates_messages_but_not_title(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    first = _msgs(("user", "First question"), ("assistant", "First answer"))
    r = client.post("/api/ai/sessions", json={"session_id": None, "messages": first})
    session_id = r.json()["id"]

    updated = first + _msgs(("user", "Follow-up question"), ("assistant", "Follow-up answer"))
    r = client.post("/api/ai/sessions", json={"session_id": session_id, "messages": updated})
    assert r.status_code == 200
    assert r.json()["id"] == session_id  # same row, not a new one

    r = client.get(f"/api/ai/sessions/{session_id}")
    assert r.json()["messages"] == updated

    # Only one row exists — the second save updated in place.
    r = client.get("/api/ai/sessions")
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["title"] == "First question"  # unchanged from creation


def test_chat_session_delete(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "hi"))})
    session_id = r.json()["id"]

    r = client.delete(f"/api/ai/sessions/{session_id}")
    assert r.status_code == 200

    assert client.get(f"/api/ai/sessions/{session_id}").status_code == 404
    assert client.get("/api/ai/sessions").json() == {"sessions": []}


def test_chat_session_delete_unknown_id_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.delete("/api/ai/sessions/999999").status_code == 404


def test_chat_session_get_unknown_id_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/sessions/999999").status_code == 404


def test_chat_session_cross_world_isolation(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "world A chat"))})
    session_id = r.json()["id"]

    client.cookies.set("active_world", seed.world_b.slug)
    assert client.get("/api/ai/sessions").json() == {"sessions": []}
    assert client.get(f"/api/ai/sessions/{session_id}").status_code == 404
    assert client.delete(f"/api/ai/sessions/{session_id}").status_code == 404

    # Saving with a session_id that belongs to a different world creates a
    # brand-new row in the active world rather than hijacking the other one.
    r = client.post("/api/ai/sessions", json={"session_id": session_id, "messages": _msgs(("user", "world B chat"))})
    assert r.status_code == 200
    new_id = r.json()["id"]
    assert new_id != session_id

    db = SessionLocal()
    try:
        original = db.get(ChatSession, session_id)
        assert original.world_id == seed.world_a.id
        assert json.loads(original.messages_json)[0]["content"] == "world A chat"
    finally:
        db.close()


def test_chat_sessions_ordered_most_recently_updated_first(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r1 = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "older chat"))})
    id1 = r1.json()["id"]
    r2 = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "newer chat"))})
    id2 = r2.json()["id"]

    r = client.get("/api/ai/sessions")
    ids = [s["id"] for s in r.json()["sessions"]]
    assert ids == [id2, id1]

    # Touching the older one again bumps it to the front.
    client.post("/api/ai/sessions", json={"session_id": id1, "messages": _msgs(("user", "older chat, edited"))})
    r = client.get("/api/ai/sessions")
    ids = [s["id"] for s in r.json()["sessions"]]
    assert ids == [id1, id2]
