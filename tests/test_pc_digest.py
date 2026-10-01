"""One system-aware sentence/paragraph per character, shared by every AI prompt that mentions the
party (chat RAG, party insights, session prep, audio hints)."""
import json
from types import SimpleNamespace

from app import database as d
from app.pc_digest import pc_digest, pc_digest_line

HITM = SimpleNamespace(slug="hunt-in-the-moonlight", is_builtin=True, name="Hunt in the Moonlight",
                       sheet_mode="custom", fields_json=json.dumps(d._HITM_FIELDS))
ASTERION = SimpleNamespace(slug="asterion", is_builtin=True, name="Asterion", sheet_mode="custom",
                           fields_json=json.dumps(d._ASTERION_FIELDS))


def _pc(**kw):
    base = dict(name="Anders", player_name="Archie", race="", char_class="", level=1, xp=0, subclass="",
                stats_json="[]", max_hp=0, current_hp=0, shock_max=0, shock_current=0, pp_current=0, mp_current=0,
                conditions_json="[]", custom_fields_json="{}", sheet_template_id=None, sheet_template=None,
                backstory="", background="", armor_class=10, temp_hp=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_native_line():
    pc = _pc(name="Vex", race="Human", char_class="Rogue", level=3, max_hp=20, current_hp=9, shock_current=2,
             stats_json=json.dumps([{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]),
             conditions_json=json.dumps(["Burn"]))
    line = pc_digest_line(pc)
    assert line.startswith("Vex (Human Rogue, level 3, played by Archie)")
    assert "HP 9/20" in line and "Shock 2/12" in line and "conditions: Burn" in line


def test_hunter_line_uses_health_and_its_tracks_not_nd_numbers():
    pc = _pc(sheet_template_id=1, sheet_template=HITM, stats_json=json.dumps([{"id": "str", "value": 3}]), level=1,
             conditions_json=json.dumps(["Stunned"]),
             custom_fields_json=json.dumps({"health_current": 3, "health_max": 5, "stamina_current": 2, "stamina_max": 5, "race": "Human"}))
    line = pc_digest_line(pc, HITM)
    assert line.startswith("Anders (Hunt in the Moonlight, Human, played by Archie)")
    assert "Health 3/5" in line and "Stamina 2/5" in line and "conditions: Stunned" in line
    assert "level" not in line and "HP" not in line and "Shock" not in line


def test_asterion_line_and_digest_shape():
    pc = _pc(name="Zeus", player_name="", sheet_template_id=2, sheet_template=ASTERION,
             custom_fields_json=json.dumps({"flesh_current": 2, "flesh_max": 5, "kind": "God (Origin)"}))
    dg = pc_digest(pc, ASTERION)
    assert dg["system"] == "Asterion" and dg["native"] is False
    assert dg["vital"] == {"label": "Flesh", "current": 2, "max": 5}
    assert any(r["label"] == "Spark Shield" for r in dg["resources"]) and not any(r["label"] == "Flesh" for r in dg["resources"])
    assert "played by" not in pc_digest_line(pc, ASTERION)


def test_detail_adds_backstory_and_key_fields_and_strips_secrets_for_players():
    pc = _pc(sheet_template_id=1, sheet_template=HITM, backstory="Raised on the docks. [gmonly]Is the heir.[/gmonly] Owes the Guild.",
             custom_fields_json=json.dumps({"health_current": 5, "health_max": 5, "armor": 2}))
    gm = pc_digest_line(pc, HITM, detail=True, viewer_is_gm=True)
    assert "Armor 2" in gm and "Is the heir" in gm
    player = pc_digest_line(pc, HITM, detail=True, viewer_is_gm=False)
    assert "Raised on the docks" in player and "Is the heir" not in player


def test_garbage_stored_json_does_not_crash():
    pc = _pc(conditions_json="not json", custom_fields_json="[1,2]", stats_json="nope")
    assert pc_digest_line(pc).startswith("Anders")
