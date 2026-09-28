"""Quests: list search/filters/sub-quest progress, and the AI session-sync
(suggest → apply) flow. The AI itself is monkeypatched — same convention as
test_npc_talk / test_maps_ai."""
import json
import time

from app.database import SessionLocal
from app.models import Fact, GameSession, Quest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _quest(world_id, title, status="active", category="main", summary="", parent_id=None):
    db = SessionLocal()
    try:
        q = Quest(world_id=world_id, title=title, status=status, category=category,
                  summary=summary, parent_id=parent_id)
        db.add(q)
        db.commit()
        db.refresh(q)
        return q.id
    finally:
        db.close()


def _session(world_id, summary=""):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world_id, session_num=1, title="S", summary=summary)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _pin(client, slug="world-a"):
    client.cookies.set("active_world", slug)


# ── List: search / filters / sub-quest progress ─────────────────────────────

def test_quests_list_search_filters(client, seed):
    active = _quest(seed.world_a.id, "Slay the Witch", status="active",
                    category="main", summary="hunt in Blackwood")
    done = _quest(seed.world_a.id, "Guild paperwork", status="complete", category="side")
    _quest(seed.world_b.id, "Other World Quest", status="active")

    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    body = client.get("/quests").text
    assert "Slay the Witch" in body and "Guild paperwork" in body
    assert "Other World Quest" not in body

    r = client.get("/quests", params={"q": "witch"})
    assert "Slay the Witch" in r.text and "Guild paperwork" not in r.text
    r = client.get("/quests", params={"category": "side"})
    assert "Guild paperwork" in r.text and "Slay the Witch" not in r.text


def test_quests_list_sub_quest_progress_chip(client, seed):
    parent = _quest(seed.world_a.id, "Main Arc", status="active")
    _quest(seed.world_a.id, "Step one", status="complete", parent_id=parent)
    _quest(seed.world_a.id, "Step two", status="active", parent_id=parent)
    _quest(seed.world_a.id, "Step three", status="active", parent_id=parent)

    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.get("/quests")
    assert "1/3 sub-quests done" in r.text


# ── AI suggest / apply ───────────────────────────────────────────────────────

def test_suggest_requires_gm(client, seed):
    sid = _session(seed.world_a.id, summary="x")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post("/api/quests/suggest/start", json={"session_id": sid})
    assert r.status_code == 403


def test_suggest_returns_board_grounded_suggestions(client, seed, monkeypatch):
    """The AI's reply is filtered to quest ids that actually exist in the
    world — hallucinated ids are dropped — and session facts/summary reach
    the prompt."""
    from app.routers import quests as quests_module

    witch_id = _quest(seed.world_a.id, "Slay the Witch", status="active",
                      category="main", summary="hunt in Blackwood")
    _quest(seed.world_a.id, "Unrelated", status="active")

    db = SessionLocal()
    try:
        gs_id = _session(seed.world_a.id, summary="placeholder")
        db.add(Fact(world_id=seed.world_a.id, game_session_id=gs_id,
                    content="The witch was slain at the standing stones.", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    canned = json.dumps({
        "new_quests": [{"title": "Report to the Guild",
                        "summary": "File the official report in Yorm.",
                        "category": "main"}],
        "quest_updates": [
            {"quest_id": witch_id, "status": "complete", "note": "Witch slain"},
            {"quest_id": 99999, "status": "complete", "note": "hallucinated"},
        ],
    })

    captured = {}
    async def _fake_generate_chat(messages, system="", model="", options=None,
                                  think=False, format=None):
        captured["messages"] = messages
        captured["system"] = system
        return canned

    monkeypatch.setattr(quests_module._ai, "generate_chat", _fake_generate_chat)
    # effective_llm_api_key() reads the module attr captured at import time —
    # patch it directly rather than the env.
    monkeypatch.setattr(quests_module._ai, "UNSLOTH_API_KEY", "sk-test")

    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post("/api/quests/suggest/start", json={"session_id": gs_id})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = {}
    for _ in range(40):  # the task runs on the app loop; poll for it
        poll = client.get(f"/api/quests/suggest/{job_id}").json()
        if poll.get("status") != "running":
            data = poll
            break
        time.sleep(0.05)
    assert data["status"] == "done", data
    suggestions = data["suggestions"]
    assert suggestions["new_quests"][0]["title"] == "Report to the Guild"
    ids = [u["quest_id"] for u in suggestions["quest_updates"]]
    assert witch_id in ids and 99999 not in ids
    # material reached the prompt: board + session facts
    prompt = captured["messages"][0]["content"]
    assert "Slay the Witch" in prompt
    assert "witch was slain" in prompt
    # thinking requested (Studio parity)
    assert captured["system"]


def test_apply_creates_and_updates(client, seed):
    witch_id = _quest(seed.world_a.id, "Slay the Witch", status="active")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post("/api/quests/apply", json={
        "new_quests": [{"title": "Report to the Guild", "summary": "File it.",
                        "category": "main"}],
        "quest_updates": [{"quest_id": witch_id, "status": "complete", "note": "done"}],
    })
    assert r.status_code == 200
    assert r.json() == {"created": 1, "updated": 1}
    db = SessionLocal()
    try:
        q = db.get(Quest, witch_id)
        assert q.status == "complete"
        new_q = db.query(Quest).filter(Quest.title == "Report to the Guild").first()
        assert new_q is not None and new_q.category == "main"
    finally:
        db.close()


def test_apply_rejects_foreign_world_quest(client, seed):
    other = _quest(seed.world_b.id, "World B Quest", status="active")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post("/api/quests/apply", json={
        "quest_updates": [{"quest_id": other, "status": "complete"}],
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 0                      # foreign id ignored
    db = SessionLocal()
    try:
        assert db.get(Quest, other).status == "active"
    finally:
        db.close()


def test_player_cannot_apply(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post("/api/quests/apply", json={"new_quests": [{"title": "x"}]})
    assert r.status_code == 403


def test_quest_page_fetch_urls_match_real_routes(client, seed):
    """Every fetch() path in the rendered quest board must correspond to an
    actual route. This is the check that was missing when the AI-sync panel
    kept calling the old single-shot POST /api/quests/suggest after the
    backend moved to the background-job flow (/suggest/start +
    /suggest/{job_id}) — a GM clicking Generate got FastAPI's bare
    {"detail": "Not Found"} (GitHub issue reproduced from production)."""
    import re

    from app.main import _fastapi_app

    _session(seed.world_a.id)  # the AI-sync panel only renders when the world has sessions
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/quests").text

    # Whole-string first arguments only — `fetch('/x/' + id)`-style
    # concatenations are URL prefixes, not literal paths.
    paths = set(re.findall(r"fetch\('([^']+)'\s*[,)]", html))
    paths = {p for p in paths if p.startswith("/api/")}
    assert "/api/quests/suggest/start" in paths, "panel fetch not found — regex drifted?"

    route_paths = [getattr(r, "path", "") for r in _fastapi_app.routes]

    def _exists(url):
        for rp in route_paths:
            if re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", rp), url):
                return True
        return False

    for p in paths:
        assert _exists(p), f"quests page fetches {p} but no route defines it"


def test_quest_panel_uses_background_job_flow(client, seed):
    """The panel must START a job and poll it — the single-shot request is
    what 524'd behind Cloudflare Tunnel in the first place."""
    _session(seed.world_a.id)  # the AI-sync panel only renders when the world has sessions
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get("/quests").text
    assert "/api/quests/suggest/start" in html
    assert "/api/quests/suggest/' + jobId" in html or "/api/quests/suggest/" in html
    assert "fetch('/api/quests/suggest'," not in html  # the stale single-shot call


def test_suggest_honors_model_think_rag_flags(client, seed, monkeypatch):
    """The AI-sync panel's controls must reach the model call: model and
    thinking verbatim, and use_rag:false must skip the world-context
    retrieval entirely (the toggle's promise)."""
    from app.routers import quests as quests_module

    gs_id = _session(seed.world_a.id)
    captured = {}

    async def _fake_generate_chat(messages, system="", model="", options=None,
                                  think=False, format=None):
        captured["model"] = model
        captured["think"] = think
        return '{"new_quests": [], "quest_updates": []}'

    rag_calls = []

    def _fake_smart_context(db, world_id, material, entity_limit=8, notes_limit=2):
        rag_calls.append(material[:40])
        return ("", 0, [])

    monkeypatch.setattr(quests_module._ai, "generate_chat", _fake_generate_chat)
    monkeypatch.setattr(quests_module._ai, "UNSLOTH_API_KEY", "sk-test")
    monkeypatch.setattr(quests_module._retrieval, "smart_world_context",
                        _fake_smart_context)

    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)

    # explicit flags: model chosen, thinking OFF, RAG OFF
    r = client.post("/api/quests/suggest/start", json={
        "session_id": gs_id, "model": "custom-x", "think": False, "use_rag": False})
    assert r.status_code == 200, r.text
    data = {}
    for _ in range(40):
        data = client.get(f"/api/quests/suggest/{r.json()['job_id']}").json()
        if data["status"] != "running":
            break
        time.sleep(0.05)
    assert data["status"] == "done", data
    assert captured["model"] == "custom-x"
    assert captured["think"] is False
    assert rag_calls == []  # RAG off → no world-context retrieval

    # defaults: thinking + RAG back on
    r2 = client.post("/api/quests/suggest/start", json={"session_id": gs_id})
    assert r2.status_code == 200
    data2 = {}
    for _ in range(40):
        data2 = client.get(f"/api/quests/suggest/{r2.json()['job_id']}").json()
        if data2["status"] != "running":
            break
        time.sleep(0.05)
    assert data2["status"] == "done", data2
    assert captured["think"] is True
    assert len(rag_calls) == 1  # RAG on → the world context was retrieved
