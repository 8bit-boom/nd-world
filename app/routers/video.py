"""The /video library: a per-world tree of GM-uploaded video clips (a
recorded cutscene, a handout clip, an NPC video message) — see
VideoClip/VideoAlbum in app/models.py. Mirrors app/routers/audio.py's
Audio Library almost exactly (same album-tree/visibility/chunked-upload
shape); the two real differences are poster_url (a best-effort ffmpeg
thumbnail frame) and optional space-saving AV1 conversion on upload (see
_convert_video, World.video_convert_enabled). Player-safe like /audio: a
player sees a read-only view of whatever clips the GM has left
visible_to_players=True. Upload/edit/delete/album-management/settings
stay GM-only, enforced in each handler rather than via _is_player_safe,
since that allowlist can't express "GET is fine, POST isn't" for a
single path."""
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import VideoAlbum, VideoClip
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
_MAX_CLIPS_PER_WORLD = 100  # lower than audio's 300 — bounds worst-case disk to a few hundred GB/world at _MAX_VIDEO_BYTES each
_MAX_ALBUMS_PER_WORLD = 100
_MAX_ALBUM_NAME = 120
# Duplicated locally rather than imported from main.py — main.py imports this
# router, so the reverse would be circular (same rationale as audio.py's own
# local _UPLOADS_DIR copy).
_UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
_ALLOWED_EXTS = {".mp4", ".m4v", ".webm", ".ogv", ".mov"}
# Video runs bigger than audio for the same length — deliberately NOT
# reusing MAX_AUDIO_UPLOAD_BYTES's default so raising one doesn't silently
# raise the other. Defaults to 2 GB; env-overridable like MAX_AUDIO_UPLOAD_BYTES.
_MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))

# Same client-side-split large-file pattern as audio.py/sessions.py/ai.py —
# see app/uploads.py's own module docstring for why (a reverse proxy/CDN's
# fixed per-request body cap, e.g. Cloudflare's free-tier 100MB, independent
# of _MAX_VIDEO_BYTES itself).
_CHUNKS_ROOT = _UPLOADS_DIR / "video" / "_chunks"

_log = logging.getLogger(__name__)

# Fallback average video bitrate (kbps) for the AV1 conversion when a world
# hasn't set World.video_convert_bitrate_kbps — chosen over letting ffmpeg's
# own libsvtav1 default apply, which is tuned for archival quality rather
# than the "make it smaller" goal this feature exists for.
_DEFAULT_VIDEO_BITRATE_KBPS = 2000


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _require_gm(request: Request) -> None:
    if not _is_gm(request):
        raise HTTPException(403)


def _clip_or_404(db: Session, world_id: int, clip_id: int) -> VideoClip:
    clip = db.get(VideoClip, clip_id)
    if not clip or clip.world_id != world_id:
        raise HTTPException(404)
    return clip


def _album_or_404(db: Session, world_id: int, album_id: int) -> VideoAlbum:
    album = db.get(VideoAlbum, album_id)
    if not album or album.world_id != world_id:
        raise HTTPException(404)
    return album


def _breadcrumb(db: Session, album: VideoAlbum) -> list:
    """Root-to-current chain of parent albums (not including `album`
    itself). Capped at 50 hops as cheap insurance against a corrupted
    parent_id chain — normal nesting never gets remotely this deep since
    _MAX_ALBUMS_PER_WORLD bounds the whole tree per world anyway."""
    chain = []
    current = album
    for _ in range(50):
        if not current.parent_id:
            break
        parent = db.get(VideoAlbum, current.parent_id)
        if not parent:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def _descendant_albums(db: Session, root_id: int) -> list:
    """Every VideoAlbum nested (at any depth) under root_id, for cascade
    delete — deleting a folder removes its sub-albums (and their clips)
    with it."""
    result = []
    frontier = [root_id]
    while frontier:
        children = db.query(VideoAlbum).filter(VideoAlbum.parent_id.in_(frontier)).all()
        if not children:
            break
        result.extend(children)
        frontier = [c.id for c in children]
    return result


def _delete_clip_file(clip: VideoClip) -> None:
    root = _UPLOADS_DIR.resolve()
    for url in (clip.file_url, clip.poster_url):
        if not url or not url.startswith("/uploads/"):
            continue
        try:
            path = (root / url[len("/uploads/"):]).resolve()
        except (OSError, RuntimeError):
            continue
        if path.is_relative_to(root) and path.is_file():
            path.unlink()


def _visible_clips_query(db: Session, request: Request, world_id: int, album_id):
    q = db.query(VideoClip).filter(VideoClip.world_id == world_id, VideoClip.album_id == album_id)
    if not _is_gm(request):
        q = q.filter(VideoClip.visible_to_players.is_(True))
    return q.order_by(VideoClip.name)


def _sub_album_counts(db: Session, album_ids: list) -> dict:
    return {aid: db.query(VideoAlbum).filter(VideoAlbum.parent_id == aid).count() for aid in album_ids}


def _clip_counts(db: Session, request: Request, album_ids: list) -> dict:
    """Clip count per album, respecting the viewer's own visibility — a
    player never sees a count that includes clips they can't play."""
    result = {}
    for aid in album_ids:
        q = db.query(VideoClip).filter(VideoClip.album_id == aid)
        if not _is_gm(request):
            q = q.filter(VideoClip.visible_to_players.is_(True))
        result[aid] = q.count()
    return result


async def _convert_video(src: Path, dest_dir: Path, max_height: Optional[int], bitrate_kbps: Optional[int]) -> Optional[Path]:
    """Best-effort AV1 re-encode of `src` for space savings, written into
    dest_dir as a new "<stem>-av1.webm" file. Returns the new file's Path
    on success — the caller deletes `src` and stores the new file's URL
    instead — or None on any failure (ffmpeg missing, this ffmpeg build
    wasn't compiled with libsvtav1, a crash, a truncated/empty result), in
    which case the caller keeps `src` completely untouched and the clip
    is stored uncompressed exactly as before this feature existed. Never
    raises — same graceful-degradation contract as _generate_poster/
    app.ai's ffmpeg-optional audio chunking: a conversion is always a
    bonus, never a required step, so a slow/missing/older ffmpeg must
    never turn into a failed upload.

    max_height (World.video_convert_max_height), if set, downscales the
    clip so its height never exceeds it — a no-op (via ffmpeg's own
    min(ih,H) clamp) for a clip already shorter than that. bitrate_kbps
    (World.video_convert_bitrate_kbps) sets the target average video
    bitrate; falls back to _DEFAULT_VIDEO_BITRATE_KBPS when unset."""
    import asyncio
    out_path = dest_dir / f"{src.stem}-av1.webm"
    bitrate = bitrate_kbps if bitrate_kbps and bitrate_kbps > 0 else _DEFAULT_VIDEO_BITRATE_KBPS
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c:v", "libsvtav1", "-b:v", f"{int(bitrate)}k"]
    if max_height and max_height > 0:
        # The comma inside min(...) has to be escaped for ffmpeg's own
        # filtergraph parser (which otherwise reads it as a filter
        # separator) — this is NOT shell quoting, argv is passed directly
        # via create_subprocess_exec with no shell involved.
        cmd += ["-vf", f"scale=-2:min(ih\\,{int(max_height)})"]
    cmd += ["-c:a", "libopus", "-b:a", "128k", str(out_path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            _log.warning("video AV1 conversion failed (rc=%s): %s", proc.returncode, stderr.decode(errors="replace")[:500])
            out_path.unlink(missing_ok=True)
            return None
        return out_path
    except Exception as exc:
        _log.warning("video AV1 conversion errored: %s: %s", type(exc).__name__, exc)
        out_path.unlink(missing_ok=True)
        return None


async def _finish_stored_file(dest: Path, target_dir, world) -> Path:
    """Shared tail of both upload routes, run once the raw file is already
    saved at `dest`: converts it to AV1 first (if the world has opted in —
    see _convert_video), THEN generates the poster from whichever file
    ends up being kept, so a poster is never generated from a file that's
    about to be deleted. Returns the final Path to store as the clip's
    file_url; `dest` itself may already be gone if conversion succeeded."""
    final_path = dest
    if world and world.video_convert_enabled:
        converted = await _convert_video(dest, target_dir, world.video_convert_max_height, world.video_convert_bitrate_kbps)
        if converted:
            dest.unlink(missing_ok=True)
            final_path = converted
    return final_path


async def _generate_poster(video_path: Path, dest_dir: Path) -> Optional[str]:
    """Best-effort ffmpeg thumbnail from ~1s into the clip, written next to
    the video as "<stem>.jpg". Returns its /uploads/... URL, or None if
    ffmpeg is missing, the clip is shorter than 1s, or anything else goes
    wrong — the caller just leaves VideoClip.poster_url unset in that case,
    same graceful-degradation shape as app.ai's ffmpeg-optional audio
    chunking (see _split_audio_into_chunks there): a missing poster only
    means the <video> element falls back to its own native preview frame,
    never a failed upload."""
    import asyncio
    poster_path = dest_dir / f"{video_path.stem}.jpg"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", "1", "-i", str(video_path), "-frames:v", "1", "-q:v", "4", str(poster_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0 or not poster_path.is_file():
            poster_path.unlink(missing_ok=True)
            return None
        return f"/uploads/video/{poster_path.name}"
    except Exception:
        poster_path.unlink(missing_ok=True)
        return None


@router.get("/video", response_class=HTMLResponse)
def video_library(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    is_gm = _is_gm(request)
    albums = (
        db.query(VideoAlbum)
        .filter(VideoAlbum.world_id == world.id, VideoAlbum.parent_id.is_(None))
        .order_by(VideoAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, None).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("video_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips, "can_edit": is_gm,
        "album": None, "albums": albums, "breadcrumb": [],
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_video_mb": _MAX_VIDEO_BYTES // (1024 * 1024),
    })


@router.get("/video/albums/{album_id}", response_class=HTMLResponse)
def video_album_detail(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    is_gm = _is_gm(request)
    albums = (
        db.query(VideoAlbum).filter(VideoAlbum.parent_id == album.id).order_by(VideoAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, album.id).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("video_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips, "can_edit": is_gm,
        "album": album, "albums": albums, "breadcrumb": _breadcrumb(db, album),
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_video_mb": _MAX_VIDEO_BYTES // (1024 * 1024),
    })


@router.post("/video/settings")
async def video_settings_save(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Saves the world's space-saving AV1 conversion preferences (codec
    is fixed — see _convert_video's docstring for why no picker for it —
    only whether conversion runs at all, the resolution cap, and the
    target bitrate are configurable). Applies to every upload from this
    point on; existing clips are never retroactively converted."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    form = await request.form()
    world.video_convert_enabled = bool(form.get("video_convert_enabled"))
    max_height_raw = str(form.get("video_convert_max_height", "")).strip()
    world.video_convert_max_height = int(max_height_raw) if max_height_raw.isdigit() and int(max_height_raw) > 0 else None
    bitrate_raw = str(form.get("video_convert_bitrate_kbps", "")).strip()
    world.video_convert_bitrate_kbps = int(bitrate_raw) if bitrate_raw.isdigit() and int(bitrate_raw) > 0 else None
    db.commit()
    return RedirectResponse("/video", status_code=303)


@router.post("/video/albums/new")
async def video_album_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_ALBUM_NAME] or "Untitled Album"
    count = db.query(VideoAlbum).filter(VideoAlbum.world_id == world.id).count()
    if count >= _MAX_ALBUMS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_ALBUMS_PER_WORLD} albums.")
    parent_id_raw = str(form.get("parent_id", "")).strip()
    parent_id = None
    if parent_id_raw.isdigit():
        parent_id = _album_or_404(db, world.id, int(parent_id_raw)).id
    album = VideoAlbum(world_id=world.id, name=name, parent_id=parent_id)
    db.add(album)
    db.commit()
    db.refresh(album)
    return RedirectResponse(f"/video/albums/{album.id}", status_code=303)


@router.post("/video/albums/{album_id}/rename")
async def video_album_rename(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
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
    return RedirectResponse(f"/video/albums/{album_id}", status_code=303)


@router.post("/video/albums/{album_id}/delete")
def video_album_delete(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    descendants = _descendant_albums(db, album.id)
    all_album_ids = [album.id] + [d.id for d in descendants]

    # Each clip is an owned file (not a shared URL), so a deleted folder
    # takes its clips — and their files — with it.
    clips = db.query(VideoClip).filter(VideoClip.album_id.in_(all_album_ids)).all()
    for clip in clips:
        _delete_clip_file(clip)
        db.delete(clip)
    for descendant in descendants:
        db.delete(descendant)
    dest = f"/video/albums/{album.parent_id}" if album.parent_id else "/video"
    db.delete(album)
    db.commit()
    return RedirectResponse(dest, status_code=303)


@router.post("/video/upload")
async def video_upload(
    request: Request, file: UploadFile = File(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
    album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if db.query(VideoClip).filter(VideoClip.world_id == world.id).count() >= _MAX_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_CLIPS_PER_WORLD} video clips.")
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")

    target_album_id = None
    album_id = (album_id or "").strip()
    if album_id.isdigit():
        target_album_id = _album_or_404(db, world.id, int(album_id)).id

    target_dir = _UPLOADS_DIR / "video"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_MAX_VIDEO_BYTES)
    final_path = await _finish_stored_file(dest, target_dir, world)
    poster_url = await _generate_poster(final_path, target_dir)

    clip_name = name.strip()[:_MAX_NAME] or Path(file.filename).stem[:_MAX_NAME] or "Untitled clip"
    clip = VideoClip(
        world_id=world.id, name=clip_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/video/{final_path.name}", poster_url=poster_url,
        visible_to_players=bool(visible_to_players), album_id=target_album_id,
    )
    db.add(clip)
    db.commit()
    return RedirectResponse(f"/video/albums/{target_album_id}" if target_album_id else "/video", status_code=303)


@router.post("/video/upload/chunk")
async def video_upload_chunk(
    request: Request, file: UploadFile = File(...),
    upload_id: str = Form(...), chunk_index: int = Form(...),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Receive one part of a large video file; /video/upload/complete
    reassembles all parts once every one has arrived — see app/uploads.py's
    save_upload_chunk/reassemble_upload_chunks."""
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    save_upload_chunk(_CHUNKS_ROOT, upload_id, chunk_index, file, max_bytes=_MAX_VIDEO_BYTES)
    return JSONResponse({"ok": True})


@router.post("/video/upload/complete")
async def video_upload_complete(
    request: Request, upload_id: str = Form(...), filename: str = Form(...),
    total_chunks: int = Form(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
    album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Reassemble the parts uploaded via /video/upload/chunk into one file
    and create the VideoClip — same validation and result shape as the
    single-request /video/upload, just fed from disk instead of the
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
    if db.query(VideoClip).filter(VideoClip.world_id == world.id).count() >= _MAX_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_CLIPS_PER_WORLD} video clips.")

    target_album_id = None
    album_id = (album_id or "").strip()
    if album_id.isdigit():
        target_album_id = _album_or_404(db, world.id, int(album_id)).id

    target_dir = _UPLOADS_DIR / "video"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(filename, ext)
    reassemble_upload_chunks(_CHUNKS_ROOT, upload_id, total_chunks, dest, max_bytes=_MAX_VIDEO_BYTES)
    final_path = await _finish_stored_file(dest, target_dir, world)
    poster_url = await _generate_poster(final_path, target_dir)

    clip_name = name.strip()[:_MAX_NAME] or Path(filename).stem[:_MAX_NAME] or "Untitled clip"
    clip = VideoClip(
        world_id=world.id, name=clip_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/video/{final_path.name}", poster_url=poster_url,
        visible_to_players=bool(visible_to_players), album_id=target_album_id,
    )
    db.add(clip)
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/video/{clip_id}/edit")
async def video_edit(
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
    dest = f"/video/albums/{clip.album_id}" if clip.album_id else "/video"
    return RedirectResponse(dest, status_code=303)


@router.post("/video/{clip_id}/delete")
def video_delete(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_gm(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    dest = f"/video/albums/{clip.album_id}" if clip.album_id else "/video"
    _delete_clip_file(clip)
    db.delete(clip)
    db.commit()
    return RedirectResponse(dest, status_code=303)
