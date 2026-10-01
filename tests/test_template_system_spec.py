"""A GM-made custom template gets the same system hooks the built-ins have — conditions, Rest
rules, page groups, roster comparison fields, rules text for the AI — from its own `system_json`
and `rules_md`, merged over the built-in table when the template is a customised built-in."""
import json
from types import SimpleNamespace

from app import database as d
from app.sheet_systems import (
    apply_rest, clean_system_spec, has_short_rest, rest_ops, rest_touched_keys, roster_field_groups, sheet_pages,
    system_conditions, system_rules_markdown, system_spec,
)

FIELDS = [
    {"id": "grit", "label": "Grit", "type": "resource", "section": "Core", "default_value": "3/3", "vital": "hp"},
    {"id": "focus", "label": "Focus", "type": "resource", "section": "Core", "default_value": "4/4"},
    {"id": "luck", "label": "Luck", "type": "number", "section": "Core", "default_value": "2"},
    {"id": "path", "label": "Path", "type": "select", "options": ["Fox", "Owl"], "section": "Identity"},
    {"id": "bio", "label": "Bio", "type": "textarea", "section": "Story"},
    {"id": "kit", "label": "Kit", "type": "textarea", "section": "Gear"},
]
SPEC = {
    "conditions": ["Shaken", "Wounded"],
    "rest": {"short": [["add", "focus", 2]], "long": [["full", "grit"], ["full", "focus"], ["set", "luck", "2"]]},
    "pages": [{"label": "Who", "icon": "🧍", "sections": ["Identity", "Story"]},
              {"label": "Stats", "icon": "❤", "sections": ["Core"], "conditions": True}],
    "roster": [["Core", ["luck", "path"]]],
}


def _tpl(**kw):
    base = dict(slug="homebrew", name="Homebrew", is_builtin=False, sheet_mode="custom",
                fields_json=json.dumps(FIELDS), system_json=json.dumps(SPEC), rules_md="Roll 2d6. Grit is HP.")
    base.update(kw)
    return SimpleNamespace(**base)


def test_conditions_come_from_the_template():
    assert system_conditions(_tpl()) == ["Shaken", "Wounded"]
    assert "Bleeding" in system_conditions(_tpl(system_json="{}")), "no list -> the neutral custom set"


def test_rest_rules_come_from_the_template():
    t = _tpl()
    assert has_short_rest(t) is True
    assert rest_ops(t, "short") == [["add", "focus", 2]]
    assert rest_touched_keys(t) >= {"focus_current", "focus_max", "grit_current", "luck"}
    cf = apply_rest(t, {"grit_current": 1, "grit_max": 3, "focus_current": 0, "focus_max": 4}, "short")
    assert cf["focus_current"] == 2 and cf["grit_current"] == 1
    cf = apply_rest(t, cf, "long")
    assert (cf["grit_current"], cf["focus_current"], cf["luck"]) == (3, 4, "2")


def test_a_template_without_rest_rules_has_none():
    t = _tpl(system_json="{}")
    assert rest_ops(t) == [] and has_short_rest(t) is False and rest_touched_keys(t) == set()


def test_pages_come_from_the_template_and_unplaced_sections_land_on_more():
    sp = sheet_pages(_tpl(), ["Core", "Identity", "Story", "Gear"])
    assert [p["label"] for p in sp["pages"]] == ["Who", "Stats", "More"]
    assert sp["section_page"]["Gear"] == "more" and sp["conditions"] == sp["section_page"]["Core"]


def test_roster_fields_come_from_the_template():
    groups = roster_field_groups(_tpl())
    assert [(t, [f["id"] for f in fs]) for t, fs in groups] == [("Core", ["luck", "path"])]


def test_rules_text_for_the_ai_is_the_templates_own():
    assert system_rules_markdown(_tpl()) == "Roll 2d6. Grit is HP."
    assert system_rules_markdown(_tpl(rules_md="")) == ""


def test_a_customised_builtin_merges_its_own_spec_over_the_table():
    hitm = SimpleNamespace(slug="hunt-in-the-moonlight", name="Hunt in the Moonlight", is_builtin=True, sheet_mode="custom",
                           fields_json=json.dumps(d._HITM_FIELDS), system_json=json.dumps({"conditions": ["Cursed"]}),
                           rules_md="House rule: no daylight hunts.")
    assert system_conditions(hitm) == ["Cursed"], "the GM's list wins"
    assert rest_ops(hitm, "long"), "...while the built-in Rest rules still apply"
    text = system_rules_markdown(hitm)
    assert "Overextended" in text and "no daylight hunts" in text, "built-in digest first, GM's text after"
    assert system_spec(hitm)["hp"] == "health"


def test_missing_or_broken_system_json_is_harmless():
    for raw in (None, "", "nope", "[]", '"x"', "null"):
        t = _tpl(system_json=raw)
        assert system_conditions(t)[0] == "Bleeding" and rest_ops(t) == []
    assert system_rules_markdown(SimpleNamespace(slug="x", is_builtin=False)) == ""


# ── clean_system_spec ────────────────────────────────────────────────────────

def test_clean_keeps_a_valid_spec_and_assigns_page_ids():
    spec, warnings = clean_system_spec(SPEC, FIELDS)
    assert spec["conditions"] == ["Shaken", "Wounded"] and spec["rest"]["long"][0] == ["full", "grit"]
    assert [p["id"] for p in spec["pages"]] == ["p0", "p1"] and spec["pages"][1]["conditions"] is True
    assert warnings == []


def test_clean_drops_bad_rest_ops_pages_and_roster_ids():
    raw = {"conditions": ["A", "a", "  ", "B" * 80] + [f"C{i}" for i in range(40)],
           "rest": {"long": [["full", "nope"], ["full", "luck"], ["explode", "grit"], ["add", "focus", "lots"],
                             ["add", "focus", 3], ["set", "path", "Fox"], "junk", ["full"]], "weekly": [["full", "grit"]]},
           "pages": [{"label": "A", "sections": ["Core", "Nope"]}, {"label": "B", "sections": ["Core"]},
                     {"label": "", "sections": ["Story"]}, {"label": "Z", "sections": ["Nope"]}],
           "roster": [["Mix", ["luck", "grit", "ghost"]], ["Empty", ["ghost"]]]}
    spec, warnings = clean_system_spec(raw, FIELDS)
    assert len(spec["conditions"]) <= 24 and spec["conditions"][0] == "A" and "a" not in spec["conditions"]
    assert all(len(c) <= 30 for c in spec["conditions"])
    assert spec["rest"] == {"long": [["add", "focus", 3], ["set", "path", "Fox"]]}, spec["rest"]
    assert [p["sections"] for p in spec["pages"]] == [["Core"], ["Story"]], "unknown sections and repeats are dropped"
    assert spec["roster"] == [["Mix", ["luck"]]], "resources and unknown ids are dropped, empty groups vanish"
    assert warnings, "what was dropped is reported"


def test_clean_handles_junk():
    for raw in (None, "text", [], 5):
        assert clean_system_spec(raw, FIELDS)[0] == {}
    assert clean_system_spec({"conditions": "Shaken"}, FIELDS)[0] == {}
