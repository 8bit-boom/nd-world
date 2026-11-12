"""Tests for the /ai sidebar's Projects section (AiProject in app/models.py,
routes in app/routers/ai.py's chat-project block): named groups of chats
that carry free-form notes the client rides into every chat's system prompt.
The server-side contract these tests pin down:

- projects are per-world and GM-only (a player 403s on every route);
- a chat saves into a project only at CREATION (the save body's project_id
  is honored on create, ignored on update — moving goes through
  POST /sessions/{id}/project, so a replayed save can't undo a move);
- a stale/foreign project id silently ungroups rather than failing a save;
- deleting a project ungroups its chats and never deletes them;
- every read is scoped to the active world (ids from world B never resolve).
"""
import json

from app.database import SessionLocal
from app.models import AiProject, ChatSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _msgs(*pairs):
    return [{"role": r, "content": c, "attachments": []} for r, c in pairs]


def _as_gm(client, seed, world="a"):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug if world == "a" else seed.world_b.slug)


def test_chat_projects_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/projects").status_code == 403
    assert client.post("/api/ai/projects", json={"name": "P"}).status_code == 403
    assert client.post("/api/ai/projects/1", json={"name": "P"}).status_code == 403
    assert client.delete("/api/ai/projects/1").status_code == 403
    assert client.post("/api/ai/sessions/1/project", json={"project_id": 1}).status_code == 403


def test_chat_project_create_defaults_and_caps(client, seed):
    _as_gm(client, seed)
    # blank name -> "New project"; overlong name/notes are capped, not 400s
    r = client.post("/api/ai/projects", json={"name": "  ", "notes": ""})
    assert r.status_code == 200
    assert r.json()["name"] == "New project"
    pid = r.json()["id"]

    r = client.post("/api/ai/projects", json={"name": "x" * 200, "notes": "n" * 20000})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        row = db.query(AiProject).filter(AiProject.id == r.json()["id"]).one()
        assert len(row.name) == 128
        assert len(row.notes) == 16000
        assert db.query(AiProject).filter(AiProject.id == pid).one().name == "New project"
    finally:
        db.close()


def test_chat_project_list_groups_chats(client, seed):
    _as_gm(client, seed)
    pid = client.post("/api/ai/projects", json={"name": "Hunt in the Moonlight", "notes": "Hunt rules."}).json()["id"]
    msgs = _msgs(("user", "Planning question"), ("assistant", "Plan"))
    sid = client.post("/api/ai/sessions", json={"session_id": None, "messages": msgs, "project_id": pid}).json()["id"]
    # an ungrouped chat too
    client.post("/api/ai/sessions", json={"session_id": None, "messages": msgs})

    projects = client.get("/api/ai/projects").json()["projects"]
    assert len(projects) == 1
    p = projects[0]
    assert p["id"] == pid
    assert p["name"] == "Hunt in the Moonlight"
    assert p["notes"] == "Hunt rules."
    assert p["chats"] == [{"id": sid, "title": "Planning question"}]

    # the main history list carries project_id so the client can filter
    sessions = client.get("/api/ai/sessions").json()["sessions"]
    grouped = {s["id"]: s["project_id"] for s in sessions}
    assert grouped[sid] == pid
    assert len(grouped) == 2 and None in grouped.values()

    # GET one chat returns its project
    assert client.get(f"/api/ai/sessions/{sid}").json()["project_id"] == pid


def test_chat_save_honors_project_id_only_on_create(client, seed):
    _as_gm(client, seed)
    pid = client.post("/api/ai/projects", json={"name": "P"}).json()["id"]
    msgs = _msgs(("user", "q"), ("assistant", "a"))
    sid = client.post("/api/ai/sessions", json={"session_id": None, "messages": msgs, "project_id": pid}).json()["id"]
    # an update replaying a DIFFERENT project_id must not move the chat
    pid2 = client.post("/api/ai/projects", json={"name": "P2"}).json()["id"]
    r = client.post("/api/ai/sessions", json={"session_id": sid, "messages": msgs, "project_id": pid2})
    assert r.status_code == 200 and r.json()["id"] == sid
    assert client.get(f"/api/ai/sessions/{sid}").json()["project_id"] == pid


def test_chat_save_stale_project_id_ungroups_silently(client, seed):
    _as_gm(client, seed, world="b")
    foreign_id = client.post("/api/ai/projects", json={"name": "B-only"}).json()["id"]
    _as_gm(client, seed)
    msgs = _msgs(("user", "q"), ("assistant", "a"))
    # a nonexistent project id silently ungroups…
    r = client.post("/api/ai/sessions", json={"session_id": None, "messages": msgs, "project_id": 99999})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert client.get(f"/api/ai/sessions/{sid}").json()["project_id"] is None
    # …and world B's id is just as invisible from world A
    client.delete(f"/api/ai/sessions/{sid}")
    r = client.post("/api/ai/sessions", json={"session_id": None, "messages": msgs, "project_id": foreign_id})
    assert r.status_code == 200
    assert client.get(f"/api/ai/sessions/{r.json()['id']}").json()["project_id"] is None


def test_chat_session_move_route(client, seed):
    _as_gm(client, seed)
    pid = client.post("/api/ai/projects", json={"name": "P"}).json()["id"]
    sid = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "q"))}).json()["id"]
    r = client.post(f"/api/ai/sessions/{sid}/project", json={"project_id": pid})
    assert r.status_code == 200 and r.json() == {"ok": True, "project_id": pid}
    assert client.get(f"/api/ai/sessions/{sid}").json()["project_id"] == pid
    # moving to a stale id ungroups (project_id None), not 400
    r = client.post(f"/api/ai/sessions/{sid}/project", json={"project_id": 424242})
    assert r.status_code == 200 and r.json()["project_id"] is None
    # moving back in, then out explicitly
    assert client.post(f"/api/ai/sessions/{sid}/project", json={"project_id": pid}).json()["project_id"] == pid
    assert client.post(f"/api/ai/sessions/{sid}/project", json={"project_id": None}).json()["project_id"] is None
    # a chat from another surface (npc) is never movable through this route
    db = SessionLocal()
    try:
        from app.models import World
        wid = db.query(World).filter(World.slug == "world-a").one().id
        db.add(ChatSession(world_id=wid, surface="npc", entity_id=1, title="npc"))
        db.commit()
        npc_id = db.query(ChatSession).filter(ChatSession.surface == "npc").one().id
    finally:
        db.close()
    assert client.post(f"/api/ai/sessions/{npc_id}/project", json={"project_id": pid}).status_code == 404
    # world-scoped: an id from world B's chats 404s from world A
    _as_gm(client, seed, world="b")
    sid_b = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "b"))}).json()["id"]
    _as_gm(client, seed)
    assert client.post(f"/api/ai/sessions/{sid_b}/project", json={"project_id": pid}).status_code == 404


def test_chat_project_edit_and_delete_ungroups(client, seed):
    _as_gm(client, seed)
    pid = client.post("/api/ai/projects", json={"name": "Old", "notes": "x"}).json()["id"]
    sid = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "q")), "project_id": pid}).json()["id"]
    # rename + notes
    r = client.post(f"/api/ai/projects/{pid}", json={"name": "  New  ", "notes": "House rules."})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/ai/projects").json()["projects"][0]["name"] == "New"
    assert client.get("/api/ai/projects").json()["projects"][0]["notes"] == "House rules."
    # blank name on edit keeps the old one
    client.post(f"/api/ai/projects/{pid}", json={"name": "   ", "notes": ""})
    assert client.get("/api/ai/projects").json()["projects"][0]["name"] == "New"
    # delete ungroups but keeps the chat
    r = client.delete(f"/api/ai/projects/{pid}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/ai/projects").json()["projects"] == []
    got = client.get(f"/api/ai/sessions/{sid}")
    assert got.status_code == 200 and got.json()["project_id"] is None
    # 404s: gone, foreign id, foreign world
    assert client.delete(f"/api/ai/projects/{pid}").status_code == 404
    _as_gm(client, seed, world="b")
    pid_b = client.post("/api/ai/projects", json={"name": "B"}).json()["id"]
    _as_gm(client, seed)
    assert client.post(f"/api/ai/projects/{pid_b}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/ai/projects/{pid_b}").status_code == 404
    assert client.post(f"/api/ai/sessions/{sid}/project", json={"project_id": pid_b}).json()["project_id"] is None


def test_chat_project_world_scoped_list(client, seed):
    _as_gm(client, seed)
    client.post("/api/ai/projects", json={"name": "A-only"})
    _as_gm(client, seed, world="b")
    assert [p["name"] for p in client.get("/api/ai/projects").json()["projects"]] == []
    client.post("/api/ai/projects", json={"name": "B-only"})
    assert [p["name"] for p in client.get("/api/ai/projects").json()["projects"]] == ["B-only"]
    _as_gm(client, seed)
    assert [p["name"] for p in client.get("/api/ai/projects").json()["projects"]] == ["A-only"]


def test_chat_projects_page_renders_section(client, seed):
    _as_gm(client, seed)
    r = client.get("/ai")
    assert r.status_code == 200
    assert 'id="project-list"' in r.text
    assert "openProjectDialog" in r.text


def test_chat_project_world_delete_removes_projects(client, seed):
    """A deleted world takes its projects with it (the _WORLD_DELETE_MODELS
    loop) — and its chats, so no chat can dangle on a removed project."""
    from app.models import World
    _as_gm(client, seed, world="b")
    pid = client.post("/api/ai/projects", json={"name": "Doomed"}).json()["id"]
    sid = client.post("/api/ai/sessions", json={"session_id": None, "messages": _msgs(("user", "q")), "project_id": pid}).json()["id"]
    db = SessionLocal()
    try:
        world_b_id = db.query(World).filter(World.slug == "world-b").one().id
    finally:
        db.close()
    assert client.post(f"/worlds/{world_b_id}/delete").status_code in (200, 303)
    db = SessionLocal()
    try:
        assert db.query(AiProject).filter(AiProject.id == pid).count() == 0
        assert db.query(ChatSession).filter(ChatSession.id == sid).count() == 0
    finally:
        db.close()
