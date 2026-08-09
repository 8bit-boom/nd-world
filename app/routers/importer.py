import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import or_, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import auth, deps
from ..constants import KINDS, ND_DEFAULT_CURRENCY, ND_DEFAULT_STATS
from ..database import get_db
from ..deps import get_world_ctx
from ..imaging import CONVERT_QUALITY, convert_image_to
from ..models import Entity, EntityTemplate, InvestBoard, MapOverlay, PlayerCharacter, RandomTable, Schematic, SheetTemplate, World
from ..templating import templates
from ..uploads import BULK_IMAGE_MAX_FILES
from .characters import _apply_form, _current_user
from .tables import _slugify as _table_slugify

router = APIRouter()

_MAPS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "maps"
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"

# The four formats the bulk image-conversion tool below can target — a
# deliberately smaller set than app.imaging's full _PILLOW_FORMAT mapping
# ("jpeg" is left out as a redundant alias of "jpg").
CONVERT_TARGET_FORMATS = {"png", "jpg", "webp", "avif"}

SCHEMATIC_ELEMENT_TYPES = {"rect", "circle", "line", "arrow", "poly", "path", "text", "pin", "image", "measure", "token"}
FIELD_TYPES = {"text", "number", "textarea", "select", "list", "resource", "table"}


# ── Content packs: nd-world Entity -> NeonDragonsApp bundled-JSON shape ────
# The counterpart to NeonDragonsEditor's nd_world_export.py (which converts
# the app's asset-JSON shape INTO nd-world entities) — this converts back
# OUT, so a world's homebrew races/professions/feats/items can be pulled
# into the Android app at runtime with no APK rebuild. See
# GameDataLoader.kt on the app side for the JSON shape being produced here.

_FEAT_SUBTYPE_TO_CATEGORY = {
    "common feat": "Common", "origin feat": "Origin", "profession feat": "Profession",
    "profession ability": "Profession Ability", "psy power": "Psy Power", "race feat": "Race",
}
_EQUIPMENT_SUBTYPE_TO_CATEGORY = {
    "weapon": "Weapon", "armor": "Armor", "augment": "Augment",
    "bio-augmentation": "Bio Augment", "drone": "Drone", "vehicle": "Vehicle", "husk": "Husk",
}
_CONTENT_PACK_KINDS = ("race", "profession", "feat", "item")


def _pack_id(world_id: int, entity_id: int) -> str:
    """Namespaced so a homebrew entity's id can never collide with a real
    KnownIds.kt constant (all lowercase snake_case with no "custom_"
    prefix) and accidentally trigger hardcoded race/feat logic it has no
    business triggering."""
    return f"custom_{world_id}_{entity_id}"


def _sections_from_body(body: Optional[str]):
    """Split a markdown body into (leading description, {heading: text})
    by "## Heading" lines — the inverse of NeonDragonsEditor's own
    _body_from_sections (data/nd_world_export.py), so content authored
    there round-trips cleanly. A body that doesn't follow that convention
    (e.g. entities authored directly in nd-world's own editor) lands
    entirely under a single "Description" section instead of being lost."""
    if not body or not body.strip():
        return "", {}
    parts = re.split(r"(?m)^##\s+(.+)$", body)
    pre = parts[0].strip()
    sections = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        text = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if heading:
            sections[heading] = text
    if not sections:
        return "", {"Description": body.strip()}
    return pre, sections


def _entity_to_pack_item(world_id: int, entity: Entity) -> dict:
    """Every field the Kotlin Race/Profession/Feat/Equipment data classes
    declare is included here, even when empty — plain Gson (no Kotlin
    adapter, see GameDataParser.kt) ignores Kotlin default values for any
    key missing from the JSON, leaving that field null at runtime despite
    its non-nullable Kotlin type. Bundled assets/data/*.json already always
    include every key for exactly this reason (extract_all_data.py); this
    output must match that convention field-for-field."""
    pre, sections = _sections_from_body(entity.body)
    try:
        custom_fields = json.loads(entity.custom_fields_json or "{}")
    except Exception:
        custom_fields = {}
    bonuses = custom_fields.get("bonuses") if isinstance(custom_fields, dict) else None
    if not isinstance(bonuses, dict):
        bonuses = {}

    item = {
        "id": _pack_id(world_id, entity.id),
        "name": entity.name,
        "type": "",
        "tags": entity.tags or "",
        "description": entity.summary or pre,
        "sections": sections,
        "filepath": "",
        "specialAttributes": [],
        "bonuses": bonuses,
        "image_url": entity.image_url or "",
        "isCustom": True,
    }

    if entity.kind in ("race", "profession"):
        item["tier"] = (entity.subtype or "standard").strip().lower()
    elif entity.kind == "feat":
        item["category"] = _FEAT_SUBTYPE_TO_CATEGORY.get((entity.subtype or "").strip().lower(), "Common")
        item["rank"] = ""  # non-nullable in the Kotlin Feat class — must always be present, see docstring above
        folder = entity.folder or ""
        m = re.match(r"^Race Feats/([^/]+)/(.+)$", folder)
        if m:
            item["associatedRace"] = m.group(1)
            item["rank"] = m.group(2)
        else:
            m = re.match(r"^Profession Feats/([^/]+)$", folder)
            if m:
                item["associatedProfession"] = m.group(1)
    elif entity.kind == "item":
        item["category"] = _EQUIPMENT_SUBTYPE_TO_CATEGORY.get((entity.subtype or "").strip().lower(), "Special")
    return item


def build_content_pack(db: Session, world_id: int) -> dict:
    """A world's homebrew races/professions/feats/items, in the same JSON
    shape as the app's bundled assets/data/*.json files — see
    GameDataLoader.kt. Every "kind" bucket is returned even when empty so
    the app's merge step can always iterate them uniformly."""
    entities = db.query(Entity).filter(
        Entity.world_id == world_id, Entity.kind.in_(_CONTENT_PACK_KINDS)
    ).order_by(Entity.name).all()
    pack = {"races": [], "professions": [], "feats": [], "items": []}
    bucket_by_kind = {"race": "races", "profession": "professions", "feat": "feats", "item": "items"}
    for e in entities:
        pack[bucket_by_kind[e.kind]].append(_entity_to_pack_item(world_id, e))
    return pack


def _schematic_slugify(name: str, db: Session) -> str:
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "schematic"
    slug = base_slug
    i = 2
    while db.query(Schematic).filter(Schematic.slug == slug).first():
        slug = f"{base_slug}-{i}"
        i += 1
    return slug


def _world_maps(world_id: int):
    """[(slug, name)] for maps belonging to this world — mirrors main.py's
    _iter_world_maps, duplicated locally to avoid importing from main.py
    (main.py imports this router, so the reverse would be circular)."""
    out = []
    if not _MAPS_DIR.exists():
        return out
    for jf in sorted(_MAPS_DIR.glob("*.json")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("world_id", 1) == world_id:
            out.append((jf.stem, data.get("name", jf.stem)))
    return out


def _map_world_id(slug: str) -> Optional[int]:
    """world_id of the filesystem-backed map at this slug, or None if it
    doesn't exist — same lookup as main.py's _map_data(), duplicated locally
    for the same reason _world_maps() is (see its docstring above)."""
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        return None
    try:
        data = json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get("world_id", 1)


_MAX_OVERLAY_ITEMS = 500  # mirrors main.py's save_map_overlay cap
_MAX_BATCH_IMPORT_ITEMS = 200  # each item can trigger its own DB commit — unbounded is a DoS vector


# ── Bulk image format conversion ────────────────────────────────────────────
# Retroactively re-encodes every image already referenced by a world, unlike
# app.imaging.convert_image (main.py's save_upload), which only ever runs
# once at upload time and only ever targets avif/webp. This walks every place
# an uploaded image can be referenced within a world and converts each one in
# place to a single GM-chosen format/quality, updating whatever DB column (or
# JSON blob field) pointed at it.

def _resolve_upload_path(url) -> Optional[Path]:
    """Map an /uploads/... URL back to the file it names, or None if it
    isn't a local upload at all (board card images are a free-text field
    that can hold any external URL) or would escape UPLOADS_DIR — same
    containment check as main.py's serve_upload, since this path also comes
    from data that predates that route's containment fix."""
    if not isinstance(url, str) or not url.startswith("/uploads/"):
        return None
    root = _UPLOADS_DIR.resolve()
    try:
        path = (root / url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _convert_one(old_url, target_format: str, quality: int) -> Optional[str]:
    """Convert the uploaded file behind old_url to target_format/quality in
    place. Returns the new /uploads/... URL on success, or None if nothing
    changed (not a local upload, already that format, or decode failure) —
    the caller should leave its reference untouched in that case."""
    path = _resolve_upload_path(old_url)
    if path is None:
        return None
    new_path = convert_image_to(path, target_format, quality)
    if new_path is None:
        return None
    return old_url.rsplit("/", 1)[0] + "/" + new_path.name


def _conversion_result(scope: str, label: str, old_url: str, new_url: Optional[str]) -> dict:
    return {
        "scope": scope, "label": label, "old_url": old_url,
        "new_url": new_url, "status": "ok" if new_url else "skipped",
    }


def convert_world_images(db: Session, world: World, target_format: str, quality: int) -> list:
    """Retroactively convert every already-uploaded image referenced by
    `world` to target_format/quality: entity portraits/art, player character
    portraits, schematic backgrounds and embedded images, investigation
    board card images, and user-uploaded (not bundled) maps. Commits once at
    the end; returns a per-image result list."""
    results = []

    entities = db.query(Entity).filter(Entity.world_id == world.id, Entity.image_url.isnot(None)).all()
    for e in entities:
        new_url = _convert_one(e.image_url, target_format, quality)
        results.append(_conversion_result("entity", e.name, e.image_url, new_url))
        if new_url:
            e.image_url = new_url

    pcs = db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world.id, PlayerCharacter.portrait_url != ""
    ).all()
    for pc in pcs:
        new_url = _convert_one(pc.portrait_url, target_format, quality)
        results.append(_conversion_result("character", pc.name, pc.portrait_url, new_url))
        if new_url:
            pc.portrait_url = new_url

    schematics = db.query(Schematic).filter(Schematic.world_id == world.id).all()
    for s in schematics:
        if s.image_url:
            new_url = _convert_one(s.image_url, target_format, quality)
            results.append(_conversion_result(f"schematic: {s.name}", "background", s.image_url, new_url))
            if new_url:
                s.image_url = new_url
        try:
            elements = json.loads(s.elements_json or "[]")
        except Exception:
            elements = []
        changed = False
        for el in elements:
            if not isinstance(el, dict):
                continue
            href = el.get("href")
            if isinstance(href, str) and href.startswith("/uploads/schematics/embeds/"):
                new_url = _convert_one(href, target_format, quality)
                results.append(_conversion_result(f"schematic: {s.name}", "embedded image", href, new_url))
                if new_url:
                    el["href"] = new_url
                    changed = True
        if changed:
            s.elements_json = json.dumps(elements)

    boards = db.query(InvestBoard).filter(InvestBoard.world_id == world.id).all()
    for b in boards:
        try:
            nodes = json.loads(b.nodes_json or "[]")
        except Exception:
            nodes = []
        changed = False
        for n in nodes:
            if not isinstance(n, dict):
                continue
            img = n.get("image_url")
            if isinstance(img, str) and img:
                new_url = _convert_one(img, target_format, quality)
                results.append(_conversion_result(f"board: {b.name}", n.get("title") or "card", img, new_url))
                if new_url:
                    n["image_url"] = new_url
                    changed = True
        if changed:
            b.nodes_json = json.dumps(nodes)

    for slug, name in _world_maps(world.id):
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
            map_path = _UPLOADS_DIR / "maps" / (slug + ext)
            if map_path.is_file():
                new_path = convert_image_to(map_path, target_format, quality)
                old_url = f"/uploads/maps/{slug}{ext}"
                new_url = f"/uploads/maps/{new_path.name}" if new_path else None
                results.append(_conversion_result("map", name, old_url, new_url))
                break

    db.commit()
    return results


# ── Detection ────────────────────────────────────────────────────────────────

def _looks_like_table(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("entries"), list) and all(
        isinstance(e, dict) and "label" in e for e in d.get("entries", [])
    )


def _looks_like_fields(lst) -> bool:
    return isinstance(lst, list) and len(lst) > 0 and all(
        isinstance(f, dict) and "id" in f and "label" in f and f.get("type") in FIELD_TYPES for f in lst
    )


def _looks_like_entity(d, kinds=None) -> bool:
    if kinds is None:
        kinds = KINDS
    return isinstance(d, dict) and d.get("kind") in kinds and isinstance(d.get("name"), str) and bool(d.get("name"))


def _normkey(k) -> str:
    """Fold a key down to bare lowercase alphanumerics so 'char_class',
    'charClass', and 'charclass' all compare equal — AI-authored (and
    hand-typed) import JSON is inconsistent about underscores/casing, and
    silently dropping a field because of that is worse than being lenient
    about how it's spelled."""
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


# Native PlayerCharacter field names _apply_form() actually reads (see
# characters.py) — used both to recognize a PC blob and, on import, to
# rewrite whatever casing/spelling the author used back to these.
_PC_SCALAR_FIELDS = (
    "player_name", "race", "race_id", "char_class", "profession_id", "level", "xp",
    "backstory", "notes", "max_hp", "current_hp", "shock_max", "shock_current",
    "pp_current", "mp_current", "minor_edge", "major_edge", "minor_edge_count",
    "major_edge_count", "sheet_template_id", "portrait_url",
)
_PC_JSON_ALIASES = {
    "stats": "stats_json", "skills": "skills_json", "currency": "currency_json",
    "equipment": "equipment_json", "feats": "feats_json", "attacks": "attacks_json",
    "cyberware": "cyberware_json", "conditions": "conditions_json",
    "custom_fields": "custom_fields_json",
}
_PC_CANONICAL_FIELDS = _PC_SCALAR_FIELDS + tuple(_PC_JSON_ALIASES.keys()) + tuple(_PC_JSON_ALIASES.values())
_PC_KEY_LOOKUP = {_normkey(f): f for f in _PC_CANONICAL_FIELDS}
_PC_MARKER_KEYS_NORM = set(_PC_KEY_LOOKUP)


def _looks_like_pc(d, kinds=None) -> bool:
    """A PC has a "name" and isn't a recognized Entity kind (Entities always
    carry a "kind" from the fixed 8-kind list — "playercharacter", "pc", etc.
    are not among them, so an author's self-declared kind string doesn't
    need special-casing here, it just falls through to the marker check)."""
    if kinds is None:
        kinds = KINDS
    if not isinstance(d, dict) or not isinstance(d.get("name"), str) or not d.get("name"):
        return False
    if d.get("kind") in kinds:
        return False
    return any(_normkey(k) in _PC_MARKER_KEYS_NORM for k in d.keys())


def _resolve_batch_item(item, kinds=None):
    """An `imports` array entry is either an explicit {"kind","data","params"}
    envelope (needed whenever that kind requires params, e.g. schematic_slug),
    or a bare self-describing blob run through the normal auto-detection."""
    if isinstance(item, dict) and "kind" in item and "data" in item:
        return item.get("kind"), item.get("data"), item.get("params") or {}
    return detect_kind(item, kinds=kinds)["kind"], item, {}


def detect_kind(data, kinds=None) -> dict:
    """Best-effort sniff of what a parsed JSON blob is meant for. Returns
    {kind, summary, count, needs} — `needs` lists extra param keys the UI
    must collect before the import can run (e.g. which schematic to attach
    elements to). `kinds` defaults to the built-in KINDS for callers with no
    world context (e.g. tests) — real requests pass deps.effective_kinds(world)[0]
    so a GM's custom kinds are recognized too."""
    if kinds is None:
        kinds = KINDS
    if isinstance(data, dict) and isinstance(data.get("imports"), list) and data["imports"]:
        items = data["imports"]
        item_kinds = [_resolve_batch_item(it, kinds)[0] for it in items]
        breakdown = ", ".join(f"{n} {k}" for k, n in Counter(item_kinds).most_common())
        return {"kind": "batch", "summary": f"{len(items)} item(s): {breakdown}", "count": len(items), "needs": []}

    if isinstance(data, dict) and isinstance(data.get("rules_md"), str):
        return {"kind": "world_rules", "summary": "World rules document", "count": 1, "needs": []}

    if isinstance(data, dict) and ("custom_markers" in data or "custom_regions" in data):
        markers = data.get("custom_markers") or []
        regions = data.get("custom_regions") or []
        return {
            "kind": "map_overlay",
            "summary": f"{len(markers)} map marker(s), {len(regions)} region(s)",
            "count": len(markers) + len(regions),
            "needs": ["map_slug"],
        }

    elements = None
    if isinstance(data, dict) and isinstance(data.get("elements"), list):
        elements = data["elements"]
    elif isinstance(data, list) and data and all(
        isinstance(x, dict) and x.get("type") in SCHEMATIC_ELEMENT_TYPES for x in data
    ):
        elements = data
    if elements is not None:
        return {
            "kind": "schematic_elements",
            "summary": f"{len(elements)} schematic element(s)",
            "count": len(elements),
            "needs": ["schematic_slug"],
        }

    if isinstance(data, list) and data and all(_looks_like_table(t) for t in data):
        return {"kind": "random_table", "summary": f"{len(data)} random table(s)", "count": len(data), "needs": []}
    if _looks_like_table(data):
        return {
            "kind": "random_table",
            "summary": f"1 random table ({len(data.get('entries', []))} entries)",
            "count": 1,
            "needs": [],
        }

    if isinstance(data, dict) and _looks_like_fields(data.get("fields")):
        return {
            "kind": "field_template",
            "summary": f"{len(data['fields'])} field(s)" + (f' — "{data["name"]}"' if data.get("name") else ""),
            "count": len(data["fields"]),
            "needs": ["template_kind", "name"],
        }
    if _looks_like_fields(data):
        return {
            "kind": "field_template",
            "summary": f"{len(data)} field(s)",
            "count": len(data),
            "needs": ["template_kind", "name"],
        }

    if isinstance(data, list) and data and all(_looks_like_entity(e, kinds) for e in data):
        return {"kind": "entity_bulk", "summary": f"{len(data)} entities", "count": len(data), "needs": []}
    if _looks_like_entity(data, kinds):
        return {"kind": "entity_single", "summary": f'1 entity: "{data.get("name")}" ({data.get("kind")})', "count": 1, "needs": []}

    if isinstance(data, list) and data and all(_looks_like_pc(e, kinds) for e in data):
        return {"kind": "player_character_bulk", "summary": f"{len(data)} player character(s)", "count": len(data), "needs": []}
    if _looks_like_pc(data, kinds):
        return {"kind": "player_character", "summary": f'1 player character: "{data.get("name")}"', "count": 1, "needs": []}

    return {"kind": "unknown", "summary": "Couldn't tell what this JSON is meant for — pick manually below.", "count": 0, "needs": ["forced_kind"]}


# ── Execution ────────────────────────────────────────────────────────────────

def _normalize_pc_data(row: dict) -> dict:
    """_apply_form is shared with the real character create/edit forms, so it
    expects every *_json field pre-encoded as a JSON string (HTML forms only
    ever send strings) under its exact snake_case name. Import JSON is
    naturally looser than that — real arrays/objects instead of encoded
    strings, friendlier aliases without the _json suffix, and inconsistent
    casing/underscores (`charClass`, `charclass`, `char_class` all mean the
    same thing) — so first fold every key back to its canonical name via the
    same fuzzy match _looks_like_pc uses, then coerce JSON fields to strings.
    Missing stats/currency default to the standard N&D starting spread so an
    import that only sets identity fields still renders a usable sheet."""
    out = {}
    for k, v in row.items():
        canon = _PC_KEY_LOOKUP.get(_normkey(k))
        out.setdefault(canon, v) if canon else out.setdefault(k, v)
    for alias, field in _PC_JSON_ALIASES.items():
        if field not in out and alias in out:
            out[field] = out[alias]
    for field in set(_PC_JSON_ALIASES.values()):
        if field in out and not isinstance(out[field], str):
            out[field] = json.dumps(out[field])
    if "stats_json" not in out:
        out["stats_json"] = json.dumps(ND_DEFAULT_STATS)
    if "currency_json" not in out:
        out["currency_json"] = json.dumps(ND_DEFAULT_CURRENCY)
    return out


def _upsert_player_character(db: Session, world_id: int, row: dict) -> PlayerCharacter:
    """Create a new PlayerCharacter, or update in place if one with this
    exact name already exists in the world — mirrors app/main.py's legacy
    entity-import upsert-by-name behavior (~line 3261), so re-running an
    import (e.g. a GM re-exporting from NeonDragonsEditor after edits)
    updates characters instead of piling up duplicates on every run."""
    name = str(row.get("name") or "").strip()
    pc = db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world_id, PlayerCharacter.name == name
    ).first()
    if not pc:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=None)
        db.add(pc)
    normalized = _normalize_pc_data(row)
    resolved_tpl = _resolve_sheet_template(db, world_id, normalized.get("sheet_template_id"))
    if resolved_tpl:
        normalized["sheet_template_id"] = resolved_tpl
    _apply_form(pc, normalized)
    if row.get("portrait_url"):
        pc.portrait_url = str(row["portrait_url"]).strip()
    return pc


def _resolve_sheet_template(db: Session, world_id: int, tpl_ref) -> Optional[str]:
    """id > slug > name fallback, mirroring _resolve_entity_template below —
    an AI-authored import file can't know this instance's real numeric
    sheet_template_id ahead of time any more than it can an entity
    template's, so "asterion" (the built-in Asterion template's slug) needs
    to work just as well as a literal numeric id."""
    if not tpl_ref and tpl_ref != 0:
        return None
    if isinstance(tpl_ref, int) or (isinstance(tpl_ref, str) and tpl_ref.isdigit()):
        return str(tpl_ref)
    t = db.query(SheetTemplate).filter(
        (SheetTemplate.world_id.is_(None)) | (SheetTemplate.world_id == world_id)
    ).filter(or_(SheetTemplate.slug == tpl_ref, SheetTemplate.name.ilike(str(tpl_ref)))).first()
    return str(t.id) if t else None


def _resolve_entity_template(db: Session, world_id: int, ent: dict) -> Optional[int]:
    """Best-effort template lookup: template_id (int) > template_slug > template
    (name, case-insensitive) — since an AI-authored import file won't know this
    instance's actual template ids."""
    tid = ent.get("template_id")
    if isinstance(tid, int) or (isinstance(tid, str) and tid.isdigit()):
        return int(tid)
    slug = ent.get("template_slug")
    if slug:
        t = db.query(EntityTemplate).filter(EntityTemplate.slug == slug).first()
        if t:
            return t.id
    name = ent.get("template")
    if name:
        t = db.query(EntityTemplate).filter(
            (EntityTemplate.world_id.is_(None)) | (EntityTemplate.world_id == world_id)
        ).filter(EntityTemplate.name.ilike(name)).first()
        if t:
            return t.id
    return None


def _create_entity(db: Session, world_id: int, ent, kinds=None) -> Entity:
    if kinds is None:
        kinds = KINDS
    if not isinstance(ent, dict) or ent.get("kind") not in kinds or not str(ent.get("name") or "").strip():
        raise ValueError(f'Not a valid entity — needs "kind" (one of {", ".join(kinds)}) and a non-empty "name"')
    cf = ent.get("custom_fields_json") if "custom_fields_json" in ent else ent.get("custom_fields")
    if not isinstance(cf, dict):
        cf = {}
    e = Entity(
        world_id=world_id, kind=ent["kind"], subtype=ent.get("subtype") or None, name=ent["name"],
        folder=ent.get("folder") or None, tags=ent.get("tags") or None,
        image_url=ent.get("image_url") or None, summary=ent.get("summary") or None, body=ent.get("body") or None,
        visible_to_players=bool(ent.get("visible_to_players", True)),
        template_id=_resolve_entity_template(db, world_id, ent),
        custom_fields_json=json.dumps(cf),
    )
    db.add(e)
    return e


def execute_import(db: Session, world: World, kind: str, data, params: dict):
    """Returns (ok, redirect_path_or_error_message). Defensive throughout —
    `kind` may have been manually forced by the user onto data that doesn't
    actually match (the "unknown" detection path), so every branch validates
    its own assumptions rather than trusting the shape."""
    entity_kinds = deps.effective_kinds(world)[0]
    if kind == "world_rules":
        if not isinstance(data, dict) or not isinstance(data.get("rules_md"), str):
            return False, 'Expected {"rules_md": "..."}'
        world.rules_md = data["rules_md"]
        db.commit()
        return True, "/rules"

    if kind == "map_overlay":
        if not isinstance(data, dict):
            return False, 'Expected an object with "custom_markers" and/or "custom_regions"'
        slug = params.get("map_slug")
        if not slug:
            return False, "No map selected"
        # Same "doesn't exist" vs "belongs to another world" ambiguity as
        # every other cross-world-write guard in this app — deliberately
        # one message for both, so a GM can't use this to probe other
        # worlds' map slugs.
        if _map_world_id(slug) != world.id:
            return False, "Map not found"
        markers_in = data.get("custom_markers") or []
        regions_in = data.get("custom_regions") or []
        if not isinstance(markers_in, list) or not isinstance(regions_in, list):
            return False, "custom_markers and custom_regions must be lists"
        try:
            # BEGIN IMMEDIATE pattern — see main.py's schematic_pull_combat
            # for the canonical version of this. Without it, two concurrent
            # imports (or an import racing a GM editing the map live) could
            # both read the pre-import overlay and the second write would
            # silently discard the first's markers/regions.
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).with_for_update().first()
            if not overlay:
                overlay = MapOverlay(slug=slug, custom_markers_json="[]", custom_regions_json="[]")
                db.add(overlay)
                db.flush()
            markers = json.loads(overlay.custom_markers_json or "[]")
            regions = json.loads(overlay.custom_regions_json or "[]")
            markers.extend(markers_in)
            regions.extend(regions_in)
            # Capped on the merged total, not just the incoming batch —
            # otherwise the cap is trivially bypassed by importing a few
            # items at a time forever.
            if len(markers) > _MAX_OVERLAY_ITEMS or len(regions) > _MAX_OVERLAY_ITEMS:
                db.rollback()
                return False, f"Too many markers/regions on this map — limit is {_MAX_OVERLAY_ITEMS} each"
            overlay.custom_markers_json = json.dumps(markers)
            overlay.custom_regions_json = json.dumps(regions)
            db.commit()
            return True, f"/maps/{slug}"
        except OperationalError:
            db.rollback()
            return False, "Someone else is editing this map right now — try again"

    if kind == "schematic_elements":
        elements = data.get("elements") if isinstance(data, dict) else data
        if not isinstance(elements, list) or not elements or not all(isinstance(el, dict) for el in elements):
            return False, 'Expected a non-empty array of schematic elements (or {"elements": [...]})'
        slug = params.get("schematic_slug")

        if slug == "__new__":
            name = (params.get("new_schematic_name") or "Imported Schematic").strip() or "Imported Schematic"
            try:
                canvas_width = int(params.get("new_canvas_width") or 2000)
                canvas_height = int(params.get("new_canvas_height") or 1500)
            except (TypeError, ValueError):
                return False, "Canvas width/height must be numbers"
            # Same bounds as main.py's schematic_new — feeds the SVG viewBox
            # and the hex-grid rendering loop's iteration bounds.
            if not (100 <= canvas_width <= 20000) or not (100 <= canvas_height <= 20000):
                return False, "Canvas width/height must be between 100 and 20000"
            # No locking needed: this slug doesn't exist until this INSERT
            # commits, so nothing else can be concurrently writing its
            # elements_json yet.
            s = Schematic(
                world_id=world.id, name=name, slug=_schematic_slugify(name, db),
                description=None, is_html=False,
                canvas_width=canvas_width, canvas_height=canvas_height,
                canvas_bg=params.get("new_canvas_bg") or "dark",
                elements_json=json.dumps(elements),
            )
            db.add(s)
            db.commit()
            return True, f"/maps/schematic/{s.slug}"

        s = db.query(Schematic).filter(Schematic.slug == slug).first()
        if not s or s.world_id != world.id:
            return False, "Schematic not found"
        try:
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            s2 = db.query(Schematic).filter(Schematic.id == s.id).with_for_update().first()
            existing = json.loads(s2.elements_json or "[]")
            by_id = {el.get("id"): i for i, el in enumerate(existing) if el.get("id")}
            for el in elements:
                eid = el.get("id")
                if eid and eid in by_id:
                    existing[by_id[eid]] = el
                else:
                    existing.append(el)
            s2.elements_json = json.dumps(existing)
            db.commit()
            return True, f"/maps/schematic/{s2.slug}"
        except OperationalError:
            db.rollback()
            return False, "Someone else is editing this schematic right now — try again"

    if kind == "random_table":
        rows = data if isinstance(data, list) else [data]
        if not rows or not all(isinstance(t, dict) for t in rows):
            return False, 'Expected a table object {"entries": [...]} or an array of them'
        for t in rows:
            name = str(t.get("name", "")).strip() or "Imported Table"
            db.add(RandomTable(
                world_id=world.id, name=name, slug=_table_slugify(name, db),
                category=str(t.get("category", "general")), description=str(t.get("description", "")),
                is_builtin=False, entries_json=json.dumps(t.get("entries", [])),
            ))
        db.commit()
        return True, "/tables"

    if kind == "field_template":
        fields = data["fields"] if isinstance(data, dict) and "fields" in data else data
        if not isinstance(fields, list) or not fields or not all(isinstance(f, dict) and "id" in f and "label" in f for f in fields):
            return False, 'Expected an array of field definitions ({"id","label","type",...}), or {"fields": [...]}'
        name = (params.get("name") or (data.get("name") if isinstance(data, dict) else None) or "Imported Template").strip()
        description = (params.get("description") or (data.get("description") if isinstance(data, dict) else "") or "")
        template_kind = params.get("template_kind", "entity")
        if template_kind == "sheet":
            sheet_mode = params.get("sheet_mode") if params.get("sheet_mode") in ("nd", "custom") else "nd"
            base_slug = name.lower().replace(" ", "-")[:50] or "template"
            slug = base_slug
            n = 1
            while db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first():
                slug = f"{base_slug}-{n}"; n += 1
            t = SheetTemplate(
                world_id=world.id, name=name, slug=slug, description=description,
                is_builtin=False, sheet_mode=sheet_mode, fields_json=json.dumps(fields),
            )
            db.add(t)
            db.commit()
            db.refresh(t)
            return True, f"/characters/templates/{t.id}/edit"
        else:
            entity_kind = params.get("entity_kind") or None
            if entity_kind not in entity_kinds:
                entity_kind = None
            base_slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50] or "template"
            slug = base_slug
            n = 1
            while db.query(EntityTemplate).filter(EntityTemplate.slug == slug).first():
                slug = f"{base_slug}-{n}"; n += 1
            t = EntityTemplate(
                world_id=world.id, name=name, slug=slug, kind=entity_kind, description=description,
                is_builtin=False, fields_json=json.dumps(fields),
            )
            db.add(t)
            db.commit()
            db.refresh(t)
            return True, f"/entity-templates/{t.id}/edit"

    if kind == "entity_bulk":
        if not isinstance(data, list) or not data:
            return False, 'Expected a non-empty array of entities ({"kind","name",...})'
        last = None
        for i, ent in enumerate(data):
            try:
                last = _create_entity(db, world.id, ent, entity_kinds)
            except ValueError as e:
                db.rollback()
                return False, f"Entity #{i + 1}: {e}"
        db.commit()
        db.refresh(last)
        return True, f"/entity/{last.id}" if len(data) == 1 else "/"

    if kind == "entity_single":
        try:
            e = _create_entity(db, world.id, data, entity_kinds)
        except ValueError as err:
            return False, str(err)
        db.commit()
        db.refresh(e)
        return True, f"/entity/{e.id}"

    if kind == "player_character_bulk":
        if not isinstance(data, list) or not data:
            return False, 'Expected a non-empty array of player characters ({"name",...})'
        last = None
        for i, row in enumerate(data):
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                db.rollback()
                return False, f'Character #{i + 1}: needs at least a "name"'
            last = _upsert_player_character(db, world.id, row)
        db.commit()
        db.refresh(last)
        return True, f"/characters/{last.id}" if len(data) == 1 else "/characters"

    if kind == "player_character":
        if not isinstance(data, dict) or not str(data.get("name") or "").strip():
            return False, 'Expected an object with at least a "name"'
        pc = _upsert_player_character(db, world.id, data)
        db.commit()
        db.refresh(pc)
        return True, f"/characters/{pc.id}"

    return False, f"Unrecognized kind: {kind}"


def execute_batch_import(db: Session, world: World, items: list) -> list:
    """Runs each item of an {"imports": [...]} envelope through the normal
    single-kind execute_import, best-effort — a mixed batch touches several
    tables and each kind branch above already commits on its own, so there's
    no realistic way to make the whole batch atomic. Report per-item results
    instead of all-or-nothing rollback."""
    results = []
    kinds = deps.effective_kinds(world)[0]
    for i, item in enumerate(items):
        kind, item_data, item_params = _resolve_batch_item(item, kinds)
        if kind == "batch":
            results.append({"index": i, "kind": "batch", "ok": False, "message": "Nested batches are not supported"})
            continue
        if kind in (None, "unknown", "invalid"):
            results.append({
                "index": i, "kind": kind or "unknown", "ok": False,
                "message": 'Could not tell what this item is — use an explicit {"kind": ..., "data": ..., "params": {...}} wrapper',
            })
            continue
        try:
            ok, result = execute_import(db, world, kind, item_data, item_params)
        except Exception as e:
            db.rollback()
            ok, result = False, str(e)
        results.append({"index": i, "kind": kind, "ok": ok, "message": result})
    return results


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    world_id = world.id if world else 1
    schematics = db.query(Schematic).filter(Schematic.world_id == world_id, Schematic.is_html == False).order_by(Schematic.name).all()  # noqa: E712
    entities = db.query(Entity.id, Entity.name, Entity.kind, Entity.image_url).filter(
        Entity.world_id == world_id
    ).order_by(Entity.kind, Entity.name).all()
    return templates.TemplateResponse("import.html", {
        "request": request, "world": world, "worlds": worlds,
        "schematics_json": json.dumps([{"slug": s.slug, "name": s.name} for s in schematics]),
        "maps_json": json.dumps([{"slug": slug, "name": name} for slug, name in _world_maps(world_id)]),
        "entities_json": json.dumps([
            {"id": e.id, "name": e.name, "kind": e.kind, "has_image": bool(e.image_url)} for e in entities
        ]),
        "bulk_image_max_files": BULK_IMAGE_MAX_FILES,
    })


@router.post("/api/import/detect")
async def import_detect(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    body = await request.json()
    raw = body.get("json_text", "")
    try:
        data = json.loads(raw)
    except Exception as e:
        return JSONResponse({"kind": "invalid", "summary": f"Not valid JSON: {e}", "count": 0, "needs": []})
    return detect_kind(data, kinds=deps.effective_kinds(world)[0])


@router.post("/api/import/execute")
async def import_execute(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    raw = body.get("json_text", "")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise HTTPException(400, f"Not valid JSON: {e}")
    kind = body.get("kind") or detect_kind(data, kinds=deps.effective_kinds(world)[0])["kind"]
    params = body.get("params") or {}

    if kind == "batch":
        items = data.get("imports") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise HTTPException(400, 'Expected {"imports": [...]}')
        if len(items) > _MAX_BATCH_IMPORT_ITEMS:
            raise HTTPException(400, f"Too many items in one batch — limit is {_MAX_BATCH_IMPORT_ITEMS}")
        results = execute_batch_import(db, world, items)
        return {"ok": True, "batch": True, "results": results}

    # execute_batch_import (above) already wraps its per-item call to
    # execute_import in try/except so one bad item in a mixed batch doesn't
    # take down the rest — this single-item path had no equivalent, so a
    # commit-time error (e.g. a forced kind mismatched against the data)
    # surfaced as an unhandled 500 instead of a clean 400.
    try:
        ok, result = execute_import(db, world, kind, data, params)
    except Exception as e:
        db.rollback()
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(400, result)
    return {"ok": True, "redirect": result}


@router.post("/api/import/convert-images")
async def api_convert_images(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Retroactively re-encode every image already in the active world to a
    single chosen format/quality — see convert_world_images above. GM-only:
    enforced by main.py's auth_gate middleware, same as every other write
    route that isn't explicitly listed as player-safe."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    target_format = body.get("format")
    if target_format not in CONVERT_TARGET_FORMATS:
        raise HTTPException(400, f"format must be one of: {', '.join(sorted(CONVERT_TARGET_FORMATS))}")
    try:
        quality = int(body.get("quality", CONVERT_QUALITY))
    except (TypeError, ValueError):
        raise HTTPException(400, "quality must be a number")
    if not (1 <= quality <= 100):
        raise HTTPException(400, "quality must be between 1 and 100")
    results = convert_world_images(db, world, target_format, quality)
    converted = sum(1 for r in results if r["status"] == "ok")
    return {"converted": converted, "total": len(results), "results": results}


@router.get("/api/worlds/{world_id}/content-pack")
def api_content_pack(world_id: int, request: Request, db: Session = Depends(get_db)):
    """A world's homebrew races/professions/feats/items, for NeonDragonsApp
    to pull at runtime and merge alongside its bundled content — no APK
    rebuild needed for anything expressible through the app's generic
    bonus system. Same auth pattern as the character-sync endpoints
    (POST /api/worlds/{id}/characters/sync): any member of the world, not
    GM-only, since players are the intended audience here."""
    user = _current_user(request)
    if not user:
        raise HTTPException(401)
    world = db.query(World).filter(World.id == world_id).first()
    if not world or not auth.user_can_access_world(db, user, world):
        raise HTTPException(404)
    return build_content_pack(db, world_id)
