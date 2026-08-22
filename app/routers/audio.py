"""The /audio library: a per-world tree of GM-uploaded audio clips
(ambiance, sound effects, an NPC voice line, a recorded handout) — see
AudioClip/AudioAlbum in app/models.py. Unlike /images (GM-only end to end),
this page itself is player-safe: a player sees a read-only view of whatever
clips the GM has left visible_to_players=True, matching how a hidden
Entity behaves (the GM has to act to hide something, not to reveal it).
Albums are pure organization (no visibility flag of their own) — a player
can browse the folder tree, they just won't see hidden clips inside it.
Upload/edit/delete/album-management stay GM-only, enforced in each handler
rather than via _is_player_safe, since that allowlist has no way to
express "GET is fine, POST isn't" for a single path — main.py's auth_gate
already lets any POST through to whatever _is_player_safe allows, so the
real gate has to live here."""
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import AudioAlbum, AudioClip
from ..templating import templates
from ..uploads import copy_upload_bounded, unique_upload_filename

router = APIRouter()

_MAX_NAME = 256
_MAX_DESCRIPTION = 512
_MAX_CLIPS_PER_WORLD = 300
_MAX_ALBUMS_PER_WORLD = 100
_MAX_ALBUM_NAME = 120
# Duplicated locally rather than imported from main.py — main.py imports this
# router, so the reverse would be circular (same rationale as gallery.py's
# own local _UPLOADS_DIR copy).
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ALLOWED_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"}
# Audio runs longer than a portrait image, so give it more room than the
# generic 20 MB upload default — still bounded so a batch of uploads can't
# fill the /data volume. Env-overridable like MAX_UPLOAD_BYTES (app/uploads.py)
# since a long ambiance/session-length track can legitimately want more —
# but a reverse proxy or CDN in front of this app (e.g. Cloudflare, whose
# free-tier edge caps request bodies at 100MB with a 413 of its own, before
# the request ever reaches this app) may still reject a large upload before
# this limit is even checked; raising this alone doesn't raise that one.
_MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(200 * 1024 * 1024)))


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _require_gm(request: Request) -> None:
    if not _is_gm(request):
        raise HTTPException(403)


def _clip_or_404(db: Session, world_id: int, clip_id: int) -> AudioClip:
    clip = db.get(AudioClip, clip_id)
    if not clip or clip.world_id != world_id:
        raise HTTPException(404)
    return clip


def _album_or_404(db: Session, world_id: int, album_id: int) -> AudioAlbum:
    album = db.get(AudioAlbum, album_id)
    if not album or album.world_id != world_id:
        raise HTTPException(404)
    return album


def _breadcrumb(db: Session, album: AudioAlbum) -> list:
    """Root-to-current chain of parent albums (not including `album`
    itself). Capped at 50 hops as cheap insurance against a corrupted
    parent_id chain — normal nesting never gets remotely this deep since
    _MAX_ALBUMS_PER_WORLD bounds the whole tree per world anyway."""
    chain = []
    current = album
    for _ in range(50):
        if not current.parent_id:
            break
        parent = db.get(AudioAlbum, current.parent_id)
        if not parent:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def _descendant_albums(db: Session, root_id: int) -> list:
    """Every AudioAlbum nested (at any depth) under root_id, for cascade
    delete — deleting a folder removes its sub-albums (and their clips)
    with it."""
    result = []
    frontier = [root_id]
    while frontier:
        children = db.query(AudioAlbum).filter(AudioAlbum.parent_id.in_(frontier)).all()
        if not children:
            break
        result.extend(children)
        frontier = [c.id for c in children]
    return result


def _delete_clip_file(clip: AudioClip) -> None:
    if not clip.file_url or not clip.file_url.startswith("/uploads/"):
        return
    root = _UPLOADS_DIR.resolve()
    try:
        path = (root / clip.file_url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return
    if path.is_relative_to(root) and path.is_file():
        path.unlink()


def _visible_clips_query(db: Session, request: Request, world_id: int, album_id):
    q = db.query(AudioClip).filter(AudioClip.world_id == world_id, AudioClip.album_id == album_id)
    if not _is_gm(request):
        q = q.filter(AudioClip.visible_to_players.is_(True))
    return q.order_by(AudioClip.name)


def _sub_album_counts(db: Session, album_ids: list) -> dict:
    return {aid: db.query(AudioAlbum).filter(AudioAlbum.parent_id == aid).count() for aid in album_ids}


def _clip_counts(db: Session, request: Request, album_ids: list) -> dict:
    """Clip count per album, respecting the viewer's own visibility — a
    player never sees a count that includes clips they can't play."""
    result = {}
    for aid in album_ids:
        q = db.query(AudioClip).filter(AudioClip.album_id == aid)
        if not _is_gm(request):
            q = q.filter(AudioClip.visible_to_players.is_(True))
        result[aid] = q.count()
    return result


@router.get("/audio", response_class=HTMLResponse)
def audio_library(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    is_gm = _is_gm(request)
    albums = (
        db.query(AudioAlbum)
        .filter(AudioAlbum.world_id == world.id, AudioAlbum.parent_id.is_(None))
        .order_by(AudioAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, None).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("audio_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips, "can_edit": is_gm,
        "album": None, "albums": albums, "breadcrumb": [],
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_audio_mb": _MAX_AUDIO_BYTES // (1024 * 1024),
    })


@router.get("/audio/albums/{album_id}", response_class=HTMLResponse)
def audio_album_detail(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    is_gm = _is_gm(request)
    albums = (
        db.query(AudioAlbum).filter(AudioAlbum.parent_id == album.id).order_by(AudioAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, album.id).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("audio_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips, "can_edit": is_gm,
        "album": album, "albums": albums, "breadcrumb": _breadcrumb(db, album),
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_audio_mb": _MAX_AUDIO_BYTES // (1024 * 1024),
    })


@router.post("/audio/albums/new")
async def audio_album_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_ALBUM_NAME] or "Untitled Album"
    count = db.query(AudioAlbum).filter(AudioAlbum.world_id == world.id).count()
    if count >= _MAX_ALBUMS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_ALBUMS_PER_WORLD} albums.")
    parent_id_raw = str(form.get("parent_id", "")).strip()
    parent_id = None
    if parent_id_raw.isdigit():
        parent_id = _album_or_404(db, world.id, int(parent_id_raw)).id
    album = AudioAlbum(world_id=world.id, name=name, parent_id=parent_id)
    db.add(album)
    db.commit()
    db.refresh(album)
    return RedirectResponse(f"/audio/albums/{album.id}", status_code=303)


@router.post("/audio/albums/{album_id}/rename")
async def audio_album_rename(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
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
    return RedirectResponse(f"/audio/albums/{album_id}", status_code=303)


@router.post("/audio/albums/{album_id}/delete")
def audio_album_delete(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    descendants = _descendant_albums(db, album.id)
    all_album_ids = [album.id] + [d.id for d in descendants]

    # Each clip is an owned file (not a shared URL like ImageAlbum's), so a
    # deleted folder takes its clips — and their files — with it.
    clips = db.query(AudioClip).filter(AudioClip.album_id.in_(all_album_ids)).all()
    for clip in clips:
        _delete_clip_file(clip)
        db.delete(clip)
    for descendant in descendants:
        db.delete(descendant)
    dest = f"/audio/albums/{album.parent_id}" if album.parent_id else "/audio"
    db.delete(album)
    db.commit()
    return RedirectResponse(dest, status_code=303)


@router.post("/audio/upload")
async def audio_upload(
    request: Request, file: UploadFile = File(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
    album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if db.query(AudioClip).filter(AudioClip.world_id == world.id).count() >= _MAX_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_CLIPS_PER_WORLD} audio clips.")
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")

    target_album_id = None
    album_id = (album_id or "").strip()
    if album_id.isdigit():
        target_album_id = _album_or_404(db, world.id, int(album_id)).id

    target_dir = _UPLOADS_DIR / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_MAX_AUDIO_BYTES)

    clip_name = name.strip()[:_MAX_NAME] or Path(file.filename).stem[:_MAX_NAME] or "Untitled clip"
    clip = AudioClip(
        world_id=world.id, name=clip_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/audio/{dest.name}", visible_to_players=bool(visible_to_players),
        album_id=target_album_id,
    )
    db.add(clip)
    db.commit()
    return RedirectResponse(f"/audio/albums/{target_album_id}" if target_album_id else "/audio", status_code=303)


@router.post("/audio/{clip_id}/edit")
async def audio_edit(
    clip_id: int, request: Request, name: str = Form(""), description: str = Form(""),
    visible_to_players: Optional[str] = Form(None), album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    name = name.strip()[:_MAX_NAME]
    if name:
        clip.name = name
    clip.description = description.strip()[:_MAX_DESCRIPTION]
    clip.visible_to_players = bool(visible_to_players)
    album_id = (album_id or "").strip()
    clip.album_id = _album_or_404(db, world.id, int(album_id)).id if album_id.isdigit() else None
    db.commit()
    dest = f"/audio/albums/{clip.album_id}" if clip.album_id else "/audio"
    return RedirectResponse(dest, status_code=303)


@router.post("/audio/{clip_id}/delete")
def audio_delete(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    dest = f"/audio/albums/{clip.album_id}" if clip.album_id else "/audio"
    _delete_clip_file(clip)
    db.delete(clip)
    db.commit()
    return RedirectResponse(dest, status_code=303)
