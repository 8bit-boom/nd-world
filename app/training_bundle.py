"""The Training Bundle: one world's text, packed for ND Lore Forge (the LLM-training program) as a `.ndtrain` zip.

`GET /export/training.ndtrain` (GM only, active world). Format: docs/TRAINING_BUNDLE_FORMAT.md. The Rust gateway
(rust/nd-gateway/src/export.rs `training_bundle`) must write the same members byte for byte (the zip's own bytes carry
fixed timestamps too, but the parity test compares member by member, and ignores the manifest's `created` moment).

Plain SQL on purpose, not the ORM: the values are written as stored (timestamps as the text SQLite holds, with the space turned
into a "T"), so the Rust side reads the same rows the same way. Access tags ([gmonly], [for=..], [reveal=..]) stay RAW in every
text: the program that trains on the bundle decides what to do with them. Never included: users, tokens, images, journals,
private notes, dice, live state.
"""
import hashlib
import json
import tempfile
import zipfile
from datetime import datetime, timezone

from sqlalchemy import text

FORMAT = "nd-training-bundle"
VERSION = 1
# The order the members are stored in (manifest first).
MEMBERS = ("manifest.json", "world.json", "entities.jsonl", "entity_notes.jsonl", "relations.jsonl", "rules.md",
           "rules_sheets.jsonl", "sessions.jsonl", "facts.jsonl", "quests.jsonl", "tables.jsonl", "ai_instructions.jsonl",
           "characters.jsonl")
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _line(rec: dict) -> bytes:
    return (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _lines(recs: list) -> bytes:
    return b"".join(_line(r) for r in recs)


def _iso(v):
    """A stored timestamp as ISO text: SQLite keeps 'YYYY-MM-DD HH:MM:SS[.ffffff]'; the space becomes a 'T'."""
    return v.replace(" ", "T", 1) if isinstance(v, str) else v


def _loads(raw, want):
    """json.loads of a *_json column; anything unreadable or of another type is the empty `want()`."""
    if not raw:
        return want()
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return want()
    return v if isinstance(v, want) else want()


def _csv(raw) -> list:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _rows(db, sql: str, **params) -> list:
    return [dict(r) for r in db.execute(text(sql), params).mappings()]


def build_members(db, world) -> dict:
    """{member name (without manifest.json): bytes} for one world. `world` is the ORM row (its rules text is deferred)."""
    wid = world.id
    out = {}
    templates = []
    for t in _rows(db, "SELECT id, slug, name, kind, fields_json FROM entity_templates "
                       "WHERE world_id = :w OR world_id IS NULL ORDER BY id", w=wid):
        templates.append({"id": t["id"], "slug": t["slug"], "name": t["name"], "kind": t["kind"],
                          "fields": _loads(t["fields_json"], list)})
    world_doc = {"id": wid, "name": world.name, "slug": world.slug, "description": world.description, "templates": templates}
    out["world.json"] = (json.dumps(world_doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    ents = []
    for e in _rows(db, "SELECT e.id, e.kind, e.subtype, e.name, e.aliases, e.folder, e.tags, e.summary, e.body, "
                       "t.slug AS template, e.custom_fields_json, e.roleplay_personality, e.visible_to_players, "
                       "e.group_ids_json, e.rag_priority, e.updated_at FROM entities e "
                       "LEFT JOIN entity_templates t ON t.id = e.template_id WHERE e.world_id = :w ORDER BY e.id", w=wid):
        ents.append({"id": e["id"], "kind": e["kind"], "subtype": e["subtype"], "name": e["name"],
                     "aliases": _csv(e["aliases"]), "folder": e["folder"], "tags": _csv(e["tags"]),
                     "summary": e["summary"], "body": e["body"], "template": e["template"],
                     "custom_fields": _loads(e["custom_fields_json"], dict),
                     "roleplay_personality": e["roleplay_personality"],
                     "visible_to_players": bool(e["visible_to_players"]),
                     "group_ids": _loads(e["group_ids_json"], list), "rag_priority": bool(e["rag_priority"]),
                     "updated_at": _iso(e["updated_at"])})
    out["entities.jsonl"] = _lines(ents)

    notes = [{"id": n["id"], "entity_id": n["entity_id"], "content": n["content"],
              "visible_to_players": bool(n["visible_to_players"]), "updated_at": _iso(n["updated_at"])}
             for n in _rows(db, "SELECT n.id, n.entity_id, n.content, n.visible_to_players, n.updated_at FROM entity_notes n "
                                "JOIN entities e ON e.id = n.entity_id WHERE e.world_id = :w ORDER BY n.id", w=wid)]
    if notes:
        out["entity_notes.jsonl"] = _lines(notes)

    rels, n = [], 0
    for r in _rows(db, "SELECT r.source_id, s.name AS sn, r.target_id, t.name AS tn, r.relation FROM entity_relations r "
                       "JOIN entities s ON s.id = r.source_id JOIN entities t ON t.id = r.target_id "
                       "WHERE r.world_id = :w ORDER BY r.id", w=wid):
        n += 1
        rels.append({"id": n, "source_id": r["source_id"], "source_name": r["sn"], "target_id": r["target_id"],
                     "target_name": r["tn"], "relation": r["relation"] or "related"})
    for r in _rows(db, "SELECT l.source_id, s.name AS sn, l.target_id, t.name AS tn FROM entity_links l "
                       "JOIN entities s ON s.id = l.source_id JOIN entities t ON t.id = l.target_id "
                       "WHERE s.world_id = :w AND t.world_id = :w ORDER BY l.source_id, l.target_id", w=wid):
        n += 1
        rels.append({"id": n, "source_id": r["source_id"], "source_name": r["sn"], "target_id": r["target_id"],
                     "target_name": r["tn"], "relation": "related"})
    if rels:
        out["relations.jsonl"] = _lines(rels)

    from .retrieval import world_rules_markdown
    rules = world_rules_markdown(world)
    if rules.strip():
        out["rules.md"] = (rules.rstrip("\n") + "\n").encode("utf-8")

    sheets = [{"id": s["id"], "template": s["name"], "rules_md": s["rules_md"]}
              for s in _rows(db, "SELECT id, name, rules_md FROM sheet_templates WHERE world_id = :w "
                                 "AND TRIM(COALESCE(rules_md, '')) <> '' ORDER BY id", w=wid)]
    if sheets:
        out["rules_sheets.jsonl"] = _lines(sheets)

    sessions = [{"id": s["id"], "session_num": s["session_num"], "session_date": s["session_date"], "title": s["title"],
                 "summary": s["summary"], "npcs": _loads(s["npcs_json"], list), "loot": _loads(s["loot_json"], list)}
                for s in _rows(db, "SELECT id, session_num, session_date, title, summary, npcs_json, loot_json "
                                   "FROM game_sessions WHERE world_id = :w ORDER BY id", w=wid)]
    if sessions:
        out["sessions.jsonl"] = _lines(sessions)

    facts = [{"id": f["id"], "game_session_id": f["game_session_id"], "content": f["content"],
              "visible_to_players": bool(f["visible_to_players"]), "tags": f["tags"]}
             for f in _rows(db, "SELECT id, game_session_id, content, visible_to_players, tags FROM facts "
                                "WHERE world_id = :w ORDER BY id", w=wid)]
    if facts:
        out["facts.jsonl"] = _lines(facts)

    quests = [{"id": q["id"], "title": q["title"], "status": q["status"], "category": q["category"],
               "summary": q["summary"], "body": q["body"], "linked_entities": _loads(q["linked_entities_json"], list),
               "parent_id": q["parent_id"]}
              for q in _rows(db, "SELECT id, title, status, category, summary, body, linked_entities_json, parent_id "
                                 "FROM quests WHERE world_id = :w ORDER BY id", w=wid)]
    if quests:
        out["quests.jsonl"] = _lines(quests)

    tables = [{"id": t["id"], "name": t["name"], "category": t["category"], "description": t["description"],
               "entries": _loads(t["entries_json"], list)}
              for t in _rows(db, "SELECT id, name, category, description, entries_json FROM random_tables "
                                 "WHERE world_id = :w ORDER BY id", w=wid)]
    if tables:
        out["tables.jsonl"] = _lines(tables)

    instr = [{"id": i["id"], "title": i["title"], "content": i["content"],
              "applies_to_players": bool(i["applies_to_players"])}
             for i in _rows(db, "SELECT id, title, content, applies_to_players FROM ai_instructions "
                                "WHERE world_id = :w AND enabled = 1 ORDER BY id", w=wid)]
    if instr:
        out["ai_instructions.jsonl"] = _lines(instr)

    chars = [{"id": c["id"], "name": c["name"], "race": c["race"], "char_class": c["char_class"], "level": c["level"],
              "backstory": c["backstory"], "notes": c["notes"], "equipment": _loads(c["equipment_json"], list),
              "feats": _loads(c["feats_json"], list)}
             for c in _rows(db, "SELECT id, name, race, char_class, level, backstory, notes, equipment_json, feats_json "
                                "FROM player_characters WHERE world_id = :w ORDER BY id", w=wid)]
    if chars:
        out["characters.jsonl"] = _lines(chars)
    return out


def manifest_bytes(world, members: dict, build: str, created: str) -> bytes:
    files = {}
    for name in MEMBERS:
        data = members.get(name)
        if data is None:
            continue
        info = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if name.endswith(".jsonl"):
            info["records"] = sum(1 for ln in data.split(b"\n") if ln.strip())
        files[name] = info
    doc = {"format": FORMAT, "version": VERSION, "created": created, "nd_build": build,
           "world": {"id": world.id, "name": world.name, "slug": world.slug, "description": world.description},
           "files": files}
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_zip(writer, world, members: dict, build: str) -> None:
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    members = dict(members, **{"manifest.json": manifest_bytes(world, members, build, created)})
    with zipfile.ZipFile(writer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in MEMBERS:
            if name in members:
                zi = zipfile.ZipInfo(name, _FIXED_TIME)
                zi.external_attr = 0o100644 << 16   # a regular file, mode 0644 (what the Rust writer stores too)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, members[name], compresslevel=6)
