"""The built-in sheet templates must follow the rulebooks they implement.

Asterion <- the Game of Gods rules; Hunt in the Moonlight <- the Revision 1.11
rulebook; Neon & Dragons <- the Player's Guide. Checked here: every field the
rules say a sheet needs exists, option lists use the books' (current) names, the
starting values match the rules, no existing field id was dropped (saved
characters keep their data), and an untouched built-in row upgrades in place
while a GM-customised one is left alone.
"""
import hashlib
import json

import pytest

from app.database import SessionLocal, _upgrade_builtin_sheet_fields
from app.models import SheetTemplate
from app.sheet_systems import (
    BUILTIN_SYSTEMS, system_conditions, system_meta, template_fields,
)

# `client` re-seeds the database (and its built-in rows) for every test.
pytestmark = pytest.mark.usefixtures("client")

OLD_ASTERION_IDS = ['origin', 'spark', 'sentence', 'appearance', 'sparkShield', 'flesh', 'ichor', 'armor', 'attackPool',
                    'defensePool', 'movement', 'abOriginName', 'abOriginTier', 'abOriginText', 'abSparkName', 'abSparkTier',
                    'abSparkText', 'abDeedName', 'abDeedTier', 'abDeedText', 'extraAbilities', 'drachma', 'weapon',
                    'armorItem', 'consumables', 'artifacts', 'glory', 'domainRank', 'reputation', 'milestones',
                    'sessionNum', 'sessionLog', 'relationships', 'freeNotes']
OLD_HITM_IDS = ['player', 'hunter', 'pronouns', 'company', 'campaign', 'hunt', 'location', 'race', 'appearance', 'use',
                'reason', 'prey', 'signature', 'desire', 'protect', 'fear', 'refuse', 'health', 'stamina', 'hunger',
                'arcane', 'signaturePoints', 'alteration', 'overcharge', 'staminaSpent', 'armor', 'movement', 'strain',
                'hungerTell', 'arcaneTell', 'alterTell', 'maintenance', 'xpCurrent', 'xpLifetime', 'xpSpent', 'xpLog',
                'moonPhase', 'moonColor', 'moonNotes', 'abilities', 'rites', 'tools', 'mods', 'sessions', 'heritage',
                'patron', 'relations', 'investigation', 'notes']
# The definitions shipped before this revision (their fields_json hashes), so an untouched row is recognised.
PREV_ASTERION_SHA = "f3c6ac2101b1d5831aecb12075a1412be0e7d133460eada5f3909ea8c6d9028c"
PREV_HITM_SHA = "91842335534832e6fbdb9102be0e8c85052fab34e65a1bfd471707c871c1edb8"


def _tpl(slug):
    db = SessionLocal()
    try:
        t = db.query(SheetTemplate).filter(SheetTemplate.slug == slug, SheetTemplate.is_builtin == True).first()  # noqa: E712
        db.expunge(t)
        return t
    finally:
        db.close()


def _by_id(slug):
    return {f["id"]: f for f in template_fields(_tpl(slug))}


def _opts(f):
    return list(f.get("options") or [])


# ── no data loss ─────────────────────────────────────────────────────────────

def test_no_field_id_was_dropped():
    assert set(OLD_ASTERION_IDS) <= set(_by_id("asterion"))
    assert set(OLD_HITM_IDS) <= set(_by_id("hunt-in-the-moonlight"))


def test_ids_are_unique():
    for slug in ("asterion", "hunt-in-the-moonlight", "nd-default"):
        ids = [f["id"] for f in template_fields(_tpl(slug))]
        assert len(ids) == len(set(ids)), slug


# ── Asterion ← Game of Gods ──────────────────────────────────────────────────

def test_asterion_starting_resources_match_the_rules():
    f = _by_id("asterion")
    assert [f[k]["default_value"] for k in ("sparkShield", "flesh", "ichor")] == ["3/3", "5/5", "5/5"]
    assert all(f[k]["type"] == "resource" for k in ("sparkShield", "flesh", "ichor"))
    assert f["movement"]["default_value"].startswith("30 ft")


def test_asterion_has_the_pools_and_character_sentence_parts():
    f = _by_id("asterion")
    assert "2d10" in f["attackPool"]["default_value"] and "3d10" in f["attackPool"]["default_value"]
    assert f["defensePool"]["default_value"].startswith("2d10")
    assert f["deed"]["type"] == "text", "the sentence is Origin/Lineage + Spark + Epic Deed / Mythic Curse"
    assert _opts(f["kind"]) == ["God (Origin)", "Mythborn (Lineage)", "Mortal"]
    for k in ("resistances", "senses"):
        assert k in f, "Resistance / Senses are the passive properties the rules define"


def test_asterion_abilities_carry_cost_and_tradeoff():
    f = _by_id("asterion")
    for k in ("abOriginTradeoff", "abSparkTradeoff", "abDeedTradeoff"):
        assert f[k]["type"] in ("text", "textarea"), k
    assert _opts(f["deedUsed"]) == ["Ready", "Used this session"], "an Active Deed is once per session"
    extra = {sf["id"] for sf in f["extraAbilities"]["item_fields"]}
    assert {"name", "tier", "text", "cost", "tradeoff"} <= extra


def test_asterion_progression_domain_followers_ambition_reputation():
    f = _by_id("asterion")
    assert f["glory"]["type"] == "number"
    assert _opts(f["domainRank"]) == ["0 · The Ruin", "1 · Sanctuary", "2 · Realm", "3 · Cosmos"]
    for k in ("domainConcept", "domainDecay", "greatWonder", "ambition"):
        assert f[k]["type"] == "textarea", k
    feats = {sf["id"] for sf in f["domainFeatures"]["item_fields"]}
    assert {"name", "tier", "category", "effect"} <= feats
    fol = {sf["id"]: sf for sf in f["followers"]["item_fields"]}
    assert _opts(fol["tier"]) == ["Tier 1 · Devotee", "Tier 2 · Disciple", "Tier 3 · Champion"]
    assert f["repScore"]["type"] == "number" and f["repScore"]["default_value"] == "0"
    assert "Cursed Name" in f["repScore"]["label"] and "Legendary" in f["repScore"]["label"]
    assert {"ambition", "result", "glory"} <= {sf["id"] for sf in f["ambitionLog"]["item_fields"]}


def test_asterion_consumable_limit_and_currency():
    f = _by_id("asterion")
    assert "max 3" in f["consumables"]["label"] and f["drachma"]["type"] == "number"


# ── Hunt in the Moonlight ← Revision 1.11 ────────────────────────────────────

HITM_RACES = ["Human", "Rootbound", "Fleshwarped", "Stoneblooded", "Baroqueborn", "Gravekin", "Lamrossa Wrought",
              "Amalgama", "All-Mother's Kin", "Third-Eyed", "Moonborn", "Yellowbound", "The Unwritten"]


def test_hitm_race_uses_the_current_names():
    race = _by_id("hunt-in-the-moonlight")["race"]
    assert race["type"] == "select" and _opts(race) == HITM_RACES
    for retired in ("Elf", "Drow", "Dwarf", "Orc", "Deviltouched", "Lamrossa Automaton", "Crimson Elves"):
        assert retired not in _opts(race)
    assert "Heart-Beast" in race["label"] or "Heritage" in race["label"]


def test_hitm_starting_values_match_the_rules():
    f = _by_id("hunt-in-the-moonlight")
    assert [f[k]["default_value"] for k in ("health", "stamina", "hunger", "arcane", "signaturePoints")] == \
        ["5/5", "5/5", "0/10", "0/10", "1/1"]
    assert f["crows"]["default_value"] == "100" and f["crows"]["type"] == "number"
    assert f["marks"]["type"] == "number" and f["marks"]["default_value"] == "0"
    assert f["movement"]["default_value"] == "6"


def test_hitm_strain_and_moon_use_the_books_option_lists():
    f = _by_id("hunt-in-the-moonlight")
    assert f["strain"]["type"] == "select" and _opts(f["strain"]) == ["0", "1", "2", "3"]
    assert "Overextended" in f["strain"]["label"]
    assert _opts(f["moonPhase"]) == ["New Moon", "Waxing Crescent", "Waxing Half", "Waxing Gibbous", "Full Moon",
                                      "Waning Gibbous", "Waning Half", "Waning Crescent"]
    colors = _opts(f["moonColor"])
    assert colors[:1] == ["Grey"] and set(colors) >= {"Red", "Violet", "Green", "Blue", "White", "Black", "Pink", "Teal", "Amber"}
    assert "Bleeding Moon" not in colors, "removed from the colour table in Revision 1.11"


def test_hitm_wounds_and_press_on():
    f = _by_id("hunt-in-the-moonlight")
    wounds = {sf["id"]: sf for sf in f["wounds"]["item_fields"]}
    assert _opts(wounds["kind"]) == ["Minor", "Major"]
    assert {"name", "mark", "effect"} <= set(wounds)
    assert _opts(f["pressOn"]) == ["Available", "Used this session"]
    assert _opts(f["signatureUsed"]) == ["Ready", "Used this session"]


def test_hitm_tools_follow_the_tool_rules_and_companions_exist():
    f = _by_id("hunt-in-the-moonlight")
    tool = {sf["id"] for sf in f["tools"]["item_fields"]}
    assert {"properties", "tradeoff"} <= tool
    comp = {sf["id"] for sf in f["companions"]["item_fields"]}
    assert {"name", "kind", "tier", "notes"} <= comp
    assert "Alteration" in f["alteration"]["label"] and "Unmade" in f["alteration"]["label"]
    assert "3" in f["armor"]["label"], "Armor never exceeds 3"


def test_hitm_identity_fields_still_mirror_the_character():
    meta = system_meta(_tpl("hunt-in-the-moonlight"))
    assert meta["binds"] == {"hunter": "name", "player": "player_name"} and meta["hp"] == "health"


# ── Neon & Dragons ← Player's Guide ──────────────────────────────────────────

def test_nd_default_carries_the_guides_trackers():
    f = _by_id("nd-default")
    assert f["stims"]["type"] == "resource" and f["stims"]["default_value"] == "0/3", "no more than 3 stims per Rest"
    assert f["netAwareness"]["type"] == "number"


def test_condition_presets_follow_each_systems_rules():
    assert system_conditions(_tpl("nd-default")) == \
        ["Burn", "Freeze", "Toxin", "Bleeding", "Blind", "Yellow", "Charm", "Daze", "Stunned"]
    assert system_conditions(_tpl("asterion")) == \
        ["Blinded", "Burning", "Bleeding", "Restrained", "Stunned", "Weakened", "Vulnerable"]
    assert system_conditions(_tpl("hunt-in-the-moonlight")) == \
        ["Bleeding", "Burning", "Blinded", "Marked", "Prone", "Restrained", "Stunned", "Weakened", "Vulnerable"]
    assert system_conditions(None)[0] == "Burn", "no template = the N&D sheet"

    class Homebrew:
        slug, is_builtin, sheet_mode, fields_json = "homebrew", False, "custom", "[]"
    assert "Burn" not in system_conditions(Homebrew), "a custom system must not inherit N&D's Yellow/Freeze"


# ── upgrade behaviour ────────────────────────────────────────────────────────

def _restore(slug, fields_json):
    db = SessionLocal()
    try:
        row = db.query(SheetTemplate).filter(SheetTemplate.slug == slug, SheetTemplate.is_builtin == True).first()  # noqa: E712
        row.fields_json = fields_json
        db.commit()
    finally:
        db.close()


def _fields_json(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug, SheetTemplate.is_builtin == True).first().fields_json  # noqa: E712
    finally:
        db.close()


def test_untouched_previous_rows_are_upgraded_and_customised_ones_are_not():
    from app import database as d
    # an old row that really is the previous shipped definition: simulate by rebuilding it from the old ids
    current = {slug: _fields_json(slug) for slug in ("asterion", "hunt-in-the-moonlight", "nd-default")}
    try:
        # a hash match means "untouched previous version" - emulate by registering the row's own text as a previous hash
        marker = json.dumps([{"id": "origin", "label": "Origin", "type": "text", "section": "Identity", "default_value": ""}])
        _restore("asterion", marker)
        d._PREVIOUS_BUILTIN_SHA["asterion"].add(hashlib.sha256(marker.encode()).hexdigest())
        db = SessionLocal()
        try:
            _upgrade_builtin_sheet_fields(db)
            db.commit()
        finally:
            db.close()
        assert _fields_json("asterion") == json.dumps(d._ASTERION_FIELDS), "an untouched old definition is replaced"

        # a GM-edited row (a label changed) must be left alone
        edited = json.loads(json.dumps(d._ASTERION_FIELDS))
        edited[0]["label"] = "My own label"
        _restore("asterion", json.dumps(edited))
        db = SessionLocal()
        try:
            _upgrade_builtin_sheet_fields(db)
            db.commit()
        finally:
            db.close()
        assert json.loads(_fields_json("asterion"))[0]["label"] == "My own label"
    finally:
        for slug, fj in current.items():
            _restore(slug, fj)


def test_the_previous_shipped_definitions_are_registered():
    from app import database as d
    assert PREV_ASTERION_SHA in d._PREVIOUS_BUILTIN_SHA["asterion"]
    assert PREV_HITM_SHA in d._PREVIOUS_BUILTIN_SHA["hunt-in-the-moonlight"]
    assert hashlib.sha256(b"[]").hexdigest() in d._PREVIOUS_BUILTIN_SHA["nd-default"]


def test_every_builtin_system_declares_rest_rules_for_fields_it_has():
    for slug, spec in BUILTIN_SYSTEMS.items():
        ids = set(_by_id(slug))
        for kind, ops in (spec.get("rest") or {}).items():
            assert kind in ("short", "long")
            for op in ops:
                assert op[1] in ids, f"{slug}: rest op {op} names a field the template doesn't have"
