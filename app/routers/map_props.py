"""The map-object library: upload pictures of beds, barrels, wall sections... and place them on a map.

Uploads go through app/prop_images.py (raster pictures re-encoded and trimmed, SVG rebuilt from an allow-list), so what is
stored is safe to show every player. Writes need the Maps section's edit level - checked in each handler, because the route
allowlist alone admits every assistant. The list is also readable by anyone who may edit maps (the editor's palette)."""
import os
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from .. import prop_images
from ..database import get_db
from ..deps import get_world_ctx, world_can_edit_section
from ..models import MapProp, Schematic
from ..uploads import unique_upload_filename

router = APIRouter()

_UPLOADS = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_PROPS_DIR = _UPLOADS / "props"
MAX_PROPS_PER_WORLD = 500
MAX_FILES_PER_REQUEST = 25
_NAME_CAP, _TAGS_CAP = 128, 256
_CELLS_MIN, _CELLS_MAX = 0.25, 40.0


def _world_for_maps(request: Request, db: Session, active_world):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404, "No world selected")
    user = getattr(request.state, "user", None)
    if not user or not world_can_edit_section(request, world, "maps"):
        raise HTTPException(403)
    return world


def _row(p: MapProp) -> dict:
    return {"id": p.id, "name": p.name, "tags": p.tags or "", "url": p.file_url, "kind": p.kind,
            "px_w": p.px_w, "px_h": p.px_h, "cells_w": p.cells_w, "cells_h": p.cells_h}


def _cells(value, fallback):
    """A footprint in squares from untrusted input: finite, within bounds, quarter-square steps."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if v != v or v in (float("inf"), float("-inf")):
        return fallback
    return min(_CELLS_MAX, max(_CELLS_MIN, round(v * 4) / 4))


def _clean_name(value, fallback="Object") -> str:
    name = " ".join(str(value or "").split())[:_NAME_CAP]
    return name or fallback


def _stem_name(filename: str) -> str:
    return _clean_name(Path(filename or "").stem.replace("_", " ").replace("-", " "), "Object")


def _file_path(url: str):
    """The file behind a stored /uploads/props/... URL, only if it really is inside the props folder."""
    if not isinstance(url, str) or not url.startswith("/uploads/props/"):
        return None
    p = (_UPLOADS / url[len("/uploads/"):]).resolve()
    return p if p.is_relative_to(_PROPS_DIR.resolve()) else None


def delete_prop_file(url: str) -> None:
    p = _file_path(url)
    if p and p.is_file():
        p.unlink()


def _used_in_a_map(db: Session, world_id: int, url: str) -> bool:
    for (raw,) in db.query(Schematic.elements_json).filter(Schematic.world_id == world_id).all():
        if raw and url in raw:
            return True
    return False


@router.get("/api/maps/props")
def props_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = _world_for_maps(request, db, active_world)
    rows = db.query(MapProp).filter(MapProp.world_id == world.id).order_by(MapProp.name, MapProp.id).all()
    return {"props": [_row(p) for p in rows], "limit": MAX_PROPS_PER_WORLD}


@router.post("/api/maps/props/upload")
async def props_upload(request: Request, files: list[UploadFile] = File(...), cells_w: str = Form(""),
                       db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Several pictures at once. Each is processed on its own: the response lists what was added and, for the files that
    could not be used, why - one bad file never loses the others. `cells_w` (optional) is the width in squares for every
    file in this batch; otherwise the footprint is guessed from the picture's size."""
    world = _world_for_maps(request, db, active_world)
    if not files:
        raise HTTPException(400, "Choose at least one picture")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(400, f"At most {MAX_FILES_PER_REQUEST} pictures at a time")
    have = db.query(MapProp).filter(MapProp.world_id == world.id).count()
    added, refused = [], []
    _PROPS_DIR.mkdir(parents=True, exist_ok=True)
    for f in files:
        name = f.filename or "object"
        if have + len(added) >= MAX_PROPS_PER_WORLD:
            refused.append({"file": name, "reason": f"The library is full ({MAX_PROPS_PER_WORLD} objects)"})
            continue
        ext = Path(name).suffix.lower()
        raw = await f.read(prop_images.MAX_RAW_BYTES + 1)
        try:
            out = prop_images.process_prop(raw, ext)
        except prop_images.PropError as e:
            refused.append({"file": name, "reason": str(e)})
            continue
        stored = unique_upload_filename(name, out["ext"])
        (_PROPS_DIR / stored).write_bytes(out["data"])
        cw, ch = prop_images.default_cells(out["width"], out["height"])
        if cells_w.strip():
            want = _cells(cells_w, cw)
            ch = max(_CELLS_MIN, round(want * (out["height"] / out["width"]) * 4) / 4)
            cw = want
        row = MapProp(world_id=world.id, name=_stem_name(name), file_url=f"/uploads/props/{stored}",
                      kind="svg" if out["ext"] == ".svg" else "raster", px_w=int(out["width"]), px_h=int(out["height"]),
                      cells_w=cw, cells_h=min(_CELLS_MAX, ch))
        db.add(row)
        db.flush()
        added.append(_row(row))
    db.commit()
    return {"added": added, "refused": refused}


@router.post("/api/maps/props/{prop_id}/update")
async def props_update(prop_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = _world_for_maps(request, db, active_world)
    p = db.get(MapProp, prop_id)
    if not p or p.world_id != world.id:
        raise HTTPException(404)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    if "name" in body:
        p.name = _clean_name(body.get("name"), p.name)
    if "tags" in body:
        p.tags = " ".join(str(body.get("tags") or "").split())[:_TAGS_CAP]
    if "cells_w" in body:
        p.cells_w = _cells(body.get("cells_w"), p.cells_w)
    if "cells_h" in body:
        p.cells_h = _cells(body.get("cells_h"), p.cells_h)
    db.commit()
    return _row(p)


@router.post("/api/maps/props/{prop_id}/delete")
def props_delete(prop_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Removes the library entry. The file stays while any of this world's maps still uses it, so deleting an object from
    the palette never leaves a hole in a map."""
    world = _world_for_maps(request, db, active_world)
    p = db.get(MapProp, prop_id)
    if not p or p.world_id != world.id:
        raise HTTPException(404)
    url = p.file_url
    db.delete(p)
    db.commit()
    kept = _used_in_a_map(db, world.id, url) or db.query(MapProp).filter(MapProp.file_url == url).count() > 0
    if not kept:
        delete_prop_file(url)
    return {"ok": True, "file_kept": kept}
