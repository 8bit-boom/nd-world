"""Tests for the small AI-answer caches added over Chronicler and the
session-log recap (plan item AI 1.9 — cooldown/gating for both landed in
Wave 1, this is the "and caching" half): repeated identical requests are
served from cache instead of re-invoking Ollama, and the session-log recap
is invalidated wherever a Fact is written so a freshly-logged fact shows
up in the next recap instead of a stale cached one. (The session-log
cache used to be an in-process dict with a TTL, then a module staleness
marker; it is now durable DB state — the latest done session_log_recap
job whose created_at postdates both the session's newest Fact timestamp
and World.recap_content_touch — same invalidation contract, restart-safe
storage.)"""
import time

from app.database import SessionLocal
from app.models import AudioJob, Fact, GameSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_session(world, title="Session 1", num=1):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=world.id, title=title, session_num=num, summary="s")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        return gs.id
    finally:
        db.close()


def _add_fact(world, session_id, content, visible=True):
    db = SessionLocal()
    try:
        f = Fact(world_id=world.id, game_session_id=session_id, content=content, visible_to_players=visible)
        db.add(f)
        db.commit()
        return f.id
    finally:
        db.close()


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


def _wait_recap_job_done(job_id, timeout=5.0):
    """Poll the job row (directly, not the /api/audio-jobs route — that one
    is GM-gated and several callers here are players) until the background
    task reaches a terminal status. The task runs on the app's event loop
    thread, so it makes progress while this thread sleeps."""
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        db = SessionLocal()
        try:
            job = db.get(AudioJob, job_id)
            status = job.status if job else None
        finally:
            db.close()
        if status in ("done", "error", "cancelled", "interrupted"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"recap job {job_id} never reached a terminal status, last seen: {status}")


def _recap_job_count(session_id):
    db = SessionLocal()
    try:
        return db.query(AudioJob).filter(
            AudioJob.purpose == "session_log_recap", AudioJob.game_session_id == session_id,
        ).count()
    finally:
        db.close()


# ── Chronicler /api/chronicler/ask ──────────────────────────────────────────

def test_chronicler_repeated_question_hits_cache(client, seed, monkeypatch):
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None):
        calls.append(1)
        return "An answer."
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})
    r2 = client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert len(calls) == 1


def test_chronicler_question_is_case_insensitive_for_caching(client, seed, monkeypatch):
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None):
        calls.append(1)
        return "An answer."
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    _login_gm_in(client, seed, seed.world_a)
    client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})
    client.post("/api/chronicler/ask", json={"question": "who is elyra?"})
    assert len(calls) == 1


def test_chronicler_different_question_is_not_cached(client, seed, monkeypatch):
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None):
        calls.append(1)
        return "An answer."
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    _login_gm_in(client, seed, seed.world_a)
    client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})
    client.post("/api/chronicler/ask", json={"question": "Who is Gareth?"})
    assert len(calls) == 2


def test_chronicler_cache_is_per_user_not_shared_across_players(client, seed, monkeypatch):
    """Two different non-GM users asking the identical question must NOT
    share a cache entry — visible_facts/find_relevant_entities can
    legitimately differ per player (an entity individually shared with one
    but not the other), so serving player B a cached answer built for
    player A's context would be a real content-leak risk, not just a
    caching nuance."""
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None):
        calls.append(1)
        return "An answer."
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})

    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/chronicler/ask", json={"question": "Who is Elyra?"})

    assert len(calls) == 2


# ── Session log /api/session-log/{id}/recap ─────────────────────────────────
#
# The "cache" here is a done session_log_recap background job: the first
# POST answers {"pending": true} and starts the job, later POSTs return the
# finished recap straight off the job row until a Fact write moves the
# session's newest Fact timestamp (or a recap-instructions save / fact
# deletion moves World.recap_content_touch) past the job's created_at.

def test_session_log_recap_repeated_call_hits_cache(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", visible=True)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(1)
        return "A recap."
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert r1.status_code == 200
    job_id = r1.json()["job_id"]
    assert r1.json()["pending"] is True
    assert _wait_recap_job_done(job_id) == "done"

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json() == {"recap": "A recap."}
    assert len(calls) == 1
    assert _recap_job_count(session_id) == 1  # served from the done job, no second one created


def test_session_log_recap_cache_separate_for_gm_and_player(client, seed, monkeypatch):
    """GM and player see different fact sets for the same session — must
    never share a cache entry (they now don't even share a JOB: the lookup
    key includes the audience, since each audience's recap is generated from
    its own fact visibility filter)."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "Public fact", visible=True)
    _add_fact(seed.world_a, session_id, "Secret fact", visible=False)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(sorted(facts))
        return "recap:" + ",".join(sorted(facts))
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r_player_create = client.post(f"/api/session-log/{session_id}/recap")
    player_job_id = r_player_create.json()["job_id"]
    assert _wait_recap_job_done(player_job_id) == "done"

    _login_gm_in(client, seed, seed.world_a)
    r_gm_create = client.post(f"/api/session-log/{session_id}/recap")
    gm_job_id = r_gm_create.json()["job_id"]
    assert gm_job_id != player_job_id
    assert _wait_recap_job_done(gm_job_id) == "done"

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r_player = client.post(f"/api/session-log/{session_id}/recap")
    _login_gm_in(client, seed, seed.world_a)
    r_gm = client.post(f"/api/session-log/{session_id}/recap")

    assert r_player.json() != r_gm.json()
    assert len(calls) == 2
    assert calls[0] == ["Public fact"]
    assert calls[1] == ["Public fact", "Secret fact"]
    assert _recap_job_count(session_id) == 2  # one per audience, not one per call


def test_session_log_recap_cache_invalidated_by_new_fact(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "First fact", visible=True)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(1)
        return "recap #" + str(len(calls))
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #1"}

    # Log a new fact via the ordinary Facts form — must invalidate the done job.
    client.post("/facts/new", data={"content": "Second fact", "game_session_id": str(session_id)})

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json()["pending"] is True  # stale recap not served — a NEW job is created
    assert _wait_recap_job_done(r2.json()["job_id"]) == "done"

    r3 = client.post(f"/api/session-log/{session_id}/recap")
    assert r3.json() == {"recap": "recap #2"}
    assert len(calls) == 2
    assert _recap_job_count(session_id) == 2


def test_session_log_recap_cache_invalidated_by_recap_instructions_save(client, seed, monkeypatch):
    """The recap baked into a done job includes World.recap_instructions (it
    is summarize_session_from_facts' extra_instructions input, see
    app/audio_jobs.py's session_log_recap branch), not just Fact content —
    a GM editing that steering text via POST /api/ai/recap-instructions
    must also bust the cache, or the finished recap would keep the old
    instructions forever (the job row never expires on its own)."""
    session_id = _make_session(seed.world_a)
    _add_fact(seed.world_a, session_id, "A fact", visible=True)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(extra_instructions)
        return "recap #" + str(len(calls))
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #1"}

    r_save = client.post("/api/ai/recap-instructions", json={"instructions": "write in French"})
    assert r_save.status_code == 200

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json()["pending"] is True
    assert _wait_recap_job_done(r2.json()["job_id"]) == "done"

    r3 = client.post(f"/api/session-log/{session_id}/recap")
    assert r3.json() == {"recap": "recap #2"}
    assert len(calls) == 2
    assert "write in French" in calls[1]


def test_session_log_recap_cache_invalidated_by_fact_delete(client, seed, monkeypatch):
    session_id = _make_session(seed.world_a)
    fact_id = _add_fact(seed.world_a, session_id, "A fact", visible=True)

    calls = []

    async def fake_summarize(facts, model="", extra_instructions="", think=True, world_context=""):
        calls.append(1)
        return "recap #" + str(len(calls))
    from app import ai as ai_module
    monkeypatch.setattr(ai_module, "summarize_session_from_facts", fake_summarize)

    _login_gm_in(client, seed, seed.world_a)
    r1 = client.post(f"/api/session-log/{session_id}/recap")
    assert _wait_recap_job_done(r1.json()["job_id"]) == "done"
    assert client.post(f"/api/session-log/{session_id}/recap").json() == {"recap": "recap #1"}

    client.post(f"/facts/{fact_id}/delete")

    r2 = client.post(f"/api/session-log/{session_id}/recap")
    assert r2.json()["pending"] is True  # the done recap is stale — regenerate
    job_id = r2.json()["job_id"]
    assert _wait_recap_job_done(job_id) == "done"

    r3 = client.post(f"/api/session-log/{session_id}/recap")
    assert r3.json()["empty"] is True
    assert r3.json()["recap"] == ""
    assert len(calls) == 1  # neither generation had anything to summarize after the delete
