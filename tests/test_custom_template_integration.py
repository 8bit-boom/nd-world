"""A GM-made template (saved through the editor routes) is integrated like a built-in system: its
Rest rules drive the party Rest, its conditions are the sheet's chips, its pages split the sheet, its
roster fields fill the roster, and its rules text grounds the AI. It also round-trips through
export/import."""
import asyncio
import json
import re
import time

import pytest

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate
from app.routers import character_ai as cai

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")

FIELDS = [
    {"id": "grit", "label": "Grit", "type": "resource", "section": "Core", "default_value": "3/3", "vital": "hp"},
    {"id": "focus", "label": "Focus", "type": "resource", "section": "Core", "default_value": "4/4"},
    {"id": "luck", "label": "Luck", "type": "number", "section": "Core", "default_value": "2"},
    {"id": "xp", "label": "XP", "type": "number", "section": "Progress", "default_value": "0", "xp": True},
    {"id": "path", "label": "Path", "type": "select", "options": ["Fox", "Owl"], "section": "Identity"},
    {"id": "bio", "label": "Bio", "type": "textarea", "section": "Story"},
    {"id": "kit", "label": "Kit", "type": "textarea", "section": "Gear"},
]
SPEC = {
    "conditions": ["Shaken", "Wounded"],
    "rest": {"short": [["add", "focus", 2]], "long": [["full", "grit"], ["full", "focus"]]},
    "pages": [{"label": "Who", "icon": "🧍", "sections": ["Identity", "Story", "Gear"]},
              {"label": "Stats", "icon": "❤", "sections": ["Core", "Progress"], "conditions": True}],
    "roster": [["Core", ["luck", "path"]]],
}
RULES = "# Homebrew\nRoll 2d6. Grit is your hit points; at 0 you are out."


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _create(client, seed, **over):
    _gm(client, seed)
    data = {"name": "Homebrew", "description": "mine", "sheet_mode": "custom", "fields_json": json.dumps(FIELDS),
            "system_json": json.dumps(SPEC), "rules_md": RULES}
    data.update(over)
    r = client.post("/characters/templates/new", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(re.search(r"/templates/(\d+)/edit", r.headers["location"]).group(1))
    return tid


def _tpl(tid):
    db = SessionLocal()
    try:
        t = db.get(SheetTemplate, tid)
        db.expunge(t)
        return t
    finally:
        db.close()


def _pc(seed, tid, **cf):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Pip", max_hp=0, sheet_template_id=tid,
                             custom_fields_json=json.dumps(cf))
        db.add(pc)
        db.commit()
        return pc.id
    finally:
        db.close()


# ── storage ──────────────────────────────────────────────────────────────────

def test_create_stores_the_cleaned_spec_and_rules(client, seed):
    tid = _create(client, seed)
    t = _tpl(tid)
    spec = json.loads(t.system_json)
    assert spec["conditions"] == ["Shaken", "Wounded"] and spec["rest"]["short"] == [["add", "focus", 2]]
    assert [p["label"] for p in spec["pages"]] == ["Who", "Stats"] and spec["roster"] == [["Core", ["luck", "path"]]]
    assert t.rules_md == RULES


def test_bad_spec_entries_are_dropped_not_stored(client, seed):
    bad = {"conditions": ["A"], "rest": {"long": [["full", "nope"], ["full", "luck"], ["full", "grit"]]},
           "pages": [{"label": "X", "sections": ["Nowhere"]}], "roster": [["R", ["ghost"]]], "evil": "<script>"}
    t = _tpl(_create(client, seed, system_json=json.dumps(bad)))
    assert json.loads(t.system_json) == {"conditions": ["A"], "rest": {"long": [["full", "grit"]]}}


def test_junk_system_json_and_oversized_rules_are_harmless(client, seed):
    t = _tpl(_create(client, seed, system_json="not json", rules_md="x" * 50000))
    assert json.loads(t.system_json) == {} and len(t.rules_md) <= 20000


def test_update_replaces_the_spec_and_keeps_a_builtins_name(client, seed):
    tid = _create(client, seed)
    r = client.post(f"/characters/templates/{tid}/edit", data={
        "name": "Homebrew 2", "description": "", "sheet_mode": "custom", "fields_json": json.dumps(FIELDS),
        "system_json": json.dumps({"conditions": ["Burned"]}), "rules_md": "new rules"}, follow_redirects=False)
    assert r.status_code == 303
    t = _tpl(tid)
    assert t.name == "Homebrew 2" and json.loads(t.system_json) == {"conditions": ["Burned"]} and t.rules_md == "new rules"
    db = SessionLocal()
    try:
        hitm = db.query(SheetTemplate).filter(SheetTemplate.slug == "hunt-in-the-moonlight").first()
        hid, fields = hitm.id, hitm.fields_json
    finally:
        db.close()
    client.post(f"/characters/templates/{hid}/edit", data={
        "name": "Hacked", "description": "", "sheet_mode": "nd", "fields_json": fields,
        "system_json": json.dumps({"conditions": ["Cursed"]}), "rules_md": "House rule."}, follow_redirects=False)
    h = _tpl(hid)
    assert h.name == "Hunt in the Moonlight" and h.sheet_mode == "custom", "a built-in keeps its identity"
    assert json.loads(h.system_json) == {"conditions": ["Cursed"]} and h.rules_md == "House rule."


def test_players_cannot_touch_template_routes(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/characters/templates/new", data={"name": "x", "fields_json": "[]", "system_json": "{}"}, follow_redirects=False)
    assert r.status_code in (403, 404)


# ── the editor ───────────────────────────────────────────────────────────────

def test_editor_shows_the_integration_panel_with_current_values(client, seed):
    tid = _create(client, seed)
    html = client.get(f"/characters/templates/{tid}/edit").text
    assert 'id="system-json-input"' in html and 'name="system_json"' in html and 'name="rules_md"' in html
    assert "Shaken, Wounded" in html or "Shaken" in html
    assert "Roll 2d6." in html
    assert "Draft from rules" in html or "ai-new" in html


def test_new_template_form_has_the_panel_too(client, seed):
    _gm(client, seed)
    html = client.get("/characters/templates/new").text
    assert 'id="system-json-input"' in html and 'name="rules_md"' in html


# ── the integration, end to end ──────────────────────────────────────────────

def test_party_rest_follows_the_templates_rules(client, seed):
    tid = _create(client, seed)
    pc = _pc(seed, tid, grit_current=1, grit_max=3, focus_current=0, focus_max=4)
    db = SessionLocal()
    try:
        party = Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc]))
        db.add(party)
        db.commit()
        pid = party.id
    finally:
        db.close()
    html = client.get(f"/parties/{pid}").text
    assert "Short Rest" in html, "the party page offers a Short Rest because this system defines one"
    r = client.post(f"/api/parties/{pid}/rest", json={"kind": "short"})
    assert r.status_code == 200 and [a["name"] for a in r.json()["applied"]] == ["Pip"]
    cf = json.loads(_load_pc(pc).custom_fields_json)
    assert cf["focus_current"] == 2 and cf["grit_current"] == 1
    client.post(f"/api/parties/{pid}/rest", json={"kind": "long"})
    cf = json.loads(_load_pc(pc).custom_fields_json)
    assert cf["grit_current"] == 3 and cf["focus_current"] == 4


def _load_pc(pc_id):
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        db.expunge(pc)
        return pc
    finally:
        db.close()


def test_the_sheet_offers_the_templates_conditions_and_pages(client, seed):
    tid = _create(client, seed)
    pc = _pc(seed, tid)
    html = client.get(f"/characters/{pc}").text
    assert 'data-cond="Shaken"' in html and 'data-cond="Wounded"' in html and 'data-cond="Bleeding"' not in html
    assert ">🧍 Who<" in html.replace("\n", "") or "Who" in html
    assert 'data-sp="p0"' in html and 'data-sp="p1"' in html


def test_the_roster_uses_the_templates_comparison_fields(client, seed):
    tid = _create(client, seed)
    pc = _pc(seed, tid, luck=7, path="Owl")
    db = SessionLocal()
    try:
        party = Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc]))
        db.add(party)
        db.commit()
        pid = party.id
    finally:
        db.close()
    html = client.get(f"/parties/{pid}/roster").text
    rows = [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'<th scope="row">(.*?)</th>', html, re.S)]
    assert "Luck" in rows and "Path" in rows and "Focus" in rows
    assert "Owl" in html


def test_xp_awards_use_the_templates_xp_field(client, seed):
    tid = _create(client, seed)
    pc = _pc(seed, tid, xp=3)
    from app.sheet_systems import apply_xp_award
    t = _tpl(tid)
    assert apply_xp_award(t, {"xp": 3}, 4)["xp"] == 7


def test_the_ai_reviews_a_character_against_the_templates_own_rules(seed, client, monkeypatch):
    tid = _create(client, seed)
    pc = _pc(seed, tid, grit_current=1, grit_max=3)
    seen = {}

    async def fake_chat(messages, system="", **kw):
        seen["user"], seen["system"] = messages[0]["content"], system
        return json.dumps({"verdict": "ok", "strengths": [], "issues": [], "suggestions": ["s"]})

    monkeypatch.setattr(cai._ai, "generate_chat", fake_chat)
    cai._ANALYSIS_JOBS[1] = {"status": "running", "started": time.time(), "pc_id": pc, "user_id": 1, "focus": "rules",
                             "result": None, "error": ""}
    asyncio.run(cai._analysis_task(1, pc, True, "rules", True, False))
    assert "HOMEBREW RULES" in seen["user"] and "Grit is your hit points" in seen["user"]
    assert "no written rules" not in seen["user"]


# ── export / import round trip ───────────────────────────────────────────────

def test_export_then_import_round_trips_the_whole_system(client, seed):
    tid = _create(client, seed)
    r = client.get(f"/characters/templates/{tid}/export.json")
    assert r.status_code == 200
    exported = r.json()
    assert exported["name"] == "Homebrew" and exported["sheet_mode"] == "custom"
    assert exported["system"]["conditions"] == ["Shaken", "Wounded"] and exported["rules_md"] == RULES
    r = client.post("/api/import/execute", json={"json_text": json.dumps(exported), "kind": "field_template",
                                                   "params": {"template_kind": "sheet", "name": "Homebrew copy"}})
    assert r.status_code == 200, r.text
    new_id = int(re.search(r"/templates/(\d+)/edit", r.json()["redirect"]).group(1))
    copy = _tpl(new_id)
    assert copy.sheet_mode == "custom" and copy.rules_md == RULES
    assert json.loads(copy.system_json) == json.loads(_tpl(tid).system_json)
    assert json.loads(copy.fields_json) == FIELDS


def test_import_cleans_a_hostile_system_block(client, seed):
    _gm(client, seed)
    payload = {"name": "Evil", "fields": FIELDS, "system": {"rest": {"long": [["full", "nope"]]}, "conditions": ["ok"]},
               "rules_md": "y" * 90000}
    r = client.post("/api/import/execute", json={"json_text": json.dumps(payload), "kind": "field_template",
                                                   "params": {"template_kind": "sheet", "sheet_mode": "custom"}})
    new_id = int(re.search(r"/templates/(\d+)/edit", r.json()["redirect"]).group(1))
    t = _tpl(new_id)
    assert json.loads(t.system_json) == {"conditions": ["ok"]} and len(t.rules_md) <= 20000


def test_the_template_api_lists_the_system(client, seed):
    tid = _create(client, seed)
    row = next(t for t in client.get("/api/characters/templates").json() if t["id"] == tid)
    assert row["sheet_mode"] == "custom" and row["system"]["conditions"] == ["Shaken", "Wounded"] and row["has_rules"] is True
