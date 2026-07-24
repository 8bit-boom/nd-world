import json
import random
import re
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RandomTable, World

router = APIRouter()

from pathlib import Path
BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []


def _get_world_ctx(db: Session, active_world: Optional[str]):
    worlds = db.query(World).order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds


def _slugify(name: str, db: Session) -> str:
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "table"
    slug = base_slug
    n = 2
    while db.query(RandomTable).filter(RandomTable.slug == slug).first():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


@router.get("/tables", response_class=HTMLResponse)
def tables_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    tables = db.query(RandomTable).filter(
        (RandomTable.world_id.is_(None)) | (RandomTable.world_id == (world.id if world else None))
    ).order_by(RandomTable.category, RandomTable.name).all()
    grouped: dict = {}
    for t in tables:
        grouped.setdefault(t.category or "general", []).append(t)
    return templates.TemplateResponse("tables/list.html", {
        "request": request, "world": world, "worlds": worlds, "grouped": grouped,
    })


@router.get("/tables/new", response_class=HTMLResponse)
def table_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    return templates.TemplateResponse("tables/form.html", {
        "request": request, "world": world, "worlds": worlds, "tbl": None, "entries": [],
    })


@router.post("/tables/new")
async def table_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    form = await request.form()
    name = str(form.get("name", "")).strip() or "Unnamed Table"
    category = str(form.get("category", "")).strip() or "general"
    description = str(form.get("description", "")).strip()
    raw_entries = str(form.get("entries_json", "[]") or "[]")
    try:
        json.loads(raw_entries)
    except Exception:
        raw_entries = "[]"
    tbl = RandomTable(
        world_id=world.id if world else None, name=name, slug=_slugify(name, db),
        category=category, description=description, is_builtin=False, entries_json=raw_entries,
    )
    db.add(tbl)
    db.commit()
    db.refresh(tbl)
    return RedirectResponse(f"/tables/{tbl.id}/edit", status_code=303)


@router.get("/tables/{table_id}/edit", response_class=HTMLResponse)
def table_edit_form(table_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl:
        raise HTTPException(404)
    entries = json.loads(tbl.entries_json or "[]")
    return templates.TemplateResponse("tables/form.html", {
        "request": request, "world": world, "worlds": worlds, "tbl": tbl, "entries": entries,
    })


@router.post("/tables/{table_id}/edit")
async def table_update(table_id: int, request: Request, db: Session = Depends(get_db)):
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl:
        raise HTTPException(404)
    form = await request.form()
    if not tbl.is_builtin:
        tbl.name = str(form.get("name", tbl.name)).strip() or tbl.name
        tbl.category = str(form.get("category", "")).strip() or "general"
        tbl.description = str(form.get("description", "")).strip()
    raw_entries = str(form.get("entries_json", "[]") or "[]")
    try:
        json.loads(raw_entries)
    except Exception:
        raw_entries = "[]"
    tbl.entries_json = raw_entries
    db.commit()
    return RedirectResponse(f"/tables/{table_id}/edit?saved=1", status_code=303)


@router.post("/tables/{table_id}/delete")
def table_delete(table_id: int, db: Session = Depends(get_db)):
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl:
        raise HTTPException(404)
    if tbl.is_builtin:
        raise HTTPException(403, "Cannot delete built-in tables")
    db.delete(tbl)
    db.commit()
    return RedirectResponse("/tables", status_code=303)


@router.post("/api/tables/{table_id}/roll")
def table_roll(table_id: int, db: Session = Depends(get_db)):
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl:
        raise HTTPException(404)
    entries = json.loads(tbl.entries_json or "[]")
    if not entries:
        raise HTTPException(400, "This table has no entries")
    weights = [max(0, int(e.get("weight", 1) or 1)) for e in entries]
    total = sum(weights) or len(entries)
    if sum(weights) == 0:
        weights = [1] * len(entries)
    roll = random.randint(1, total)
    choice = random.choices(entries, weights=weights, k=1)[0]
    return {"result": choice.get("label", ""), "roll": roll, "total": total}


@router.get("/tables/export")
def tables_export(db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    tables = db.query(RandomTable).filter(
        (RandomTable.world_id.is_(None)) | (RandomTable.world_id == (world.id if world else None))
    ).all()
    payload = [{
        "name": t.name, "category": t.category, "description": t.description,
        "entries": json.loads(t.entries_json or "[]"),
    } for t in tables]
    return JSONResponse(payload)


@router.post("/tables/import")
async def tables_import(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(400, "No file uploaded")
    raw = await file.read()
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, list):
        raise HTTPException(400, "Expected a JSON array of tables")
    for item in payload:
        name = str(item.get("name", "")).strip() or "Imported Table"
        db.add(RandomTable(
            world_id=world.id if world else None, name=name, slug=_slugify(name, db),
            category=str(item.get("category", "general")), description=str(item.get("description", "")),
            is_builtin=False, entries_json=json.dumps(item.get("entries", [])),
        ))
    db.commit()
    return RedirectResponse("/tables", status_code=303)
