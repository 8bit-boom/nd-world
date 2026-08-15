"""Race catalog: a bundled markdown library of playable races (standard/advanced/
exceptional tiers) a GM can browse and one-click add into their world as normal
`Entity(kind="race")` rows — same model as every other entity kind, so once added
a race is a fully editable lore entry like any character or location.

This is a convenience on top of generic entity CRUD (which already supports
kind="race" via /kind/race and /entity/new), not a replacement for it: the
bundled catalog is reference content to start from, not something a GM is
required to use.
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
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}

_RACES_DIR = Path(__file__).parent.parent / "races"
_RACE_TIERS = ["standard", "advanced", "exceptional"]


def _upload_race_image(file: Optional[UploadFile], db: Optional[Session] = None) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    races_dir = UPLOADS_DIR / "races"
    races_dir.mkdir(parents=True, exist_ok=True)
    fname = unique_upload_filename(file.filename, ext)
    dest = races_dir / fname
    copy_upload_bounded(file, dest)
    if db is not None:
        settings = get_app_settings(db)
        dest = convert_image(dest, static_format=settings.static_format,
                              animated_format=settings.animated_format)
    else:
        dest = convert_image(dest)
    return f"/uploads/races/{dest.name}"


def _load_builtin_races() -> list[dict]:
    races = []
    for tier in _RACE_TIERS:
        tier_dir = _RACES_DIR / tier
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
                image_url = f"/static/races/{tier}/{img_match.group(1)}"
                raw = "\n".join(lines[1:]).lstrip()

            raw_fixed = re.sub(
                r'!\[([^\]]*)\]\((?!http)([^)]+)\)',
                lambda m: f'![{m.group(1)}](/static/races/{tier}/{m.group(2)})',
                raw,
            )

            name = md_file.stem.split("_")[0].replace("-", " ").title()
            for line in raw_fixed.splitlines():
                if line.startswith("# "):
                    name = line[2:].strip()
                    break

            summary = ""
            in_entry = False
            for line in raw_fixed.splitlines():
                if line.strip().startswith("## Entry"):
                    in_entry = True
                    continue
                if in_entry and line.strip() and not line.startswith("#") and not line.startswith("**"):
                    summary = line.strip()[:200]
                    break

            slug = md_file.stem.split("_")[0]
            races.append({
                "slug": slug, "name": name, "tier": tier, "image_url": image_url,
                "summary": summary, "body_html": render_md(raw_fixed), "raw": raw,
            })
    return races


@router.get("/races", response_class=HTMLResponse)
def races_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    builtin = _load_builtin_races()

    world_races = []
    names_in_world = set()
    if world:
        world_races = db.query(Entity).filter(
            Entity.world_id == world.id, Entity.kind == "race"
        ).order_by(Entity.subtype, Entity.name).all()
        names_in_world = {e.name for e in world_races if e.name}

    available_builtin = [r for r in builtin if r["name"] not in names_in_world]
    available_by_tier = {t: [r for r in available_builtin if r["tier"] == t] for t in _RACE_TIERS}
    world_by_tier = {t: [e for e in world_races if e.subtype == t] for t in _RACE_TIERS}

    return templates.TemplateResponse("races.html", {
        "request": request, "world": world, "worlds": worlds,
        "world_races": world_races, "world_by_tier": world_by_tier,
        "available_builtin": available_builtin, "available_by_tier": available_by_tier,
    })


@router.get("/races/new", response_class=HTMLResponse)
def races_new_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds", status_code=303)
    return templates.TemplateResponse("race_new.html", {"request": request, "world": world, "worlds": worlds})


@router.post("/races/new")
async def races_new(
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
    image_url = _upload_race_image(file, db)
    db.add(Entity(
        world_id=world.id,
        kind="race",
        subtype=tier if tier in _RACE_TIERS else "standard",
        name=name.strip(),
        summary=summary.strip() or None,
        body=body.strip() or None,
        image_url=image_url,
    ))
    db.commit()
    return RedirectResponse("/races", status_code=303)


@router.post("/races/add-builtin")
async def races_add_builtin(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    payload = await request.json()
    slug = payload.get("slug", "")
    tier = payload.get("tier", "")
    builtin = _load_builtin_races()
    race = next((r for r in builtin if r["slug"] == slug and r["tier"] == tier), None)
    if not race:
        raise HTTPException(404, "Built-in race not found")
    exists = db.query(Entity).filter(
        Entity.world_id == world.id, Entity.kind == "race", Entity.name == race["name"]
    ).first()
    if not exists:
        db.add(Entity(
            world_id=world.id, kind="race", subtype=race["tier"],
            name=race["name"], summary=race["summary"] or None,
            body=race["raw"] or None, image_url=race["image_url"] or None,
        ))
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/races/add-all-builtin")
def races_add_all_builtin(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    builtin = _load_builtin_races()
    added = 0
    for r in builtin:
        exists = db.query(Entity).filter(
            Entity.world_id == world.id, Entity.kind == "race", Entity.name == r["name"]
        ).first()
        if not exists:
            db.add(Entity(
                world_id=world.id, kind="race", subtype=r["tier"],
                name=r["name"], summary=r["summary"] or None,
                body=r["raw"] or None, image_url=r["image_url"] or None,
            ))
            added += 1
    db.commit()
    return RedirectResponse(f"/races?added={added}", status_code=303)


@router.post("/races/{race_id}/delete")
def races_delete(race_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    entity = db.get(Entity, race_id)
    if not entity or entity.kind != "race" or not world or entity.world_id != world.id:
        raise HTTPException(404)
    db.delete(entity)
    db.commit()
    return RedirectResponse("/races", status_code=303)
