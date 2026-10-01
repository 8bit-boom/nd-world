"""Local-AI sheet analysis (app/routers/character_ai.py): owner-or-GM only,
read-only, rules-grounded, and safe to hand to a player — GM-only rule blocks
and world lore must never reach a player-triggered prompt.

The endpoint tests monkeypatch the background task (the sibling suites'
convention) to check gating/validation; the task tests call it directly with
a stubbed model to inspect the real prompt and the cleaning of the reply.
"""
import asyncio
import json
import time

from app.database import SessionLocal
from app.models import PlayerCharacter, User, World, WorldMembership
from app.routers import character_ai as cai

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, _PLAYER_PASSWORD_HASH, login


# ── Setup ────────────────────────────────────────────────────────────────────

def _pc(owner, world, name="Kestrel", **fields):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world.id, owner_user_id=owner.id if owner else None, name=name,
                             race="Human", char_class="Rogue", level=3, xp=120, **fields)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _set_rules(world, md):
    db = SessionLocal()
    try:
        db.get(World, world.id).rules_md = md
        db.commit()
    finally:
        db.close()


RULES = "# Rules\nStats go from 1 to 10.\n\n:::gm\nSECRET_GM_RULE: the boss cheats.\n:::\n\nHP is 10 + Body."


class FakeTask:
    """Stands in for _analysis_task: records the call, leaves the job running."""
    def __init__(self):
        self.calls = []

    async def __call__(self, job_id, pc_id, viewer_is_gm, focus, think, use_rag):
        self.calls.append(dict(job_id=job_id, pc_id=pc_id, viewer_is_gm=viewer_is_gm,
                               focus=focus, think=think, use_rag=use_rag))


def _setup_endpoint(monkeypatch):
    cai._ANALYSIS_JOBS.clear()
    fake = FakeTask()
    monkeypatch.setattr(cai, "_analysis_task", fake)
    monkeypatch.setattr(cai._ai, "effective_llm_api_key", lambda: "sk-test")
    # cooldown is process-global state; start each test clean
    from app import deps
    deps._llm_cooldowns.clear()
    return fake


def _start(client, pc, **data):
    return client.post(f"/api/characters/{pc}/analyze", data=data)


# ── Endpoint gating ──────────────────────────────────────────────────────────

def test_owner_and_gm_can_start_an_analysis(client, seed, monkeypatch):
    _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = _start(client, pc, focus="rules")
    assert r.status_code == 200 and r.json()["status"] == "running"
    cai._ANALYSIS_JOBS.clear()
    login(client, seed.gm.email, GM_PASSWORD)
    assert _start(client, pc).status_code == 200


def test_everyone_else_gets_404(client, seed, monkeypatch):
    fake = _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert _start(client, pc).status_code == 404
    assert _start(client, 999999).status_code == 404
    assert fake.calls == [], "no generation may start for a non-owner"


def test_anonymous_is_refused(client, seed, monkeypatch):
    _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    assert _start(client, pc).status_code in (401, 403)


def test_validation(client, seed, monkeypatch):
    _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert _start(client, pc, focus="nonsense").status_code == 400
    monkeypatch.setattr(cai._ai, "effective_llm_api_key", lambda: "")
    r = _start(client, pc)
    assert r.status_code == 400 and "No AI backend" in r.json()["detail"]


def test_double_click_joins_the_running_job(client, seed, monkeypatch):
    fake = _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    first = _start(client, pc).json()["job_id"]
    second = _start(client, pc).json()["job_id"]
    assert first == second and len(fake.calls) == 1


def test_players_are_rate_limited_but_gms_are_not(client, seed, monkeypatch):
    _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _start(client, pc)
    for job in cai._ANALYSIS_JOBS.values():
        job["status"] = "done"
    assert _start(client, pc).status_code == 429
    login(client, seed.gm.email, GM_PASSWORD)
    assert _start(client, pc).status_code == 200
    for job in cai._ANALYSIS_JOBS.values():
        job["status"] = "done"
    assert _start(client, pc).status_code == 200


def test_task_receives_the_callers_role(client, seed, monkeypatch):
    fake = _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _start(client, pc, focus="next_steps", think="false", use_rag="true")
    assert fake.calls[0]["viewer_is_gm"] is False and fake.calls[0]["focus"] == "next_steps"
    assert fake.calls[0]["think"] is False


# ── Polling ──────────────────────────────────────────────────────────────────

def test_poll_is_starter_or_gm_and_scoped_to_the_character(client, seed, monkeypatch):
    _setup_endpoint(monkeypatch)
    pc = _pc(seed.player_a, seed.world_a)
    other = _pc(seed.player_a, seed.world_a, "Other")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    jid = _start(client, pc).json()["job_id"]
    assert client.get(f"/api/characters/{pc}/analyze/{jid}").json()["status"] == "running"
    assert client.get(f"/api/characters/{other}/analyze/{jid}").status_code == 404, "job belongs to another character"
    assert client.get(f"/api/characters/{pc}/analyze/9999").status_code == 404

    cai._ANALYSIS_JOBS[jid].update(status="done", result={"verdict": "ok"}, focus="overview")
    d = client.get(f"/api/characters/{pc}/analyze/{jid}").json()
    assert d["status"] == "done" and d["result"]["verdict"] == "ok"
    cai._ANALYSIS_JOBS[jid].update(status="error", error="boom")
    assert client.get(f"/api/characters/{pc}/analyze/{jid}").json() == {"status": "error", "error": "boom"}

    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert client.get(f"/api/characters/{pc}/analyze/{jid}").status_code == 404
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get(f"/api/characters/{pc}/analyze/{jid}").status_code == 200


# ── The task itself: prompt contents and reply handling ─────────────────────

def _run_task(pc, viewer_is_gm, monkeypatch, reply, focus="rules", use_rag=True, lore_spy=None):
    captured = {}

    async def fake_chat(messages, system="", model="", think=True, format=None, **kw):
        captured.update(system=system, user=messages[0]["content"], think=think, format=format)
        return reply

    monkeypatch.setattr(cai._ai, "generate_chat", fake_chat)
    if lore_spy is not None:
        monkeypatch.setattr(cai._retrieval, "smart_world_context", lore_spy)
    cai._ANALYSIS_JOBS[1] = {"status": "running", "started": time.time(), "pc_id": pc,
                             "user_id": 1, "focus": focus, "result": None, "error": ""}
    asyncio.run(cai._analysis_task(1, pc, viewer_is_gm, focus, True, use_rag))
    return captured, cai._ANALYSIS_JOBS[1]


GOOD = json.dumps({
    "verdict": "Solid rogue.", "strengths": ["Fast"],
    "issues": [{"severity": "warn", "text": "HP is low."}], "suggestions": ["Buy armor."],
})


def test_player_prompt_has_the_sheet_but_no_gm_only_rules(client, seed, monkeypatch):
    _set_rules(seed.world_a, RULES)
    pc = _pc(seed.player_a, seed.world_a, backstory="Raised on the docks.")
    cap, job = _run_task(pc, False, monkeypatch, GOOD)
    assert job["status"] == "done" and job["result"]["verdict"] == "Solid rogue."
    assert "Stats go from 1 to 10" in cap["user"] and "HP is 10 + Body" in cap["user"]
    assert "SECRET_GM_RULE" not in cap["user"] and "SECRET_GM_RULE" not in cap["system"]
    assert "Kestrel" in cap["user"] and "Raised on the docks." in cap["user"], "the sheet must be in the prompt"
    assert "Never" in cap["system"] and "FOCUS" in cap["system"] and cap["format"]


def test_gm_prompt_keeps_the_gm_rules(client, seed, monkeypatch):
    _set_rules(seed.world_a, RULES)
    pc = _pc(seed.player_a, seed.world_a)
    cap, _ = _run_task(pc, True, monkeypatch, GOOD)
    assert "SECRET_GM_RULE" in cap["user"]


def test_world_lore_is_gm_only(client, seed, monkeypatch):
    pc = _pc(seed.player_a, seed.world_a)
    calls = []

    def spy(db, world_id, query, **kw):
        calls.append(query)
        return ("SECRET LORE: the duke is a vampire", 1, 0)

    cap, _ = _run_task(pc, False, monkeypatch, GOOD, use_rag=True, lore_spy=spy)
    assert calls == [] and "SECRET LORE" not in cap["user"], "a player-triggered run must not retrieve lore"
    cap, _ = _run_task(pc, True, monkeypatch, GOOD, use_rag=True, lore_spy=spy)
    assert "SECRET LORE" in cap["user"]
    cap, _ = _run_task(pc, True, monkeypatch, GOOD, use_rag=False, lore_spy=spy)
    assert "SECRET LORE" not in cap["user"]


def test_reply_is_clamped_to_a_safe_shape(client, seed, monkeypatch):
    pc = _pc(seed.player_a, seed.world_a)
    messy = json.dumps({
        "verdict": "v" * 2000,
        "strengths": ["s"] * 20 + [{"not": "a string"}],
        "issues": [{"severity": "CATASTROPHIC", "text": "bad"}, "plain string", {"severity": "error"},
                   {"severity": "error", "text": "t" * 2000}] + [{"text": "x"}] * 20,
        "suggestions": "not a list",
    })
    _, job = _run_task(pc, True, monkeypatch, f"```json\n{messy}\n```")
    r = job["result"]
    assert len(r["verdict"]) == 600 and len(r["strengths"]) == 8 and r["suggestions"] == []
    assert len(r["issues"]) <= 10 and all(i["severity"] in ("error", "warn", "note") for i in r["issues"])
    assert r["issues"][0] == {"severity": "note", "text": "bad"}
    assert {"severity": "note", "text": "plain string"} in r["issues"]
    assert all(i["text"] for i in r["issues"]) and all(len(i["text"]) <= 420 for i in r["issues"])


def test_failures_become_a_job_error_not_a_crash(client, seed, monkeypatch):
    pc = _pc(seed.player_a, seed.world_a)
    _, job = _run_task(pc, True, monkeypatch, "[AI error: backend exploded]")
    assert job["status"] == "error" and "backend exploded" in job["error"]
    _, job = _run_task(pc, True, monkeypatch, "I refuse to answer in JSON.")
    assert job["status"] == "error"
    _, job = _run_task(pc, True, monkeypatch, json.dumps({"verdict": "", "issues": []}))
    assert job["status"] == "error" and "usable" in job["error"]


# ── The panel on the sheet page ──────────────────────────────────────────────

def test_panel_shows_for_owner_and_gm_but_not_for_a_viewer(client, seed):
    pc = _pc(seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    owner_html = client.get(f"/characters/{pc}").text
    assert 'id="pc-ai"' in owner_html and 'id="pcai-rag"' not in owner_html, "world-lore toggle is GM-only"
    login(client, seed.gm.email, GM_PASSWORD)
    gm_html = client.get(f"/characters/{pc}").text
    assert 'id="pc-ai"' in gm_html and 'id="pcai-rag"' in gm_html

    db = SessionLocal()
    try:
        mate = User(email="mate3@test.local", password_hash=_PLAYER_PASSWORD_HASH, display_name="Mate3", is_gm=False)
        db.add(mate)
        db.commit()
        db.refresh(mate)
        db.add(WorldMembership(world_id=seed.world_a.id, user_id=mate.id))
        db.commit()
        mate_email = mate.email
    finally:
        db.close()
    login(client, mate_email, PLAYER_PASSWORD)
    r = client.get(f"/characters/{pc}")
    assert r.status_code == 200 and 'id="pc-ai"' not in r.text
