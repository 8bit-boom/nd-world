from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
import re
import markdown2
import os
import uuid
import shutil
from pathlib import Path

from .database import init_db, get_db
from .models import Entity, World, entity_links

BASE_DIR = Path(__file__).parent.parent
UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"

app = FastAPI(title="N&D World")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

KINDS = ["character", "location", "organization", "creature", "event", "item", "feat", "note"]
SUBTYPES = {
    "character": ["NPC", "PC", "villain", "ally", "neutral"],
    "location": ["district", "city", "country", "void station", "moon", "ruin", "corp facility"],
    "organization": ["megacorp", "syndicate", "government", "cult", "secret society", "gang", "AI entity", "family"],
    "creature": ["mutant", "animal", "abomination", "corp-enhanced", "ice creature", "undead"],
    "event": ["corporate war", "outbreak", "disaster", "political", "yellow corruption", "discovery"],
    "item": ["weapon", "armor", "augment", "bio-augmentation", "drone", "husk", "vehicle", "oddity", "metal", "item"],
    "feat": ["common feat", "origin feat", "profession feat", "profession ability", "psy power", "race feat"],
    "note": ["lore", "session note", "rumor", "prophecy", "theory"],
}
KIND_ICONS = {
    "character": "👤", "location": "🗺", "organization": "🏢",
    "creature": "☠", "event": "⚡", "item": "⚙", "feat": "✦", "note": "📄",
}
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
ENTITY_COLS = {"kind", "subtype", "folder", "name", "tags", "summary", "body", "image_url", "world_id"}

@app.on_event("startup")
def startup():
    init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

def render_md(text):
    return markdown2.markdown(text, extras=["fenced-code-blocks", "tables", "strike"]) if text else ""

def save_upload(file: UploadFile):
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

templates.env.globals.update(kinds=KINDS, subtypes=SUBTYPES, kind_icons=KIND_ICONS)
templates.env.filters["md"] = render_md

# ── World helpers ─────────────────────────────────────────────────────────────

DEFAULT_WORLD_COOKIE = "active_world"

def get_active_world(db: Session, active_world: str = Cookie(None)) -> World:
    if active_world:
        w = db.query(World).filter(World.slug == active_world).first()
        if w:
            return w
    return db.query(World).order_by(World.id).first()

# ── Uploads ───────────────────────────────────────────────────────────────────

@app.get("/uploads/{filename}")
def serve_upload(filename: str):
    path = UPLOADS_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)

# ── Worlds management ─────────────────────────────────────────────────────────

@app.get("/worlds", response_class=HTMLResponse)
def worlds_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    worlds = db.query(World).order_by(World.id).all()
    current = get_active_world(db, active_world)
    return templates.TemplateResponse("worlds.html", {"request": request, "worlds": worlds, "current": current})

@app.post("/worlds/new")
def world_create(
    name: str = Form(...),
    description: str = Form(""),
    accent: str = Form("#00f0ff"),
    db: Session = Depends(get_db),
):
    slug = name.lower().replace(" ", "-").replace("&", "and")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    w = World(name=name, slug=slug, description=description or None, accent=accent)
    db.add(w)
    db.commit()
    db.refresh(w)
    resp = RedirectResponse("/worlds", status_code=303)
    resp.set_cookie(DEFAULT_WORLD_COOKIE, w.slug, max_age=60*60*24*365)
    return resp

@app.post("/worlds/{world_id}/delete")
def world_delete(world_id: int, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    db.delete(w)
    db.commit()
    resp = RedirectResponse("/worlds", status_code=303)
    resp.delete_cookie(DEFAULT_WORLD_COOKIE)
    return resp

@app.get("/worlds/switch/{slug}")
def world_switch(slug: str):
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(DEFAULT_WORLD_COOKIE, slug, max_age=60*60*24*365)
    return resp

# ── Home ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    if not world:
        return RedirectResponse("/worlds")
    counts = {k: db.query(Entity).filter(Entity.kind == k, Entity.world_id == world.id).count() for k in KINDS}
    recent = db.query(Entity).filter(Entity.world_id == world.id).order_by(Entity.updated_at.desc()).limit(8).all()
    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("index.html", {
        "request": request, "counts": counts, "recent": recent,
        "world": world, "worlds": worlds,
    })

# ── List ──────────────────────────────────────────────────────────────────────

@app.get("/kind/{kind}", response_class=HTMLResponse)
def list_entities(request: Request, kind: str, q: str = "", folder: str = "",
                  db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    query = db.query(Entity).filter(Entity.kind == kind, Entity.world_id == world.id)
    if q:
        query = query.filter(or_(
            Entity.name.ilike(f"%{q}%"), Entity.tags.ilike(f"%{q}%"),
            Entity.summary.ilike(f"%{q}%"), Entity.body.ilike(f"%{q}%"),
        ))
    if folder:
        query = query.filter(Entity.folder == folder)
    entities = query.order_by(Entity.folder.nulls_last(), Entity.name).all()

    # Build folder list for sidebar
    folder_rows = (db.query(Entity.folder)
                   .filter(Entity.kind == kind, Entity.world_id == world.id, Entity.folder.isnot(None))
                   .distinct().order_by(Entity.folder).all())
    folders = [r[0] for r in folder_rows]

    # Group by folder for display
    grouped: dict[str, list] = {}
    for e in entities:
        key = e.folder or ""
        grouped.setdefault(key, []).append(e)

    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("entities/list.html", {
        "request": request, "kind": kind, "entities": entities,
        "grouped": grouped, "folders": folders, "active_folder": folder,
        "q": q, "world": world, "worlds": worlds,
    })

# ── Detail ────────────────────────────────────────────────────────────────────

@app.get("/entity/{entity_id}", response_class=HTMLResponse)
def detail(request: Request, entity_id: int, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    world = get_active_world(db, active_world)
    all_entities = db.query(Entity).filter(Entity.id != entity_id, Entity.world_id == entity.world_id).order_by(Entity.name).all()
    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("entities/detail.html", {
        "request": request, "entity": entity, "all_entities": all_entities,
        "world": world, "worlds": worlds,
    })

# ── Create ────────────────────────────────────────────────────────────────────

@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request, kind: str = "character", db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("entities/form.html", {
        "request": request, "entity": None, "kind": kind,
        "world": world, "worlds": worlds,
    })

@app.post("/new")
async def create(
    request: Request,
    kind: str = Form(...), subtype: str = Form(""), name: str = Form(...),
    folder: str = Form(""), tags: str = Form(""), image_url: str = Form(""),
    image_file: UploadFile = File(None), summary: str = Form(""), body: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    world = get_active_world(db, active_world)
    final_image = save_upload(image_file) or (image_url.strip() or None)
    e = Entity(world_id=world.id, kind=kind, subtype=subtype or None, name=name,
               folder=folder.strip() or None, tags=tags or None,
               image_url=final_image, summary=summary or None, body=body or None)
    db.add(e)
    db.commit()
    db.refresh(e)
    return RedirectResponse(f"/entity/{e.id}", status_code=303)

# ── Edit ──────────────────────────────────────────────────────────────────────

@app.get("/entity/{entity_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, entity_id: int, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("entities/form.html", {
        "request": request, "entity": entity, "kind": entity.kind,
        "world": world, "worlds": worlds,
    })

@app.post("/entity/{entity_id}/edit")
async def update(
    entity_id: int,
    kind: str = Form(...), subtype: str = Form(""), name: str = Form(...),
    folder: str = Form(""), tags: str = Form(""), image_url: str = Form(""),
    image_file: UploadFile = File(None), summary: str = Form(""), body: str = Form(""),
    db: Session = Depends(get_db),
):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    uploaded = save_upload(image_file)
    entity.kind = kind
    entity.subtype = subtype or None
    entity.folder = folder.strip() or None
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

def _snippet(text: str, q: str, window: int = 120) -> str:
    if not text or not q:
        return ""
    low = text.lower()
    idx = low.find(q.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window // 2)
    end = min(len(text), idx + window // 2)
    snippet = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    # bold the match
    pattern = re.compile(re.escape(q), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{m.group()}</mark>", snippet)

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", kind: str = "",
           db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    results = []
    grouped: dict[str, list] = {}
    snippets: dict[int, str] = {}

    if q:
        query = db.query(Entity).filter(
            Entity.world_id == world.id,
            or_(
                Entity.name.ilike(f"%{q}%"),
                Entity.tags.ilike(f"%{q}%"),
                Entity.summary.ilike(f"%{q}%"),
                Entity.body.ilike(f"%{q}%"),
            )
        )
        if kind:
            query = query.filter(Entity.kind == kind)
        results = query.order_by(Entity.kind, Entity.name).all()

        for e in results:
            grouped.setdefault(e.kind, []).append(e)
            # build snippet from body if name/summary didn't match
            if q.lower() not in (e.name or "").lower() and q.lower() not in (e.summary or "").lower():
                snippets[e.id] = _snippet(e.body or "", q)

    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("search.html", {
        "request": request, "results": results, "grouped": grouped,
        "snippets": snippets, "q": q, "kind_filter": kind,
        "world": world, "worlds": worlds,
    })

# ── Import API ────────────────────────────────────────────────────────────────

@app.post("/api/import")
def api_import(payload: dict, db: Session = Depends(get_db)):
    created = 0
    world_id = payload.get("world_id", 1)
    for item in payload.get("entities", []):
        item["world_id"] = world_id
        existing = db.query(Entity).filter(
            Entity.name == item["name"],
            Entity.kind == item["kind"],
            Entity.world_id == world_id,
        ).first()
        if not existing:
            e = Entity(**{k: v for k, v in item.items() if k in ENTITY_COLS})
            db.add(e)
            created += 1
        else:
            if item.get("image_url") and not existing.image_url:
                existing.image_url = item["image_url"]
            if item.get("body"):
                existing.body = item["body"]
            if item.get("summary"):
                existing.summary = item["summary"]
    db.commit()
    return {"created": created}

@app.post("/api/upload-image")
async def api_upload_image(file: UploadFile = File(...)):
    uploaded = save_upload(file)
    if not uploaded:
        raise HTTPException(400, "Unsupported file type")
    return {"url": uploaded}

@app.post("/api/worlds")
def api_create_world(payload: dict, db: Session = Depends(get_db)):
    slug = payload["name"].lower().replace(" ", "-").replace("&", "and")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    existing = db.query(World).filter(World.slug == slug).first()
    if existing:
        return {"id": existing.id, "slug": existing.slug, "created": False}
    w = World(name=payload["name"], slug=slug,
              description=payload.get("description"), accent=payload.get("accent", "#b44fff"))
    db.add(w)
    db.commit()
    db.refresh(w)
    return {"id": w.id, "slug": w.slug, "created": True}
