"""The AI reviews a character against ITS system's rules. A Hunt in the Moonlight
character used to be judged against "standard N&D" (the bundled core rules / the
"(no custom rules — standard N&D)" fallback), and its saved resource values and
conditions were missing from the sheet text the model saw."""
import asyncio
import json
import time

import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate, World
from app.routers import character_ai as cai

pytestmark = pytest.mark.usefixtures("client")

GOOD = json.dumps({"verdict": "ok", "strengths": [], "issues": [], "suggestions": ["x"]})


def _tpl(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _pc(seed, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name=kw.pop("name", "Anders"), max_hp=0, **kw)
        db.add(pc)
        db.commit()
        return pc.id
    finally:
        db.close()


def _hunter(seed):
    return _pc(seed, sheet_template_id=_tpl("hunt-in-the-moonlight"), conditions_json=json.dumps(["Stunned"]),
               custom_fields_json=json.dumps({"health_current": 2, "health_max": 5, "stamina_current": 1, "stamina_max": 6}))


def _set_rules(seed, md):
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).rules_md = md
        db.commit()
    finally:
        db.close()


def _run(pc, monkeypatch, viewer_is_gm=True, focus="rules"):
    captured = {}

    async def fake_chat(messages, system="", model="", think=True, format=None, **kw):
        captured.update(system=system, user=messages[0]["content"])
        return GOOD

    monkeypatch.setattr(cai._ai, "generate_chat", fake_chat)
    cai._ANALYSIS_JOBS[1] = {"status": "running", "started": time.time(), "pc_id": pc, "user_id": 1,
                             "focus": focus, "result": None, "error": ""}
    asyncio.run(cai._analysis_task(1, pc, viewer_is_gm, focus, True, False))
    assert cai._ANALYSIS_JOBS[1]["status"] == "done", cai._ANALYSIS_JOBS[1]
    return captured


def test_a_hunter_is_reviewed_against_hunt_in_the_moonlight(seed, monkeypatch):
    c = _run(_hunter(seed), monkeypatch)
    assert "HUNT IN THE MOONLIGHT RULES" in c["user"]
    assert "Overextended" in c["user"] and "Press On" in c["user"], "the system's rules digest is in the prompt"
    assert "standard N&D" not in c["user"]
    assert "Hunt in the Moonlight" in c["system"] and "do not apply Neon & Dragons" in c["system"]


def test_the_models_sheet_text_has_the_real_state(seed, monkeypatch):
    c = _run(_hunter(seed), monkeypatch)
    assert "2 / 5" in c["user"] and "1 / 6" in c["user"], "saved current/max, not template defaults"
    assert "Stunned" in c["user"]


def test_the_world_default_core_rules_are_not_used_for_a_custom_system(seed, monkeypatch):
    from app import retrieval
    core = retrieval.world_rules_markdown(None)
    assert core.strip(), "the bundled N&D core rules exist"
    c = _run(_hunter(seed), monkeypatch)
    assert core[:200] not in c["user"]


def test_a_gms_own_world_rules_are_added_after_the_digest(seed, monkeypatch):
    _set_rules(seed, "# House rules\nNo hunting in daylight.")
    c = _run(_hunter(seed), monkeypatch)
    assert c["user"].index("Overextended") < c["user"].index("No hunting in daylight")
    assert "This world's own rules" in c["user"]


def test_gm_only_blocks_stay_out_of_a_players_prompt(seed, monkeypatch):
    _set_rules(seed, "# House\nVisible rule.\n\n:::gm\nSECRET_GM_RULE\n:::\n")
    c = _run(_hunter(seed), monkeypatch, viewer_is_gm=False)
    assert "Visible rule" in c["user"] and "SECRET_GM_RULE" not in c["user"]


def test_asterion_gets_its_own_digest(seed, monkeypatch):
    pc = _pc(seed, sheet_template_id=_tpl("asterion"), custom_fields_json=json.dumps({"flesh_current": 3, "flesh_max": 5}))
    c = _run(pc, monkeypatch)
    assert "ASTERION RULES" in c["user"] and "Spark Shield" in c["user"] and "3 / 5" in c["user"]


def test_a_gms_own_template_without_world_rules_says_so(seed, monkeypatch):
    db = SessionLocal()
    try:
        t = SheetTemplate(world_id=seed.world_a.id, name="Homebrew", slug="homebrew-ai", sheet_mode="custom",
                          fields_json=json.dumps([{"id": "grit", "label": "Grit", "type": "number"}]))
        db.add(t)
        db.commit()
        tid = t.id
    finally:
        db.close()
    c = _run(_pc(seed, sheet_template_id=tid, custom_fields_json=json.dumps({"grit": 4})), monkeypatch)
    assert "HOMEBREW RULES" in c["user"] and "no written rules for this system" in c["user"]
    assert "Grit" in c["user"]


def test_a_native_character_is_unchanged(seed, monkeypatch):
    pc = _pc(seed, stats_json=json.dumps([{"id": "str", "value": 3}]))
    c = _run(pc, monkeypatch)
    assert "=== WORLD RULES ===" in c["user"] and "this character plays Neon & Dragons" in c["system"]
    assert "do not apply Neon & Dragons" not in c["system"]
