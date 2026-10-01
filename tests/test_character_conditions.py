"""Conditions on a character sheet are real, shared state.

The native sheet's condition buttons only toggled a CSS class — nothing was
saved, so a reload cleared them and the party vitals strip / combat tracker
(which read conditions_json) never saw them. POST /api/characters/{id}/conditions
persists add / remove / toggle / set with a tight cap, notifies live-sync, and is
limited to the character's owner and the GM like the other quick-edit routes.
"""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate
from app.pc_stats import MAX_CONDITIONS, MAX_CONDITION_LEN, clean_condition, clean_conditions

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _pc(seed, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Vex", **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _conds(pc_id):
    db = SessionLocal()
    try:
        return json.loads(db.get(PlayerCharacter, pc_id).conditions_json or "[]")
    finally:
        db.close()


def _post(client, pc, **body):
    return client.post(f"/api/characters/{pc}/conditions", json=body)


def _owner(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)


# ── cleaning ─────────────────────────────────────────────────────────────────

def test_clean_condition_normalises_and_caps():
    assert clean_condition("  Stunned \n ") == "Stunned"
    assert clean_condition("a\x00b\x07c") == "abc"
    assert clean_condition("x" * 100) == "x" * MAX_CONDITION_LEN
    assert clean_condition(5) == "" and clean_condition(None) == "" and clean_condition("   ") == ""


def test_clean_conditions_dedupes_and_caps():
    assert clean_conditions(["Burn", "burn", " BURN ", "Freeze"]) == ["Burn", "Freeze"]
    assert len(clean_conditions([f"c{i}" for i in range(50)])) == MAX_CONDITIONS
    assert clean_conditions("not a list") == [] and clean_conditions([1, None, {}, "ok"]) == ["ok"]


# ── the route ────────────────────────────────────────────────────────────────

def test_add_remove_toggle_persist(client, seed):
    pc = _pc(seed)
    _owner(client, seed)
    r = _post(client, pc, action="add", name="Burn")
    assert r.status_code == 200 and r.json()["conditions"] == ["Burn"]
    assert _conds(pc) == ["Burn"], "the condition must be stored, not just returned"
    assert _post(client, pc, action="add", name="burn").json()["conditions"] == ["Burn"], "adding twice is a no-op"
    assert _post(client, pc, action="toggle", name="Freeze").json()["conditions"] == ["Burn", "Freeze"]
    assert _post(client, pc, action="toggle", name="FREEZE").json()["conditions"] == ["Burn"], "toggle is case-insensitive"
    assert _post(client, pc, action="remove", name="burn").json()["conditions"] == []
    assert _conds(pc) == []


def test_set_replaces_the_list_cleaned(client, seed):
    pc = _pc(seed, conditions_json=json.dumps(["Old"]))
    _owner(client, seed)
    r = _post(client, pc, action="set", conditions=["  Daze ", "daze", "", "Blind"])
    assert r.json()["conditions"] == ["Daze", "Blind"] and _conds(pc) == ["Daze", "Blind"]


def test_cap_is_enforced_with_a_400(client, seed):
    pc = _pc(seed, conditions_json=json.dumps([f"c{i}" for i in range(MAX_CONDITIONS)]))
    _owner(client, seed)
    r = _post(client, pc, action="add", name="one too many")
    assert r.status_code == 400 and len(_conds(pc)) == MAX_CONDITIONS
    assert _post(client, pc, action="remove", name="c0").status_code == 200, "removing is always allowed when full"


def test_bad_input_is_a_400_not_a_500(client, seed):
    pc = _pc(seed)
    _owner(client, seed)
    assert _post(client, pc, action="add").status_code == 400, "a name is required"
    assert _post(client, pc, action="add", name="   ").status_code == 400
    assert _post(client, pc, action="add", name=["x"]).status_code == 400
    assert _post(client, pc, action="explode", name="Burn").status_code == 400
    assert _post(client, pc, action="set", conditions="nope").status_code == 400
    assert client.post(f"/api/characters/{pc}/conditions", content=b"not json").status_code == 400
    assert _conds(pc) == []


def test_only_owner_or_gm_may_change_conditions(client, seed):
    pc = _pc(seed)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert _post(client, pc, action="add", name="Burn").status_code in (403, 404)
    assert _conds(pc) == []
    client.post("/logout")
    login(client, seed.gm.email, GM_PASSWORD)
    assert _post(client, pc, action="add", name="Burn").status_code == 200
    assert _conds(pc) == ["Burn"]
    assert _post(client, 99999, action="add", name="x").status_code == 404


def test_change_notifies_live_sync(client, seed, monkeypatch):
    from app.routers import characters as chars
    touched = []
    monkeypatch.setattr(chars.live, "touch", lambda world_id: touched.append(world_id))
    pc = _pc(seed)
    _owner(client, seed)
    _post(client, pc, action="add", name="Burn")
    assert touched == [seed.world_a.id]


# ── what the pages render ────────────────────────────────────────────────────

def test_native_sheet_renders_saved_conditions_and_posts_them(client, seed):
    pc = _pc(seed, conditions_json=json.dumps(["Burn", "Hexed"]))
    _owner(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert f"/api/characters/{pc}/conditions" in html, "toggleCondition must persist to the API"
    import re
    active = re.findall(r'cond-chip active"[^>]*>([^<]+)<', html)
    assert "Burn" in active, "a saved preset condition renders active after reload"
    assert "Hexed" in html, "a saved custom condition is shown too"


def test_custom_sheet_has_a_conditions_row(client, seed):
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first().id
    finally:
        db.close()
    pc = _pc(seed, sheet_template_id=tpl, conditions_json=json.dumps(["Dazed"]))
    _owner(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert "Dazed" in html and f"/api/characters/{pc}/conditions" in html


def test_party_vitals_show_conditions_as_chips(client, seed):
    from app.models import Party
    pc = _pc(seed, conditions_json=json.dumps(["Burn"]))
    db = SessionLocal()
    try:
        p = Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc]))
        db.add(p)
        db.commit()
        db.refresh(p)
        party = p.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert 'class="pv-cond"' in client.get(f"/parties/{party}").text
