"""Tests for the Facts feature: GM-only CRUD, the local-model recap parser
(mocked — no real Ollama needed), and world/role scoping."""
import datetime as _dt
import json
import time
import types

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import AudioJob, Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def test_gm_can_create_fact_via_quick_add(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/facts/new", data={"content": "The party met Elyra.", "visible_to_players": "1"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        facts = db.query(Fact).filter(Fact.world_id == seed.world_a.id).all()
        assert len(facts) == 1
        assert facts[0].content == "The party met Elyra."
        assert facts[0].visible_to_players is True
    finally:
        db.close()


def test_quick_add_defaults_hidden_when_checkbox_omitted(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/facts/new", data={"content": "Elyra secretly serves the cult."}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        assert fact.visible_to_players is False
    finally:
        db.close()


def test_player_cannot_create_fact(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/facts/new", data={"content": "Sneaky"}, follow_redirects=False)
    assert r.status_code == 403

    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0
    finally:
        db.close()


def test_player_cannot_view_facts_page(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/facts")
    assert r.status_code == 403


def test_gm_can_edit_and_delete_fact(client, seed):
    _login_gm_in(client, seed, seed.world_a)
    client.post("/facts/new", data={"content": "Original", "visible_to_players": "1"})
    db = SessionLocal()
    try:
        fact = db.query(Fact).filter(Fact.world_id == seed.world_a.id).first()
        fact_id = fact.id
    finally:
        db.close()

    r = client.post(f"/facts/{fact_id}/edit", data={"content": "Edited"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        fact = db.get(Fact, fact_id)
        assert fact.content == "Edited"
        assert fact.visible_to_players is False  # checkbox omitted on edit
    finally:
        db.close()

    r = client.post(f"/facts/{fact_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(Fact, fact_id) is None
    finally:
        db.close()


def test_facts_scoped_to_active_world(client, seed):
    db = SessionLocal()
    try:
        db.add(Fact(world_id=seed.world_b.id, content="World B secret", visible_to_players=True))
        db.commit()
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.get("/facts")
    assert r.status_code == 200
    assert "World B secret" not in r.text


def test_api_facts_parse_returns_draft_without_saving(client, seed, monkeypatch):
    async def fake_parse(raw_text, model="", think=False, world_context="", on_progress=None):
        assert "tavern" in raw_text
        return [
            {"content": "The party visited the tavern.", "visible_to_players": True},
            {"content": "Elyra is secretly a cult agent.", "visible_to_players": False},
        ]
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/parse", json={"text": "went to the tavern, met Elyra, she's a cult agent"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["facts"]) == 2
    assert data["facts"][1]["visible_to_players"] is False

    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0  # parse never writes to the DB
    finally:
        db.close()


def test_api_facts_parse_surfaces_model_failure(client, seed, monkeypatch):
    async def fake_parse(raw_text, model="", think=False, world_context="", on_progress=None):
        raise ValueError("Could not parse facts from that recap — try rephrasing it.")
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/parse", json={"text": "gibberish"})
    assert r.status_code == 502


def test_api_facts_bulk_saves_reviewed_drafts(client, seed):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        session_id = gs.id
    finally:
        db.close()

    _login_gm_in(client, seed, seed.world_a)
    r = client.post("/api/facts/bulk", json={
        "facts": [
            {"content": "Fact one", "visible_to_players": True},
            {"content": "Fact two", "visible_to_players": False},
            {"content": "  ", "visible_to_players": True},  # blank, dropped
        ],
        "game_session_id": session_id,
    })
    assert r.status_code == 200
    assert r.json()["created"] == 2

    db = SessionLocal()
    try:
        facts = db.query(Fact).filter(Fact.world_id == seed.world_a.id).all()
        assert len(facts) == 2
        assert all(f.game_session_id == session_id for f in facts)
    finally:
        db.close()


def test_player_cannot_call_facts_api(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/facts/parse", json={"text": "whatever"})
    assert r.status_code == 403
    r = client.post("/api/facts/bulk", json={"facts": [{"content": "x", "visible_to_players": True}]})
    assert r.status_code == 403


# ── POST /api/facts/parse-job + GET /api/facts/last-parse ───────────────────
# The parse as a durable background job (see app/audio_jobs.py
# create_facts_parse_job's docstring): the synchronous route held one HTTP
# request open for the whole model call and tripped Cloudflare Tunnel's
# ~100s timeout on long recaps, losing everything; the job row also gives
# the parsed draft a persistent home (result_json) a reload can restore.

def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_api_facts_parse_job_creates_job_and_returns_id(client, seed, monkeypatch):
    async def fake_parse(raw_text, model="", think=False, world_context="", on_progress=None):
        return [
            {"content": "The party visited the tavern.", "visible_to_players": True},
            {"content": "Elyra is secretly a cult agent.", "visible_to_players": False},
        ]
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        session_id = gs.id
    finally:
        db.close()

    _login_gm(client, seed)
    r = client.post("/api/facts/parse-job", json={
        "text": "went to the tavern, met Elyra, she's a cult agent",
        "game_session_id": session_id,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # The row is persisted immediately, with the input text and session
    # attribution — not just handed to the browser.
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job is not None
        assert job.world_id == seed.world_a.id
        assert job.purpose == "facts_parse"
        assert job.filename == "Facts"
        assert job.transcript == "went to the tavern, met Elyra, she's a cult agent"
        assert job.game_session_id == session_id
    finally:
        db.close()

    # The background run lands the draft in result_json (via the ordinary
    # audio-jobs status route the page polls) — and no Fact rows exist yet:
    # parsing still never writes facts, only Confirm & Save does.
    deadline = time.time() + 5.0
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/audio-jobs/{job_id}").json()
        if data["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert data["status"] == "done", data.get("error")
    assert len(json.loads(data["result_json"])) == 2
    db = SessionLocal()
    try:
        assert db.query(Fact).count() == 0
    finally:
        db.close()


def test_api_facts_parse_job_blank_text_is_400(client, seed):
    _login_gm(client, seed)
    r = client.post("/api/facts/parse-job", json={"text": "   "})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(AudioJob).count() == 0  # nothing created, not even an erroring job
    finally:
        db.close()


def test_api_facts_parse_job_requires_world_and_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/facts/parse-job", json={"text": "whatever"})
    assert r.status_code == 403
    r = client.get("/api/facts/last-parse")
    assert r.status_code == 403


def test_api_facts_last_parse_returns_latest_done_job(client, seed):
    db = SessionLocal()
    try:
        older = AudioJob(world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
                         status="done", result_json='[{"content": "older", "visible_to_players": true}]')
        newer = AudioJob(world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
                         status="done", result_json='[{"content": "newer", "visible_to_players": false}]')
        errored = AudioJob(world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
                           status="error", error="boom", result_json="")
        db.add_all([older, newer, errored])
        db.commit()
        # Identical microsecond timestamps would make "latest" ambiguous —
        # space them out deterministically.
        older.created_at = _dt.datetime(2026, 1, 1, 12, 0, 0)
        newer.created_at = _dt.datetime(2026, 1, 2, 12, 0, 0)
        db.commit()
        newer_id = newer.id  # read before close() detaches the instances
    finally:
        db.close()

    _login_gm(client, seed)
    r = client.get("/api/facts/last-parse")
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == newer_id
    assert data["facts"] == [{"content": "newer", "visible_to_players": False}]
    assert data["created_at"] == "2026-01-02T12:00:00"


def test_api_facts_last_parse_404_when_none(client, seed):
    _login_gm(client, seed)
    assert client.get("/api/facts/last-parse").status_code == 404


def test_api_facts_last_parse_scoped_to_active_world(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_b.id, purpose="facts_parse", filename="Facts",
                        status="done", result_json="[]"))
        db.commit()
    finally:
        db.close()
    _login_gm(client, seed)
    assert client.get("/api/facts/last-parse").status_code == 404


def test_facts_page_wires_background_parse_and_restore(client, seed):
    """The page must drive the JOB-based flow (parse-job + status polling +
    Restore last parse), not the old blocking /api/facts/parse — that route
    is what produced HTTP 524s on long recaps."""
    _login_gm(client, seed)
    html = client.get("/facts").text
    assert "id=\"parse-btn\"" in html
    assert "/api/facts/parse-job" in html
    assert "pollFactsParseJob" in html
    assert "Parsing in background… (poll" in html
    assert "id=\"restore-parse-btn\"" in html
    assert "restoreLastParse" in html
    assert "/api/facts/last-parse" in html
    # Empty parse outcome is explained, not silently swallowed.
    assert "No in-character facts found" in html


def test_facts_page_ships_recap_templates(client, seed):
    """The template chips and their skeletons ship with the page — a GM
    clicking one gets a parser-shaped skeleton (discrete lines the splitter
    turns into one fact each) instead of a blank textarea."""
    _login_gm(client, seed)
    html = client.get("/facts").text
    for key in ("standard", "timeline", "bullets", "combat", "investigation"):
        assert f'data-tpl="{key}"' in html
    assert "RECAP_TEMPLATES" in html
    # A couple of distinctive skeleton lines per template, so a regression
    # that empties the skeletons is caught even without executing the JS.
    assert "Hidden from players: <the secret behind it>" in html
    assert "Persons of interest: <names and why>" in html
    assert "Clues found:" in html


# ── Model / Thinking / RAG pickers for "Parse with AI" ──────────────────────
# The GM chooses which model parses (and whether it thinks / retrieves World
# lore) the same way the Sessions page's recording flow does — the choices
# ride the parse-job POST body onto the AudioJob row, and _run_job passes
# them into parse_facts_from_recap.

class _CaptureChatClient:
    """Records every .chat() kwargs dict and answers with an empty facts
    payload, so the parse unit tests can assert exactly what
    parse_facts_from_recap sends (model/messages/think) without an Ollama
    server — same idea as test_ollama_options.py's _FakeChatClient. Reports
    the model as thinking-capable so a requested think=True isn't downgraded
    by _chat_kwargs' capability check (it cleans its cache entry up after
    itself — see the try/finally in the tests below)."""

    def __init__(self, calls):
        self._calls = calls

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        return types.SimpleNamespace(
            message=types.SimpleNamespace(content='{"facts": []}'),
        )

    async def show(self, model):
        return types.SimpleNamespace(capabilities=["thinking"])


def _user_content(kwargs):
    return [m for m in kwargs["messages"] if m["role"] == "user"][0]["content"]


@pytest.mark.asyncio
async def test_parse_facts_from_recap_passes_model_think_and_world_context(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _CaptureChatClient(calls))
    try:
        await ai_module.parse_facts_from_recap(
            "went to the tavern", model="gemma4:26b", think=True,
            world_context="- [npc] Elyra: an enchanter",
        )
    finally:
        # _model_supports_thinking caches per-model for the process — don't
        # leak this test's fake capability answer into any other test.
        ai_module._model_capabilities_cache.pop("gemma4:26b", None)

    kwargs = calls[0]
    assert kwargs["model"] == "gemma4:26b"  # the GM's picker choice reaches Ollama
    assert kwargs["think"] is True  # the Thinking checkbox reaches the chat call
    assert kwargs["format"]  # the JSON-schema constraint is still sent alongside
    user_content = _user_content(kwargs)
    # World lore is prepended ahead of the recap text in the user message,
    # framed by the same _with_world_context helper condense_recap uses —
    # reference material for name accuracy, not extra facts to invent.
    assert user_content.startswith("Relevant world lore and notes")
    assert "- [npc] Elyra: an enchanter" in user_content
    assert user_content.endswith("went to the tavern")


@pytest.mark.asyncio
async def test_parse_facts_from_recap_defaults_to_clean_json_and_bare_text(monkeypatch):
    """No Thinking checkbox tick and no RAG context means exactly the old
    call shape: think=False (a parse needs clean JSON back) and the recap
    text alone in the user message."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _CaptureChatClient(calls))
    await ai_module.parse_facts_from_recap("went to the tavern", model="m1")
    kwargs = calls[0]
    assert kwargs["model"] == "m1"
    assert kwargs["think"] is False
    assert _user_content(kwargs) == "went to the tavern"  # no lore, no framing


def test_api_facts_parse_job_round_trips_model_think_and_rag(client, seed, monkeypatch):
    async def fake_parse(raw_text, model="", think=False, world_context="", on_progress=None):
        return []
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    _login_gm(client, seed)
    r = client.post("/api/facts/parse-job", json={
        "text": "went to the tavern, met Elyra",
        "model": "gemma4:26b",
        "think": True,
        "use_rag": True,
        "rag_entity_limit": 7,
        "rag_notes_limit": 2,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # Everything the pickers held persists on the row immediately — the
    # background run reads it from there (and a resume after a restart
    # reproduces the same configuration).
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.model == "gemma4:26b"
        assert job.think is True
        assert job.use_rag is True
        assert job.rag_entity_limit == 7
        assert job.rag_notes_limit == 2
    finally:
        db.close()


def test_api_facts_parse_job_rejects_negative_rag_limits(client, seed):
    """Same validation the Sessions routes apply via _rag_options_from_body —
    a bad limit fails fast with a 400 instead of creating a doomed job."""
    _login_gm(client, seed)
    r = client.post("/api/facts/parse-job", json={"text": "some recap", "rag_entity_limit": -1})
    assert r.status_code == 400


def test_facts_page_ships_parse_model_think_and_rag_pickers(client, seed):
    """The pickers must exist on the page AND their values must ride the
    parse-job POST body (via parseModelOptions()) — a picker that renders
    but never sends would silently parse with the defaults instead."""
    _login_gm(client, seed)
    html = client.get("/facts").text
    assert 'id="parse-model"' in html
    assert "(default model)" in html
    assert 'id="parse-think"' in html
    assert 'id="parse-rag-checkbox"' in html
    assert 'id="parse-rag-entity-limit"' in html
    assert 'id="parse-rag-notes-limit"' in html
    # Same population mechanism as the Sessions page's model dropdown.
    assert "loadParseModelOptions" in html
    assert "/api/ai/models" in html
    # The POST body includes the pickers' values.
    assert "parseModelOptions()" in html


# ── Chunked parsing (parse_facts_from_recap v2) ─────────────────────────────
# A long paste used to go out as ONE unconstrained chat call (+RAG) and
# overflowed a small local model's context window outright (Ollama 400
# "request ... exceeds the available context size"); it now splits with the
# same _split_transcript_into_chunks/_transcript_chunk_char_budget machinery
# summarize_transcript uses, calls the model once per chunk, and merges the
# results deduplicated. These tests pin that machinery end to end.

class _ScriptedParseClient:
    """Answers each .chat() call with the next scripted payload (a JSON
    string — or an Exception to raise), recording every kwargs dict, so a
    test can drive a multi-chunk parse through different per-chunk model
    answers without an Ollama server. Once the scripted list is exhausted
    the LAST answer repeats, so the exact number of chunks a paste splits
    into doesn't need predicting for tests that don't care about it."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return types.SimpleNamespace(message=types.SimpleNamespace(content=response))

    async def show(self, model):
        return types.SimpleNamespace(capabilities=["thinking"])


@pytest.mark.asyncio
async def test_parse_facts_long_text_chunked_deduped_in_order(monkeypatch):
    """A paste too long for one chunk is extracted one chunk at a time
    (each call seeing only its own slice, plus the RAG lore), and the
    per-chunk facts merge in order with duplicates collapsed — the same
    event extracted from adjacent chunks (reworded, re-punctuated) appears
    once, in its first-seen form."""
    budget = 200  # forces the paste into several chunks via the real splitter
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: budget)
    fake = _ScriptedParseClient([
        '{"facts": [{"content": "The party met Elyra at the tavern.", "visible_to_players": true}]}',
        '{"facts": [{"content": "the party met Elyra at the tavern!", "visible_to_players": true},'
        ' {"content": "The cult met under the clock tower.", "visible_to_players": false}]}',
        '{"facts": [{"content": "The party fled into the sewers.", "visible_to_players": true}]}',
        '{"facts": []}',
    ])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    expected_ctx = ai_module.effective_ollama_options().get("num_ctx") or ai_module._DEFAULT_ASSUMED_CTX_TOKENS
    world_context = "- [npc] Elyra: an enchanter"
    framing = ai_module._with_world_context("", world_context)  # the lore wrapper with an empty body

    text = ("The party met Elyra at the tavern. " * 6
            + "The cult met under the clock tower. " * 6
            + "The party fled into the sewers. " * 6)
    facts = await ai_module.parse_facts_from_recap(text, model="m1", world_context=world_context)

    assert len(fake.calls) >= 3  # actually chunked under the tiny forced budget
    assert [(f["content"], f["visible_to_players"]) for f in facts] == [
        ("The party met Elyra at the tavern.", True),  # first-seen form wins over chunk 2's rewording
        ("The cult met under the clock tower.", False),
        ("The party fled into the sewers.", True),
    ]
    for kwargs in fake.calls:
        assert kwargs["model"] == "m1"
        assert kwargs["format"]  # the schema constraint rides EVERY chunk
        assert kwargs["options"]["num_ctx"] == expected_ctx  # window pinned to the budgeted one
        assert kwargs["messages"][0]["role"] == "system"
        # The OOC-skip guidance reaches every chunk — the reported failure
        # was a model answering a mixed transcript with a meta-DESCRIPTION
        # of the text instead of facts.
        assert kwargs["messages"][0]["content"] == ai_module._RECAP_SYSTEM
        assert "out-of-character" in kwargs["messages"][0]["content"]
        user = [m for m in kwargs["messages"] if m["role"] == "user"][0]["content"]
        # World lore rides EVERY chunk (name accuracy is not chunk-local)...
        assert user.startswith("Relevant world lore and notes")
        assert world_context in user
        # ...and the message body past the lore wrapper is exactly one
        # chunk's worth of the paste — never the whole text again.
        chunk = user[len(framing):]
        assert chunk in text
        assert len(chunk) <= budget


@pytest.mark.asyncio
async def test_parse_facts_all_ooc_chunks_return_empty_list(monkeypatch):
    """The model answering {"facts": []} for EVERY chunk (pure
    out-of-character chatter — rules questions, setup, table talk) is a
    SUCCESSFUL parse of zero facts, not an error: nothing raised, so the
    caller gets [] and the job lands done with "[]" exactly as before."""
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 200)
    fake = _ScriptedParseClient(['{"facts": []}'])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    text = "Rules question about spell slots. " * 12  # several chunks of pure OOC chatter
    assert await ai_module.parse_facts_from_recap(text) == []
    assert len(fake.calls) >= 2


@pytest.mark.asyncio
async def test_parse_facts_huge_world_context_never_raises_just_skips_reserve(monkeypatch):
    """RAG lore so big it alone fills a whole chunk budget can't be reserved
    for (the chunk would floor out and overflow anyway) — the parse logs a
    warning and proceeds with the un-reserved budget instead of raising;
    a bad lore blob must never turn into a hard parse failure."""
    budget_calls = []

    def fake_budget(transcript, system, think=True, extra_reserve_tokens=0):
        budget_calls.append(extra_reserve_tokens)
        return 200

    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", fake_budget)
    fake = _ScriptedParseClient(['{"facts": []}'])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    big_lore = "- [npc] Elyra: " + "x" * 5000  # ~1250 tokens, far over a 200-char budget
    assert await ai_module.parse_facts_from_recap("met Elyra", world_context=big_lore) == []
    # First call computed the base budget with no extra reserve; the world_
    # context-alone-overflows check then took the fallback — no second call
    # asking for the (impossible) lore + response reserve.
    assert budget_calls == [0]
