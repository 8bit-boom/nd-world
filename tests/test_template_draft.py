"""AI-drafted sheet templates: the model's JSON is untrusted. clean_template_draft() turns it into a
template that is guaranteed to work (valid ids and types, selects with options, a single HP track,
system hooks that only refer to real fields), and the rulebook chunker lets a very long rulebook be
read in pieces without losing text."""
import json

from app.template_draft import clean_template_draft, clean_template_fields, pick_chunks, split_rules_text


def _draft(**over):
    base = {
        "name": "Ashfall", "description": "Post-war survival.", "sheet_mode": "nd",
        "fields": [
            {"id": "vigor", "label": "Vigor", "type": "resource", "section": "Body", "default_value": "6/6", "vital": "hp"},
            {"id": "nerve", "label": "Nerve", "type": "resource", "section": "Body", "default_value": "4"},
            {"id": "xp", "label": "Experience", "type": "number", "section": "Growth", "default_value": "0", "xp": True},
            {"id": "callsign", "label": "Callsign", "type": "text", "section": "Who", "binds": "name"},
            {"id": "role", "label": "Role", "type": "select", "section": "Who", "options": ["Scout", "Medic"]},
            {"id": "gear", "label": "Gear", "type": "list", "section": "Kit",
             "item_fields": [{"id": "item", "label": "Item", "type": "text"}, {"id": "note", "label": "Note", "type": "textarea"}]},
            {"id": "story", "label": "Story", "type": "textarea", "section": "Notes"},
        ],
        "system": {"conditions": ["Wounded"], "rest": {"long": [["full", "vigor"], ["full", "nerve"]]},
                   "roster": [["Core", ["role"]]]},
        "rules_md": "# Ashfall\nRoll 2d6.",
    }
    base.update(over)
    return base


def test_a_good_draft_passes_through_intact():
    d, warnings = clean_template_draft(_draft())
    assert d["name"] == "Ashfall" and d["sheet_mode"] == "custom", "an AI-drafted system is always a fully custom sheet"
    assert [f["id"] for f in d["fields"]] == ["vigor", "nerve", "xp", "callsign", "role", "gear", "story"]
    assert d["system"]["conditions"] == ["Wounded"] and d["system"]["rest"]["long"][0] == ["full", "vigor"]
    assert d["rules_md"].startswith("# Ashfall") and warnings == []
    nerve = next(f for f in d["fields"] if f["id"] == "nerve")
    assert nerve["default_value"] == "4/4", "a bare number becomes cur/max"


def test_ids_are_sanitised_made_unique_and_system_references_follow():
    raw = _draft(fields=[
        {"id": "Hit Points!", "label": "Hit Points", "type": "resource", "default_value": "5/5", "vital": "hp", "section": "A"},
        {"label": "Hit Points", "type": "number", "section": "A"},          # no id: derived from the label, deduped
        {"id": "9lives", "label": "Lives", "type": "number", "section": "A"},
    ], system={"rest": {"long": [["full", "Hit Points!"]]}, "roster": [["R", ["9lives"]]]})
    d, warnings = clean_template_draft(raw)
    ids = [f["id"] for f in d["fields"]]
    assert all(i[0].isalpha() and i.replace("_", "").isalnum() for i in ids) and len(set(ids)) == 3
    hp = ids[0]
    assert d["system"]["rest"]["long"] == [["full", hp]], "references follow a renamed id"
    assert d["system"]["roster"] == [["R", [ids[2]]]]
    assert warnings


def test_types_defaults_and_options_are_normalised():
    fields, _ = clean_template_fields([
        {"id": "a", "label": "A", "type": "wizardry", "section": "S"},
        {"id": "b", "label": "B", "type": "select", "section": "S"},                      # select with no options
        {"id": "c", "label": "C", "type": "number", "default_value": "lots", "section": "S"},
        {"id": "d", "label": "D", "type": "resource", "default_value": "x/y", "section": "S"},
        {"id": "e", "label": "E", "type": "select", "options": ["One", "", 7, "One"], "section": "S"},
        {"id": "f", "label": "F", "type": "list", "section": "S", "item_fields": []},
    ])
    by = {f["id"]: f for f in fields}
    assert by["a"]["type"] == "text" and by["b"]["type"] == "text"
    assert by["c"]["default_value"] == "0" and by["d"]["default_value"] == "0/0"
    assert by["e"]["options"] == ["One", "7"]
    assert by["f"]["item_fields"] and by["f"]["item_fields"][0]["type"] == "text"


def test_flags_only_where_they_make_sense():
    fields, _ = clean_template_fields([
        {"id": "hp1", "label": "HP", "type": "resource", "vital": "hp"},
        {"id": "hp2", "label": "HP2", "type": "resource", "vital": "hp"},               # second HP track
        {"id": "n", "label": "N", "type": "number", "vital": "hp", "xp": True},          # vital on a number: no; xp: yes
        {"id": "t", "label": "T", "type": "text", "xp": True, "binds": "player_name"},   # xp on text: no; binds: yes
        {"id": "t2", "label": "T2", "type": "text", "binds": "player_name"},             # column already bound
        {"id": "t3", "label": "T3", "type": "text", "binds": "email"},                   # not a real column
    ])
    by = {f["id"]: f for f in fields}
    assert by["hp1"].get("vital") == "hp" and "vital" not in by["hp2"] and "vital" not in by["n"]
    assert by["n"].get("xp") is True and "xp" not in by["t"]
    assert by["t"].get("binds") == "player_name" and "binds" not in by["t2"] and "binds" not in by["t3"]


def test_limits_and_junk():
    many = [{"id": f"f{i}", "label": f"F{i}", "type": "number", "section": "S"} for i in range(300)]
    d, warnings = clean_template_draft({"name": "N" * 500, "fields": many, "rules_md": "r" * 50000, "description": "d" * 2000})
    assert len(d["fields"]) <= 120 and len(d["name"]) <= 80 and len(d["rules_md"]) <= 20000 and len(d["description"]) <= 300
    for junk in (None, "text", [], 5, {}, {"fields": "nope"}, {"fields": [None, 3, "x", {}]}):
        d, _ = clean_template_draft(junk)
        assert d is None or d["fields"] == [] or all("id" in f for f in d["fields"])


def test_a_draft_with_no_usable_fields_is_rejected():
    d, warnings = clean_template_draft({"name": "Empty", "fields": [{"id": "x"}, "junk"]})
    assert d is None and warnings


def test_script_looking_text_is_kept_as_inert_text_only():
    d, _ = clean_template_draft(_draft(name="<script>x</script>", description="<img src=x onerror=alert(1)>"))
    assert "<" not in d["name"] and "<" not in d["description"], "stored escaped/stripped; the app renders these with autoescape too"


# ── reading a long rulebook in pieces ────────────────────────────────────────

BOOK = "\n\n".join(f"## Chapter {i}\n" + ("Lore about the ancient wars. " * 60 if i % 3 else
        "Your character sheet tracks Health, Stamina and Hunger. Resting restores Stamina. Advancement costs XP. " * 12)
        for i in range(30))


def test_split_keeps_every_character_of_text_and_respects_the_size():
    chunks = split_rules_text(BOOK, 6000)
    assert all(len(c) <= 6000 for c in chunks) and len(chunks) > 1
    assert "".join(c.replace("\n", "") for c in chunks).replace(" ", "") == BOOK.replace("\n", "").replace(" ", "")


def test_a_chunk_that_is_one_giant_paragraph_is_still_split():
    chunks = split_rules_text("word " * 10000, 5000)
    assert len(chunks) >= 10 and all(len(c) <= 5000 for c in chunks)


def test_pick_chunks_prefers_sheet_relevant_ones_in_document_order_and_caps():
    chunks = split_rules_text(BOOK, 3000)
    picked = pick_chunks(chunks, 5)
    assert len(picked) == 5
    assert all("Stamina" in c for c in picked), "the lore chapters are skipped"
    assert [chunks.index(c) for c in picked] == sorted(chunks.index(c) for c in picked), "document order is kept"


def test_pick_chunks_returns_everything_when_under_the_cap():
    assert pick_chunks(["a", "b"], 5) == ["a", "b"]
