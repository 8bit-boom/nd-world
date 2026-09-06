"""Tests for the shared AI-assist layer (app/ai_assist.py + its routes in
app/routers/ai.py + the ai_assist/world_summary job purposes in
app/audio_jobs.py) — the engine behind every editor surface's ✨ panel.

Engine tests follow the suite's established two-tier faking: free-text ops
monkeypatch ai.generate_chat directly; structured ops monkeypatch
ai._client with a fake AsyncClient (the test_ollama_options pattern). No
real Ollama is ever contacted.
"""
import asyncio
import json
import time

import pytest

from app import ai as ai_module
from app import ai_assist as assist_module
from app import audio_jobs as audio_jobs_module
from app.auth import hash_password as _auth_hash
from app.database import SessionLocal
from app.models import AudioJob, Entity, Fact, Quest, User, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── Engine: op validation ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_op_raises():
    with pytest.raises(ValueError, match="Unknown AI assist operation"):
        await assist_module.run_assist("explode")


@pytest.mark.asyncio
async def test_custom_requires_instruction():
    with pytest.raises(ValueError, match="needs an instruction"):
        await assist_module.run_assist("custom", content="text")


@pytest.mark.asyncio
async def test_expand_requires_content():
    with pytest.raises(ValueError, match="Nothing to work on"):
        await assist_module.run_assist("expand", content="   ")


@pytest.mark.asyncio
async def test_analyze_requires_anything():
    with pytest.raises(ValueError):
        await assist_module.run_assist("analyze", content="", meta="")


# ── Engine: free-text ops ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_free_text_op_returns_generated_text(monkeypatch):
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        captured["system"], captured["messages"], captured["options"] = system, messages, options
        return "Improved prose."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await assist_module.run_assist(
        "improve", content="rough text", meta="Kind: location\nName: The Spire",
    )
    assert result == {"op": "improve", "mode": "text", "text": "Improved prose.", "model": result["model"]}
    # The meta block and the content both reach the model.
    assert "Kind: location" in captured["messages"][0]["content"]
    assert "rough text" in captured["messages"][0]["content"]
    # Degeneration guard: an unbounded run must have been capped
    # (_recap_num_predict_default_if_unbounded fired on the options dict).
    assert captured["options"]["num_predict"] == 1024


@pytest.mark.asyncio
async def test_failure_sentinel_passes_through_unchanged(monkeypatch):
    """Free-text ops never raise on an Ollama failure — the sentinel string
    comes back verbatim so the route can 502 it (and the job engine can
    error-row it) instead of an exception nobody expects."""
    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        return "[AI unavailable: ConnectionError: boom]"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await assist_module.run_assist("improve", content="text")
    assert result["mode"] == "text"
    assert ai_module.is_failure_sentinel(result["text"])


@pytest.mark.asyncio
async def test_degenerate_artifacts_are_cleaned(monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        return "Good line.\n<|im_start|>user\nGood line.\n"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await assist_module.run_assist("improve", content="text")
    assert "<|im_start|>" not in result["text"]
    assert "Good line." in result["text"]


@pytest.mark.asyncio
async def test_translate_uses_lang_and_drops_instruction_from_user(monkeypatch):
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        captured["system"], captured["user"] = system, messages[0]["content"]
        return "Переведено."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await assist_module.run_assist(
        "translate", content="Hello there", instruction="Russian",
    )
    assert "Target language: Russian" in captured["system"]
    # The instruction box carried the language — it must not ALSO appear as
    # a redundant "GM instruction" in the user message.
    assert "GM instruction" not in captured["user"]
    assert result["text"] == "Переведено."


@pytest.mark.asyncio
async def test_world_context_is_prepended_to_system(monkeypatch):
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await assist_module.run_assist("analyze", content="text", world_context="- [npc] Elyra")
    assert "Relevant world lore" in captured["system"]
    assert "Elyra" in captured["system"]


def test_compose_meta_drops_empty_fields():
    assert assist_module.compose_meta({"Kind": "note", "Name": "", "Tags": "  "}) == "Kind: note"


# ── Engine: structured ops (fake client, test_ollama_options pattern) ───────

class _FakeChatClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def chat(self, model="", messages=None, format=None, **kwargs):
        self.calls.append({"model": model, "messages": messages, "format": format})

        class _Msg:
            def __init__(self, content):
                self.content = content

        class _Resp:
            def __init__(self, content):
                self.message = _Msg(content)

        return _Resp(json.dumps(self.payload))


@pytest.mark.asyncio
async def test_suggest_parses_and_strips_fields(monkeypatch):
    fake = _FakeChatClient({
        "summary": "  A haunted cathedral  ", "tags": "city, ruin",
        "subtype": "Landmark", "folder": "Places/Cities", "junk": "ignored",
    })
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await assist_module.run_assist("suggest", content="long body", meta="Name: Spire")
    assert result["mode"] == "data"
    assert result["data"] == {
        "summary": "A haunted cathedral", "tags": "city, ruin",
        "subtype": "Landmark", "folder": "Places/Cities",
    }
    # Schema-constrained call was made with the suggest system prompt.
    assert fake.calls[0]["format"] == assist_module._SUGGEST_SCHEMA


@pytest.mark.asyncio
async def test_table_entries_clamps_weights(monkeypatch):
    fake = _FakeChatClient({"entries": [
        {"text": "A band of mercenaries", "weight": 99},
        {"text": "An empty shrine", "weight": 0},
        {"text": "", "weight": 5},          # empty text → dropped
        {"text": "A lost courier", "weight": "bogus"},  # bad weight → 1
    ]})
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await assist_module.run_assist("table_entries", meta="Name: Road encounters")
    assert result["data"]["entries"] == [
        {"text": "A band of mercenaries", "weight": 10},
        {"text": "An empty shrine", "weight": 1},
        {"text": "A lost courier", "weight": 1},
    ]


@pytest.mark.asyncio
async def test_table_entries_all_empty_raises(monkeypatch):
    fake = _FakeChatClient({"entries": [{"text": ""}]})
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError, match="no usable table entries"):
        await assist_module.run_assist("table_entries", meta="Name: x")


@pytest.mark.asyncio
async def test_structured_malformed_json_raises(monkeypatch):
    class _BadClient:
        async def chat(self, **kwargs):
            class _Msg:
                content = "not json at all"
            class _Resp:
                message = _Msg()
            return _Resp()

    monkeypatch.setattr(ai_module, "_client", lambda: _BadClient())
    with pytest.raises(ValueError, match="malformed JSON"):
        await assist_module.run_assist("suggest", content="text")


# ── Routes ──────────────────────────────────────────────────────────────────

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _make_assistant(seed):
    db = SessionLocal()
    try:
        u = User(email="assistant@test.local",
                 password_hash=_auth_hash("assistant-password-123"),
                 display_name="Assistant", is_gm=False)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=u.id, role="assistant"))
        db.commit()
        return u.email, "assistant-password-123"
    finally:
        db.close()


def test_assist_route_gm_ok(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        return "Polished text."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    _login_gm(client, seed)
    r = client.post("/api/ai/assist", json={"op": "improve", "body": "rough"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "text" and data["text"] == "Polished text."


def test_assist_route_player_403(client, seed, monkeypatch):
    async def fake_generate_chat(*a, **k):
        return "never reached"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/assist", json={"op": "improve", "body": "rough"})
    assert r.status_code == 403


def test_assist_route_assistant_ok(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        return "Assistant polish."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    email, password = _make_assistant(seed)
    login(client, email, password)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/assist", json={"op": "improve", "body": "rough"})
    assert r.status_code == 200, r.text


def test_assist_route_caps_input(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/ai/assist", json={"op": "improve", "body": "x" * (assist_module.MAX_INTERACTIVE_CHARS + 1)})
    assert r.status_code == 400
    assert "too large" in r.json()["detail"]


def test_assist_route_unknown_op_400(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/ai/assist", json={"op": "explode", "body": "x"})
    assert r.status_code == 400


def test_assist_route_sentinel_becomes_502(client, seed, monkeypatch):
    async def fake_generate_chat(*a, **k):
        return "[AI unavailable: nope]"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    _login_gm(client, seed)
    r = client.post("/api/ai/assist", json={"op": "improve", "body": "rough"})
    assert r.status_code == 502
    assert "AI unavailable" in r.json()["detail"]


def test_assist_route_rag_feeds_world_context(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Elyra the Blade",
                      summary="a duelist", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None, think=False):
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    _login_gm(client, seed)
    r = client.post("/api/ai/assist", json={
        "op": "improve", "body": "Elyra fights", "use_rag": True,
        "rag_entity_limit": 5, "rag_notes_limit": 1,
    })
    assert r.status_code == 200, r.text
    assert "Elyra the Blade" in captured["system"]


# ── The ai_assist job purpose ───────────────────────────────────────────────

async def _await_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached a terminal status, last seen {job.status!r}")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_assist_job_done_result_json(client, seed, monkeypatch):
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["op"] = op
        captured["kwargs"] = kwargs
        return {"op": op, "mode": "text", "text": "Rewritten rules.", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    job_id = audio_jobs_module.create_assist_job(
        seed.world_a.id, op="rules_rewrite", surface="rules-edit",
        content="# Part I\nlong document", meta="Name: Rules", instruction="tighten",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["op"] == "rules_rewrite"
    assert captured["kwargs"]["content"].startswith("# Part I")
    assert captured["kwargs"]["instruction"] == "tighten"
    assert json.loads(job.result_json)["text"] == "Rewritten rules."
    # Result lands in result_json (never recap) so the jobs UI can't render
    # it as session-recap prose.
    assert job.recap == ""


@pytest.mark.asyncio
async def test_assist_job_sentinel_error_row(client, seed, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "[AI unavailable: down]", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    job_id = audio_jobs_module.create_assist_job(seed.world_a.id, op="improve", content="x")
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "AI unavailable" in job.error


@pytest.mark.asyncio
async def test_assist_job_value_error_error_row(client, seed, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        raise ValueError("The model returned malformed JSON — try again or switch models.")

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    job_id = audio_jobs_module.create_assist_job(seed.world_a.id, op="suggest", content="x")
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "malformed JSON" in job.error


def test_create_assist_job_rejects_unknown_op(client, seed):
    with pytest.raises(ValueError, match="Unknown AI assist operation"):
        audio_jobs_module.create_assist_job(seed.world_a.id, op="explode", content="x")


def test_assist_job_routes(client, seed, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "data", "data": {"summary": "s"}, "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    _login_gm(client, seed)
    r = client.post("/api/ai/assist-job", json={"op": "suggest", "surface": "entity-form", "body": "text"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # Poll to done — same shape the interactive route returns.
    deadline = time.time() + 5
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/ai/assist-job/{job_id}").json()
        if data["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert data["status"] == "done", data
    assert data["result"]["data"] == {"summary": "s"}

    # Player cannot poll someone else's job listing shape — wrong tier.
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/ai/assist-job/{job_id}").status_code == 403


# ── The world_summary job purpose ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_world_summary_job_happy_path(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="location", name="The Spire",
                      summary="a haunted cathedral", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["op"] = op
        captured["content"] = kwargs.get("content", "")
        return {"op": op, "mode": "text", "text": "The campaign stands at a turning point.", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    job_id = audio_jobs_module.create_world_summary_job(seed.world_a.id)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["op"] == assist_module.OP_WORLD_SUMMARY
    # The deterministic digest carried the world's content into the call.
    assert "The Spire" in captured["content"]
    # Displayable prose → recap, the field the dashboard widget reads.
    assert job.recap == "The campaign stands at a turning point."


@pytest.mark.asyncio
async def test_world_summary_empty_world_errors(client, seed):
    job_id = audio_jobs_module.create_world_summary_job(seed.world_a.id)
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "no content" in job.error


def test_world_summary_routes_cache_until_regenerate(client, seed, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "State of the world.", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="note", name="Lore", body="stuff",
                      visible_to_players=True))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)

    assert client.get("/api/ai/world-summary").json() == {"recap": ""}
    r = client.post("/api/ai/world-summary")
    assert r.status_code == 200 and r.json()["pending"] is True

    deadline = time.time() + 5
    data = None
    while time.time() < deadline:
        data = client.get("/api/ai/world-summary").json()
        if not data.get("pending"):
            break
        time.sleep(0.02)
    assert data["recap"] == "State of the world."
    assert data.get("generated_at")


@pytest.mark.asyncio
async def test_world_summary_job_threads_model_and_think(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="location", name="The Spire", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["think"] = kwargs.get("think")
        return {"op": op, "mode": "text", "text": "ok", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    job_id = audio_jobs_module.create_world_summary_job(seed.world_a.id, model="llama3:latest", think=False)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["model"] == "llama3:latest"
    assert captured["think"] is False


@pytest.mark.asyncio
async def test_world_summary_job_use_rag_builds_world_context(client, seed, monkeypatch):
    """RAG is opt-in for the world summary (it defaults off — the digest
    already lists entities/quests/facts by name) and, when it IS on,
    queries against the assembled state_text itself (the same convention
    the ai_assist purpose's own RAG uses) since there's no separate short
    question to query on here."""
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="location", name="The Spire", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return {"op": op, "mode": "text", "text": "ok", "model": "m"}

    def fake_build_rag_context(world_id, query, entity_limit, notes_limit, **kw):
        captured["rag_query"] = query
        captured["entity_limit"] = entity_limit
        captured["notes_limit"] = notes_limit
        return "## Relevant lore\n- The Spire: a haunted cathedral"

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    monkeypatch.setattr(audio_jobs_module, "_build_rag_context", fake_build_rag_context)
    job_id = audio_jobs_module.create_world_summary_job(
        seed.world_a.id, use_rag=True, rag_entity_limit=7, rag_notes_limit=3,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["world_context"] == "## Relevant lore\n- The Spire: a haunted cathedral"
    assert "The Spire" in captured["rag_query"]  # queried on the state_text digest itself
    assert captured["entity_limit"] == 7
    assert captured["notes_limit"] == 3


@pytest.mark.asyncio
async def test_world_summary_job_rag_off_by_default(client, seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="location", name="The Spire", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return {"op": op, "mode": "text", "text": "ok", "model": "m"}

    def fail_if_called(*a, **kw):
        raise AssertionError("_build_rag_context should not be called when use_rag=False")

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    monkeypatch.setattr(audio_jobs_module, "_build_rag_context", fail_if_called)
    job_id = audio_jobs_module.create_world_summary_job(seed.world_a.id)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["world_context"] == ""


def test_world_summary_create_accepts_settings_body(client, seed, monkeypatch):
    captured = {}

    def fake_create(world_id, **kwargs):
        captured.update(kwargs)
        db = SessionLocal()
        try:
            job = AudioJob(world_id=world_id, purpose="world_summary", filename="World summary",
                            status="done", recap="ok", audio_path="")
            db.add(job)
            db.commit()
            db.refresh(job)
            return job.id
        finally:
            db.close()

    monkeypatch.setattr(audio_jobs_module, "create_world_summary_job", fake_create)
    _login_gm(client, seed)
    r = client.post("/api/ai/world-summary", json={
        "model": "llama3:latest", "think": False, "use_rag": True,
        "rag_entity_limit": 9, "rag_notes_limit": 2,
    })
    assert r.status_code == 200
    assert captured["model"] == "llama3:latest"
    assert captured["think"] is False
    assert captured["use_rag"] is True
    assert captured["rag_entity_limit"] == 9
    assert captured["rag_notes_limit"] == 2


def test_world_summary_clear_deletes_all_jobs_for_world(client, seed, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "State of the world.", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="note", name="Lore", body="stuff", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)

    r = client.post("/api/ai/world-summary")
    assert r.status_code == 200

    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/ai/world-summary").json().get("pending"):
            break
        time.sleep(0.02)
    assert client.get("/api/ai/world-summary").json()["recap"] == "State of the world."

    r = client.delete("/api/ai/world-summary")
    assert r.status_code == 200

    db = SessionLocal()
    try:
        remaining = db.query(AudioJob).filter(
            AudioJob.world_id == seed.world_a.id, AudioJob.purpose == "world_summary",
        ).count()
        assert remaining == 0
    finally:
        db.close()
    assert client.get("/api/ai/world-summary").json() == {"recap": ""}


def test_world_summary_clear_requires_can_edit(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete("/api/ai/world-summary")
    assert r.status_code == 403


# ── Audience scoping (GM-only secrets must not reach a GM-Assistant) ───────
#
# The widget is shown to GM + GM-Assistant (can_edit), but an assistant is
# treated like a player for SEEING GM-only secrets everywhere else in this
# app (_entity_view_gate's own `not (user and user.is_gm)` check) — these
# tests confirm the world summary follows that same boundary instead of
# baking every secret into whatever digest happens to be cached most
# recently, regardless of who's actually looking at the card.

def _login_assistant(client, seed):
    email, password = _make_assistant(seed)
    login(client, email, password)
    client.cookies.set("active_world", seed.world_a.slug)


def test_world_summary_state_text_excludes_gm_only_content_for_players_audience():
    db = SessionLocal()
    try:
        from app.models import World
        world = World(name="Secrets World", slug="secrets-world")
        db.add(world)
        db.commit()
        db.refresh(world)
        db.add(Entity(world_id=world.id, kind="character", name="Public Ally", summary="a friend",
                       visible_to_players=True))
        db.add(Entity(world_id=world.id, kind="character", name="Hidden Villain", summary="the true traitor",
                       visible_to_players=False))
        db.add(Quest(world_id=world.id, title="Open Quest", summary="known to all", visible_to_players=True))
        db.add(Quest(world_id=world.id, title="Secret Plot", summary="the GM's own plan", visible_to_players=False))
        db.add(Fact(world_id=world.id, content="A public fact", visible_to_players=True))
        db.add(Fact(world_id=world.id, content="A GM-only secret fact", visible_to_players=False))
        db.commit()
        world_id = world.id
    finally:
        db.close()

    gm_text = audio_jobs_module._world_summary_state_text(world_id, "gm")
    for needle in ("Public Ally", "Hidden Villain", "Open Quest", "Secret Plot", "A public fact", "A GM-only secret fact"):
        assert needle in gm_text

    players_text = audio_jobs_module._world_summary_state_text(world_id, "players")
    assert "Public Ally" in players_text and "Hidden Villain" not in players_text
    assert "Open Quest" in players_text and "Secret Plot" not in players_text
    assert "A public fact" in players_text and "A GM-only secret fact" not in players_text


def test_world_summary_create_uses_gm_audience_for_a_real_gm(client, seed, monkeypatch):
    captured = {}

    def fake_create(world_id, **kwargs):
        captured.update(kwargs)
        db = SessionLocal()
        try:
            job = AudioJob(world_id=world_id, purpose="world_summary", filename="World summary",
                            status="done", recap="ok", audio_path="")
            db.add(job)
            db.commit()
            db.refresh(job)
            return job.id
        finally:
            db.close()

    monkeypatch.setattr(audio_jobs_module, "create_world_summary_job", fake_create)
    _login_gm(client, seed)
    r = client.post("/api/ai/world-summary")
    assert r.status_code == 200
    assert captured["audience"] == "gm"


def test_world_summary_create_uses_players_audience_and_disables_rag_for_an_assistant(client, seed, monkeypatch):
    captured = {}

    def fake_create(world_id, **kwargs):
        captured.update(kwargs)
        db = SessionLocal()
        try:
            job = AudioJob(world_id=world_id, purpose="world_summary", filename="World summary",
                            status="done", recap="ok", audio_path="")
            db.add(job)
            db.commit()
            db.refresh(job)
            return job.id
        finally:
            db.close()

    monkeypatch.setattr(audio_jobs_module, "create_world_summary_job", fake_create)
    _login_assistant(client, seed)
    r = client.post("/api/ai/world-summary", json={"use_rag": True, "rag_entity_limit": 20})
    assert r.status_code == 200
    assert captured["audience"] == "players"
    # RAG is force-disabled server-side for a non-GM caller — _build_rag_context
    # has no visibility filter of its own, so letting an assistant's RAG
    # through would leak GM-only lore straight back into the digest the
    # audience-scoped state_text just excluded it from.
    assert captured["use_rag"] is False


def test_world_summary_get_never_serves_a_gms_secrets_included_digest_to_an_assistant(client, seed, monkeypatch):
    captured_content = []

    async def fake_run_assist(op, **kwargs):
        content = kwargs.get("content", "")
        captured_content.append(content)
        return {"op": op, "mode": "text", "text": "Digest: " + content, "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Hidden Villain",
                       summary="the true traitor", visible_to_players=False))
        db.commit()
    finally:
        db.close()

    _login_gm(client, seed)
    r = client.post("/api/ai/world-summary")
    assert r.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/ai/world-summary").json().get("pending"):
            break
        time.sleep(0.02)
    gm_recap = client.get("/api/ai/world-summary").json()
    assert "Hidden Villain" in gm_recap["recap"]

    # An assistant loading the same card must NOT see the GM's cached,
    # secrets-included digest — their own tier has no summary yet.
    _login_assistant(client, seed)
    assert client.get("/api/ai/world-summary").json() == {"recap": ""}

    # Generating one as the assistant produces a separate artifact that
    # never saw the GM-only entity in the first place.
    r = client.post("/api/ai/world-summary")
    assert r.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/ai/world-summary").json().get("pending"):
            break
        time.sleep(0.02)
    assistant_recap = client.get("/api/ai/world-summary").json()
    assert "Hidden Villain" not in assistant_recap["recap"]

    # And the GM's own cached digest is untouched by the assistant's run.
    _login_gm(client, seed)
    assert client.get("/api/ai/world-summary").json()["recap"] == gm_recap["recap"]


def test_world_summary_clear_is_scoped_to_callers_own_audience(client, seed, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "ok", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="note", name="Lore", body="stuff", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    _login_gm(client, seed)
    client.post("/api/ai/world-summary")
    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/ai/world-summary").json().get("pending"):
            break
        time.sleep(0.02)
    assert client.get("/api/ai/world-summary").json()["recap"] == "ok"

    # An assistant clicking Clear (on a card that has no summary of their
    # own yet) must not be able to wipe the GM's separately-cached digest.
    _login_assistant(client, seed)
    r = client.delete("/api/ai/world-summary")
    assert r.status_code == 200

    _login_gm(client, seed)
    assert client.get("/api/ai/world-summary").json()["recap"] == "ok"


def test_world_summary_legacy_blank_audience_row_reads_as_gm_not_assistant(client, seed):
    """A row created before this audience scoping existed has
    AudioJob.audience == "" (the column's own default) — it represents the
    old unconditional "secrets included" behavior, so it must still surface
    for the GM (no silently-lost cached digest from this change) but must
    NEVER be served to an assistant, since it might contain exactly the
    secrets this scoping exists to hide."""
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="world_summary", filename="World summary",
                        status="done", recap="a legacy secrets-included digest", audio_path="")
        db.add(job)
        db.commit()
    finally:
        db.close()

    _login_gm(client, seed)
    assert client.get("/api/ai/world-summary").json()["recap"] == "a legacy secrets-included digest"

    _login_assistant(client, seed)
    assert client.get("/api/ai/world-summary").json() == {"recap": ""}
