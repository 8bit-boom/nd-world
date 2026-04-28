from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File, Cookie
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from typing import Optional
import re
import markdown2
import os
import uuid
import shutil
import json
import base64
import io
from pathlib import Path

from .database import init_db, get_db
from .models import Entity, World, Schematic, MapOverlay, InvestBoard, entity_links
from .routers.ai import router as ai_router
from . import ai as _ai_module

BASE_DIR = Path(__file__).parent.parent
UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"

app = FastAPI(title="N&D World")
_allowed = [h.strip() for h in os.getenv("ND_ALLOWED_HOSTS", "*").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed)
app.include_router(ai_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
SCHEMATICS_STATIC_DIR = BASE_DIR / "static" / "schematics"
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

def _rules_toc(html: str):
    toc = []
    def _repl(m):
        lvl, inner = m.group(1), m.group(2)
        text = re.sub(r'<[^>]+>', '', inner)
        slug = re.sub(r'[^\w]+', '-', text.lower()).strip('-') or 'sec'
        toc.append({'level': int(lvl), 'text': text, 'id': slug})
        return f'<h{lvl} id="{slug}">{inner}</h{lvl}>'
    html = re.sub(r'<h([23])>(.*?)</h\1>', _repl, html, flags=re.DOTALL)
    return html, toc

def strip_md(text):
    if not text:
        return ""
    text = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'^\*\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

_STAT_SKIP = {"visibility", "type", "physical stats", "other", "costs", ""}
_STAT_WANT = {
    "damage", "rarity", "armor", "cost", "special conditions", "effect",
    "requirement", "requirements", "range", "rounds", "strength", "power",
    "speed", "feats", "capacity", "augment slots", "max health", "max pp",
}
_STAT_TABLE_SKIP = {"visibility", "physical stats", "other", "costs"}

def _clean_val(v: str) -> str:
    v = re.sub(r'\\\*\\\*', '', v)
    v = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', v)
    return v.strip()

_PLAIN_KEY_RE = re.compile(r'^([A-Z][A-Za-z ()]{1,38}):\s*(.*)')

def parse_stats(body: str) -> list[dict]:
    """Return list of {key, val, special} dicts from ## Attributes or ## Entry."""
    if not body:
        return []
    lines = body.splitlines()
    in_section = False
    rows = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if re.match(r'^##\s+(Attributes|Entry|Profile)', ln, re.IGNORECASE):
            in_section = True
            continue
        if in_section and re.match(r'^##', ln):
            break
        if not in_section:
            continue
        if re.match(r'^(---|!\[|\s*$)', ln):
            continue

        key = val = None
        # Format 1 – bullet: * **Key[optional bracket]**: Value
        m = re.match(r'\*\s+\*\*([^*\[]+)(?:\[[^\]]*\])?\*\*[:\s]\s*(.*)', ln)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
        if not key:
            # Format 2 – Kanka escaped: \*\*Key\*\* = Value
            m = re.match(r'\\\*\\\*([^\\]+)\\\*\\\*\s*[=:]\s*(.*)', ln)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
        if not key:
            # Format 3 – inline bold: **Key:** Value  (race feats)
            m = re.match(r'\*\*([^*:]+)\*\*[:\s]\s*(.*)', ln)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
        if not key:
            # Format 4 – plain title-case: Key: Value  (flesh grafts, "Points: 12")
            m = _PLAIN_KEY_RE.match(ln.strip())
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
                # Multi-line: key-only line, value is on the next non-empty line
                if not val:
                    j = i
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and not re.match(r'^##|^---|^\*\s+\*\*|^\\\*\\\*', lines[j]):
                        # next line is plain text — use as value, advance pointer
                        val = lines[j].strip()
                        i = j + 1

        if not key or not val:
            continue
        val = _clean_val(val)
        if not val or val.startswith('{') or key.lower() in _STAT_TABLE_SKIP:
            continue
        rows.append({"key": key, "val": val, "special": key.lower() == "special conditions"})
    return rows

def _decode(text: str) -> str:
    return (text.replace('&#39;', "'").replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))

def entry_text(body: str) -> str:
    """Extract ## Entry section as clean plain text (for feat descriptions)."""
    if not body:
        return ""
    lines = body.splitlines()
    in_entry = False
    chunks = []
    for ln in lines:
        if re.match(r'^##\s+Entry', ln, re.IGNORECASE):
            in_entry = True
            continue
        if in_entry and re.match(r'^##', ln):
            break
        if not in_entry or re.match(r'^(---|!\[|\s*$)', ln):
            continue
        if re.match(r'^\|', ln):          # markdown table row — skip
            continue
        ln = re.sub(r'\\\*\\\*', '', ln)  # \*\*
        ln = re.sub(r'\\-', '-', ln)
        ln = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', ln)
        ln = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', ln)
        ln = re.sub(r'^[-*]\s+', '', ln)
        chunks.append(ln.strip())
    text = ' '.join(c for c in chunks if c)
    return _decode(re.sub(r'\s+', ' ', text).strip())

def body_summary(text):
    """Compact stat line for cards: key stats or entry text fallback."""
    if not text:
        return ""
    pairs = []
    special = None
    for ln in text.splitlines():
        if re.match(r'^\|', ln):          # skip markdown tables
            continue
        m = re.match(r'\*\s+\*\*([^*]+)\*\*[:\s]\s*(.+)', ln)
        if not m:
            m = re.match(r'\\\*\\\*([^\\]+)\\\*\\\*\s*[=:]\s*(.+)', ln)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = _decode(re.sub(r'\\\*\\\*', '', m.group(2)).strip().rstrip('\\').strip())
        if not val or val.startswith('{') or key in _STAT_SKIP:
            continue
        if key == "special conditions":
            special = val[:220]
        elif key in _STAT_WANT and len(pairs) < 4:
            pairs.append(f"{m.group(1).strip()}: {val}")
    return special or "  ·  ".join(pairs) or entry_text(text)

def save_upload(file: UploadFile, subdir: str = ""):
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    filename = f"{uuid.uuid4().hex}{ext}"
    target_dir = UPLOADS_DIR / subdir if subdir else UPLOADS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    url_path = f"/uploads/{subdir}/{filename}" if subdir else f"/uploads/{filename}"
    return url_path

templates.env.globals.update(kinds=KINDS, subtypes=SUBTYPES, kind_icons=KIND_ICONS)
templates.env.filters["md"] = render_md
templates.env.filters["strip_md"] = strip_md
templates.env.filters["body_summary"] = body_summary
templates.env.filters["parse_stats"] = parse_stats
templates.env.filters["entry_text"] = entry_text

# ── World helpers ─────────────────────────────────────────────────────────────

DEFAULT_WORLD_COOKIE = "active_world"

def get_active_world(db: Session, active_world: str = Cookie(None)) -> World:
    if active_world:
        w = db.query(World).filter(World.slug == active_world).first()
        if w:
            return w
    return db.query(World).order_by(World.id).first()

# ── Uploads ───────────────────────────────────────────────────────────────────

@app.get("/uploads/{filepath:path}")
def serve_upload(filepath: str):
    path = UPLOADS_DIR / filepath
    if not path.exists() or not path.is_file():
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

@app.get("/worlds/{world_id}/edit", response_class=HTMLResponse)
def world_edit_form(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    world, worlds = _get_world_ctx(db, active_world)
    return templates.TemplateResponse("world_edit.html", {
        "request": request, "world": world, "worlds": worlds,
        "edit_world": w, "kinds": KINDS, "kind_icons": KIND_ICONS,
    })

@app.post("/worlds/{world_id}/edit")
def world_edit_post(
    world_id: int,
    name: str = Form(...),
    description: str = Form(""),
    accent: str = Form("#00f0ff"),
    db: Session = Depends(get_db),
):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    w.name = name.strip() or w.name
    w.description = description
    w.accent = accent
    db.commit()
    return RedirectResponse("/worlds", status_code=303)

@app.post("/folders/rename")
def folder_rename(
    kind: str = Form(...),
    old_path: str = Form(...),
    new_path: str = Form(...),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _get_world_ctx(db, active_world)
    if not world or not old_path.strip() or not new_path.strip():
        raise HTTPException(400)
    ents = db.query(Entity).filter(
        Entity.world_id == world.id,
        or_(Entity.folder == old_path, Entity.folder.like(old_path + "/%"))
    ).all()
    for e in ents:
        if e.folder == old_path:
            e.folder = new_path
        elif e.folder:
            e.folder = new_path + e.folder[len(old_path):]
    db.commit()
    return RedirectResponse(f"/kind/{kind}", status_code=303)

@app.get("/worlds/{world_id}/export")
def world_export(world_id: int, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    entities = db.query(Entity).filter(Entity.world_id == world_id).all()
    export_entities = []
    for e in entities:
        d = {
            "name": e.name, "kind": e.kind, "subtype": e.subtype,
            "folder": e.folder, "tags": e.tags, "summary": e.summary,
            "body": e.body, "image_url": e.image_url, "image_data": None,
        }
        # Embed local uploaded images as base64
        if e.image_url and e.image_url.startswith("/uploads/"):
            img_path = UPLOADS_DIR / Path(e.image_url).name
            if img_path.exists():
                ext = img_path.suffix.lower().lstrip(".")
                d["image_data"] = f"data:image/{ext};base64," + base64.b64encode(img_path.read_bytes()).decode()
        export_entities.append(d)
    payload = json.dumps({
        "world": {"name": w.name, "slug": w.slug, "description": w.description, "accent": w.accent},
        "entities": export_entities,
    }, ensure_ascii=False, indent=2)
    filename = f"{w.slug}-export.json"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/worlds/{world_id}/import")
async def world_import(world_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    raw = await file.read()
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON file")
    entities = data.get("entities", [])
    created = updated = 0
    for item in entities:
        name = (item.get("name") or "").strip()
        kind = item.get("kind", "note")
        if not name:
            continue
        # Restore embedded image
        image_url = item.get("image_url")
        img_data = item.get("image_data")
        if img_data and img_data.startswith("data:image/"):
        # parse data URI
            header, b64 = img_data.split(",", 1)
            ext = header.split("/")[1].split(";")[0]
            ext = "." + ext if not ext.startswith(".") else ext
            if ext not in ALLOWED_EXTS:
                ext = ".jpg"
            filename = f"{uuid.uuid4().hex}{ext}"
            (UPLOADS_DIR / filename).write_bytes(base64.b64decode(b64))
            image_url = f"/uploads/{filename}"
        existing = db.query(Entity).filter(
            Entity.name == name, Entity.kind == kind, Entity.world_id == world_id
        ).first()
        if existing:
            existing.subtype  = item.get("subtype") or existing.subtype
            existing.folder   = item.get("folder")  or existing.folder
            existing.tags     = item.get("tags")     or existing.tags
            existing.summary  = item.get("summary")  or existing.summary
            existing.body     = item.get("body")     or existing.body
            if image_url:
                existing.image_url = image_url
            updated += 1
        else:
            db.add(Entity(
                name=name, kind=kind, world_id=world_id,
                subtype=item.get("subtype"), folder=item.get("folder"),
                tags=item.get("tags"), summary=item.get("summary"),
                body=item.get("body"), image_url=image_url,
            ))
            created += 1
    db.commit()
    return RedirectResponse(f"/worlds?imported={created}&updated={updated}", status_code=303)

# ── Home ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    if not world:
        return RedirectResponse("/worlds")
    counts = {k: db.query(Entity).filter(Entity.kind == k, Entity.world_id == world.id).count() for k in KINDS}
    recent = db.query(Entity).filter(Entity.world_id == world.id).order_by(Entity.updated_at.desc()).limit(8).all()
    worlds = db.query(World).order_by(World.id).all()
    # collect a few maps for the homepage preview
    preview_maps = []
    if _MAPS_DIR.exists():
        _sm = BASE_DIR / "static" / "maps"
        for jf in sorted(_MAPS_DIR.glob("*.json"))[:6]:
            try:
                d = json.loads(jf.read_text(encoding="utf-8"))
                s = jf.stem; img = None
                for ext in (".webp", ".jpg", ".jpeg", ".png"):
                    if (_sm / (s + ext)).exists(): img = f"/static/maps/{s}{ext}"; break
                    if (UPLOADS_DIR / "maps" / (s + ext)).exists(): img = f"/uploads/maps/{s}{ext}"; break
                preview_maps.append({"slug": s, "name": d.get("name", s), "image_url": img})
            except Exception: pass
    # Most-linked entities
    most_linked = []
    if world:
        most_linked = (
            db.query(Entity, func.count(entity_links.c.source_id).label('link_count'))
            .join(entity_links, entity_links.c.target_id == Entity.id)
            .filter(Entity.world_id == world.id)
            .group_by(Entity.id)
            .order_by(func.count(entity_links.c.source_id).desc())
            .limit(6).all()
        )

    # Tag cloud
    top_tags = []
    if world:
        raw_tags = db.query(Entity.tags).filter(
            Entity.world_id == world.id, Entity.tags.isnot(None)
        ).all()
        tag_counts: dict = {}
        for (ts,) in raw_tags:
            for t in (ts or '').split(','):
                t = t.strip()
                if t: tag_counts[t] = tag_counts.get(t, 0) + 1
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:40]

    # Recent boards + schematics
    recent_boards = db.query(InvestBoard).filter(
        InvestBoard.world_id == world.id
    ).order_by(InvestBoard.updated_at.desc()).limit(3).all() if world else []
    recent_schematics = db.query(Schematic).filter(
        Schematic.world_id == world.id
    ).order_by(Schematic.updated_at.desc()).limit(3).all() if world else []

    return templates.TemplateResponse("index.html", {
        "request": request, "counts": counts, "recent": recent,
        "world": world, "worlds": worlds, "preview_maps": preview_maps,
        "most_linked": most_linked, "top_tags": top_tags,
        "recent_boards": recent_boards, "recent_schematics": recent_schematics,
    })

_MAPS_DIR = Path(__file__).parent / "maps"

@app.get("/maps", response_class=HTMLResponse)
def maps_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    maps = []
    _STATIC_MAPS = BASE_DIR / "static" / "maps"
    if _MAPS_DIR.exists():
        for jf in sorted(_MAPS_DIR.glob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                slug = jf.stem
                image_url = None
                # Check static/maps first (bundled), then uploads/maps (user-uploaded)
                for ext in (".webp", ".jpg", ".jpeg", ".png"):
                    if (_STATIC_MAPS / (slug + ext)).exists():
                        image_url = f"/static/maps/{slug}{ext}"
                        break
                    if (UPLOADS_DIR / "maps" / (slug + ext)).exists():
                        image_url = f"/uploads/maps/{slug}{ext}"
                        break
                maps.append({
                    "slug": slug,
                    "name": data.get("name", slug),
                    "width": data.get("width", 0),
                    "height": data.get("height", 0),
                    "markers": len(data.get("markers", [])),
                    "image_url": image_url,
                })
            except Exception:
                pass
    schematics = db.query(Schematic).filter(Schematic.world_id == world.id).order_by(Schematic.name).all()
    return templates.TemplateResponse("maps.html", {
        "request": request, "world": world, "worlds": worlds, "maps": maps,
        "schematics": schematics,
    })

@app.post("/maps/{slug}/upload")
async def map_upload_image(slug: str, file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, "Unsupported file type")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    for old_ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
        old = maps_upload_dir / (slug + old_ext)
        if old.exists():
            old.unlink()
    dest = maps_upload_dir / (slug + ext)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return RedirectResponse("/maps", status_code=303)

@app.get("/maps/{slug}", response_class=HTMLResponse)
def map_viewer(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        raise HTTPException(404)
    map_data = json.loads(jf.read_text(encoding="utf-8"))
    _sm = BASE_DIR / "static" / "maps"
    image_url = None
    for ext in (".webp", ".jpg", ".jpeg", ".png"):
        if (_sm / (slug + ext)).exists(): image_url = f"/static/maps/{slug}{ext}"; break
        if (UPLOADS_DIR / "maps" / (slug + ext)).exists(): image_url = f"/uploads/maps/{slug}{ext}"; break
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).first()
    if not overlay:
        overlay = MapOverlay(slug=slug, custom_markers_json="[]", custom_regions_json="[]")
        db.add(overlay); db.commit()
    # build name→id map for local entity linking
    ename_map = {}
    if world:
        for e in db.query(Entity.name, Entity.id).filter(Entity.world_id == world.id).all():
            ename_map[e.name.lower()] = e.id
    return templates.TemplateResponse("map_viewer.html", {
        "request": request, "world": world, "worlds": worlds,
        "map_data": map_data, "image_url": image_url or "", "slug": slug,
        "overlay": overlay, "ename_map": json.dumps(ename_map),
    })

@app.post("/api/maps/{slug}/overlay")
async def save_map_overlay(slug: str, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).first()
    if not overlay:
        overlay = MapOverlay(slug=slug); db.add(overlay)
    overlay.custom_markers_json = json.dumps(body.get("custom_markers", []))
    overlay.custom_regions_json = json.dumps(body.get("custom_regions", []))
    db.commit()
    return {"ok": True}

@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    rules_path = Path(__file__).parent / "core_rules.md"
    content, toc = _rules_toc(render_md(rules_path.read_text(encoding="utf-8", errors="ignore")) if rules_path.exists() else "<p>Rules file not found.</p>")
    return templates.TemplateResponse("rules.html", {
        "request": request, "world": world, "worlds": worlds, "content": content, "toc": toc,
    })

# ── Schematics ────────────────────────────────────────────────────────────────

def _slug_from_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s

@app.get("/maps/schematic/new", response_class=HTMLResponse)
def schematic_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("schematic_form.html", {
        "request": request, "world": world, "worlds": worlds, "schematic": None,
    })

@app.post("/maps/schematic/new")
async def schematic_new(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    canvas_width: int = Form(2000),
    canvas_height: int = Form(1500),
    canvas_bg: str = Form("dark"),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world = get_active_world(db, active_world)
    slug = _slug_from_name(name)
    base = slug; i = 2
    while db.query(Schematic).filter(Schematic.slug == slug).first():
        slug = f"{base}-{i}"; i += 1
    s = Schematic(world_id=world.id, name=name, slug=slug,
                  description=description or None, is_html=False,
                  canvas_width=canvas_width, canvas_height=canvas_height,
                  canvas_bg=canvas_bg, elements_json="[]")
    db.add(s); db.commit(); db.refresh(s)
    return RedirectResponse(f"/maps/schematic/{s.slug}", status_code=303)

@app.get("/maps/schematic/{slug}", response_class=HTMLResponse)
def schematic_view(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    if s.is_html and s.html_file:
        html_path = SCHEMATICS_STATIC_DIR / s.html_file
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8", errors="ignore"))
        raise HTTPException(404, "HTML schematic file not found")
    world = get_active_world(db, active_world)
    worlds = db.query(World).order_by(World.id).all()
    elements = json.loads(s.elements_json or "[]")
    _BG = {"dark": "#111111", "blueprint": "#0d1b2a", "grid-light": "#1a1a2e", "light": "#f0f0f0"}
    canvas_bg_color = _BG.get(s.canvas_bg or "dark", "#111111")
    return templates.TemplateResponse("schematic.html", {
        "request": request, "world": world, "worlds": worlds,
        "schematic": s, "elements_json": json.dumps(elements),
        "canvas_bg_color": canvas_bg_color,
    })

@app.post("/maps/schematic/{slug}/elements")
async def schematic_save_elements(slug: str, request: Request, db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s: raise HTTPException(404)
    body = await request.json()
    s.elements_json = json.dumps(body.get("elements", []))
    db.commit()
    return {"ok": True}

@app.post("/maps/schematic/{slug}/upload")
async def schematic_upload_image(slug: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, "Unsupported file type")
    sch_dir = UPLOADS_DIR / "schematics"
    sch_dir.mkdir(parents=True, exist_ok=True)
    for old_ext in (".webp", ".jpg", ".jpeg", ".png", ".gif"):
        old = sch_dir / (slug + old_ext)
        if old.exists(): old.unlink()
    dest = sch_dir / (slug + ext)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    s.image_url = f"/uploads/schematics/{slug}{ext}"
    db.commit()
    return RedirectResponse(f"/maps/schematic/{slug}", status_code=303)

@app.post("/maps/schematic/{slug}/delete")
def schematic_delete(slug: str, db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    db.delete(s); db.commit()
    return RedirectResponse("/maps", status_code=303)

# ── Investigation Boards ──────────────────────────────────────────────────────

def _get_world_ctx(db, active_world):
    worlds = db.query(World).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds

@app.get("/api/ai/world-context")
def ai_world_context(db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _get_world_ctx(db, active_world)
    if not world:
        return {"context": "", "world_name": ""}
    lines = [f"# {world.name}", world.description or "", ""]
    # Characters, locations, orgs, creatures, events: name + subtype + summary
    for kind in ["character", "location", "organization", "creature", "event"]:
        ents = db.query(Entity).filter(
            Entity.world_id == world.id, Entity.kind == kind
        ).order_by(Entity.name).all()
        if not ents:
            continue
        lines.append(f"## {kind.upper()}S ({len(ents)})")
        for e in ents:
            line = f"- **{e.name}**"
            if e.subtype:
                line += f" [{e.subtype}]"
            if e.summary:
                line += f": {e.summary}"
            lines.append(line)
        lines.append("")
    # Notes: full body text (lore documents)
    notes = db.query(Entity).filter(
        Entity.world_id == world.id, Entity.kind == "note"
    ).order_by(Entity.name).all()
    if notes:
        lines.append("## LORE DOCUMENTS (full text)")
        for e in notes:
            lines.append(f"\n### {e.name}" + (f" [{e.subtype}]" if e.subtype else ""))
            if e.summary:
                lines.append(e.summary)
            if e.body:
                lines.append(e.body[:5000])
        lines.append("")
    # Items and feats: grouped by subtype, names only
    for kind in ["item", "feat"]:
        ents = db.query(Entity.name, Entity.subtype).filter(
            Entity.world_id == world.id, Entity.kind == kind
        ).order_by(Entity.subtype, Entity.name).all()
        if not ents:
            continue
        lines.append(f"## {kind.upper()}S ({len(ents)} total)")
        by_sub: dict = {}
        for name, sub in ents:
            by_sub.setdefault(sub or "other", []).append(name)
        for sub, names in by_sub.items():
            snippet = ", ".join(names[:25])
            if len(names) > 25:
                snippet += f" … +{len(names)-25} more"
            lines.append(f"  [{sub}]: {snippet}")
        lines.append("")
    return {"context": "\n".join(lines), "world_name": world.name}


def _find_relevant_entities(db: Session, world_id: int, query: str, limit: int = 25) -> list:
    words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
    if not words:
        return (
            db.query(Entity)
            .filter(Entity.world_id == world_id)
            .order_by(Entity.kind, Entity.name)
            .limit(limit)
            .all()
        )
    filters = [
        or_(
            Entity.name.ilike(f'%{w}%'),
            Entity.summary.ilike(f'%{w}%'),
            Entity.tags.ilike(f'%{w}%'),
        )
        for w in words
    ]
    return (
        db.query(Entity)
        .filter(Entity.world_id == world_id, or_(*filters))
        .order_by(Entity.kind, Entity.name)
        .limit(limit)
        .all()
    )


def _format_context_from_entities(entities: list) -> str:
    lines = []
    for e in entities:
        line = f"- [{e.kind}] {e.name}"
        if e.subtype:
            line += f" ({e.subtype})"
        if e.summary:
            line += f": {e.summary}"
        lines.append(line)
    return "\n".join(lines)


class _SmartCtxBody(BaseModel):
    query: str = ""


@app.post("/api/ai/world-context-smart")
def ai_world_context_smart(
    body: _SmartCtxBody,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _get_world_ctx(db, active_world)
    if not world:
        return {"context": "", "count": 0}
    entities = _find_relevant_entities(db, world.id, body.query)
    return {"context": _format_context_from_entities(entities), "count": len(entities)}


class _SaveNoteBody(BaseModel):
    title: str
    content: str


@app.post("/api/ai/save-note")
def ai_save_note(
    body: _SaveNoteBody,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _get_world_ctx(db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    note = Entity(
        world_id=world.id,
        kind="note",
        name=body.title,
        body=body.content,
        summary=body.content[:120],
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"id": note.id, "name": note.name}


class _SmartGenBody(BaseModel):
    name: str
    kind: str
    subtype: str = ""
    summary: str = ""


@app.post("/api/ai/generate/entity-smart")
async def gen_entity_smart(
    body: _SmartGenBody,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _get_world_ctx(db, active_world)
    related_ctx = ""
    if world:
        related = _find_relevant_entities(db, world.id, f"{body.name} {body.summary}", limit=12)
        related_ctx = _format_context_from_entities(related)
    prompt = (
        f"Write an expanded lore entry for this {body.kind}"
        + (f" ({body.subtype})" if body.subtype else "")
        + f" named '{body.name}'."
        + (f" Summary: {body.summary}." if body.summary else "")
        + ("\n\nRelated world lore for context:\n" + related_ctx if related_ctx else "")
    )
    return {"result": await _ai_module.generate(prompt)}


@app.get("/ai", response_class=HTMLResponse)
def ai_chat_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    entity_counts = {}
    world_system = (
        "You are a creative world-building AI assistant for a Neon & Dragons "
        "cyberpunk-fantasy TTRPG setting. Help the Game Master with world-building, "
        "lore, NPC backstories, plot hooks, and creative writing. Be vivid and immersive."
    )
    if world:
        for k in KINDS:
            entity_counts[k] = db.query(Entity).filter(
                Entity.world_id == world.id, Entity.kind == k
            ).count()
        counts_str = ", ".join(f"{v} {k}s" for k, v in entity_counts.items() if v > 0)
        world_system = (
            f"You are a creative world-building AI assistant for '{world.name}', "
            f"a Neon & Dragons cyberpunk-fantasy TTRPG setting. "
            f"The world currently contains: {counts_str}. "
            f"Help the Game Master with world-building, lore, NPC backstories, plot hooks, "
            f"and creative writing. Be vivid, immersive, and consistent with the cyberpunk-fantasy tone. "
            f"Keep responses focused; expand only when asked."
        )
    return templates.TemplateResponse("ai_chat.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
        "entity_counts": entity_counts, "world_system": world_system,
    })

@app.get("/boards", response_class=HTMLResponse)
def boards_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    boards = db.query(InvestBoard).filter(InvestBoard.world_id == (world.id if world else 1)).all()
    return templates.TemplateResponse("boards.html", {
        "request": request, "world": world, "worlds": worlds,
        "boards": boards, "kinds": KINDS, "kind_icons": KIND_ICONS,
    })

@app.get("/boards/new", response_class=HTMLResponse)
def board_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    return templates.TemplateResponse("board_new.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
    })

@app.post("/boards/new")
def board_new_post(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    canvas_bg: str = Form("cork"),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = _get_world_ctx(db, active_world)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "board"
    base_slug = slug
    i = 2
    while db.query(InvestBoard).filter(InvestBoard.slug == slug).first():
        slug = f"{base_slug}-{i}"; i += 1
    b = InvestBoard(world_id=world.id if world else 1, name=name, slug=slug,
                    description=description, canvas_bg=canvas_bg)
    db.add(b); db.commit()
    return RedirectResponse(f"/boards/{slug}", status_code=303)

@app.get("/boards/{slug}", response_class=HTMLResponse)
def board_view(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _get_world_ctx(db, active_world)
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b: raise HTTPException(404)
    # Build entity name→{id,kind,image_url} map for quick lookup
    entities = db.query(Entity.id, Entity.name, Entity.kind, Entity.image_url).all()
    entity_list = [{"id": e.id, "name": e.name, "kind": e.kind, "image_url": e.image_url} for e in entities]
    # nodes_json may be a legacy bare array OR the new {nodes, groups} object
    raw_nodes = json.loads(b.nodes_json or "[]")
    if isinstance(raw_nodes, list):
        nodes_payload = {"nodes": raw_nodes, "groups": []}
    else:
        nodes_payload = {"nodes": raw_nodes.get("nodes", []), "groups": raw_nodes.get("groups", [])}
    edges = json.loads(b.edges_json or "[]")
    return templates.TemplateResponse("board.html", {
        "request": request, "world": world, "worlds": worlds,
        "board": b, "kinds": KINDS, "kind_icons": KIND_ICONS,
        "nodes_json": json.dumps(nodes_payload),
        "edges_json": json.dumps(edges),
        "entity_list_json": json.dumps(entity_list),
    })

@app.post("/boards/{slug}/save")
async def board_save(slug: str, request: Request, db: Session = Depends(get_db)):
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b: raise HTTPException(404)
    body = await request.json()
    # stash nodes + groups together to avoid schema change
    b.nodes_json = json.dumps({"nodes": body.get("nodes", []), "groups": body.get("groups", [])})
    b.edges_json = json.dumps(body.get("edges", []))
    db.commit()
    return {"ok": True}

@app.post("/boards/{slug}/delete")
def board_delete(slug: str, db: Session = Depends(get_db)):
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b: raise HTTPException(404)
    db.delete(b); db.commit()
    return RedirectResponse("/boards", status_code=303)

@app.get("/boards/{slug}/export", response_class=HTMLResponse)
def board_export(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    from datetime import date as _date
    world, worlds = _get_world_ctx(db, active_world)
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b:
        raise HTTPException(404)
    raw_nodes = json.loads(b.nodes_json or "[]")
    if isinstance(raw_nodes, list):
        nodes = raw_nodes
        groups = []
    else:
        nodes = raw_nodes.get("nodes", [])
        groups = raw_nodes.get("groups", [])
    edges = json.loads(b.edges_json or "[]")
    xs = [n.get("x", 0) for n in nodes] or [0]
    ys = [n.get("y", 0) for n in nodes] or [0]
    canvas_w = max(xs) + 300
    canvas_h = max(ys) + 200
    resp = templates.TemplateResponse("board_export.html", {
        "request": request, "world": world, "worlds": worlds,
        "board": b, "nodes": nodes, "edges": edges, "groups": groups,
        "canvas_w": canvas_w, "canvas_h": canvas_h,
        "export_date": _date.today().isoformat(),
    })
    resp.headers["Content-Disposition"] = f'attachment; filename="{slug}-board.html"'
    return resp

# ── World Book Export ─────────────────────────────────────────────────────────

@app.get("/export")
def world_export_book(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    import zipfile, io as _io
    from datetime import date
    world, worlds = _get_world_ctx(db, active_world)
    if not world:
        return RedirectResponse("/worlds")

    image_files: dict[str, bytes] = {}  # zip path -> bytes

    entities_raw = (
        db.query(Entity)
        .filter(Entity.world_id == world.id)
        .order_by(Entity.kind, Entity.name)
        .all()
    )
    entities_by_kind: dict[str, list] = {}
    for ent in entities_raw:
        ent.image_rel = None  # type: ignore[attr-defined]
        if ent.image_url and ent.image_url.startswith("/uploads/"):
            try:
                rel = ent.image_url[len("/uploads/"):]
                img_path = UPLOADS_DIR / rel
                if img_path.exists():
                    zip_img_path = "assets/images/" + rel.replace("\\", "/")
                    image_files[zip_img_path] = img_path.read_bytes()
                    ent.image_rel = "./" + zip_img_path  # type: ignore[attr-defined]
            except Exception:
                pass
        raw_html = render_md(ent.body) if ent.body else ""
        ent.body_html = re.sub(r'^<h1[^>]*>.*?</h1>\s*', '', raw_html, count=1, flags=re.DOTALL)  # type: ignore[attr-defined]
        entities_by_kind.setdefault(ent.kind, []).append(ent)

    raw_boards = db.query(InvestBoard).filter(InvestBoard.world_id == world.id).all()
    boards_export = []
    for b in raw_boards:
        raw_nodes = json.loads(b.nodes_json or "[]")
        nodes = raw_nodes if isinstance(raw_nodes, list) else raw_nodes.get("nodes", [])
        edges = json.loads(b.edges_json or "[]")
        boards_export.append({"name": b.name, "description": b.description, "nodes": nodes, "edges": edges})

    maps_export = []
    if _MAPS_DIR.exists():
        for jf in sorted(_MAPS_DIR.glob("*.json")):
            try:
                d = json.loads(jf.read_text(encoding="utf-8"))
                maps_export.append({"name": d.get("name", jf.stem), "markers": len(d.get("markers", []))})
            except Exception:
                pass

    rules_path = Path(__file__).parent / "core_rules.md"
    rules_html = render_md(rules_path.read_text(encoding="utf-8", errors="ignore")) if rules_path.exists() else ""

    html = templates.env.get_template("world_export.html").render(
        world=world, worlds=worlds, kinds=KINDS, kind_icons=KIND_ICONS,
        entities_by_kind=entities_by_kind, boards=boards_export,
        maps=maps_export, rules_html=rules_html,
        export_date=date.today().isoformat(),
    )

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html.encode("utf-8"))
        css_path = BASE_DIR / "static" / "style.css"
        if css_path.exists():
            zf.writestr("assets/style.css", css_path.read_bytes())
        for zpath, data in image_files.items():
            zf.writestr(zpath, data)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{world.slug}-worldbook.zip"'},
    )

# ── List ──────────────────────────────────────────────────────────────────────

_COL_PRIORITY = [
    "Rank",
    "Rarity", "Damage", "Armor", "Rounds", "Strength", "Body", "Dexterity",
    "Perception", "Augment Slots", "Max Health", "Max PP", "Feats", "Cost",
    "Difficulty to craft", "Effect", "Type of Feat", "Requirement",
    "Requirements", "Special conditions",
]
_COL_PRIORITY_IDX = {c.lower(): i for i, c in enumerate(_COL_PRIORITY)}

@app.get("/kind/{kind}", response_class=HTMLResponse)
def list_entities(request: Request, kind: str, q: str = "", folder: Optional[str] = None,
                  view: str = "", db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(db, active_world)

    # Base searchable query (no folder filter — used for counts/sidebar)
    base_q = db.query(Entity).filter(Entity.kind == kind, Entity.world_id == world.id)
    if q:
        base_q = base_q.filter(or_(
            Entity.name.ilike(f"%{q}%"), Entity.tags.ilike(f"%{q}%"),
            Entity.summary.ilike(f"%{q}%"), Entity.body.ilike(f"%{q}%"),
        ))

    # Folder counts and list (computed before entity query so we can detect parent folders)
    count_rows = (base_q.with_entities(Entity.folder, func.count(Entity.id))
                  .group_by(Entity.folder).all())
    folder_counts = {(row[0] or ""): row[1] for row in count_rows}
    total_count = sum(folder_counts.values())
    folders = sorted(k for k in folder_counts if k)

    # Detect parent folder (has child folders) and apply recursive or exact query
    is_parent_folder = False
    query = base_q
    if folder is not None:
        if folder:
            child_folders = [f for f in folders if f.startswith(folder + '/')]
            is_parent_folder = bool(child_folders)
            if is_parent_folder:
                query = query.filter(
                    or_(Entity.folder == folder, Entity.folder.like(folder + '/%'))
                )
            else:
                query = query.filter(Entity.folder == folder)
        else:
            query = query.filter(Entity.folder.is_(None))

    entities = query.order_by(Entity.folder.nulls_last(), Entity.name).all()

    # Group by folder for root view display
    grouped: dict[str, list] = {}
    for e in entities:
        grouped.setdefault(e.folder or "", []).append(e)

    # Subfolder groups for parent folder table view
    # Key = immediate child path (e.g. "Weapons/Handguns"), value = entities in that subtree
    subfolder_groups: list[tuple[str, list]] = []
    if is_parent_folder:
        gmap: dict[str, list] = {}
        prefix = folder + '/'
        for e in entities:
            if not e.folder or e.folder == folder:
                key = folder
            elif e.folder.startswith(prefix):
                seg = e.folder[len(prefix):].split('/')[0]
                key = prefix + seg
            else:
                key = folder
            gmap.setdefault(key, []).append(e)
        subfolder_groups = sorted(gmap.items(),
                                  key=lambda x: (x[0] == folder, x[0].lower()))

    # Kinds that get stat-column extraction vs. description-only table
    _STAT_KINDS = {"item", "feat", "creature", "character"}
    _DESC_ONLY_KINDS = {"character", "organization", "location", "event", "note"}

    # Character-creation feat folders get a simple Rank+Description table only
    _CHAR_FEAT_ROOTS = ("Race Feats", "Common Feats", "Profession Feats")
    char_feat_folder = (
        kind == "feat" and bool(folder) and
        any(folder == r or folder.startswith(r + "/") for r in _CHAR_FEAT_ROOTS)
    )

    # Stat table columns (derived from all entities currently shown)
    entity_stats: dict[int, dict[str, str]] = {}
    table_cols: list[str] = []
    _rank_re = re.compile(r'\b(rank\s+(\d+)|edge|origin)\b', re.IGNORECASE)
    if folder is not None:
        col_freq: dict[str, int] = {}
        for e in entities:
            d: dict[str, str] = {}
            if kind in _STAT_KINDS and not char_feat_folder:
                rows = parse_stats(e.body or "")
                if rows:
                    d = {r["key"]: r["val"] for r in rows}
            # inject Rank from folder path for character-creation feats
            if kind == "feat" and e.folder:
                m = _rank_re.search(e.folder)
                if m:
                    seg = m.group(1).lower()
                    d["Rank"] = seg.split()[-1] if seg.startswith("rank") else seg.title()
            if d:
                entity_stats[e.id] = d
                for k in d:
                    col_freq[k] = col_freq.get(k, 0) + 1
        if kind in _STAT_KINDS and not char_feat_folder:
            table_cols = sorted(
                col_freq,
                key=lambda k: (_COL_PRIORITY_IDX.get(k.lower(), 999), -col_freq[k])
            )[:8]

    if not view:
        view = "table" if folder is not None else "grid"

    worlds = db.query(World).order_by(World.id).all()
    return templates.TemplateResponse("entities/list.html", {
        "request": request, "kind": kind, "entities": entities,
        "grouped": grouped, "folders": folders, "active_folder": folder,
        "folder_counts": folder_counts, "total_count": total_count,
        "q": q, "world": world, "worlds": worlds,
        "view": view, "entity_stats": entity_stats, "table_cols": table_cols,
        "is_parent_folder": is_parent_folder, "subfolder_groups": subfolder_groups,
        "char_feat_folder": char_feat_folder,
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
    backlinks = (
        db.query(Entity)
        .join(entity_links, entity_links.c.source_id == Entity.id)
        .filter(entity_links.c.target_id == entity_id)
        .order_by(Entity.kind, Entity.name)
        .all()
    )
    return templates.TemplateResponse("entities/detail.html", {
        "request": request, "entity": entity, "all_entities": all_entities,
        "world": world, "worlds": worlds, "backlinks": backlinks,
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
            if item.get("image_url"):
                existing.image_url = item["image_url"]
            if "body" in item:
                existing.body = item["body"] or None
            if "summary" in item:
                existing.summary = item["summary"] or None
            if "folder" in item:
                existing.folder = item["folder"] or None
            if item.get("subtype") and not existing.subtype:
                existing.subtype = item["subtype"]
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
