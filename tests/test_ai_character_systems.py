"""The AI character creator drafts for the chosen system: a custom sheet gets its own
field values (sanitised), the right rules and the field catalogue — not N&D stats —
and the review form posts the template id + custom_fields_json to the normal create route."""
import asyncio
import json
import time

import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate
from app.routers import character_ai as cai

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _tpl_id(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _run(seed, reply, template_id, monkeypatch, prompt="a haunted tracker"):
    captured = {}

    async def fake_chat(messages, system="", model="", think=True, format=None, **kw):
        captured.update(system=system, user=messages[0]["content"], format=format)
        return reply

    monkeypatch.setattr(cai._ai, "generate_chat", fake_chat)
    cai._PC_AI_JOBS[1] = {"status": "running", "started": time.time(), "user_id": 1, "draft": None, "error": ""}
    asyncio.run(cai._pc_ai_task(1, seed.world_a.id, prompt, "", True, False, template_id))
    return captured, cai._PC_AI_JOBS[1]


DRAFT = json.dumps({
    "name": "Anders de Vallière", "player_name": "Archie", "backstory": "Raised on the docks.", "notes": "owes the Guild",
    "fields": {"race": "Human", "health": {"current": 5, "max": 5}, "armor": 2, "bogus": "x",
               "abilities": [{"source": "Tracker", "tier": "1", "effect": "Follow any trail"}]},
})


def test_custom_draft_uses_the_systems_rules_catalogue_and_sanitises(seed, monkeypatch):
    cap, job = _run(seed, DRAFT, _tpl_id("hunt-in-the-moonlight"), monkeypatch)
    assert job["status"] == "done", job
    assert "HUNT IN THE MOONLIGHT RULES" in cap["user"] and "Overextended" in cap["user"]
    assert "FIELD CATALOGUE" in cap["system"] and '"id": "health"' in cap["system"]
    assert "never Neon & Dragons attributes" in cap["system"]
    d = job["draft"]
    assert d["sheet_template_id"] == _tpl_id("hunt-in-the-moonlight") and d["system"] == "Hunt in the Moonlight"
    cf = d["custom_fields"]
    assert cf["race"] == "Human" and (cf["health_current"], cf["health_max"]) == (5, 5) and cf["armor"] == 2
    assert "bogus" not in cf and cf["abilities"][0]["effect"] == "Follow any trail"
    assert {"label": "Health", "value": "5 / 5"} in d["preview"]
    assert "stats" not in d and "max_hp" not in d, "no N&D shape on a custom draft"


def test_native_draft_is_unchanged(seed, monkeypatch):
    reply = json.dumps({"name": "Vex", "level": 2, "stats": {"str": 3}, "max_hp": 14})
    cap, job = _run(seed, reply, 0, monkeypatch)
    assert job["status"] == "done" and job["draft"]["stats"] == {"str": 3} and "sheet_template_id" not in job["draft"]
    assert "=== WORLD RULES ===" in cap["user"] and "FIELD CATALOGUE" not in cap["system"]


def test_a_non_custom_or_foreign_template_falls_back_to_native(seed, monkeypatch):
    db = SessionLocal()
    try:
        nd = db.query(SheetTemplate).filter(SheetTemplate.slug == "nd-default").first().id
        foreign = SheetTemplate(world_id=seed.world_b.id, name="B only", slug="b-only", sheet_mode="custom", fields_json="[]")
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    finally:
        db.close()
    reply = json.dumps({"name": "Vex", "stats": {"str": 3}})
    for tid in (nd, foreign_id):
        cap, job = _run(seed, reply, tid, monkeypatch)
        assert "FIELD CATALOGUE" not in cap["system"] and "sheet_template_id" not in job["draft"]


def test_endpoint_rejects_an_unlisted_system(client, seed, monkeypatch):
    monkeypatch.setattr(cai._ai, "effective_llm_api_key", lambda: "sk-test")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    nd = SessionLocal().query(SheetTemplate).filter(SheetTemplate.slug == "nd-default").first().id
    r = client.post("/api/characters/ai/start", data={"prompt": "a hunter", "template_id": str(nd)})
    assert r.status_code == 400 and "listed systems" in r.text
    r = client.post("/api/characters/ai/start", data={"prompt": "a hunter", "template_id": "999999"})
    assert r.status_code == 400


def test_endpoint_accepts_a_builtin_custom_system(client, seed, monkeypatch):
    calls = []

    async def fake_task(*args):
        calls.append(args)

    monkeypatch.setattr(cai, "_pc_ai_task", fake_task)
    monkeypatch.setattr(cai._ai, "effective_llm_api_key", lambda: "sk-test")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    tid = _tpl_id("asterion")
    r = client.post("/api/characters/ai/start", data={"prompt": "a minor god of storms", "template_id": str(tid)})
    assert r.status_code == 200, r.text
    assert calls and calls[0][6] == tid          # (job_id, world_id, prompt, source_text, think, use_rag, template_id, source_limit)


def test_page_offers_the_systems_and_can_preselect(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    tid = _tpl_id("hunt-in-the-moonlight")
    html = client.get(f"/characters/ai-new?template_id={tid}").text
    assert 'id="ai-system"' in html and "Asterion" in html
    assert f'<option value="{tid}" selected>' in html
    assert 'name="custom_fields_json"' in html and 'name="sheet_template_id"' in html


def test_applying_a_custom_draft_creates_the_character(client, seed):
    """The review form posts exactly these fields to the normal create route."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    tid = _tpl_id("hunt-in-the-moonlight")
    cf = {"race": "Human", "health_current": 5, "health_max": 5}
    r = client.post("/characters/new", data={"name": "Anders", "player_name": "Archie", "backstory": "b", "notes": "n",
                                             "sheet_template_id": str(tid), "custom_fields_json": json.dumps(cf)},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    db = SessionLocal()
    try:
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.name == "Anders").first()
        assert pc.sheet_template_id == tid and json.loads(pc.custom_fields_json)["health_current"] == 5
        from app.pc_stats import pc_maxima
        assert pc_maxima(pc)["native"] is False
    finally:
        db.close()
