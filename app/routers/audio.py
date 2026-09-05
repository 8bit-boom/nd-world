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
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from .. import audio_jobs as _audio_jobs
from ..database import get_app_settings, get_db
from ..deps import get_world_ctx, can_edit_content
from ..models import AudioAlbum, AudioClip
from ..templating import templates
from ..uploads import copy_upload_bounded, effective_upload_bytes, unique_upload_filename

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
# fill the /data volume. Defaults generously (1 GB) since a full multi-hour
# session recording or ambiance track can legitimately be this big; the
# chunked upload pair below already splits anything over Cloudflare's fixed
# 100MB free-tier request-body cap client-side, so raising this actually
# takes effect end to end instead of being capped by that proxy limit first.
# Env-overridable like MAX_UPLOAD_BYTES (app/uploads.py).
_MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(1024 * 1024 * 1024)))


def _effective_audio_bytes(db: Session) -> int:
    """This request's audio upload cap: the GM's saved AppSettings.max_audio_mb
    (Settings > System's "Upload limits" — applies to new uploads immediately,
    no restart) or the _MAX_AUDIO_BYTES env default when left blank. Computed
    per request rather than at import so a settings save takes effect without
    a process restart; see effective_upload_bytes (app/uploads.py)."""
    settings = get_app_settings(db)
    return effective_upload_bytes(getattr(settings, "max_audio_mb", None), _MAX_AUDIO_BYTES)

# A big audio file (a whole session recording, a long ambiance loop) can
# still be blocked by a reverse proxy/CDN's own per-request body cap even
# once _MAX_AUDIO_BYTES itself is raised — Cloudflare's free tier is a fixed
# 100 MB with no way to raise it (see docs/DEPLOYMENT.md's "Upload size
# limit" section). The browser-side upload (static/js's audioUploadChunked
# in audio_library.html) works around that by splitting a large file into
# sub-100MB parts and sending each as its own request; these two routes
# receive those parts and reassemble them server-side, so the result is one
# ordinary AudioClip identical to what a small direct upload would create.
_CHUNK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_CHUNK_INDEX = 2000  # generous upper bound on parts per upload — catches a runaway/misbehaving client, not a real ceiling on file size
_STALE_CHUNK_SESSION_SECONDS = 24 * 60 * 60  # sweep an abandoned session (browser closed mid-upload) next time a new one starts, rather than running a background job for this


def _chunks_root() -> Path:
    return _UPLOADS_DIR / "audio" / "_chunks"


def _chunk_session_dir(upload_id: str) -> Path:
    return _chunks_root() / upload_id


def _sweep_stale_chunk_sessions() -> None:
    root = _chunks_root()
    if not root.is_dir():
        return
    cutoff = time.time() - _STALE_CHUNK_SESSION_SECONDS
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _require_can_edit(request: Request) -> None:
    """The write-side gate for clip/album content: a GM, or a GM-Assistant
    (WorldMembership.role == "assistant") — same tier the auth_gate's
    _is_assistant_safe already enforced on the way in; this re-check keeps
    each handler safe on its own (every route in this router is content
    write-side, so this replaces the old GM-only gate wholesale). Deliberately
    NOT used by the visibility filters below (_visible_clips_query/
    _clip_counts): an assistant SEES what a player sees, per the role's whole
    premise."""
    if not can_edit_content(request):
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


def _clip_abs_path(clip: AudioClip) -> Optional[Path]:
    """Resolves clip.file_url back to its real path under _UPLOADS_DIR, the
    same safe-resolve _delete_clip_file already does — factored out so
    audio_transcribe below (which reads the file rather than deleting it)
    doesn't duplicate the traversal check. None if the URL is missing/
    malformed or doesn't resolve to a file that actually exists."""
    if not clip.file_url or not clip.file_url.startswith("/uploads/"):
        return None
    root = _UPLOADS_DIR.resolve()
    try:
        path = (root / clip.file_url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def _delete_clip_file(clip: AudioClip) -> None:
    path = _clip_abs_path(clip)
    if path:
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


@router.get("/api/audio/clips")
def api_audio_clips(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Flat JSON list of every clip in the active world, across all
    albums — powers the Session page's "choose from Audio Library" picker
    (app/routers/sessions.py's from-clip audio-job route), which needs to
    search/select from everything at once rather than browse album by
    album the way the /audio page itself does. GM-only (not in
    _is_player_safe, unlike /audio itself) since this is Session-page
    tooling, not the read-only player-facing soundboard."""
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clips = (
        db.query(AudioClip).filter(AudioClip.world_id == world.id)
        .order_by(AudioClip.name).all()
    )
    return [{"id": c.id, "name": c.name, "description": c.description or "", "album_id": c.album_id} for c in clips]


@router.get("/audio", response_class=HTMLResponse)
def audio_library(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    albums = (
        db.query(AudioAlbum)
        .filter(AudioAlbum.world_id == world.id, AudioAlbum.parent_id.is_(None))
        .order_by(AudioAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, None).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("audio_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips,
        "album": None, "albums": albums, "breadcrumb": [],
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_audio_mb": _effective_audio_bytes(db) // (1024 * 1024),
    })


@router.get("/audio/albums/{album_id}", response_class=HTMLResponse)
def audio_album_detail(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    album = _album_or_404(db, world.id, album_id)
    albums = (
        db.query(AudioAlbum).filter(AudioAlbum.parent_id == album.id).order_by(AudioAlbum.name).all()
    )
    clips = _visible_clips_query(db, request, world.id, album.id).all()
    album_ids = [a.id for a in albums]
    return templates.TemplateResponse("audio_library.html", {
        "request": request, "world": world, "worlds": worlds,
        "clips": clips,
        "album": album, "albums": albums, "breadcrumb": _breadcrumb(db, album),
        "sub_album_counts": _sub_album_counts(db, album_ids),
        "clip_counts": _clip_counts(db, request, album_ids),
        "max_audio_mb": _effective_audio_bytes(db) // (1024 * 1024),
    })


@router.post("/audio/albums/new")
async def audio_album_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_can_edit(request)
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
    return RedirectResponse(f"/audio/albums/{album_id}", status_code=303)


@router.post("/audio/albums/{album_id}/delete")
def audio_album_delete(album_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _require_can_edit(request)
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
    _require_can_edit(request)
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
    copy_upload_bounded(file, dest, max_bytes=_effective_audio_bytes(db))

    clip_name = name.strip()[:_MAX_NAME] or Path(file.filename).stem[:_MAX_NAME] or "Untitled clip"
    clip = AudioClip(
        world_id=world.id, name=clip_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/audio/{dest.name}", visible_to_players=bool(visible_to_players),
        album_id=target_album_id,
    )
    db.add(clip)
    db.commit()
    return RedirectResponse(f"/audio/albums/{target_album_id}" if target_album_id else "/audio", status_code=303)


@router.post("/audio/upload/chunk")
async def audio_upload_chunk(
    request: Request, file: UploadFile = File(...),
    upload_id: str = Form(...), chunk_index: int = Form(...),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Receive one part of a large audio file (see the _CHUNK_ID_RE block
    above) and stash it on disk under its upload_id; /audio/upload/complete
    reassembles all parts once every one has arrived."""
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _CHUNK_ID_RE.match(upload_id):
        raise HTTPException(400, "Invalid upload id")
    if not (0 <= chunk_index <= _MAX_CHUNK_INDEX):
        raise HTTPException(400, "Invalid chunk index")

    session_dir = _chunk_session_dir(upload_id)
    if chunk_index == 0 and not session_dir.exists():
        _sweep_stale_chunk_sessions()
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / f"{chunk_index:06d}.part"
    # A single part can never legitimately exceed the whole clip's own cap.
    copy_upload_bounded(file, dest, max_bytes=_effective_audio_bytes(db))
    return JSONResponse({"ok": True})


@router.post("/audio/upload/complete")
async def audio_upload_complete(
    request: Request, upload_id: str = Form(...), filename: str = Form(...),
    total_chunks: int = Form(...), name: str = Form(""),
    description: str = Form(""), visible_to_players: Optional[str] = Form(None),
    album_id: str = Form(""),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Reassemble the parts uploaded via /audio/upload/chunk into one file
    and create the AudioClip — same validation and result shape as the
    single-request /audio/upload, just fed from disk instead of the request
    body directly."""
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not _CHUNK_ID_RE.match(upload_id):
        raise HTTPException(400, "Invalid upload id")
    if not filename:
        raise HTTPException(400, "No filename given")
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")
    if not (1 <= total_chunks <= _MAX_CHUNK_INDEX + 1):
        raise HTTPException(400, "Invalid total_chunks")
    if db.query(AudioClip).filter(AudioClip.world_id == world.id).count() >= _MAX_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_CLIPS_PER_WORLD} audio clips.")

    session_dir = _chunk_session_dir(upload_id)
    parts = [session_dir / f"{i:06d}.part" for i in range(total_chunks)]
    if not session_dir.is_dir() or not all(p.is_file() for p in parts):
        raise HTTPException(400, "Upload incomplete — one or more parts are missing. Please retry the upload.")

    target_album_id = None
    album_id = (album_id or "").strip()
    if album_id.isdigit():
        target_album_id = _album_or_404(db, world.id, int(album_id)).id

    target_dir = _UPLOADS_DIR / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / unique_upload_filename(filename, ext)
    # Resolved per request so a Settings > System save applies without a
    # restart — same value the direct upload and per-part paths above use.
    max_bytes = _effective_audio_bytes(db)
    try:
        total_bytes = 0
        with dest.open("wb") as out:
            for part in parts:
                with part.open("rb") as pf:
                    while True:
                        buf = pf.read(1024 * 1024)
                        if not buf:
                            break
                        total_bytes += len(buf)
                        if total_bytes > max_bytes:
                            raise HTTPException(
                                413, f"File too large — limit is {max_bytes // (1024 * 1024)} MB"
                            )
                        out.write(buf)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)

    clip_name = name.strip()[:_MAX_NAME] or Path(filename).stem[:_MAX_NAME] or "Untitled clip"
    clip = AudioClip(
        world_id=world.id, name=clip_name, description=description.strip()[:_MAX_DESCRIPTION],
        file_url=f"/uploads/audio/{dest.name}", visible_to_players=bool(visible_to_players),
        album_id=target_album_id,
    )
    db.add(clip)
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/audio/{clip_id}/edit")
async def audio_edit(
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
    dest = f"/audio/albums/{clip.album_id}" if clip.album_id else "/audio"
    return RedirectResponse(dest, status_code=303)


@router.post("/audio/{clip_id}/transcribe")
async def audio_transcribe(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Generate an AI transcript + WebVTT subtitle track for one clip via
    Whisper (see app.ai.transcribe_audio_with_subtitles) — honors the same
    world-level glossary/language/denoise settings as every other Whisper
    call in the app (Settings > AI page's Whisper tab). Synchronous, not a
    background job: a soundboard/ambiance clip is bounded by
    _effective_audio_bytes and typically much shorter than a session
    recording, the class of upload the background-job system exists for
    (see app/audio_jobs.py) — one blocking request here is an acceptable
    trade for not needing a second job-polling UI just for this. Re-running
    overwrites whatever transcript/subtitles the clip already had."""
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    path = _clip_abs_path(clip)
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


@router.post("/audio/{clip_id}/delete")
def audio_delete(
    clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    _require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    clip = _clip_or_404(db, world.id, clip_id)
    dest = f"/audio/albums/{clip.album_id}" if clip.album_id else "/audio"
    _delete_clip_file(clip)
    db.delete(clip)
    db.commit()
    return RedirectResponse(dest, status_code=303)
