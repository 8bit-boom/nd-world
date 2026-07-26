import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..constants import KINDS, ND_DEFAULT_CURRENCY, ND_DEFAULT_STATS
from ..database import get_db
from ..models import Entity, EntityTemplate, MapOverlay, PlayerCharacter, RandomTable, Schematic, SheetTemplate, World
from .characters import _apply_form
from .tables import _slugify as _table_slugify

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

_MAPS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "maps"

SCHEMATIC_ELEMENT_TYPES = {"rect", "circle", "line", "arrow", "poly", "path", "text", "pin", "image", "measure"}
FIELD_TYPES = {"text", "number", "textarea", "select", "list", "resource", "table"}


def _get_world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


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


# ── Detection ────────────────────────────────────────────────────────────────

def _looks_like_table(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("entries"), list) and all(
        isinstance(e, dict) and "label" in e for e in d.get("entries", [])
    )


def _looks_like_fields(lst) -> bool:
    return isinstance(lst, list) and len(lst) > 0 and all(
        isinstance(f, dict) and "id" in f and "label" in f and f.get("type") in FIELD_TYPES for f in lst
    )


def _looks_like_entity(d) -> bool:
    return isinstance(d, dict) and d.get("kind") in KINDS and isinstance(d.get("name"), str) and bool(d.get("name"))


# Keys that only ever show up on a player character — used to tell a bare PC
# object apart from an Entity (which always carries "kind") or any other blob
# that merely happens to have a "name".
PC_MARKER_KEYS = {
    "player_name", "race", "race_id", "char_class", "profession_id", "level", "xp",
    "max_hp", "current_hp", "shock_max", "shock_current", "pp_current", "mp_current",
    "stats", "stats_json", "equipment", "equipment_json", "feats", "feats_json",
    "attacks", "attacks_json", "cyberware", "cyberware_json", "conditions", "conditions_json",
    "sheet_template_id", "backstory", "minor_edge", "major_edge",
}


def _looks_like_pc(d) -> bool:
    return (
        isinstance(d, dict) and "kind" not in d
        and isinstance(d.get("name"), str) and bool(d.get("name"))
        and any(k in d for k in PC_MARKER_KEYS)
    )


def _resolve_batch_item(item):
    """An `imports` array entry is either an explicit {"kind","data","params"}
    envelope (needed whenever that kind requires params, e.g. schematic_slug),
    or a bare self-describing blob run through the normal auto-detection."""
    if isinstance(item, dict) and "kind" in item and "data" in item:
        return item.get("kind"), item.get("data"), item.get("params") or {}
    return detect_kind(item)["kind"], item, {}


def detect_kind(data) -> dict:
    """Best-effort sniff of what a parsed JSON blob is meant for. Returns
    {kind, summary, count, needs} — `needs` lists extra param keys the UI
    must collect before the import can run (e.g. which schematic to attach
    elements to)."""
    if isinstance(data, dict) and isinstance(data.get("imports"), list) and data["imports"]:
        items = data["imports"]
        kinds = [_resolve_batch_item(it)[0] for it in items]
        breakdown = ", ".join(f"{n} {k}" for k, n in Counter(kinds).most_common())
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

    if isinstance(data, list) and data and all(_looks_like_entity(e) for e in data):
        return {"kind": "entity_bulk", "summary": f"{len(data)} entities", "count": len(data), "needs": []}
    if _looks_like_entity(data):
        return {"kind": "entity_single", "summary": f'1 entity: "{data.get("name")}" ({data.get("kind")})', "count": 1, "needs": []}

    if isinstance(data, list) and data and all(_looks_like_pc(e) for e in data):
        return {"kind": "player_character_bulk", "summary": f"{len(data)} player character(s)", "count": len(data), "needs": []}
    if _looks_like_pc(data):
        return {"kind": "player_character", "summary": f'1 player character: "{data.get("name")}"', "count": 1, "needs": []}

    return {"kind": "unknown", "summary": "Couldn't tell what this JSON is meant for — pick manually below.", "count": 0, "needs": ["forced_kind"]}


# ── Execution ────────────────────────────────────────────────────────────────

_PC_JSON_ALIASES = {
    "stats": "stats_json", "skills": "skills_json", "currency": "currency_json",
    "equipment": "equipment_json", "feats": "feats_json", "attacks": "attacks_json",
    "cyberware": "cyberware_json", "conditions": "conditions_json",
    "custom_fields": "custom_fields_json",
}


def _normalize_pc_data(row: dict) -> dict:
    """_apply_form is shared with the real character create/edit forms, so it
    expects every *_json field pre-encoded as a JSON string (HTML forms only
    ever send strings). Import JSON naturally carries these as real arrays/
    objects, and may use friendlier names without the _json suffix — accept
    both and coerce to what _apply_form needs. Missing stats/currency default
    to the standard N&D starting spread so an import that only sets identity
    fields still renders a usable sheet."""
    out = dict(row)
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


def _create_entity(db: Session, world_id: int, ent) -> Entity:
    if not isinstance(ent, dict) or ent.get("kind") not in KINDS or not str(ent.get("name") or "").strip():
        raise ValueError(f'Not a valid entity — needs "kind" (one of {", ".join(KINDS)}) and a non-empty "name"')
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
        overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).first()
        if not overlay:
            overlay = MapOverlay(slug=slug, custom_markers_json="[]", custom_regions_json="[]")
            db.add(overlay)
        markers = json.loads(overlay.custom_markers_json or "[]")
        regions = json.loads(overlay.custom_regions_json or "[]")
        markers.extend(data.get("custom_markers") or [])
        regions.extend(data.get("custom_regions") or [])
        overlay.custom_markers_json = json.dumps(markers)
        overlay.custom_regions_json = json.dumps(regions)
        db.commit()
        return True, f"/maps/{slug}"

    if kind == "schematic_elements":
        elements = data.get("elements") if isinstance(data, dict) else data
        if not isinstance(elements, list) or not elements or not all(isinstance(el, dict) for el in elements):
            return False, 'Expected a non-empty array of schematic elements (or {"elements": [...]})'
        slug = params.get("schematic_slug")
        if slug == "__new__":
            name = (params.get("new_schematic_name") or "Imported Schematic").strip() or "Imported Schematic"
            s = Schematic(
                world_id=world.id, name=name, slug=_schematic_slugify(name, db),
                description=None, is_html=False,
                canvas_width=int(params.get("new_canvas_width") or 2000),
                canvas_height=int(params.get("new_canvas_height") or 1500),
                canvas_bg=params.get("new_canvas_bg") or "dark",
                elements_json="[]",
            )
            db.add(s)
            db.flush()
        else:
            s = db.query(Schematic).filter(Schematic.slug == slug).first()
            if not s:
                return False, "No schematic selected"
        existing = json.loads(s.elements_json or "[]")
        by_id = {el.get("id"): i for i, el in enumerate(existing) if el.get("id")}
        for el in elements:
            eid = el.get("id")
            if eid and eid in by_id:
                existing[by_id[eid]] = el
            else:
                existing.append(el)
        s.elements_json = json.dumps(existing)
        db.commit()
        return True, f"/maps/schematic/{s.slug}"

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
            if entity_kind not in KINDS:
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
                last = _create_entity(db, world.id, ent)
            except ValueError as e:
                db.rollback()
                return False, f"Entity #{i + 1}: {e}"
        db.commit()
        db.refresh(last)
        return True, f"/entity/{last.id}" if len(data) == 1 else "/"

    if kind == "entity_single":
        try:
            e = _create_entity(db, world.id, data)
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
            pc = PlayerCharacter(world_id=world.id, owner_user_id=None)
            _apply_form(pc, _normalize_pc_data(row))
            if row.get("portrait_url"):
                pc.portrait_url = str(row["portrait_url"]).strip()
            db.add(pc)
            last = pc
        db.commit()
        db.refresh(last)
        return True, f"/characters/{last.id}" if len(data) == 1 else "/characters"

    if kind == "player_character":
        if not isinstance(data, dict) or not str(data.get("name") or "").strip():
            return False, 'Expected an object with at least a "name"'
        pc = PlayerCharacter(world_id=world.id, owner_user_id=None)
        _apply_form(pc, _normalize_pc_data(data))
        if data.get("portrait_url"):
            pc.portrait_url = str(data["portrait_url"]).strip()
        db.add(pc)
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
    for i, item in enumerate(items):
        kind, item_data, item_params = _resolve_batch_item(item)
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
    world, worlds = _get_world_ctx(db, active_world)
    world_id = world.id if world else 1
    schematics = db.query(Schematic).filter(Schematic.world_id == world_id, Schematic.is_html == False).order_by(Schematic.name).all()  # noqa: E712
    return templates.TemplateResponse("import.html", {
        "request": request, "world": world, "worlds": worlds,
        "schematics_json": json.dumps([{"slug": s.slug, "name": s.name} for s in schematics]),
        "maps_json": json.dumps([{"slug": slug, "name": name} for slug, name in _world_maps(world_id)]),
    })


@router.post("/api/import/detect")
async def import_detect(request: Request):
    body = await request.json()
    raw = body.get("json_text", "")
    try:
        data = json.loads(raw)
    except Exception as e:
        return JSONResponse({"kind": "invalid", "summary": f"Not valid JSON: {e}", "count": 0, "needs": []})
    return detect_kind(data)


@router.post("/api/import/execute")
async def import_execute(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    raw = body.get("json_text", "")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise HTTPException(400, f"Not valid JSON: {e}")
    kind = body.get("kind") or detect_kind(data)["kind"]
    params = body.get("params") or {}

    if kind == "batch":
        items = data.get("imports") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise HTTPException(400, 'Expected {"imports": [...]}')
        results = execute_batch_import(db, world, items)
        return {"ok": True, "batch": True, "results": results}

    ok, result = execute_import(db, world, kind, data, params)
    if not ok:
        raise HTTPException(400, result)
    return {"ok": True, "redirect": result}
