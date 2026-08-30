"""The /pages library: a per-world tree of GM-uploaded standalone .html
documents (a styled calendar, a lore handout, a reference page with its own
embedded CSS/fonts) — see PageDoc/PageAlbum in app/models.py. Mirrors
app/routers/video.py's album-tree/visibility/chunked-upload shape almost
exactly, minus anything video-specific (no conversion, no poster frame).
Player-safe like /audio and /video: a player sees a read-only view of
whatever pages the GM has left visible_to_players=True. Upload/edit/delete/
album-management stay GM-only, enforced in each handler rather than via
_is_player_safe, since that allowlist can't express "GET is fine, POST
isn't" for a single path.

Security note (the one place this can't just copy video.py): unlike audio/
video, an uploaded .html file can carry its own <script> and is served
same-origin — see main.py's serve_upload for the CSP `sandbox`/
X-Frame-Options headers that isolate it (no access to this app's cookies/
session even if a page's own script runs), and page_viewer.html for the
matching iframe sandbox attribute. Both layers deliberately omit
allow-same-origin."""
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import PageAlbum, PageDoc
from ..templating import templates
from ..uploads import (
    copy_upload_bounded,
    reassemble_upload_chunks,
    save_upload_chunk,
    unique_upload_filename,
)

router = APIRouter()

_MAX_NAME = 256
_MAX_DESCRIPTION = 512
_MAX_DOCS_PER_WORLD = 200
_MAX_ALBUMS_PER_WORLD = 100
_MAX_ALBUM_NAME = 120
# Duplicated locally rather than imported from main.py — main.py imports this
# router, so the reverse would be circular (same rationale as audio.py's/
# video.py's own local _UPLOADS_DIR copy).
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ALLOWED_EXTS = {".html", ".htm"}
# A self-contained HTML page (embedded fonts/images as data URIs) can run
# well past a plain text page but nowhere near audio/video territory —
# deliberately its own cap rather than reusing MAX_UPLOAD_BYTES's 20MB
# default, which is really sized for a single portrait image. Env-tunable
# like the other MAX_*_UPLOAD_BYTES knobs.
_MAX_PAGE_BYTES = int(os.environ.get("MAX_PAGE_UPLOAD_BYTES", str(50 * 1024 * 1024)))

# Same client-side-split large-file pattern as audio.py/video.py/sessions.py —
# see app/uploads.py's own module docstring for why (a reverse proxy/CDN's
# fixed per-request body cap, e.g. Cloudflare's free-tier 100MB, independent
# of _MAX_PAGE_BYTES itself — in practice a page this large would already be
# well past MAX_PAGE_UPLOAD_BYTES's own default, but the pair exists for
# consistency with every other upload surface and a GM who's raised the cap).
_CHUNKS_ROOT = _UPLOADS_DIR / "pages" / "_chunks"

_log = logging.getLogger(__name__)


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _require_gm(request: Request) -> None:
    if not _is_gm(request):
        raise HTTPException(403)


def _doc_or_404(db: Session, world_id: int, doc_id: int) -> PageDoc:
    doc = db.get(PageDoc, doc_id)
    if not doc or doc.world_id != world_id:
        raise HTTPException(404)
    return doc


def _album_or_404(db: Session, world_id: int, album_id: int) -> PageAlbum:
    album = db.get(PageAlbum, album_id)
    if not album or album.world_id != world_id:
        raise HTTPException(404)
    return album


def _breadcrumb(db: Session, album: PageAlbum) -> list:
    """Root-to-current chain of parent albums (not including `album`
    itself). Capped at 50 hops as cheap insurance against a corrupted
    parent_id chain — normal nesting never gets remotely this deep since
    _MAX_ALBUMS_PER_WORLD bounds the whole tree per world anyway."""
    chain = []
    current = album
    for _ in range(50):
        if not current.parent_id:
            break
        parent = db.get(PageAlbum, current.parent_id)
        if not parent:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def _descendant_albums(db: Session, root_id: int) -> list:
    """Every PageAlbum nested (at any depth) under root_id, for cascade
    delete — deleting a folder removes its sub-albums (and their pages)
    with it."""
    result = []
    frontier = [root_id]
    while frontier:
        children = db.query(PageAlbum).filter(PageAlbum.parent_id.in_(frontier)).all()
        if not children:
            break
        result.extend(children)
        frontier = [c.id for c in children]
    return result


def _delete_doc_file(doc: PageDoc) -> None:
    root = _UPLOADS_DIR.resolve()
    if not doc.file_url or not doc.file_url.startswith("/uploads/"):
        return
    try:
        path = (root / doc.file_url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return
    if path.is_relative_to(root) and path.is_file():
        path.unlink()


def _visible_docs_query(db: Session, request: Request, world_id: int, album_id):
    q = db.query(PageDoc).filter(PageDoc.world_id == world_id, PageDoc.album_id == album_id)
    if not _is_gm(request):
        q = q.filter(PageDoc.visible_to_players.is_(True))
    return q.order_by(PageDoc.name)


def _sub_album_counts(db: Session, album_ids: list) -> dict:
    return {aid: db.query(PageAlbum).filter(PageAlbum.parent_id == aid).count() for aid in album_ids}


def _doc_counts(db: Session, request: Request, album_ids: list) -> dict:
    """Page count per album, respecting the viewer's own visibility — a
    player never sees a count that includes pages they can't open."""
    result = {}
    for aid in album_ids:
        q = db.query(PageDoc).filter(PageDoc.album_id == aid)
        if not _is_gm(request):
            q = q.filter(PageDoc.visible_to_players.is_(True))
        result[aid] = q.count()
    return result


@router.get("/pages", response_class=HTMLResponse)
def pages_library(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    is_gm = _is_gm(request)
    albums = (
        db.query(PageAlbum)
        .filter(PageAlbum.world_id == world.id, PageAlbum.parent_id.is_(None))
        .order_by(PageAlbum.name).all()
    )
    docs = _visible_docs_query(db, request, world.id, None).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("pages_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "docs": docs, "can_edit": is_gm,
        "album": None, "albums": albums, "breadcrumb": [],
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "doc_counts": _doc_counts(db, request, album_ids),
        "max_page_mb": _MAX_PAGE_BYTES // (1024 * 1024),
    })


@router.get("/pages/albums/{album_id}", response_class=HTMLResponse)
def pages_album_detail(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    is_gm = _is_gm(request)
    albums = db.query(PageAlbum).filter(PageAlbum.parent_id == album.id).order_by(PageAlbum.name).all()
    docs = _visible_docs_query(db, request, world.id, album.id).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("pages_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "docs": docs, "can_edit": is_gm,
        "album": album, "albums": albums, "breadcrumb": _breadcrumb(db, album),
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "doc_counts": _doc_counts(db, request, album_ids),
        "max_page_mb": _MAX_PAGE_BYTES // (1024 * 1024),
    })


@router.get("/pages/{doc_id}", response_class=HTMLResponse)
def pages_viewer(doc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Full-page sandboxed viewer for one PageDoc — a document meant to be
    read as a whole page (a calendar, a multi-section handout) benefits
    from more room than the list/album view's card layout gives it.
    404s (not 403) for a hidden page a non-GM viewer has no business
    knowing exists, same convention as _entity_view_gate in main.py."""
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    doc = _doc_or_404(db, world.id, doc_id)
    if not doc.visible_to_players and not _is_gm(request):
        raise HTTPException(404)
    return templates.TemplateResponse("page_viewer.html", {
        "request": request, "world": world, "worlds": worlds, "doc": doc,
    })


@router.post("/pages/albums/new")
async def pages_album_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_ALBUM_NAME] or "Untitled Album"
    count = db.query(PageAlbum).filter(PageAlbum.world_id == world.id).count()
    if count >= _MAX_ALBUMS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_ALBUMS_PER_WORLD} albums.")
    parent_id_raw = str(form.get("parent_id", "")).strip()
    parent_id = None
    if parent_id_raw.isdigit():
        parent_id = _album_or_404(db, world.id, int(parent_id_raw)).id
    album = PageAlbum(world_id=world.id, name=name, parent_id=parent_id)
    db.add(album)
    db.commit()
    db.refresh(album)
    return RedirectResponse(f"/pages/albums/{album.id}", status_code=303)


@router.post("/pages/albums/{album_id}/rename")
async def pages_album_rename(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_ALBUM_NAME]
    if name:
        album.name = name
        db.commit()
    return RedirectResponse(f"/pages/albums/{album_id}", status_code=303)


@router.post("/pages/albums/{album_id}/delete")
def pages_album_delete(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    descendants = _descendant_albums(db, album.id)
    all_album_ids = [album.id] + [d.id for d in descendants]

    # Each page is an owned file (not a shared URL), so a deleted folder
    # takes its pages — and their files — with it.
    docs = db.query(PageDoc).filter(PageDoc.album_id.in_(all_album_ids)).all()
    for doc in docs:
        _delete_doc_file(doc)
        db.delete(doc)
    for descendant in descendants:
        db.delete(descendant)
    dest = f"/pages/albums/{album.parent_id}" if album.parent_id else "/pages"
    db.delete(album)
    db.commit()
    return RedirectResponse(dest, status_code=303)


def _resolve_target_album(db: Session, world_id: int, album_id: str) -> Optional[int]:
    album_id = (album_id or "").strip()
    if album_id.isdigit():
        return _album_or_404(db, world_id, int(album_id)).id
    return None


@router.post("/pages/upload")
async def pages_upload(
    request: Request, file: UploadFile = File(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
    album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if db.query(PageDoc).filter(PageDoc.world_id == world.id).count() >= _MAX_DOCS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_DOCS_PER_WORLD} pages.")
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")

    target_album_id = _resolve_target_album(db, world.id, album_id)
    target_dir = _UPLOADS_DIR / "pages"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_MAX_PAGE_BYTES)

    doc_name = name.strip()[:_MAX_NAME] or Path(file.filename).stem[:_MAX_NAME] or "Untitled page"
    doc = PageDoc(
        world_id=world.id, name=doc_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/pages/{dest.name}",
        visible_to_players=bool(visible_to_players), album_id=target_album_id,
    )
    db.add(doc)
    db.commit()
    return RedirectResponse(f"/pages/albums/{target_album_id}" if target_album_id else "/pages", status_code=303)


@router.post("/pages/upload/chunk")
async def pages_upload_chunk(
    request: Request, file: UploadFile = File(...),
    upload_id: str = Form(...), chunk_index: int = Form(...),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Receive one part of a large HTML page; /pages/upload/complete
    reassembles all parts once every one has arrived — see app/uploads.py's
    save_upload_chunk/reassemble_upload_chunks."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    save_upload_chunk(_CHUNKS_ROOT, upload_id, chunk_index, file, max_bytes=_MAX_PAGE_BYTES)
    return JSONResponse({"ok": True})


@router.post("/pages/upload/complete")
async def pages_upload_complete(
    request: Request, upload_id: str = Form(...), filename: str = Form(...),
    total_chunks: int = Form(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
    album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Reassemble the parts uploaded via /pages/upload/chunk into one file
    and create the PageDoc — same validation and result shape as the
    single-request /pages/upload, just fed from disk instead of the
    request body directly."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not filename:
        raise HTTPException(400, "No filename given")
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")
    if db.query(PageDoc).filter(PageDoc.world_id == world.id).count() >= _MAX_DOCS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_DOCS_PER_WORLD} pages.")

    target_album_id = _resolve_target_album(db, world.id, album_id)
    target_dir = _UPLOADS_DIR / "pages"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(filename, ext)
    reassemble_upload_chunks(_CHUNKS_ROOT, upload_id, total_chunks, dest, max_bytes=_MAX_PAGE_BYTES)

    doc_name = name.strip()[:_MAX_NAME] or Path(filename).stem[:_MAX_NAME] or "Untitled page"
    doc = PageDoc(
        world_id=world.id, name=doc_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/pages/{dest.name}",
        visible_to_players=bool(visible_to_players), album_id=target_album_id,
    )
    db.add(doc)
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/pages/{doc_id}/edit")
async def pages_edit(
    doc_id: int, request: Request, name: str = Form(""), description: str = Form(""),
    visible_to_players: Optional[str] = Form(None), album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    doc = _doc_or_404(db, world.id, doc_id)
    name = name.strip()[:_MAX_NAME]
    if name:
        doc.name = name
    doc.description = description.strip()[:_MAX_DESCRIPTION]
    doc.visible_to_players = bool(visible_to_players)
    doc.album_id = _resolve_target_album(db, world.id, album_id)
    db.commit()
    dest = f"/pages/albums/{doc.album_id}" if doc.album_id else "/pages"
    return RedirectResponse(dest, status_code=303)


@router.post("/pages/{doc_id}/delete")
def pages_delete(doc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    doc = _doc_or_404(db, world.id, doc_id)
    dest = f"/pages/albums/{doc.album_id}" if doc.album_id else "/pages"
    _delete_doc_file(doc)
    db.delete(doc)
    db.commit()
    return RedirectResponse(dest, status_code=303)
