"""AI-drafted values for a custom sheet are untrusted model output: only the template's own
field ids survive, select values must be real options, numbers are clamped, resources are
stored the way the sheet stores them ({id}_current / {id}_max), lists are bounded."""
import json
from types import SimpleNamespace

from app import database as d
from app.sheet_systems import ai_field_catalog, clean_ai_fields

HITM = SimpleNamespace(slug="hunt-in-the-moonlight", is_builtin=True, fields_json=json.dumps(d._HITM_FIELDS))


def test_catalog_lists_fillable_fields_but_not_bound_or_internal_ones():
    cat = {f["id"]: f for f in ai_field_catalog(HITM)}
    assert "race" in cat and cat["race"]["type"] == "select" and "Human" in cat["race"]["options"]
    assert cat["health"]["type"] == "resource" and cat["abilities"]["type"] == "list"
    assert "hunter" not in cat and "player" not in cat, "name/player are bound to the character columns"
    assert all(len(f["label"]) <= 60 for f in cat.values()), "rule hints are trimmed from labels"


def test_unknown_ids_are_dropped():
    assert clean_ai_fields(HITM, {"nope": "x", "race": "Human"}) == {"race": "Human"}


def test_select_must_be_a_real_option():
    assert clean_ai_fields(HITM, {"race": "Dragon King"}) == {}
    assert clean_ai_fields(HITM, {"race": "human"}) == {"race": "Human"}, "case-insensitive match to the option"


def test_numbers_are_clamped_and_coerced():
    out = clean_ai_fields(HITM, {"armor": "3", "movement": 99999, "alteration": -5, "overcharge": "lots"})
    assert out["armor"] == 3 and out["movement"] <= 999 and out["alteration"] == 0 and "overcharge" not in out


def test_resources_become_current_and_max_keys():
    out = clean_ai_fields(HITM, {"health": {"current": 3, "max": 5}, "stamina": "4/6", "hunger": 2})
    assert (out["health_current"], out["health_max"]) == (3, 5)
    assert (out["stamina_current"], out["stamina_max"]) == (4, 6)
    assert out["hunger_current"] == 2 and "hunger_max" not in out
    assert "health" not in out


def test_text_is_stripped_and_bounded():
    out = clean_ai_fields(HITM, {"company": "  Widow-Bound Hunter  ", "heritage": "x" * 20000})
    assert out["company"] == "Widow-Bound Hunter"
    assert all(len(v) <= 4000 for v in out.values() if isinstance(v, str))


def test_lists_keep_only_known_columns_and_are_capped():
    rows = [{"source": "Tracker", "tier": "1", "effect": "Follow any trail", "evil": "<script>"} for _ in range(40)]
    out = clean_ai_fields(HITM, {"abilities": rows})
    assert len(out["abilities"]) <= 12
    assert set(out["abilities"][0]) <= {"source", "tier", "cost", "rangeDamage", "effect", "used"}
    assert out["abilities"][0]["effect"] == "Follow any trail"


def test_garbage_input_is_harmless():
    assert clean_ai_fields(HITM, None) == {} and clean_ai_fields(HITM, "text") == {} and clean_ai_fields(HITM, []) == {}
    assert clean_ai_fields(HITM, {"abilities": "not a list", "health": [1, 2]}) == {}
