"""The /images gallery tab: browse every image already used somewhere in
the active world (via app/gallery.py's discover_world_images) and organize
any of them — or brand-new uploads — into GM-defined named albums
(ImageAlbum in app/models.py). GM-only by default (not in main.py's
_is_player_safe allowlist)."""
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_app_settings, get_db
from ..deps import get_world_ctx
from ..gallery import all_world_image_urls, discover_world_images, image_display_name
from ..imaging import convert_image
from ..models import ImageAlbum, World
from ..templating import templates
from ..uploads import copy_upload_bounded, unique_upload_filename

router = APIRouter()

_MAX_ALBUMS_PER_WORLD = 100
_MAX_ALBUM_NAME = 120
_MAX_IMAGES_PER_ALBUM = 500

# Duplicated locally rather than imported from main.py — main.py imports
# this router, so the reverse would be circular (same rationale as every
# other router's local _UPLOADS_DIR copy, e.g. home_content.py).
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


def _upload_album_image(file: Optional[UploadFile], db: Session) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        return None
    target_dir = _UPLOADS_DIR / "gallery"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest)
    settings = get_app_settings(db)
    dest = convert_image(dest, static_format=settings.static_format, animated_format=settings.animated_format)
    return f"/uploads/gallery/{dest.name}"


def _load_urls(album: ImageAlbum) -> list:
    try:
        urls = json.loads(album.image_urls_json or "[]")
    except (TypeError, ValueError):
        urls = []
    return urls if isinstance(urls, list) else []


def _resolve_upload_path(url: str) -> Optional[Path]:
    """Map an /uploads/... URL back to the file it names, or None if it
    isn't a local upload or would escape UPLOADS_DIR — same containment
    check as importer.py's _resolve_upload_path / main.py's serve_upload."""
    if not isinstance(url, str) or not url.startswith("/uploads/"):
        return None
    root = _UPLOADS_DIR.resolve()
    try:
        path = (root / url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _album_or_404(db: Session, world_id: int, album_id: int) -> ImageAlbum:
    album = db.get(ImageAlbum, album_id)
    if not album or album.world_id != world_id:
        raise HTTPException(404)
    return album


def _breadcrumb(db: Session, album: ImageAlbum) -> list:
    """Root-to-current chain of parent albums (not including `album`
    itself). Capped at 50 hops as cheap insurance against a corrupted
    parent_id chain — normal nesting never gets remotely this deep since
    _MAX_ALBUMS_PER_WORLD bounds the whole tree per world anyway."""
    chain = []
    current = album
    for _ in range(50):
        if not current.parent_id:
            break
        parent = db.get(ImageAlbum, current.parent_id)
        if not parent:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def _descendant_albums(db: Session, root_id: int) -> list:
    """Every ImageAlbum nested (at any depth) under root_id, for cascade
    delete — deleting a folder/album removes its sub-albums with it (the
    images themselves are just URLs and are never deleted)."""
    result = []
    frontier = [root_id]
    while frontier:
        children = db.query(ImageAlbum).filter(ImageAlbum.parent_id.in_(frontier)).all()
        if not children:
            break
        result.extend(children)
        frontier = [c.id for c in children]
    return result


@router.get("/images", response_class=HTMLResponse)
def images_gallery(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    images = discover_world_images(db, world)
    albums = (
        db.query(ImageAlbum)
        .filter(ImageAlbum.world_id == world.id, ImageAlbum.parent_id.is_(None))
        .order_by(ImageAlbum.name).all()
    )
    album_urls = {a.id: _load_urls(a) for a in albums}
    sub_album_counts = {
        a.id: db.query(ImageAlbum).filter(ImageAlbum.parent_id == a.id).count() for a in albums
    }
    return templates.TemplateResponse("gallery_index.html", {
        "request": request, "world": world, "worlds": worlds,
        "images": images, "albums": albums, "album_urls": album_urls,
        "sub_album_counts": sub_album_counts,
    })


@router.post("/images/delete")
async def image_delete(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Permanently delete an image file. Only allowed while the image is
    unused (no entity/PC references it) — the common case for something
    sitting in an album that was uploaded but never attached to anything,
    which is otherwise unreachable to clean up (the album's ✕ only unlinks
    it from the album, it doesn't remove the file). An image still in use
    must be detached from whatever references it first, so deleting here
    can never leave a dangling <img> in an entity's body/notes."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise HTTPException(400, "Missing url")

    discovered = {e["url"]: e for e in discover_world_images(db, world)}
    entry = discovered.get(url)
    if entry and entry["uses"]:
        places = ", ".join(u["label"] for u in entry["uses"][:3])
        if len(entry["uses"]) > 3:
            places += f", and {len(entry['uses']) - 3} more"
        raise HTTPException(400, f"Still used in {len(entry['uses'])} place(s) ({places}) — remove it there first.")

    # Authorization boundary: only an image already reachable from this
    # world (in one of its albums, since discover_world_images found
    # nothing above) may be deleted — a GM can't blindly delete an
    # arbitrary uploaded file by URL.
    albums = db.query(ImageAlbum).filter(ImageAlbum.world_id == world.id).all()
    found_in_album = False
    for album in albums:
        urls = _load_urls(album)
        if url in urls:
            found_in_album = True
            album.image_urls_json = json.dumps([u for u in urls if u != url])
    if not found_in_album and not entry:
        raise HTTPException(404, "Image not found")
    db.commit()

    path = _resolve_upload_path(url)
    if path:
        path.unlink()

    return {"ok": True}


@router.post("/images/spotlight")
async def image_spotlight_send(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Push an image to every player's screen as a full-screen popup — see
    GET /api/spotlight (main.py) for the player-side poller that picks this
    up, and app/templates/base.html for where it's shown (reuses the
    existing lightbox). GM-only (not in main.py's _is_player_safe)."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise HTTPException(400, "Missing url")

    # Same authorization boundary as image_delete: only an image already
    # reachable from this world may be broadcast to its players.
    entries = {e["url"]: e["name"] for e in all_world_image_urls(db, world)}
    if url not in entries:
        raise HTTPException(404, "Image not found")

    world.spotlight_image_url = url
    world.spotlight_label = entries[url][:256]
    world.spotlight_version = (world.spotlight_version or 0) + 1
    db.commit()
    return {"ok": True}


@router.post("/images/spotlight/clear")
def image_spotlight_clear(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    world.spotlight_image_url = None
    world.spotlight_label = None
    world.spotlight_version = (world.spotlight_version or 0) + 1
    db.commit()
    return {"ok": True}


@router.post("/images/albums/new")
async def album_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_ALBUM_NAME] or "Untitled Album"
    count = db.query(ImageAlbum).filter(ImageAlbum.world_id == world.id).count()
    if count >= _MAX_ALBUMS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_ALBUMS_PER_WORLD} albums.")
    parent_id_raw = str(form.get("parent_id", "")).strip()
    parent_id = None
    if parent_id_raw.isdigit():
        parent_id = _album_or_404(db, world.id, int(parent_id_raw)).id
    album = ImageAlbum(world_id=world.id, name=name, image_urls_json="[]", parent_id=parent_id)
    db.add(album)
    db.commit()
    db.refresh(album)
    return RedirectResponse(f"/images/albums/{album.id}", status_code=303)


@router.get("/images/albums/{album_id}", response_class=HTMLResponse)
def album_detail(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    urls = _load_urls(album)
    # A name for each image: the label of wherever it's already used (an
    # entity/PC name — what the GM actually thinks of it as), falling back
    # to its filename for an image that only lives in this album so far.
    discovered_names = {e["url"]: e["name"] for e in discover_world_images(db, world)}
    image_names = {u: discovered_names.get(u, image_display_name(u)) for u in urls}
    child_albums = (
        db.query(ImageAlbum).filter(ImageAlbum.parent_id == album.id).order_by(ImageAlbum.name).all()
    )
    child_album_urls = {a.id: _load_urls(a) for a in child_albums}
    return templates.TemplateResponse("gallery_album.html", {
        "request": request, "world": world, "worlds": worlds,
        "album": album, "image_urls": urls, "image_names": image_names,
        "breadcrumb": _breadcrumb(db, album),
        "child_albums": child_albums, "child_album_urls": child_album_urls,
    })


@router.post("/images/albums/{album_id}/rename")
async def album_rename(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_ALBUM_NAME]
    if name:
        album.name = name
        db.commit()
    return RedirectResponse(f"/images/albums/{album_id}", status_code=303)


@router.post("/images/albums/{album_id}/delete")
def album_delete(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    # Deleting a folder/album removes its sub-albums with it — they have no
    # meaning without their parent. The images themselves are just URLs
    # (see ImageAlbum's docstring) and are never touched.
    for descendant in _descendant_albums(db, album.id):
        db.delete(descendant)
    db.delete(album)
    db.commit()
    return RedirectResponse("/images", status_code=303)


@router.post("/images/albums/{album_id}/add")
async def album_add_images(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise HTTPException(400, "Invalid urls")
    current = _load_urls(album)
    for u in urls:
        if isinstance(u, str) and u and u not in current and len(current) < _MAX_IMAGES_PER_ALBUM:
            current.append(u)
    album.image_urls_json = json.dumps(current)
    db.commit()
    return {"ok": True, "count": len(current)}


@router.post("/images/albums/{album_id}/remove")
async def album_remove_image(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    url = payload.get("url")
    current = [u for u in _load_urls(album) if u != url]
    album.image_urls_json = json.dumps(current)
    db.commit()
    return {"ok": True, "count": len(current)}


@router.post("/images/albums/{album_id}/upload")
async def album_upload_image(
    album_id: int, request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    url = _upload_album_image(file, db)
    if not url:
        raise HTTPException(400, "Unsupported file type")
    current = _load_urls(album)
    if url not in current and len(current) < _MAX_IMAGES_PER_ALBUM:
        current.append(url)
    album.image_urls_json = json.dumps(current)
    db.commit()
    return RedirectResponse(f"/images/albums/{album_id}", status_code=303)
