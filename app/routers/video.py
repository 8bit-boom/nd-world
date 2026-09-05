"""The /video library: a per-world tree of GM-uploaded video clips (a
recorded cutscene, a handout clip, an NPC video message) — see
VideoClip/VideoAlbum in app/models.py. Mirrors app/routers/audio.py's
Audio Library almost exactly (same album-tree/visibility/chunked-upload
shape); the two real differences are poster_url (a best-effort ffmpeg
thumbnail frame) and the upload-time conversion ladder: optional
space-saving AV1 for any container (see _convert_video,
World.video_convert_enabled) plus an unconditional remux/transcode into a
browser-playable MP4 for containers browsers can't play natively, like
MKV/AVI (see _remux_or_transcode). Player-safe like /audio: a
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

from .. import ai as _ai_module
from .. import audio_jobs as _audio_jobs
from ..database import get_app_settings, get_db
from ..deps import get_world_ctx, can_edit_content
from ..models import VideoAlbum, VideoClip
from ..templating import templates
from ..uploads import (
    copy_upload_bounded,
    effective_upload_bytes,
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
# Extensions stored exactly as uploaded: every mainstream browser plays
# these containers natively, so they only ever face the optional AV1 pass
# (a world's space-saving choice), never a mandatory conversion.
_NATIVE_EXTS = {".mp4", ".m4v", ".webm", ".ogv", ".mov"}
# Extensions accepted but never stored as-is: ffmpeg reads every one of
# these containers natively on the server, yet browsers mostly can't play
# them (MKV/AVI/MPEG-PS are desktop/legacy formats, MPEG-TS/FLV are
# streaming/broadcast containers) — so the upload pipeline converts them
# to a native container before storing, via _remux_or_transcode's
# remux-then-transcode ladder. _convert_video's AV1 pass works on these
# too, since ffmpeg's read side is container-agnostic either way.
_CONVERTIBLE_EXTS = {".mkv", ".avi", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}
# What the upload routes' ext check admits — the rejection message below
# lists this union, so it already covers both classes without its own
# special-casing.
_ALLOWED_EXTS = _NATIVE_EXTS | _CONVERTIBLE_EXTS
# Video runs bigger than audio for the same length — deliberately NOT
# reusing MAX_AUDIO_UPLOAD_BYTES's default so raising one doesn't silently
# raise the other. Defaults to 2 GB; env-overridable like MAX_AUDIO_UPLOAD_BYTES.
_MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))


def _effective_video_bytes(db: Session) -> int:
    """This request's video upload cap: the GM's saved AppSettings.max_video_mb
    (Settings > System's "Upload limits" — applies to new uploads immediately,
    no restart) or the _MAX_VIDEO_BYTES env default when left blank. Computed
    per request rather than at import so a settings save takes effect without
    a process restart; see effective_upload_bytes (app/uploads.py)."""
    settings = get_app_settings(db)
    return effective_upload_bytes(getattr(settings, "max_video_mb", None), _MAX_VIDEO_BYTES)

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


def _require_can_edit(request: Request) -> None:
    """The write-side gate for clip/album content: a GM, or a GM-Assistant
    (WorldMembership.role == "assistant") — same tier the auth_gate's
    _is_assistant_safe already enforced on the way in; this re-check keeps
    each handler safe on its own. _require_gm above stays for the one
    genuinely administrative route in this router (POST /video/settings —
    the world's AV1 upload-policy preferences). Deliberately NOT used by the
    visibility filters below (_visible_clips_query/_clip_counts): an
    assistant SEES what a player sees, per the role's whole premise."""
    if not can_edit_content(request):
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


def _resolve_upload_path(url: Optional[str]) -> Optional[Path]:
    """Resolves a "/uploads/..." URL back to its real path under
    _UPLOADS_DIR, with the same traversal check _delete_clip_file always
    applied inline — factored out so audio_transcribe below (which reads
    clip.file_url rather than deleting it) doesn't duplicate it. None if
    the URL is missing/malformed or doesn't resolve to a file that
    actually exists."""
    if not url or not url.startswith("/uploads/"):
        return None
    root = _UPLOADS_DIR.resolve()
    try:
        path = (root / url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def _delete_clip_file(clip: VideoClip) -> None:
    for url in (clip.file_url, clip.poster_url):
        path = _resolve_upload_path(url)
        if path:
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


def _remux_command(src: Path, out: Path, hevc: bool) -> list:
    """The lossless remux argv for _remux_or_transcode's first rung,
    factored out pure so tests can pin the exact flags without spawning
    ffmpeg. -map v:0+a:0 deliberately maps ONLY the first video and audio
    stream — subtitle streams are excluded on purpose: MP4 cannot hold
    PGS bitmap subs, and mov_text-converting them would fail the whole
    remux, so dropping them matches the documented manual re-container
    workflow this automates. hevc adds -tag:v hvc1 because Apple/QuickTime
    refuse HEVC-in-MP4 unless it carries that tag — tagging an H.264
    stream hvc1 would be wrong, so the caller only passes hevc=True when
    the ffprobe probe identified the source video codec as hevc."""
    cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
    if hevc:
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


async def _remux_or_transcode(src: Path) -> Optional[Path]:
    """Best-effort conversion of a non-native container (MKV/AVI/TS/...)
    into a browser-playable MP4 at src.with_suffix(".mp4") — the caller's
    dest stem is already unique, so no extra collision handling is needed.
    Unlike _convert_video (a space-saving bonus a world opts into), this
    ladder is what makes a convertible-container upload playable at all —
    but it keeps the identical never-raises graceful-degradation contract:
    returns the new Path on success (the caller deletes `src` and stores
    the new name) or None on ANY failure (ffmpeg/ffprobe missing, a crash,
    a truncated/empty result), in which case the caller keeps `src`
    untouched and the clip is stored unconverted.

    Two-rung ladder — ffmpeg reads every input container natively, so no
    format-specific handling is needed on the read side:
    1. Remux (-c copy): lossless and near-instant; most MKVs carry
       HEVC/H.264 video plus AAC/AC3 audio, which an MP4 muxer takes
       as-is. The video codec is probed first (ffprobe) so an HEVC stream
       gets the hvc1 tag Apple requires (see _remux_command); if ffprobe
       is missing or fails the remux simply runs without the tag.
    2. Transcode: if the remux failed, some stream isn't MP4-muxable
       (theora video, flv1, ...) — re-encode to the most universally
       playable H.264/AAC pairing instead."""
    import asyncio
    out_path = src.with_suffix(".mp4")
    try:
        # Probe (best-effort): ask ffprobe for the first video stream's
        # codec name; anything other than a clean "hevc" answer leaves the
        # hvc1 tag off, which is always safe for H.264 and every other codec.
        hevc = False
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        probe_stdout, _ = await probe.communicate()
        if probe.returncode == 0 and probe_stdout.decode(errors="replace").strip().lower() == "hevc":
            hevc = True
        # Rung 1: lossless remux into MP4.
        remux = await asyncio.create_subprocess_exec(
            *_remux_command(src, out_path, hevc),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await remux.communicate()
        if remux.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        _log.warning("video remux failed (rc=%s), falling back to transcode: %s",
                     remux.returncode, stderr.decode(errors="replace")[:500])
        out_path.unlink(missing_ok=True)
        # Rung 2: full transcode to max-compatibility H.264/AAC.
        transcode = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await transcode.communicate()
        if transcode.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        _log.warning("video transcode fallback failed (rc=%s): %s",
                     transcode.returncode, stderr.decode(errors="replace")[:500])
        out_path.unlink(missing_ok=True)
        return None
    except Exception as exc:
        _log.warning("video remux/transcode errored: %s: %s", type(exc).__name__, exc)
        out_path.unlink(missing_ok=True)
        return None


def _is_non_native_container(path: Path) -> bool:
    """True when `path`'s suffix is one of the accepted-but-not-playable
    containers (_CONVERTIBLE_EXTS) that _finish_stored_file must convert
    before a browser can play the clip."""
    return path.suffix.lower() in _CONVERTIBLE_EXTS


async def _finish_stored_file(dest: Path, target_dir, world) -> Path:
    """Shared tail of both upload routes, run once the raw file is already
    saved at `dest`: converts it to AV1 first (if the world has opted in —
    see _convert_video; ffmpeg reads every container natively, so this
    works on MKV/AVI uploads exactly as on native ones), and — only for
    non-native containers that the AV1 pass didn't already replace —
    remuxes/transcodes into a browser-playable MP4 (see
    _remux_or_transcode). THEN generates the poster from whichever file
    ends up being kept, so a poster is never generated from a file that's
    about to be deleted. Returns the final Path to store as the clip's
    file_url; `dest` itself may already be gone if a conversion succeeded."""
    final_path = dest
    if world and world.video_convert_enabled:
        converted = await _convert_video(dest, target_dir, world.video_convert_max_height, world.video_convert_bitrate_kbps)
        if converted:
            dest.unlink(missing_ok=True)
            final_path = converted
    if _is_non_native_container(dest) and final_path is dest:
        # The AV1 pass didn't run or didn't succeed, so the file is still a
        # container browsers can't play — unlike a native container, where
        # keeping the original is merely "no space saved", here keeping it
        # means an usually-unplayable clip, so a remux/transcode is worth
        # attempting even for worlds that never opted into AV1.
        remuxed = await _remux_or_transcode(dest)
        if remuxed:
            dest.unlink(missing_ok=True)
            final_path = remuxed
        else:
            # Every rung failed — keep the original file. The clip is
            # stored unconverted (browsers may not play it, but the GM can
            # still download it), same graceful-degradation contract as
            # _convert_video: a conversion is a bonus, never a failed upload.
            _log.warning("video %s upload stored unconverted — no conversion path succeeded", dest.suffix)
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
            # scale=min(iw\,480):-2 caps the poster's width at 480px (a no-op,
            # via ffmpeg's own min(), for anything already narrower) — the
            # Video tab grid only ever displays this at a small card size, so
            # writing it out at full source resolution (often 1080p+) was
            # pure wasted bytes on every page load. -2 keeps height even, as
            # required by JPEG's 4:2:0 chroma subsampling.
            "ffmpeg", "-y", "-ss", "1", "-i", str(video_path), "-frames:v", "1",
            "-vf", "scale=min(iw\\,480):-2", "-q:v", "4", str(poster_path),
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
    albums = (
        db.query(VideoAlbum)
        .filter(VideoAlbum.world_id == world.id, VideoAlbum.parent_id.is_(None))
        .order_by(VideoAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, None).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("video_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips,
        "album": None, "albums": albums, "breadcrumb": [],
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_video_mb": _effective_video_bytes(db) // (1024 * 1024),
    })


@router.get("/video/albums/{album_id}", response_class=HTMLResponse)
def video_album_detail(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    albums = (
        db.query(VideoAlbum).filter(VideoAlbum.parent_id == album.id).order_by(VideoAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, album.id).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("video_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips,
        "album": album, "albums": albums, "breadcrumb": _breadcrumb(db, album),
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_video_mb": _effective_video_bytes(db) // (1024 * 1024),
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
    _require_can_edit(request)
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
    _require_can_edit(request)
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
    _require_can_edit(request)
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
    _require_can_edit(request)
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
    copy_upload_bounded(file, dest, max_bytes=_effective_video_bytes(db))
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
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    save_upload_chunk(_CHUNKS_ROOT, upload_id, chunk_index, file, max_bytes=_effective_video_bytes(db))
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
    _require_can_edit(request)
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
    reassemble_upload_chunks(_CHUNKS_ROOT, upload_id, total_chunks, dest, max_bytes=_effective_video_bytes(db))
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
    _require_can_edit(request)
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


@router.post("/video/{clip_id}/transcribe")
async def video_transcribe(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Generate an AI transcript + WebVTT subtitle track for one clip via
    Whisper (see app.ai.transcribe_audio_with_subtitles — it works directly
    on a video file too, since whisper.cpp shells out to ffmpeg to decode
    the audio track from any container it's given). Mirrors app/routers/
    audio.py's audio_transcribe exactly — same world-level glossary/
    language/denoise settings, same "synchronous, not a background job"
    rationale, same "re-running overwrites" behavior."""
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    path = _resolve_upload_path(clip.file_url)
    if not path:
        raise HTTPException(404, "Clip file not found")
    glossary = _audio_jobs._glossary_for_world(world.id)
    language = _audio_jobs._whisper_language_for_world(world.id)
    denoise = _audio_jobs._denoise_for_world(world.id)
    try:
        transcript, vtt = await _ai_module.transcribe_audio_with_subtitles(
            path, glossary=glossary, language=language, denoise=denoise,
        )
    except _ai_module.WhisperError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not transcript:
        raise HTTPException(400, "Whisper transcribed this clip successfully but found no speech in it.")
    clip.transcript = transcript
    clip.subtitles_vtt = vtt
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/video/{clip_id}/delete")
def video_delete(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    dest = f"/video/albums/{clip.album_id}" if clip.album_id else "/video"
    _delete_clip_file(clip)
    db.delete(clip)
    db.commit()
    return RedirectResponse(dest, status_code=303)
