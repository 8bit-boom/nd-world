"""End-to-end tests for the MCP server (app/mcp_server.py) — bearer-token
auth at the /mcp mount, and the tool-level GM/player boundary, exercised
through the real `mcp` client SDK against the app in-process (no real
network), not just HTTP-status assertions.

No app lifespan/startup event is triggered here on purpose: `app` (imported
below) is the plain ASGI dispatcher function app/main.py exports, routing
/mcp around the FastAPI app's own middleware stack entirely (see that
module's docstring on `app` for why) — ASGITransport never sends lifespan
events regardless, and none of app.main's startup work (init_db's migration
healing, uploads dir, AI settings refresh) is needed here: _reset_db()
below creates every table fresh from the current models (no pre-existing
schema to heal), and the one tool that touches AI (ask_chronicler) has
generate_chat monkeypatched directly.
"""
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app import auth as auth_module
from app.database import SessionLocal, engine
from app.database import _migrate as _db_migrate
from app.main import app
from app.models import ApiToken, Base, Entity, Fact, Quest, User, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # entity_fts (and its sync triggers) live outside Base.metadata — see
    # database._migrate's own comment on why drop_all/create_all alone
    # leaves it stale — so search_entities/ask_chronicler (both now routed
    # through app.retrieval.find_relevant_entities, which tries FTS first)
    # would otherwise silently retrieve nothing rather than falling back to
    # ILIKE: an unsynced FTS5 query doesn't raise, it just returns no rows.
    # _migrate() alone (not the rest of init_db, i.e. no _seed()) repairs
    # this without adding an unwanted default World to these tests' counts.
    _db_migrate()


def _seed():
    db = SessionLocal()
    try:
        gm = User(email="mcp-gm@test.local", password_hash=auth_module.hash_password(GM_PASSWORD),
                  display_name="GM", is_gm=True)
        player = User(email="mcp-player@test.local", password_hash=auth_module.hash_password(PLAYER_PASSWORD),
                      display_name="Player", is_gm=False)
        world_a = World(name="World A", slug="world-a")
        world_b = World(name="World B", slug="world-b")
        db.add_all([gm, player, world_a, world_b])
        db.commit()
        for obj in (gm, player, world_a, world_b):
            db.refresh(obj)
        db.add(WorldMembership(world_id=world_a.id, user_id=player.id))
        db.commit()
        return {"gm_id": gm.id, "player_id": player.id, "world_a_id": world_a.id, "world_b_id": world_b.id}
    finally:
        db.close()


def _issue_token(user_id, label="test"):
    raw = auth_module.generate_api_token()
    db = SessionLocal()
    try:
        db.add(ApiToken(user_id=user_id, token_hash=auth_module.hash_api_token(raw), label=label))
        db.commit()
    finally:
        db.close()
    return raw


def _http_factory(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers)


async def _call(token: str, tool: str, arguments: dict):
    async with streamablehttp_client(
        "http://testserver/mcp", headers={"Authorization": f"Bearer {token}"},
        httpx_client_factory=_http_factory,
    ) as (read, write, _get_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)


def _result(res):
    """The tool's JSON return value. A list[dict]-returning tool renders one
    content block per item plus a structuredContent {"result": [...]}
    wrapper — read that directly rather than content[0], which is only the
    first item. A dict-returning tool (create/update/delete_fact) has no
    structuredContent at all, just one block — parse that instead."""
    assert not res.isError, res.content[0].text if res.content else "(no content)"
    if res.structuredContent is not None and "result" in res.structuredContent:
        return res.structuredContent["result"]
    return json.loads(res.content[0].text)


_INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


async def test_mcp_requires_bearer_token():
    _reset_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                           "Content-Type": "application/json"}, json=_INIT_BODY)
        assert r.status_code == 401


async def test_mcp_rejects_invalid_token():
    _reset_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.post("/mcp", headers={"Authorization": "Bearer bogus-token",
                                           "Accept": "application/json, text/event-stream",
                                           "Content-Type": "application/json"}, json=_INIT_BODY)
        assert r.status_code == 401


async def test_mcp_gm_can_create_and_list_facts():
    _reset_db()
    ids = _seed()
    token = _issue_token(ids["gm_id"], "gm token")
    created = _result(await _call(token, "create_fact", {
        "world_id": ids["world_a_id"], "content": "The party arrived.", "visible_to_players": False,
    }))
    assert created["content"] == "The party arrived."

    facts = _result(await _call(token, "list_facts", {"world_id": ids["world_a_id"]}))
    assert len(facts) == 1
    assert facts[0]["content"] == "The party arrived."
    assert facts[0]["visible_to_players"] is False


async def test_mcp_player_token_cannot_create_fact():
    _reset_db()
    ids = _seed()
    token = _issue_token(ids["player_id"], "player token")
    res = await _call(token, "create_fact", {
        "world_id": ids["world_a_id"], "content": "Sneaky", "visible_to_players": True,
    })
    assert res.isError
    assert "GM" in res.content[0].text

    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0
    finally:
        db.close()


async def test_mcp_player_never_sees_gm_only_facts():
    _reset_db()
    ids = _seed()
    gm_token = _issue_token(ids["gm_id"], "gm")
    player_token = _issue_token(ids["player_id"], "player")
    await _call(gm_token, "create_fact", {
        "world_id": ids["world_a_id"], "content": "Public fact", "visible_to_players": True,
    })
    await _call(gm_token, "create_fact", {
        "world_id": ids["world_a_id"], "content": "Secret fact", "visible_to_players": False,
    })
    facts = _result(await _call(player_token, "list_facts", {"world_id": ids["world_a_id"]}))
    contents = {f["content"] for f in facts}
    assert contents == {"Public fact"}


async def test_mcp_token_cannot_reach_a_world_its_user_is_not_a_member_of():
    _reset_db()
    ids = _seed()
    player_token = _issue_token(ids["player_id"], "player")
    res = await _call(player_token, "list_facts", {"world_id": ids["world_b_id"]})
    assert res.isError


async def test_mcp_list_worlds_scoped_by_role():
    _reset_db()
    ids = _seed()
    gm_token = _issue_token(ids["gm_id"], "gm")
    player_token = _issue_token(ids["player_id"], "player")
    gm_worlds = {w["slug"] for w in _result(await _call(gm_token, "list_worlds", {}))}
    assert gm_worlds == {"world-a", "world-b"}

    player_worlds = {w["slug"] for w in _result(await _call(player_token, "list_worlds", {}))}
    assert player_worlds == {"world-a"}


async def test_mcp_search_entities_and_list_quests():
    _reset_db()
    ids = _seed()
    gm_token = _issue_token(ids["gm_id"], "gm")
    db = SessionLocal()
    try:
        db.add(Entity(world_id=ids["world_a_id"], kind="character", name="Elyra the Cult Agent",
                       summary="Tavern owner", visible_to_players=True))
        db.add(Quest(world_id=ids["world_a_id"], title="Find the clock", status="active",
                      category="main", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    entities = _result(await _call(gm_token, "search_entities", {
        "world_id": ids["world_a_id"], "query": "Elyra tavern",
    }))
    assert entities[0]["name"] == "Elyra the Cult Agent"

    quests = _result(await _call(gm_token, "list_quests", {"world_id": ids["world_a_id"]}))
    assert quests[0]["title"] == "Find the clock"


async def test_mcp_ask_chronicler_filters_by_role(monkeypatch):
    _reset_db()
    ids = _seed()
    gm_token = _issue_token(ids["gm_id"], "gm")
    player_token = _issue_token(ids["player_id"], "player")

    db = SessionLocal()
    try:
        db.add(Fact(world_id=ids["world_a_id"], content="Secret cult plot", visible_to_players=False))
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_generate_chat(messages, system="", model=""):
        captured["system"] = system
        return "The Chronicler speaks."
    import app.ai as ai_module
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    answer = await _call(player_token, "ask_chronicler", {
        "world_id": ids["world_a_id"], "question": "What is the cult plot?",
    })
    assert not answer.isError
    assert "Secret cult plot" not in captured["system"]

    await _call(gm_token, "ask_chronicler", {
        "world_id": ids["world_a_id"], "question": "What is the cult plot?",
    })
    assert "Secret cult plot" in captured["system"]


async def test_mcp_update_and_delete_fact_gm_only():
    _reset_db()
    ids = _seed()
    gm_token = _issue_token(ids["gm_id"], "gm")
    player_token = _issue_token(ids["player_id"], "player")

    created = _result(await _call(gm_token, "create_fact", {
        "world_id": ids["world_a_id"], "content": "Original", "visible_to_players": True,
    }))
    fact_id = created["id"]

    res = await _call(player_token, "update_fact", {"fact_id": fact_id, "content": "Hacked"})
    assert res.isError

    updated = _result(await _call(gm_token, "update_fact", {"fact_id": fact_id, "content": "Edited"}))
    assert updated["content"] == "Edited"

    res2 = await _call(player_token, "delete_fact", {"fact_id": fact_id})
    assert res2.isError

    deleted = _result(await _call(gm_token, "delete_fact", {"fact_id": fact_id}))
    assert deleted["deleted"] == fact_id
