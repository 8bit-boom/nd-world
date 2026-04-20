from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
import markdown2
import os
import uuid
import shutil
from pathlib import Path

from .database import init_db, get_db
from .models import Entity, entity_links

BASE_DIR = Path(__file__).parent.parent  # NeonDragonsWorld/
UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"

app = FastAPI(title="Neon & Dragons World")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

KINDS = ["character", "location", "organization", "creature", "event", "note"]
SUBTYPES = {
    "character": ["NPC", "PC", "villain", "ally", "neutral"],
    "location": ["district", "city", "country", "void station", "moon", "ruin", "corp facility"],
    "organization": ["megacorp", "syndicate", "government", "cult", "secret society", "gang", "AI entity"],
    "creature": ["mutant", "animal", "abomination", "corp-enhanced", "ice creature", "undead"],
    "event": ["corporate war", "outbreak", "disaster", "political", "yellow corruption", "discovery"],
    "note": ["lore", "session note", "rumor", "prophecy", "theory"],
}
KIND_ICONS = {
    "character": "👤",
    "location": "🗺",
    "organization": "🏢",
    "creature": "☠",
    "event": "⚡",
    "note": "📄",
}

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}

@app.on_event("startup")
def startup():
    init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

def render_md(text: str) -> str:
    if not text:
        return ""
    return markdown2.markdown(text, extras=["fenced-code-blocks", "tables", "strike"])

def save_upload(file: UploadFile) -> str | None:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS_DIR / filename
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return f"/uploads/{filename}"

templates.env.globals["kinds"] = KINDS
templates.env.globals["subtypes"] = SUBTYPES
templates.env.globals["kind_icons"] = KIND_ICONS
templates.env.filters["md"] = render_md

# ── Uploaded images ───────────────────────────────────────────────────────────

@app.get("/uploads/{filename}")
def serve_upload(filename: str):
    path = UPLOADS_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)

# ── Home ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    counts = {k: db.query(Entity).filter(Entity.kind == k).count() for k in KINDS}
    recent = db.query(Entity).order_by(Entity.updated_at.desc()).limit(8).all()
    return templates.TemplateResponse("index.html", {"request": request, "counts": counts, "recent": recent})

# ── List ──────────────────────────────────────────────────────────────────────

@app.get("/kind/{kind}", response_class=HTMLResponse)
def list_entities(request: Request, kind: str, q: str = "", db: Session = Depends(get_db)):
    query = db.query(Entity).filter(Entity.kind == kind)
    if q:
        query = query.filter(or_(Entity.name.ilike(f"%{q}%"), Entity.tags.ilike(f"%{q}%"), Entity.summary.ilike(f"%{q}%")))
    entities = query.order_by(Entity.name).all()
    return templates.TemplateResponse("entities/list.html", {"request": request, "kind": kind, "entities": entities, "q": q})

# ── Detail ────────────────────────────────────────────────────────────────────

@app.get("/entity/{entity_id}", response_class=HTMLResponse)
def detail(request: Request, entity_id: int, db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    all_entities = db.query(Entity).filter(Entity.id != entity_id).order_by(Entity.name).all()
    return templates.TemplateResponse("entities/detail.html", {"request": request, "entity": entity, "all_entities": all_entities})

# ── Create ────────────────────────────────────────────────────────────────────

@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request, kind: str = "character"):
    return templates.TemplateResponse("entities/form.html", {"request": request, "entity": None, "kind": kind})

@app.post("/new")
async def create(
    request: Request,
    kind: str = Form(...),
    subtype: str = Form(""),
    name: str = Form(...),
    tags: str = Form(""),
    image_url: str = Form(""),
    image_file: UploadFile = File(None),
    summary: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    final_image = save_upload(image_file) or (image_url.strip() or None)
    e = Entity(kind=kind, subtype=subtype or None, name=name, tags=tags or None,
               image_url=final_image, summary=summary or None, body=body or None)
    db.add(e)
    db.commit()
    db.refresh(e)
    return RedirectResponse(f"/entity/{e.id}", status_code=303)

# ── Edit ──────────────────────────────────────────────────────────────────────

@app.get("/entity/{entity_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, entity_id: int, db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    return templates.TemplateResponse("entities/form.html", {"request": request, "entity": entity, "kind": entity.kind})

@app.post("/entity/{entity_id}/edit")
async def update(
    entity_id: int,
    kind: str = Form(...),
    subtype: str = Form(""),
    name: str = Form(...),
    tags: str = Form(""),
    image_url: str = Form(""),
    image_file: UploadFile = File(None),
    summary: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    uploaded = save_upload(image_file)
    entity.kind = kind
    entity.subtype = subtype or None
    entity.name = name
    entity.tags = tags or None
    entity.image_url = uploaded or (image_url.strip() or None)
    entity.summary = summary or None
    entity.body = body or None
    db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

# ── Delete ────────────────────────────────────────────────────────────────────

@app.post("/entity/{entity_id}/delete")
def delete(entity_id: int, db: Session = Depends(get_db)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    db.execute(entity_links.delete().where(
        (entity_links.c.source_id == entity_id) | (entity_links.c.target_id == entity_id)
    ))
    db.delete(entity)
    db.commit()
    return RedirectResponse("/", status_code=303)

# ── Relations ─────────────────────────────────────────────────────────────────

@app.post("/entity/{entity_id}/link/{target_id}")
def link(entity_id: int, target_id: int, db: Session = Depends(get_db)):
    src = db.get(Entity, entity_id)
    tgt = db.get(Entity, target_id)
    if not src or not tgt:
        raise HTTPException(404)
    if tgt not in src.related:
        src.related.append(tgt)
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

@app.post("/entity/{entity_id}/unlink/{target_id}")
def unlink(entity_id: int, target_id: int, db: Session = Depends(get_db)):
    src = db.get(Entity, entity_id)
    tgt = db.get(Entity, target_id)
    if src and tgt and tgt in src.related:
        src.related.remove(tgt)
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", db: Session = Depends(get_db)):
    results = []
    if q:
        results = db.query(Entity).filter(
            or_(Entity.name.ilike(f"%{q}%"), Entity.tags.ilike(f"%{q}%"), Entity.summary.ilike(f"%{q}%"))
        ).order_by(Entity.kind, Entity.name).all()
    return templates.TemplateResponse("search.html", {"request": request, "results": results, "q": q})

# ── Import API (used by import script) ───────────────────────────────────────

ENTITY_COLS = {"kind", "subtype", "name", "tags", "summary", "body", "image_url"}

@app.post("/api/import")
def api_import(payload: dict, db: Session = Depends(get_db)):
    created = 0
    for item in payload.get("entities", []):
        existing = db.query(Entity).filter(Entity.name == item["name"], Entity.kind == item["kind"]).first()
        if not existing:
            e = Entity(**{k: v for k, v in item.items() if k in ENTITY_COLS})
            db.add(e)
            created += 1
        elif item.get("image_url") and not existing.image_url:
            existing.image_url = item["image_url"]
    db.commit()
    return {"created": created}

@app.post("/api/upload-image")
async def api_upload_image(file: UploadFile = File(...)):
    uploaded = save_upload(file)
    if not uploaded:
        raise HTTPException(400, "Unsupported file type")
    return {"url": uploaded}
