"""Per-category world export: the existing GET /worlds/{id}/export (see
main.py's world_export) is a single combined JSON backup of every entity in
a world, meant for full disaster-recovery restore via its POST counterpart.
This router adds a second, complementary export path — one JSON file per
category (rules, player characters, sheet templates, and each entity kind
separately) — for a GM who wants to move just "the races" or "the rules"
into another world/instance rather than the whole thing.

Every download here uses the same field shapes the /import page's
detect_kind()/execute_import() already understand (entity_bulk,
player_character_bulk, world_rules, field_template — see importer.py), so
whatever comes out re-imports cleanly.
"""
import base64
import io
import json
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..constants import KIND_ICONS, KINDS
from ..database import get_db
from ..deps import get_world_ctx
from ..models import Entity, PlayerCharacter, SheetTemplate, World
from ..routers.importer import _PC_JSON_ALIASES, _PC_SCALAR_FIELDS
from ..templating import templates

router = APIRouter()

UPLOADS_DIR = Path(__import__("os").environ.get("DB_PATH", "/data/world.db")).parent / "uploads"


def _json_download(payload, filename: str) -> StreamingResponse:
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return StreamingResponse(
        io.BytesIO(body.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _entity_to_export_dict(e: Entity) -> dict:
    d = {
        "kind": e.kind, "subtype": e.subtype, "name": e.name,
        "folder": e.folder, "tags": e.tags, "summary": e.summary,
        "body": e.body, "image_url": e.image_url, "image_data": None,
        "visible_to_players": bool(e.visible_to_players),
        "template": e.template.slug if e.template else None,
        "custom_fields_json": json.loads(e.custom_fields_json or "{}"),
    }
    if e.image_url and e.image_url.startswith("/uploads/"):
        img_path = UPLOADS_DIR / Path(e.image_url).name
        if img_path.exists():
            ext = img_path.suffix.lower().lstrip(".")
            d["image_data"] = f"data:image/{ext};base64," + base64.b64encode(img_path.read_bytes()).decode()
    return d


def _pc_to_export_dict(pc: PlayerCharacter) -> dict:
    d = {"name": pc.name}
    for field in _PC_SCALAR_FIELDS:
        d[field] = getattr(pc, field, None)
    for alias, json_field in _PC_JSON_ALIASES.items():
        raw = getattr(pc, json_field, None)
        try:
            d[alias] = json.loads(raw) if raw else ([] if json_field != "custom_fields_json" else {})
        except (TypeError, ValueError):
            d[alias] = raw
    return d


@router.get("/worlds/{world_id}/export/split", response_class=HTMLResponse)
def export_split_page(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    export_world = db.get(World, world_id)
    if not export_world:
        raise HTTPException(404)
    world, worlds = get_world_ctx(request, db, active_world)

    kind_counts = {
        k: db.query(Entity).filter(Entity.world_id == world_id, Entity.kind == k).count()
        for k in KINDS
    }
    pc_count = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).count()
    custom_templates = db.query(SheetTemplate).filter(SheetTemplate.world_id == world_id).order_by(SheetTemplate.name).all()
    has_rules = bool((export_world.rules_md or "").strip())

    return templates.TemplateResponse("world_export_split.html", {
        "request": request, "world": world, "worlds": worlds, "export_world": export_world,
        "kind_counts": kind_counts, "kind_icons": KIND_ICONS, "pc_count": pc_count,
        "custom_templates": custom_templates, "has_rules": has_rules,
    })


@router.get("/worlds/{world_id}/export/rules.json")
def export_rules(world_id: int, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    if not (w.rules_md or "").strip():
        raise HTTPException(404, "This world has no custom rules set")
    return _json_download({"name": f"{w.name} Rules", "rules_md": w.rules_md}, f"{w.slug}-rules.json")


@router.get("/worlds/{world_id}/export/player-characters.json")
def export_player_characters(world_id: int, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world_id).order_by(PlayerCharacter.name).all()
    return _json_download([_pc_to_export_dict(pc) for pc in pcs], f"{w.slug}-player-characters.json")


@router.get("/worlds/{world_id}/export/entities/{kind}.json")
def export_entities_by_kind(world_id: int, kind: str, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    if kind not in KINDS:
        raise HTTPException(404, f"Unknown entity kind: {kind}")
    entities = db.query(Entity).filter(Entity.world_id == world_id, Entity.kind == kind).order_by(Entity.name).all()
    return _json_download([_entity_to_export_dict(e) for e in entities], f"{w.slug}-{kind}.json")


@router.get("/worlds/{world_id}/export/templates/{template_id}.json")
def export_sheet_template(world_id: int, template_id: int, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    t = db.query(SheetTemplate).filter(SheetTemplate.id == template_id, SheetTemplate.world_id == world_id).first()
    if not t:
        raise HTTPException(404)
    payload = {
        "name": t.name, "description": t.description,
        "sheet_mode": t.sheet_mode, "fields": json.loads(t.fields_json or "[]"),
    }
    slug = t.slug or f"template-{t.id}"
    return _json_download(payload, f"{w.slug}-sheet-template-{slug}.json")
