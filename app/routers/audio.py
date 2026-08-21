"""The /audio library: a flat per-world list of GM-uploaded audio clips
(ambiance, sound effects, an NPC voice line, a recorded handout) — see
AudioClip in app/models.py. Unlike /images (GM-only end to end), this page
itself is player-safe: a player sees a read-only list of whatever clips the
GM has left visible_to_players=True, matching how a hidden Entity behaves
(the GM has to act to hide something, not to reveal it). Upload/edit/delete
stay GM-only, enforced in each handler rather than via _is_player_safe,
since that allowlist has no way to express "GET is fine, POST isn't" for a
single path — main.py's auth_gate already lets any POST through to whatever
_is_player_safe allows, so the real gate has to live here."""
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import AudioClip
from ..templating import templates
from ..uploads import copy_upload_bounded, unique_upload_filename

router = APIRouter()

_MAX_NAME = 256
_MAX_DESCRIPTION = 512
_MAX_CLIPS_PER_WORLD = 300
# Duplicated locally rather than imported from main.py — main.py imports this
# router, so the reverse would be circular (same rationale as gallery.py's
# own local _UPLOADS_DIR copy).
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ALLOWED_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac", ".opus", ".webm", ".aac"}
# Audio runs longer than a portrait image, so give it more room than the
# generic 20 MB upload default — still bounded so a batch of uploads can't
# fill the /data volume.
_MAX_AUDIO_BYTES = 60 * 1024 * 1024


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


@router.get("/audio", response_class=HTMLResponse)
def audio_library(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    is_gm = _is_gm(request)
    q = db.query(AudioClip).filter(AudioClip.world_id == world.id)
    if not is_gm:
        q = q.filter(AudioClip.visible_to_players.is_(True))
    clips = q.order_by(AudioClip.name).all()
    return templates.TemplateResponse("audio_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips, "can_edit": is_gm,
    })


@router.post("/audio/upload")
async def audio_upload(
    request: Request, file: UploadFile = File(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
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

    target_dir = _UPLOADS_DIR / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_MAX_AUDIO_BYTES)

    clip_name = name.strip()[:_MAX_NAME] or Path(file.filename).stem[:_MAX_NAME] or "Untitled clip"
    clip = AudioClip(
        world_id=world.id, name=clip_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/audio/{dest.name}", visible_to_players=bool(visible_to_players),
    )
    db.add(clip)
    db.commit()
    return RedirectResponse("/audio", status_code=303)


@router.post("/audio/{clip_id}/edit")
async def audio_edit(
    clip_id: int, request: Request, name: str = Form(""), description: str = Form(""),
    visible_to_players: Optional[str] = Form(None),
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
    db.commit()
    return RedirectResponse("/audio", status_code=303)


@router.post("/audio/{clip_id}/delete")
def audio_delete(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    _delete_clip_file(clip)
    db.delete(clip)
    db.commit()
    return RedirectResponse("/audio", status_code=303)
