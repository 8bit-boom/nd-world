"""GM-defined custom entity kinds (categories) — on top of the fixed
built-ins in app/constants.py's KINDS, a GM can add their own per-world
content category (e.g. "Vehicles", "Deities") that behaves exactly like a
built-in one: a real top-nav tab, a live-count home stat tile, and entities
filable under it via the normal create/edit/import flows.

Storage lives on World itself (custom_kinds_json), matching
home_content.py's home_sections_json precedent — see app/models.py's World
class for the exact JSON shape and app/deps.py's load_custom_kinds/
effective_kinds for parsing and world-scoped merging with the built-ins.
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import CUSTOM_KIND_PREFIX, MAX_CUSTOM_KINDS, get_world_ctx, load_custom_kinds
from ..models import Entity, World
from ..templating import templates

router = APIRouter()

_MAX_LABEL_LEN = 40
_MAX_ICON_LEN = 8
_MAX_SUBTYPES = 20
_MAX_SUBTYPE_LEN = 40


def _slugify_label(label: str) -> str:
    """label -> lowercase [a-z0-9_]+ body, <=24 chars (leaves room for the
    7-char "custom_" prefix under Entity.kind's 32-char column budget)."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return (slug or "kind")[:24]


def _parse_subtypes(raw: str) -> list:
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()[:_MAX_SUBTYPE_LEN]
        if part and part not in out:
            out.append(part)
        if len(out) >= _MAX_SUBTYPES:
            break
    return out


def _unique_id(base_id: str, existing_ids: set) -> str:
    if base_id not in existing_ids:
        return base_id
    n = 2
    while f"{base_id}_{n}" in existing_ids:
        n += 1
    return f"{base_id}_{n}"


def _validate_new_kind(label: str, icon: str, subtypes_raw: str, world: World):
    """Returns (entry, None) or (None, error_message)."""
    label = (label or "").strip()[:_MAX_LABEL_LEN]
    if not label:
        return None, "A label is required."
    existing = load_custom_kinds(world)
    if len(existing) >= MAX_CUSTOM_KINDS:
        return None, f"This world already has the maximum of {MAX_CUSTOM_KINDS} custom kinds."
    slug = _slugify_label(label)
    base_id = f"{CUSTOM_KIND_PREFIX}{slug}"
    existing_ids = {e["id"] for e in existing}
    kind_id = _unique_id(base_id, existing_ids)
    icon = (icon or "").strip()[:_MAX_ICON_LEN] or "🏷"
    entry = {
        "id": kind_id, "label": label, "icon": icon,
        "subtypes": _parse_subtypes(subtypes_raw),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return entry, None


def _sanitize_saved_list(raw_json: str, world: World) -> list:
    """Re-validates the full posted list on /kinds/edit's save (labels/
    icons/subtypes/order) — ids are immutable and taken only from what's
    already stored for this world; any id in the payload that doesn't
    match an existing custom kind here is dropped, so this can't be used
    to smuggle in a new id or steal another world's entry."""
    try:
        posted = json.loads(raw_json or "[]")
    except (TypeError, ValueError):
        posted = []
    if not isinstance(posted, list):
        posted = []
    by_id = {e["id"]: e for e in load_custom_kinds(world)}
    out = []
    seen = set()
    for item in posted[:MAX_CUSTOM_KINDS]:
        if not isinstance(item, dict):
            continue
        kid = item.get("id")
        if kid not in by_id or kid in seen:
            continue
        seen.add(kid)
        original = by_id[kid]
        label = str(item.get("label", "")).strip()[:_MAX_LABEL_LEN] or original["label"]
        icon = str(item.get("icon", "")).strip()[:_MAX_ICON_LEN] or "🏷"
        subtypes_raw = item.get("subtypes")
        if isinstance(subtypes_raw, list):
            subtypes = [str(s).strip()[:_MAX_SUBTYPE_LEN] for s in subtypes_raw if str(s).strip()][:_MAX_SUBTYPES]
        else:
            subtypes = original["subtypes"]
        out.append({
            "id": kid, "label": label, "icon": icon,
            "subtypes": subtypes, "created_at": original["created_at"],
        })
    return out


@router.get("/worlds/{world_id}/kinds/edit", response_class=HTMLResponse)
def kinds_edit_form(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    edit_world = db.get(World, world_id)
    if not edit_world:
        raise HTTPException(404)
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("kinds_edit.html", {
        "request": request, "world": world, "worlds": worlds, "edit_world": edit_world,
        "initial_kinds": load_custom_kinds(edit_world),
        "max_custom_kinds": MAX_CUSTOM_KINDS,
    })


@router.post("/worlds/{world_id}/kinds/edit")
async def kinds_edit_save(world_id: int, request: Request, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    form = await request.form()
    w.custom_kinds_json = json.dumps(_sanitize_saved_list(str(form.get("custom_kinds_json", "[]") or "[]"), w))
    db.commit()
    return RedirectResponse(f"/worlds/{world_id}/kinds/edit?saved=1", status_code=303)


@router.post("/worlds/{world_id}/kinds/new")
async def kinds_new(world_id: int, request: Request, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    form = await request.form()
    entry, error = _validate_new_kind(
        str(form.get("label", "")), str(form.get("icon", "")), str(form.get("subtypes", "")), w,
    )
    if error:
        raise HTTPException(400, error)
    kinds = load_custom_kinds(w)
    kinds.append(entry)
    w.custom_kinds_json = json.dumps(kinds)
    db.commit()
    return RedirectResponse(f"/worlds/{world_id}/kinds/edit", status_code=303)


@router.post("/worlds/{world_id}/kinds/{kind_id}/delete")
def kinds_delete(world_id: int, kind_id: str, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    kinds = load_custom_kinds(w)
    if not any(k["id"] == kind_id for k in kinds):
        raise HTTPException(404, "Not a custom kind for this world")
    in_use = db.query(Entity).filter(Entity.world_id == world_id, Entity.kind == kind_id).count()
    if in_use:
        raise HTTPException(400, f"{in_use} entit{'y' if in_use == 1 else 'ies'} still use this kind — recategorize or delete them first")
    w.custom_kinds_json = json.dumps([k for k in kinds if k["id"] != kind_id])
    db.commit()
    return RedirectResponse(f"/worlds/{world_id}/kinds/edit", status_code=303)
