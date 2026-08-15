"""Profession catalog: a bundled markdown library of playable professions
(standard/advanced/exceptional tiers) a GM can browse and one-click add into
their world as normal `Entity(kind="profession")` rows — same model as every
other entity kind, so once added a profession is a fully editable lore entry
like any character or location.

Mirrors app/routers/races.py's structure exactly. This is a convenience on
top of generic entity CRUD (which already supports kind="profession" via
/kind/profession and /entity/new), not a replacement for it: the bundled
catalog is reference content to start from, not something a GM is required
to use.
"""
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db, get_app_settings
from ..deps import get_world_ctx
from ..imaging import convert_image
from ..models import Entity
from ..rendering import render_md
from ..templating import templates
from ..uploads import copy_upload_bounded, unique_upload_filename

router = APIRouter()

UPLOADS_DIR = Path(__import__("os").environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_PROFESSIONS_DIR = Path(__file__).parent.parent / "professions"
_PROFESSION_TIERS = ["standard", "advanced", "exceptional"]


def _upload_profession_image(file: Optional[UploadFile], db: Optional[Session] = None) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    professions_dir = UPLOADS_DIR / "professions"
    professions_dir.mkdir(parents=True, exist_ok=True)
    fname = unique_upload_filename(file.filename, ext)
    dest = professions_dir / fname
    copy_upload_bounded(file, dest)
    if db is not None:
        settings = get_app_settings(db)
        dest = convert_image(dest, static_format=settings.static_format,
                              animated_format=settings.animated_format)
    else:
        dest = convert_image(dest)
    return f"/uploads/professions/{dest.name}"


def _load_builtin_professions() -> list[dict]:
    professions = []
    for tier in _PROFESSION_TIERS:
        tier_dir = _PROFESSIONS_DIR / tier
        if not tier_dir.exists():
            continue
        for md_file in sorted(tier_dir.glob("*.md")):
            try:
                raw = md_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            image_url = ""
            lines = raw.splitlines()
            img_match = re.match(r'!\[.*?\]\((.+?)\)', lines[0].strip()) if lines else None
            if img_match:
                image_url = f"/static/professions/{tier}/{img_match.group(1)}"
                raw = "\n".join(lines[1:]).lstrip()

            raw_fixed = re.sub(
                r'!\[([^\]]*)\]\((?!http)([^)]+)\)',
                lambda m: f'![{m.group(1)}](/static/professions/{tier}/{m.group(2)})',
                raw,
            )

            name = md_file.stem.split("_")[0].replace("-", " ").title()
            for line in raw_fixed.splitlines():
                if line.startswith("# "):
                    name = line[2:].strip()
                    break

            summary = ""
            in_section = False
            for line in raw_fixed.splitlines():
                if line.strip().startswith("## "):
                    in_section = True
                    continue
                if in_section and line.strip() and not line.startswith("#") and not line.startswith("**"):
                    summary = line.strip()[:200]
                    break

            slug = md_file.stem.split("_")[0]
            professions.append({
                "slug": slug, "name": name, "tier": tier, "image_url": image_url,
                "summary": summary, "body_html": render_md(raw_fixed), "raw": raw,
            })
    return professions


@router.get("/professions", response_class=HTMLResponse)
def professions_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    builtin = _load_builtin_professions()

    world_professions = []
    names_in_world = set()
    if world:
        world_professions = db.query(Entity).filter(
            Entity.world_id == world.id, Entity.kind == "profession"
        ).order_by(Entity.subtype, Entity.name).all()
        names_in_world = {e.name for e in world_professions if e.name}

    available_builtin = [p for p in builtin if p["name"] not in names_in_world]
    available_by_tier = {t: [p for p in available_builtin if p["tier"] == t] for t in _PROFESSION_TIERS}
    world_by_tier = {t: [e for e in world_professions if e.subtype == t] for t in _PROFESSION_TIERS}

    return templates.TemplateResponse("professions.html", {
        "request": request, "world": world, "worlds": worlds,
        "world_professions": world_professions, "world_by_tier": world_by_tier,
        "available_builtin": available_builtin, "available_by_tier": available_by_tier,
    })


@router.get("/professions/new", response_class=HTMLResponse)
def professions_new_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds", status_code=303)
    return templates.TemplateResponse("profession_new.html", {"request": request, "world": world, "worlds": worlds})


@router.post("/professions/new")
async def professions_new(
    request: Request,
    name: str = Form(...),
    tier: str = Form("standard"),
    summary: str = Form(""),
    body: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    image_url = _upload_profession_image(file, db)
    db.add(Entity(
        world_id=world.id,
        kind="profession",
        subtype=tier if tier in _PROFESSION_TIERS else "standard",
        name=name.strip(),
        summary=summary.strip() or None,
        body=body.strip() or None,
        image_url=image_url,
    ))
    db.commit()
    return RedirectResponse("/professions", status_code=303)


@router.post("/professions/add-builtin")
async def professions_add_builtin(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    payload = await request.json()
    slug = payload.get("slug", "")
    tier = payload.get("tier", "")
    builtin = _load_builtin_professions()
    profession = next((p for p in builtin if p["slug"] == slug and p["tier"] == tier), None)
    if not profession:
        raise HTTPException(404, "Built-in profession not found")
    exists = db.query(Entity).filter(
        Entity.world_id == world.id, Entity.kind == "profession", Entity.name == profession["name"]
    ).first()
    if not exists:
        db.add(Entity(
            world_id=world.id, kind="profession", subtype=profession["tier"],
            name=profession["name"], summary=profession["summary"] or None,
            body=profession["raw"] or None, image_url=profession["image_url"] or None,
        ))
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/professions/add-all-builtin")
def professions_add_all_builtin(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    builtin = _load_builtin_professions()
    added = 0
    for p in builtin:
        exists = db.query(Entity).filter(
            Entity.world_id == world.id, Entity.kind == "profession", Entity.name == p["name"]
        ).first()
        if not exists:
            db.add(Entity(
                world_id=world.id, kind="profession", subtype=p["tier"],
                name=p["name"], summary=p["summary"] or None,
                body=p["raw"] or None, image_url=p["image_url"] or None,
            ))
            added += 1
    db.commit()
    return RedirectResponse(f"/professions?added={added}", status_code=303)


@router.post("/professions/{profession_id}/delete")
def professions_delete(profession_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    entity = db.get(Entity, profession_id)
    if not entity or entity.kind != "profession" or not world or entity.world_id != world.id:
        raise HTTPException(404)
    db.delete(entity)
    db.commit()
    return RedirectResponse("/professions", status_code=303)
