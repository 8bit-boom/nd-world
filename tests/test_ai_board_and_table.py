"""Tests for the AI generators added 2026-09-30: the investigation-board
generator (boards_generate) and the random-table generator (tables). The
chat model is mocked at the app.ai boundary the way the sibling suites
do; the defensive JSON extraction is pinned pure."""
import json

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import InvestBoard, RandomTable

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture
def ai_on(monkeypatch):
    from app.routers import boards_generate as _bg
    from app.routers import tables as _tables
    monkeypatch.setattr(_bg._ai_module, "effective_llm_api_key", lambda: "sk-test")
    monkeypatch.setattr(_tables._ai, "effective_llm_api_key", lambda: "sk-test")


# ── defensive JSON extraction (pure) ─────────────────────────────────────────

def test_extract_json_object_handles_fences_and_truncation():
    from app.routers.boards_generate import _extract_json_object as ex
    assert ex('```json\n{"a": 1}\n```') == {"a": 1}
    assert ex('Sure! {"a": {"b": [1, 2]}} hope that helps') == {"a": {"b": [1, 2]}}
    # Truncated mid-list: everything before the cut survives.
    truncated = '{"title": "T", "nodes": [{"role": "victim", "title": "A", "links": []}, {"role": "suspect", "title": "B", "lin'
    out = ex(truncated)
    assert out and out["nodes"][0]["title"] == "A"
    assert ex("no json here at all") is None


# ── investigation board generator ────────────────────────────────────────────

_GOOD_BOARD = {
    "title": "The Drowned Ledger",
    "nodes": [
        {"role": "victim", "title": "The Harbor-master", "body": "Drowned; ledger lists odd debts.", "links": [1, 3]},
        {"role": "suspect", "title": "The Chandlery Owner", "body": "Owed him money.", "links": [3]},
        {"role": "suspect", "title": "The Nun", "body": "Saw the fight.", "links": []},
        {"role": "clue", "title": "Waterlogged Ledger", "body": "Three names, none local.", "links": [1]},
        {"role": "location", "title": "The Slip", "body": "Where the body surfaced.", "links": []},
    ],
}


def _mock_chat(monkeypatch, payload):
    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        captured = {"system": system, "prompt": messages[0]["content"],
                    "options": options, "think": think}
        return json.dumps(payload) if not isinstance(payload, str) else payload
    return _gen


def test_mystery_board_generates_and_persists(client, seed, monkeypatch, ai_on):
    from app.routers import boards_generate as _bg
    seen = {}
    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        seen["prompt"] = messages[0]["content"]
        seen["num_predict"] = (options or {}).get("num_predict")
        seen["think"] = think
        return json.dumps(_GOOD_BOARD)
    monkeypatch.setattr(ai_module, "generate_chat", _gen)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/boards/generate-mystery",
                    data={"premise": "The harbor-master drowned; his ledger lists three debts nobody can place."},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    slug = r.headers["location"].rsplit("/", 1)[-1]
    db = SessionLocal()
    try:
        b = db.query(InvestBoard).filter(InvestBoard.slug == slug, InvestBoard.world_id == seed.world_a.id).one()
        nodes = json.loads(b.nodes_json)["nodes"]
        edges = json.loads(b.edges_json)
        assert len(nodes) == 5
        roles = {n["status"] for n in nodes}
        assert "suspect" in roles and "victim" in roles
        # Edges dedupe and map indices → node ids: (0,1),(0,3),(1,3) — the 3→1 dup folds in.
        assert len(edges) == 3
        assert all(e["from"].startswith("my-") for e in edges)
        # Thinking is deliberately off for a bounded structured generation.
        assert seen["think"] is False and seen["num_predict"] == 2000
        assert "harbor-master drowned" in seen["prompt"]
    finally:
        db.close()


def test_mystery_board_rejects_garbage_reply(client, seed, monkeypatch, ai_on):
    monkeypatch.setattr(ai_module, "generate_chat", _mock_chat(monkeypatch, "I cannot do that."))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/boards/generate-mystery", data={"premise": "a mystery"})
    assert r.status_code == 502


def test_mystery_board_requires_premise_and_backend(client, seed, ai_on):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/boards/generate-mystery", data={"premise": "  "}).status_code == 400


def test_mystery_board_is_not_player_reachable(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/boards/generate-mystery", data={"premise": "x"}).status_code == 403


# ── random-table generator ───────────────────────────────────────────────────

def test_table_generator_creates_reviewable_table(client, seed, monkeypatch, ai_on):
    payload = {"name": "Harbor Omens", "description": "Strange sights at night.",
               "entries": ["A rope that hums.", "Green lanternlight.", "A drowned bell rings.",
                           "Salt figures on the pier.", "The tide goes out too far."]}
    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        return json.dumps(payload)
    monkeypatch.setattr(ai_module, "generate_chat", _gen)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/tables/generate-ai", data={"description": "Strange harbor sights", "count": "5"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(r.headers["location"].rsplit("/", 2)[-2])
    db = SessionLocal()
    try:
        t = db.get(RandomTable, tid)
        assert t.world_id == seed.world_a.id
        assert t.name == "Harbor Omens"
        entries = json.loads(t.entries_json)
        assert len(entries) == 5
        assert all(e["weight"] == 1 and e["label"] for e in entries)
    finally:
        db.close()


def test_table_generator_rejects_thin_replies(client, seed, monkeypatch, ai_on):
    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        return json.dumps({"name": "Thin", "entries": ["only one"]})
    monkeypatch.setattr(ai_module, "generate_chat", _gen)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/tables/generate-ai", data={"description": "x"}).status_code == 502


def test_table_generator_player_denied(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/tables/generate-ai", data={"description": "x"}).status_code == 403
