"""AI template drafting: a rulebook becomes ONE reviewed template draft. The model runs in pieces for a
long book, its JSON is sanitised before the GM sees it, only a GM can start or poll a job, and the
reviewed draft saves through the same route as a hand-made template — fully integrated."""
import asyncio
import json
import time

import pytest

from app.database import SessionLocal
from app.models import SheetTemplate
from app.routers import template_ai as tai
from app.sheet_systems import system_meta, system_spec, template_fields

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")

GOOD = {
    "name": "Model Name", "description": "Post-war survival.",
    "fields": [
        {"id": "callsign", "label": "Callsign", "type": "text", "section": "Who", "binds": "name"},
        {"id": "vigor", "label": "Vigor", "type": "resource", "section": "Body", "default_value": "6/6", "vital": "hp"},
        {"id": "xp", "label": "XP", "type": "number", "section": "Growth", "xp": True},
        {"id": "gear", "label": "Gear", "type": "list", "section": "Kit", "item_fields": [{"id": "item", "label": "Item", "type": "text"}]},
    ],
    "system": {"conditions": ["Wounded"], "rest": {"long": [["full", "vigor"]]}},
    "rules_md": "# Ashfall\nRoll 2d6.",
}


def _run(source, replies, monkeypatch, name="", wishes="", think=True):
    """Run the draft job with a scripted model; returns (calls, job)."""
    calls = []
    script = list(replies)

    async def fake_chat(messages, system="", model="", think=True, format=None, **kw):
        calls.append({"system": system, "user": messages[0]["content"], "format": format, "think": think})
        r = script.pop(0) if len(script) > 1 else script[0]
        return r if isinstance(r, str) else json.dumps(r)

    monkeypatch.setattr(tai._ai, "generate_chat", fake_chat)
    tai._TPL_AI_JOBS[1] = {"status": "running", "started": time.time(), "user_id": 1, "draft": None, "warnings": [],
                           "error": "", "progress": "", "truncated": False, "parts_total": 0, "parts_read": 0}
    asyncio.run(tai._draft_task(1, source, name, wishes, think))
    return calls, tai._TPL_AI_JOBS[1]


def test_a_short_rulebook_goes_straight_to_one_drafting_call(monkeypatch):
    calls, job = _run("Roll 2d6. Vigor is your hit points.", [GOOD], monkeypatch, wishes="keep it small")
    assert job["status"] == "done", job
    assert len(calls) == 1 and "Roll 2d6" in calls[0]["user"] and "keep it small" in calls[0]["user"]
    assert "ONE JSON object" in calls[0]["system"] and calls[0]["format"]["required"] == ["name", "fields"]
    d = job["draft"]
    assert d["sheet_mode"] == "custom" and d["name"] == "Model Name"
    assert [f["id"] for f in d["fields"]] == ["callsign", "vigor", "xp", "gear"]
    assert d["system"]["rest"]["long"] == [["full", "vigor"]] and job["warnings"] == []


def test_the_gms_system_name_wins_and_the_model_output_is_sanitised(monkeypatch):
    bad = dict(GOOD, fields=GOOD["fields"] + [
        {"id": "Hit Points!", "label": "HP", "type": "resource", "vital": "hp"},      # second HP track
        {"id": "mood", "label": "Mood", "type": "select"},                           # select, no options
    ], system={"rest": {"long": [["full", "ghost"], ["full", "vigor"]]}, "hp": "nope"})
    calls, job = _run("short rules", [bad], monkeypatch, name="Ashfall")
    d = job["draft"]
    assert d["name"] == "Ashfall"
    assert sum(1 for f in d["fields"] if f.get("vital")) == 1
    assert next(f for f in d["fields"] if f["id"] == "mood")["type"] == "text"
    assert d["system"]["rest"]["long"] == [["full", "vigor"]]
    assert job["warnings"], "everything it had to fix is reported"


def test_a_missing_rules_digest_falls_back_to_the_reading_notes(monkeypatch):
    calls, job = _run("short rules text", [dict(GOOD, rules_md="")], monkeypatch)
    assert job["status"] == "done"
    assert job["draft"]["rules_md"].startswith("# Model Name — rules notes") and "short rules text" in job["draft"]["rules_md"]
    assert any("digest" in w for w in job["warnings"])


def test_a_long_rulebook_is_read_in_pieces_then_drafted_once(monkeypatch):
    book = "\n\n".join(f"## Chapter {i}\n" + ("Your character sheet tracks Health and Stamina. Resting restores Stamina. " * 80)
                       for i in range(8))
    assert len(book) > tai._DIRECT_CHARS
    calls, job = _run(book, ["- Health and Stamina are resources", GOOD], monkeypatch)
    assert job["status"] == "done", job
    notes_calls, draft_call = calls[:-1], calls[-1]
    assert len(notes_calls) == job["parts_read"] >= 2 and job["parts_total"] >= job["parts_read"]
    assert all(c["system"] == tai.NOTES_SYSTEM and c["think"] is False and c["format"] is None for c in notes_calls)
    assert draft_call["system"] == tai.DRAFT_SYSTEM and draft_call["format"] is tai._DRAFT_FORMAT
    assert "Health and Stamina are resources" in draft_call["user"], "the notes, not the raw book, reach the final call"
    assert "Your character sheet tracks" not in draft_call["user"]


def test_a_huge_book_reads_only_the_most_relevant_parts(monkeypatch):
    lore = "The ancient wars were long and cruel. " * 400
    sheet = "Your character sheet tracks Health, Stamina and Hunger. Resting restores Stamina. Advancement costs XP. " * 50
    book = "\n\n".join(f"## Part {i}\n" + (sheet if i % 5 == 0 else lore) for i in range(60))
    calls, job = _run(book, ["- notes", GOOD], monkeypatch)
    assert job["status"] == "done" and job["parts_read"] == tai._MAX_PARTS < job["parts_total"]
    assert len(calls) == tai._MAX_PARTS + 1


def test_a_book_with_nothing_about_sheets_is_an_error(monkeypatch):
    book = "Lore. " * 6000
    calls, job = _run(book, ["NOTHING"], monkeypatch)
    assert job["status"] == "error" and "Nothing in this text" in job["error"]
    assert len(calls) == job["parts_read"], "no drafting call is wasted when there are no notes"


def test_garbage_from_the_model_is_a_clean_error_not_a_crash(monkeypatch):
    for reply in ("I cannot do that.", "{}", json.dumps({"name": "x", "fields": [{"id": "a"}]}), "[1,2,3]"):
        calls, job = _run("short rules", [reply], monkeypatch)
        assert job["status"] == "error" and job["error"], reply


# ── endpoints ────────────────────────────────────────────────────────────────

def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_only_a_gm_can_open_the_page_start_or_poll(client, seed, monkeypatch):
    monkeypatch.setattr(tai._ai, "effective_llm_api_key", lambda: "sk-test")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/characters/templates/ai-new").status_code == 403
    assert client.post("/api/sheet-templates/ai/start", data={"rules_text": "rules"}).status_code == 403
    tai._TPL_AI_JOBS[7] = {"status": "done", "started": time.time(), "user_id": 1, "draft": {"name": "x"}, "warnings": [],
                           "error": "", "progress": "", "parts_read": 0, "parts_total": 0}
    assert client.get("/api/sheet-templates/ai/7").status_code == 403
    client.cookies.clear()
    r = client.post("/api/sheet-templates/ai/start", data={"rules_text": "rules"}, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)


def test_the_page_renders_for_the_gm_and_is_linked_from_the_template_list(client, seed):
    _gm(client, seed)
    html = client.get("/characters/templates/ai-new").text
    assert 'id="t-go"' in html and 'action="/characters/templates/new' in html and 'name="rules_md"' in html
    assert "/characters/templates/ai-new" in client.get("/characters/templates").text


def test_start_validates_the_input(client, seed, monkeypatch):
    _gm(client, seed)
    started = []

    async def fake_task(*a):
        started.append(a)

    monkeypatch.setattr(tai, "_draft_task", fake_task)
    monkeypatch.setattr(tai._ai, "effective_llm_api_key", lambda: "")
    assert client.post("/api/sheet-templates/ai/start", data={"rules_text": "rules"}).status_code == 400, "no AI backend"
    monkeypatch.setattr(tai._ai, "effective_llm_api_key", lambda: "sk-test")
    assert client.post("/api/sheet-templates/ai/start", data={"rules_text": "   "}).status_code == 400, "nothing to read"
    r = client.post("/api/sheet-templates/ai/start", files={"file": ("book.docx", b"x")})
    assert r.status_code == 400 and "Word" in r.text
    r = client.post("/api/sheet-templates/ai/start", files={"file": ("book.pdf", b"not a pdf")})
    assert r.status_code == 400
    r = client.post("/api/sheet-templates/ai/start", files={"file": ("book.exe", b"MZ")})
    assert r.status_code == 400 and "Unsupported" in r.text
    assert not started

    r = client.post("/api/sheet-templates/ai/start", data={"rules_text": "Roll 2d6.", "system_name": "  Ash   fall ", "wishes": "small"},
                    files={"file": ("book.md", b"# Book\nVigor is HP.")})
    assert r.status_code == 200, r.text
    assert started and "Roll 2d6." in started[0][1] and "Vigor is HP." in started[0][1]
    assert started[0][2] == "Ash fall" and started[0][3] == "small"


def test_start_can_read_the_worlds_own_rules(client, seed, monkeypatch):
    _gm(client, seed)
    started = []

    async def fake_task(*a):
        started.append(a)

    monkeypatch.setattr(tai, "_draft_task", fake_task)
    monkeypatch.setattr(tai._ai, "effective_llm_api_key", lambda: "sk-test")
    r = client.post("/api/sheet-templates/ai/start", data={"use_world_rules": "true"})
    assert r.status_code == 200 and started and started[0][1].strip()


def test_poll_reports_running_error_and_done(client, seed):
    _gm(client, seed)
    base = {"started": time.time(), "user_id": 1, "draft": None, "warnings": [], "error": "", "progress": "Reading part 2 of 5…",
            "parts_read": 5, "parts_total": 9}
    tai._TPL_AI_JOBS[11] = dict(base, status="running")
    tai._TPL_AI_JOBS[12] = dict(base, status="error", error="boom")
    tai._TPL_AI_JOBS[13] = dict(base, status="done", draft={"name": "X"}, warnings=["w"])
    assert client.get("/api/sheet-templates/ai/11").json()["progress"] == "Reading part 2 of 5…"
    assert client.get("/api/sheet-templates/ai/12").json() == {"status": "error", "error": "boom"}
    d = client.get("/api/sheet-templates/ai/13").json()
    assert d["status"] == "done" and d["draft"] == {"name": "X"} and d["warnings"] == ["w"] and d["parts_total"] == 9
    assert client.get("/api/sheet-templates/ai/9999").status_code == 404


# ── the reviewed draft becomes a fully integrated template ───────────────────

def test_a_reviewed_draft_saves_through_the_normal_route_and_is_integrated(client, seed, monkeypatch):
    calls, job = _run("Vigor is your hit points.", [GOOD], monkeypatch, name="Ashfall")
    d = job["draft"]
    _gm(client, seed)
    r = client.post("/characters/templates/new", data={
        "name": d["name"], "description": d["description"], "sheet_mode": d["sheet_mode"],
        "fields_json": json.dumps(d["fields"]), "system_json": json.dumps(d["system"]), "rules_md": d["rules_md"]},
        follow_redirects=False)
    assert r.status_code == 303, r.text
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.name == "Ashfall").first()
        assert tpl.sheet_mode == "custom" and not tpl.is_builtin
        meta = system_meta(tpl)
        assert meta["hp"] == "vigor" and meta["xp"] == ["xp"] and meta["binds"] == {"callsign": "name"}
        assert system_spec(tpl)["conditions"] == ["Wounded"] and system_spec(tpl)["rest"]["long"] == [["full", "vigor"]]
        assert len(template_fields(tpl)) == 4 and "Roll 2d6" in tpl.rules_md
    finally:
        db.close()


def test_the_handlers_refuse_non_gms_even_if_the_middleware_were_bypassed():
    """Defence in depth: the auth middleware already 403s a player, but the handlers check too."""
    from types import SimpleNamespace
    from fastapi import HTTPException

    def req(user):
        return SimpleNamespace(state=SimpleNamespace(user=user))

    tai._TPL_AI_JOBS[21] = {"status": "done", "started": time.time(), "user_id": 5, "draft": {"name": "x"}, "warnings": [],
                            "error": "", "progress": "", "parts_read": 0, "parts_total": 0}
    for user in (None, SimpleNamespace(id=5, is_gm=False)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(tai.template_ai_poll(21, req(user)))
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e:
            asyncio.run(tai.template_ai_start(req(user), rules_text="rules", db=None, active_world=None))
        assert e.value.status_code == 403
