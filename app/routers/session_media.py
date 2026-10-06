"""Pictures, videos and audio on a session - drag them onto the session page, or pick them from the libraries.

One shared door for all three kinds: a file's extension says which kind it is (a hint from the browser settles the few
that fit two, such as .webm), and the file is added to the matching library (audio -> /audio, video -> /video, pictures ->
the gallery's uploads) or, for a picture, kept on the session itself. The session remembers which items go with it
(app/session_media.py); players see only the ones the GM ticked on the Session Log page.

A session carries at most N items in all, N = Settings -> System (10 by default); the check runs BEFORE a file is
written, so a refused upload leaves nothing behind. Large files arrive in parts (static/js/chunked-upload.js) exactly as
for the libraries, because a reverse proxy may cap one request.

Writes need the Sessions section's edit level - checked in each handler (via _require_gm_edit), because the route
allowlist alone admits every assistant."""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from .. import gallery as _gallery
from .. import session_media as sm
from ..database import get_db
from ..models import AudioClip, VideoClip
from ..uploads import copy_upload_bounded, reassemble_upload_chunks, save_upload_chunk, unique_upload_filename
from .audio import _ALLOWED_EXTS as AUDIO_EXTS, _MAX_CLIPS_PER_WORLD as AUDIO_CLIPS_PER_WORLD
from .audio import _MAX_DESCRIPTION, _MAX_NAME, _effective_audio_bytes
from .gallery import _ALLOWED_EXTS as IMAGE_EXTS, _effective_gallery_upload_bytes, _finalize_album_image
from .session_audio import _UPLOADS, _attach as _attach_audio, _require_gm_edit, _store as _store_audio
from .video import _ALLOWED_EXTS as VIDEO_EXTS, _MAX_CLIPS_PER_WORLD as VIDEO_CLIPS_PER_WORLD
from .video import _effective_video_bytes, _finish_stored_file, _generate_poster

router = APIRouter()

_CHUNKS_ROOT = _UPLOADS / "session_media" / "_chunks"
_CHUNK_MAX_BYTES = 128 * 1024 * 1024        # one ~80 MB part from chunked-upload.js plus multipart overhead
_KIND_EXTS = (("image", IMAGE_EXTS), ("video", VIDEO_EXTS), ("audio", AUDIO_EXTS))
_TRUE = ("1", "true", "on", "yes")


def _kind_for(filename: str, hint: str = "") -> tuple:
    """(kind, extension) for an uploaded file name; 400 for anything that is not a picture, a video or audio."""
    ext = Path(filename or "").suffix.lower()
    candidates = [kind for kind, exts in _KIND_EXTS if ext in exts]
    if not candidates:
        allowed = sorted(set().union(*(exts for _k, exts in _KIND_EXTS)))
        raise HTTPException(400, f"Unsupported file type {ext!r} - allowed: {', '.join(allowed)}")
    hint = (hint or "").strip().lower()
    return (hint if hint in candidates else candidates[0]), ext


def _stage(kind: str, filename: str, ext: str) -> Path:
    """Where a file of this kind is written (the folder exists afterwards)."""
    folder = {"image": "gallery", "video": "video", "audio": "audio"}[kind]
    target = _UPLOADS / folder
    target.mkdir(parents=True, exist_ok=True)
    return target / unique_upload_filename(filename, ext)


def _max_bytes(db: Session, kind: str) -> int:
    return {"image": _effective_gallery_upload_bytes, "video": _effective_video_bytes, "audio": _effective_audio_bytes}[kind](db)


def _check_library_room(db: Session, world, kind: str) -> None:
    if kind == "video" and db.query(VideoClip).filter(VideoClip.world_id == world.id).count() >= VIDEO_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {VIDEO_CLIPS_PER_WORLD} video clips.")
    if kind == "audio" and db.query(AudioClip).filter(AudioClip.world_id == world.id).count() >= AUDIO_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {AUDIO_CLIPS_PER_WORLD} audio clips.")


def _add_item(db: Session, gs, item: dict) -> None:
    items = sm.media_items(gs)
    items.append(item)
    sm.store_media(gs, items)
    db.commit()


async def _finish(db: Session, world, gs, kind: str, dest: Path, filename: str, name: str, visible: bool) -> dict:
    """The upload is saved at `dest`: put it in its library / on the session. The file is removed again if anything fails."""
    try:
        label = (name or "").strip()
        stem = Path(filename).stem
        if kind == "image":
            url = _finalize_album_image(dest, db)
            item = {"uid": sm.new_uid(), "kind": "image", "url": url, "caption": (label or stem)[:120], "visible": visible, "own": True}
            _add_item(db, gs, item)
            return sm.image_dict(item)
        if kind == "video":
            final = await _finish_stored_file(dest, dest.parent, world)
            poster = await _generate_poster(final, dest.parent)
            clip = VideoClip(world_id=world.id, name=(label[:_MAX_NAME] or stem[:_MAX_NAME] or "Session video"),
                             description=f"Session media for {gs.title}"[:_MAX_DESCRIPTION],
                             file_url=f"/uploads/video/{final.name}", poster_url=poster, visible_to_players=visible)
            db.add(clip)
            db.commit()
            db.refresh(clip)
            uid = sm.new_uid()
            _add_item(db, gs, {"uid": uid, "kind": "video", "clip_id": clip.id})
            return sm.video_dict(clip, uid)
        clip = AudioClip(world_id=world.id, name=(label[:_MAX_NAME] or stem[:_MAX_NAME] or "Session audio"),
                         description=f"Session audio for {gs.title}"[:_MAX_DESCRIPTION], file_url=f"/uploads/audio/{dest.name}",
                         visible_to_players=visible)
        db.add(clip)
        db.commit()
        db.refresh(clip)
        _attach_audio(db, gs, clip, None)
        return sm.audio_dict(clip)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


# ── reading ──────────────────────────────────────────────────────────────────────────────────────────

@router.get("/api/sessions/{session_id}/media")
def media_list(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _world, gs = _require_gm_edit(request, db, session_id, active_world)
    return sm.panel(db, gs)


# ── uploading ────────────────────────────────────────────────────────────────────────────────────────

@router.post("/api/sessions/{session_id}/media/upload")
async def media_upload(session_id: int, request: Request, file: UploadFile = File(...), name: str = Form(""),
                       visible_to_players: str = Form(""), kind: str = Form(""),
                       db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Add one dropped file: a picture, a video or an audio clip, decided by its extension."""
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    which, ext = _kind_for(file.filename, kind)
    sm.require_room(db, gs)
    _check_library_room(db, world, which)
    dest = _stage(which, file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_max_bytes(db, which))
    item = await _finish(db, world, gs, which, dest, file.filename, name, visible_to_players.strip().lower() in _TRUE)
    return {**sm.panel(db, gs), "kind": which, "item": item}


@router.post("/api/sessions/{session_id}/media/upload/chunk")
async def media_upload_chunk(session_id: int, request: Request, file: UploadFile = File(...), upload_id: str = Form(...),
                             chunk_index: int = Form(...), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """One part of a large file (static/js/chunked-upload.js); /media/upload/complete puts them together."""
    _require_gm_edit(request, db, session_id, active_world)
    cap = max(_effective_gallery_upload_bytes(db), _effective_video_bytes(db), _effective_audio_bytes(db))
    save_upload_chunk(_CHUNKS_ROOT, upload_id, chunk_index, file, max_bytes=min(_CHUNK_MAX_BYTES, cap))
    return {"ok": True}


@router.post("/api/sessions/{session_id}/media/upload/complete")
async def media_upload_complete(session_id: int, request: Request, upload_id: str = Form(...), filename: str = Form(...),
                                total_chunks: int = Form(...), name: str = Form(""), visible_to_players: str = Form(""),
                                kind: str = Form(""), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    which, ext = _kind_for(filename, kind)
    sm.require_room(db, gs)
    _check_library_room(db, world, which)
    dest = _stage(which, filename, ext)
    reassemble_upload_chunks(_CHUNKS_ROOT, upload_id, total_chunks, dest, max_bytes=_max_bytes(db, which))
    item = await _finish(db, world, gs, which, dest, filename, name, visible_to_players.strip().lower() in _TRUE)
    return {**sm.panel(db, gs), "kind": which, "item": item}


# ── picking from the libraries ───────────────────────────────────────────────────────────────────────

@router.post("/api/sessions/{session_id}/media/attach")
async def media_attach(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Attach something that is already in this world: {kind: "audio"|"video", id} or {kind: "image", url}."""
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    kind, vis = body.get("kind"), body.get("visible_to_players")
    vis = vis if isinstance(vis, bool) else None
    if kind in ("audio", "video"):
        cid = body.get("id")
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise HTTPException(400, "id must be a clip id")
        model = AudioClip if kind == "audio" else VideoClip
        clip = db.get(model, cid)
        if not clip or clip.world_id != world.id:
            raise HTTPException(404, f"No such {kind} clip in this world")
        if kind == "audio":
            _attach_audio(db, gs, clip, vis)
        else:
            items = sm.media_items(gs)
            if not any(i["kind"] == "video" and i["clip_id"] == cid for i in items):
                sm.require_room(db, gs)
                items.append({"uid": sm.new_uid(), "kind": "video", "clip_id": cid})
                sm.store_media(gs, items)
            if vis is not None:
                clip.visible_to_players = vis
            db.commit()
    elif kind == "image":
        url = body.get("url")
        names = {e["url"]: e["name"] for e in _gallery.all_world_image_urls(db, world)}
        if not isinstance(url, str) or url not in names:
            raise HTTPException(404, "That picture is not one of this world's images")
        items = sm.media_items(gs)
        if not any(i["kind"] == "image" and i["url"] == url for i in items):
            sm.require_room(db, gs)
            items.append({"uid": sm.new_uid(), "kind": "image", "url": url, "caption": str(names[url])[:120],
                          "visible": bool(vis), "own": False})
            sm.store_media(gs, items)
            db.commit()
    else:
        raise HTTPException(400, "kind must be audio, video or image")
    return sm.panel(db, gs)


# ── per-item switches ────────────────────────────────────────────────────────────────────────────────

def _clip_for(db: Session, world, gs, kind: str, ref: str):
    """The attached audio/video clip `ref` names, or 404."""
    try:
        cid = int(ref)
    except ValueError:
        raise HTTPException(404)
    attached = (cid in sm.recap_clip_ids(gs)) if kind == "audio" else any(
        i["kind"] == "video" and i["clip_id"] == cid for i in sm.media_items(gs))
    clip = db.get(AudioClip if kind == "audio" else VideoClip, cid) if attached else None
    if not clip or clip.world_id != world.id:
        raise HTTPException(404)
    return clip


@router.post("/api/sessions/{session_id}/media/{kind}/{ref}/visibility")
async def media_visibility(session_id: int, kind: str, ref: str, request: Request, db: Session = Depends(get_db),
                           active_world: str = Cookie(None)):
    """Let players see (or not) one attached item - for audio and video the same switch as in their library."""
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    if kind not in ("audio", "video", "image"):
        raise HTTPException(404)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    vis = body.get("visible_to_players") if isinstance(body, dict) else None
    if not isinstance(vis, bool):
        raise HTTPException(400, "visible_to_players must be true or false")
    if kind == "image":
        items = sm.media_items(gs)
        hit = next((i for i in items if i["kind"] == "image" and i["uid"] == ref), None)
        if not hit:
            raise HTTPException(404)
        hit["visible"] = vis
        sm.store_media(gs, items)
    else:
        _clip_for(db, world, gs, kind, ref).visible_to_players = vis
    db.commit()
    return sm.panel(db, gs)


@router.post("/api/sessions/{session_id}/media/{kind}/{ref}/remove")
def media_remove(session_id: int, kind: str, ref: str, request: Request, db: Session = Depends(get_db),
                 active_world: str = Cookie(None)):
    """Take an item off the session. Audio and video stay in their libraries, and so does a picture picked from the
    gallery; a picture uploaded for this session is deleted with it."""
    _world, gs = _require_gm_edit(request, db, session_id, active_world)
    if kind == "audio":
        try:
            cid = int(ref)
        except ValueError:
            raise HTTPException(404)
        _store_audio(gs, [i for i in sm.recap_clip_ids(gs) if i != cid])
    elif kind == "video":
        try:
            cid = int(ref)
        except ValueError:
            raise HTTPException(404)
        sm.store_media(gs, [i for i in sm.media_items(gs) if not (i["kind"] == "video" and i["clip_id"] == cid)])
    elif kind == "image":
        items = sm.media_items(gs)
        hit = next((i for i in items if i["kind"] == "image" and i["uid"] == ref), None)
        if not hit:
            raise HTTPException(404)
        sm.store_media(gs, [i for i in items if i is not hit])
        db.commit()
        if hit["own"]:
            sm.delete_picture_file(hit["url"])
    else:
        raise HTTPException(404)
    db.commit()
    return sm.panel(db, gs)
